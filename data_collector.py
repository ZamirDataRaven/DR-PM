from __future__ import annotations

import re
import subprocess
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import requests

from config_manager import ConfigLoadError, ConfigValidationError, load_config


class DataCollectorError(Exception):
    pass


_STEP_MAP: dict[int, str] = {
    0: "HLD Review",
    1: "HLD Decomposition",
    2: "Create Spec",
    3: "Spec to Task",
    4: "Req Verification",
    5: "Coding Assist",
    6: "Unit Test",
    7: "Code Review",
    8: "Pre-Commit Review",
    9: "PR Evidence",
}

# Updated regex — supports COMP-001 and COMP-PM-001 formats
_COMP_ID_RE = re.compile(r"\bCOMP(?:-[A-Z]+)?-\d+\b")

STEP_COLORS: dict[str, str] = {
    "HLD Review":        "#52526e",
    "HLD Decomposition": "#7c3aed",
    "Create Spec":       "#a855f7",
    "Spec to Task":      "#3b82f6",
    "Req Verification":  "#06b6d4",
    "Coding Assist":     "#eab308",
    "Unit Test":         "#f97316",
    "Code Review":       "#ef4444",
    "Pre-Commit Review": "#f43f5e",
    "PR Evidence":       "#22c55e",
}

# Ported verbatim from create_pipeline1_report.py
_PASS_RE = re.compile(
    r"(?:Approval:\s*Approved"
    r"|Registry\s+Approval:\s*Approved"
    r"|Verdict:\s*PASS(?:-WITH-CAVEATS)?"
    r"|(?:Artifacts?|Note\s+artifact|Issues\s+drafted|Files):\s*\S)",
    re.IGNORECASE,
)
_FAIL_RE = re.compile(r"Verdict:\s*FAIL\b", re.IGNORECASE)

_GH_API = "https://api.github.com"
_EMPTY_ISSUES = {"blockers": [], "data_requests": [], "open_tasks": [], "closed_tasks": []}


# ── Registry and step resolution (Issues #16) ────────────────────────────────

def parse_registry(registry_text: str) -> list[dict]:
    components = []
    for line in registry_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cols = [c.strip() for c in stripped.split("|")]
        cols = [c for c in cols if c]
        if len(cols) < 2:
            continue
        if re.match(r"COMP(?:-[A-Z]+)?-\d+", cols[0]):
            components.append({"id": cols[0], "name": cols[1]})
    return components


def resolve_workflow_step(comp_id: str, action_summary: str) -> int:
    """Returns the highest DR step number completed for this component. 0 if none found."""
    sections = re.split(r"(?m)(?=^## Step \d+)", action_summary)
    highest = -1
    for section in sections:
        m = re.match(r"^## Step (\d+)", section)
        if not m:
            continue
        step_num = int(m.group(1))
        if step_num > 1 and comp_id not in section:
            continue
        if _FAIL_RE.search(section):
            continue
        if _PASS_RE.search(section):
            highest = max(highest, step_num)
    if highest < 0:
        warnings.warn(f"No step entry found for {comp_id} — defaulting to step 0")
        return 0
    return highest


def resolve_steps(registry_path: Path, action_summary_path: Path) -> tuple[dict[str, int], dict[str, str]]:
    """Returns ({comp_id: step_number}, {comp_id: name}) for all components in the registry."""
    try:
        registry_text = registry_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DataCollectorError(f"Component registry not found at {registry_path}")
    components = parse_registry(registry_text)
    if not components:
        raise DataCollectorError(f"No components parsed from registry at {registry_path}")
    if action_summary_path.exists():
        action_summary = action_summary_path.read_text(encoding="utf-8")
    else:
        warnings.warn("No _action-summary.md found — all components default to step 0")
        action_summary = ""
    steps = {c["id"]: resolve_workflow_step(c["id"], action_summary) for c in components}
    names = {c["id"]: c["name"] for c in components}
    return steps, names


# ── GitHub Issues API (Issue #17) ────────────────────────────────────────────

def _handle_gh_response(resp: requests.Response) -> list:
    if resp.status_code == 401:
        raise DataCollectorError("GITHUB_TOKEN rejected by Issues API — check token permissions")
    if resp.status_code == 404:
        raise DataCollectorError("Project repo not found — check repo_url in dr-pm-config.json")
    if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
        warnings.warn(f"GitHub rate limited — resets at {resp.headers.get('X-RateLimit-Reset')}.")
        return []
    try:
        data = resp.json()
    except ValueError:
        warnings.warn(f"GitHub Issues API unexpected response (status {resp.status_code}).")
        return []
    if len(data) == 100:
        warnings.warn("Issue list may be truncated — pagination not implemented in v0.1.")
    return data


def _gh_get(url: str, token: str, params: dict) -> list:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    for attempt in range(2):
        try:
            return _handle_gh_response(requests.get(url, headers=headers, params=params, timeout=30))
        except requests.Timeout:
            if attempt == 0:
                time.sleep(15)
            else:
                warnings.warn("GitHub Issues API timed out — board will show last known issue state.")
                return []


def _categorise_issue(result: dict[str, dict], issue: dict, state: str) -> None:
    match = _COMP_ID_RE.search(issue.get("title", ""))
    if not match or match.group() not in result:
        return
    comp_id = match.group()
    slim = {"number": issue.get("number"), "title": issue.get("title", "")}
    label_names = {lb["name"] for lb in issue.get("labels", [])}
    if state == "closed":
        result[comp_id]["closed_tasks"].append(slim)
    elif "blocker" in label_names:
        result[comp_id]["blockers"].append(slim)
    elif "data-request" in label_names:
        result[comp_id]["data_requests"].append(slim)
    else:
        result[comp_id]["open_tasks"].append(slim)


def collect_issues(repo: str, github_token: str, comp_ids: list[str]) -> dict[str, dict]:
    result: dict[str, dict] = {
        cid: {"blockers": [], "data_requests": [], "open_tasks": [], "closed_tasks": []}
        for cid in comp_ids
    }
    base = f"{_GH_API}/repos/{repo}/issues"
    for state in ("open", "closed"):
        for issue in _gh_get(base, github_token, {"state": state, "per_page": 100}):
            _categorise_issue(result, issue, state)
    return result


# ── 24h delta extraction (Issue #18) ─────────────────────────────────────────

def _parse_advances(added_lines: list[str]) -> list[dict]:
    advances, current_step = [], None
    for line in added_lines:
        step_match = re.match(r"^## Step (\d+)", line)
        if step_match:
            current_step = int(step_match.group(1))
        comp_match = _COMP_ID_RE.search(line)
        if comp_match and current_step is not None:
            advances.append({"comp_id": comp_match.group(), "new_step": _STEP_MAP.get(current_step, "Pending HLD Approval")})
            current_step = None
    return advances


def collect_delta(action_summary_path: Path) -> dict:
    _empty: dict = {"advances": [], "raw_lines": ""}
    try:
        result = subprocess.run(
            ["git", "log", "--since=24 hours ago", "-p", "--", str(action_summary_path)],
            capture_output=True, text=True, check=True,
            cwd=action_summary_path.parent,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        warnings.warn("Could not collect 24h delta — git log failed.")
        return _empty
    added = [line[1:] for line in result.stdout.splitlines() if line.startswith("+") and not line.startswith("+++")]
    return {"advances": _parse_advances(added), "raw_lines": "\n".join(added)}


# ── Payload assembly (Issue #19) ─────────────────────────────────────────────

def _phase_status(comp_ids: list[str], steps: dict[str, int]) -> str:
    nums = [steps.get(c, 0) for c in comp_ids]
    if not nums:
        return "tbd"
    if all(n == 9 for n in nums):
        return "done"
    if any(n > 0 for n in nums):
        return "in_progress"
    return "tbd"


def _comp_entry(comp_id: str, phase_id: str | None, step_num: int, idata: dict, name: str | None = None) -> dict:
    step_label = _STEP_MAP.get(step_num, "HLD Review")
    has_b = bool(idata["blockers"])
    has_dr = bool(idata["data_requests"])
    return {
        "name": name or comp_id,
        "phase": phase_id,
        "step": step_label,
        "step_num": step_num,
        "step_color": STEP_COLORS.get(step_label, "#52526e"),
        "has_blocker": has_b,
        "has_data_request": has_dr,
        "border_status": "red" if has_b else ("yellow" if has_dr else "none"),
        "tasks": {"open": idata["open_tasks"], "closed": idata["closed_tasks"]},
    }


def build_payload(config: dict, steps: dict[str, int], issues: dict[str, dict], generated_at: str,
                  names: dict[str, str] | None = None) -> dict:
    names = names or {}
    components: dict[str, dict] = {}
    all_blockers: list = []
    all_dr: list = []
    assigned: set = set()
    for phase in config.get("phases", []):
        for comp_id in phase.get("component_ids", []):
            assigned.add(comp_id)
            idata = issues.get(comp_id, _EMPTY_ISSUES)
            components[comp_id] = _comp_entry(comp_id, phase["id"], steps.get(comp_id, 0), idata, names.get(comp_id))
            all_blockers.extend(idata["blockers"])
            all_dr.extend(idata["data_requests"])
    for comp_id in steps:
        if comp_id not in assigned:
            warnings.warn(f"Component {comp_id} not assigned to any phase — included without phase")
            components[comp_id] = _comp_entry(comp_id, None, steps[comp_id], issues.get(comp_id, _EMPTY_ISSUES), names.get(comp_id))
    phases = [
        {"id": p["id"], "name": p["name"],
         "status": _phase_status(p.get("component_ids", []), steps),
         "component_ids": p.get("component_ids", [])}
        for p in config.get("phases", [])
    ]
    return {"project": config["project_name"], "generated": generated_at, "board_url": config["board_url"],
            "phases": phases, "components": components, "blockers": all_blockers, "data_requests": all_dr}


# ── Top-level orchestrator (Issue #20) ───────────────────────────────────────

def collect(project_repo_root: str, engagement_folder: str, github_token: str) -> tuple[dict, dict]:
    root = Path(project_repo_root)
    eng_path = root / engagement_folder
    registry_path = eng_path / "01-decomposition" / "component-registry-v0.1.md"
    summary_path = eng_path / "_action-summary.md"
    try:
        config = load_config(project_repo_root, engagement_folder)
    except (ConfigLoadError, ConfigValidationError) as e:
        raise DataCollectorError(f"Config load failed: {e}")
    steps, names = resolve_steps(registry_path, summary_path)
    repo = config["repo_url"].replace("https://github.com/", "").rstrip("/")
    issues = collect_issues(repo, github_token, list(steps.keys()))
    delta_raw = collect_delta(summary_path)
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload = build_payload(config, steps, issues, generated_at, names)
    delta = {**delta_raw, "new_blockers": [], "resolved_blockers": [], "new_data_requests": [], "resolved_data_requests": []}
    return payload, delta
