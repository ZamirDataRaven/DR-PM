#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

import anthropic

from board_html_validator import BoardHTMLValidationError, validate_board_html
from config_manager import ConfigValidationError, save_config
from data_collector import parse_registry


class PreflightError(Exception):
    pass


class BoardGenerationError(Exception):
    pass


class DeployError(Exception):
    pass


class ProvisionError(Exception):
    pass


_DO_HOST = "146.190.186.206"
_DO_USER = "root"
_SSH_KEY = str(Path("~/.ssh/dr_pm_actions").expanduser())
_REMOTE_BASE = "/var/www/dr-pm"
_DRPM_ROOT = Path(__file__).parent
_MODEL = "claude-sonnet-4-6"
_VALID_DAYS = frozenset({"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"})
_DAY_CRON = {"Mon": "1", "Tue": "2", "Wed": "3", "Thu": "4", "Fri": "5", "Sat": "6", "Sun": "0"}
_DAY_WEEKDAY = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
_REPO_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+$")

_WORKFLOW_YAML = """\
name: DR-PM | {project_name} | Daily Runner

on:
  schedule:
    - cron: '{cron_expression}'
  workflow_dispatch:

concurrency:
  group: dr-pm-{project_slug}-daily
  cancel-in-progress: false

permissions:
  contents: read
  issues: read

jobs:
  daily:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout project repo
        uses: actions/checkout@v4

      - name: Checkout DR-PM repo
        uses: actions/checkout@v4
        with:
          repository: ZamirDataRaven/DR-PM
          path: dr-pm

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: pip install requests anthropic

      - name: Write SSH key
        run: |
          echo "${{{{ secrets.DO_SSH_KEY }}}}" > $RUNNER_TEMP/dr_pm_ssh_key
          chmod 600 $RUNNER_TEMP/dr_pm_ssh_key

      - name: Run daily pipeline
        env:
          GITHUB_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}
          CLAUDE_API_KEY: ${{{{ secrets.CLAUDE_API_KEY }}}}
          SMTP_SERVER: ${{{{ secrets.SMTP_SERVER }}}}
          SMTP_PORT: ${{{{ secrets.SMTP_PORT }}}}
          SMTP_USERNAME: ${{{{ secrets.SMTP_USERNAME }}}}
          SMTP_PASSWORD: ${{{{ secrets.SMTP_PASSWORD }}}}
          SMTP_SENDER_ADDRESS: ${{{{ secrets.SMTP_SENDER_ADDRESS }}}}
          DR_PM_SSH_KEY_PATH: ${{{{ runner.temp }}}}/dr_pm_ssh_key
          DR_PM_PROJECT_REPO_ROOT: ${{{{ github.workspace }}}}
          DR_PM_ENGAGEMENT_FOLDER: {engagement_folder}
        run: python dr-pm/run_daily.py
"""


# ── Form collection (Issue #53) ───────────────────────────────────────────────

def _ask(prompt: str) -> str:
    return input(prompt).strip()


def _ask_loop(prompt: str, validate) -> str:
    while True:
        val = _ask(prompt)
        err = validate(val)
        if err is None:
            return val
        print(f"Invalid: {err}. Please re-enter.")


def _ask_emails(prompt: str) -> list[str]:
    while True:
        raw = _ask(prompt)
        if not raw:
            return []
        parts = raw.split()
        bad = [e for e in parts if not _EMAIL_RE.match(e)]
        if bad:
            print(f"Invalid email(s): {', '.join(bad)}. Please re-enter.")
        else:
            return parts


def _ask_schedule() -> dict:
    while True:
        days_raw = _ask("Email days (comma-separated, e.g. Mon,Tue,Wed,Thu,Fri): ")
        days = [d.strip() for d in days_raw.split(",") if d.strip()]
        if not days or not all(d in _VALID_DAYS for d in days):
            print("Invalid: use day names Mon Tue Wed Thu Fri Sat Sun, min 1.")
            continue
        hour_raw = _ask("Email hour (0-23, local timezone): ")
        try:
            hour = int(hour_raw)
            if not 0 <= hour <= 23:
                raise ValueError
        except ValueError:
            print("Invalid: hour must be an integer 0-23.")
            continue
        tz = _ask("Timezone (IANA, e.g. Asia/Jerusalem): ")
        if tz not in available_timezones():
            print(f"Invalid: unknown timezone '{tz}'. Use an IANA timezone name.")
            continue
        return {"days": days, "hour": hour, "timezone": tz}


def _ask_phases() -> list[dict]:
    phases = []
    print("Enter phases (type 'done' when finished):")
    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    while True:
        name = _ask(f"Phase {len(phases)+1} name (or 'done'): ")
        if name.lower() == "done":
            if not phases:
                print("At least one phase is required.")
                continue
            break
        desc = _ask("  Description: ")
        start = _ask_loop("  Start date (YYYY-MM-DD): ", lambda v: None if date_re.match(v) else "use YYYY-MM-DD format")
        end = _ask_loop("  End date (YYYY-MM-DD): ", lambda v: None if date_re.match(v) else "use YYYY-MM-DD format")
        comps = _ask("  Component IDs (space-separated): ").split()
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or f"phase-{len(phases)+1}"
        phases.append({"id": slug, "name": name, "description": desc, "start_date": start, "end_date": end, "component_ids": comps})
    return phases


def collect_form() -> dict:
    print("=== DR-PM Initialization ===\n")
    project_name = _ask_loop("Project name: ", lambda v: None if v else "cannot be empty")
    project_slug = _ask_loop("Project slug (lowercase, hyphens only, e.g. ns01): ",
                              lambda v: None if _SLUG_RE.match(v) else "must match ^[a-z0-9][a-z0-9-]*[a-z0-9]$")
    repo_url = _ask_loop("GitHub repo URL (https://github.com/owner/repo): ",
                          lambda v: None if _REPO_RE.match(v) else "must be https://github.com/owner/repo")
    engagement_folder = _ask_loop("Engagement folder (no leading /): ",
                                   lambda v: None if v and not v.startswith("/") else "non-empty, no leading /")
    internal = _ask_emails("Internal recipients (space-separated, Enter for none): ")
    client = _ask_emails("Client recipients (space-separated, Enter for none): ")
    schedule = _ask_schedule()
    phases = _ask_phases()
    board_ui_notes = _ask("Board UI notes (Enter to skip): ")
    return {
        "project_name": project_name, "project_slug": project_slug,
        "repo_url": repo_url, "engagement_folder": engagement_folder,
        "recipients_internal": internal, "recipients_client": client,
        "email_schedule": schedule, "phases": phases,
        "board_ui_notes": board_ui_notes,
    }


# ── Pre-flight validation (Issue #54) ────────────────────────────────────────

def _check_registry(form: dict, root: str) -> tuple[str, list[dict]]:
    reg_path = (Path(root) / form["engagement_folder"] / "01-decomposition" / "component-registry-v0.1.md")
    try:
        text = reg_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PreflightError(f"Component registry not found at {reg_path}")
    components = parse_registry(text)
    if not components:
        raise PreflightError(f"Registry not parseable at {reg_path}")
    return text, components


def _check_phase_assignments(form: dict, comp_ids: set[str]) -> None:
    seen: dict[str, str] = {}
    for phase in form["phases"]:
        for cid in phase["component_ids"]:
            if cid not in comp_ids:
                raise PreflightError(f"Component {cid} in phase '{phase['name']}' not found in registry")
            if cid in seen:
                raise PreflightError(f"Component {cid} appears in phases '{seen[cid]}' and '{phase['name']}'")
            seen[cid] = phase["name"]


def _preflight_claude(form: dict, registry_text: str, summary_text: str) -> None:
    client = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])
    system = ("You are a DR framework project validator. Given a component registry, action summary, "
              "and phases definition, return JSON only with keys 'verdict' (PASS or FAIL) and 'issues' (array of strings).")
    user = (f"Registry:\n{registry_text}\n\nAction summary:\n{summary_text}\n\n"
            f"Phases:\n{json.dumps(form['phases'], indent=2)}")
    try:
        msg = client.messages.create(model=_MODEL, max_tokens=1024, system=system,
                                     messages=[{"role": "user", "content": user}])
        result = json.loads(msg.content[0].text)
    except json.JSONDecodeError:
        result = {"verdict": "FAIL", "issues": ["Claude returned malformed validation response"]}
    except (anthropic.AuthenticationError, anthropic.APIConnectionError) as e:
        raise PreflightError(f"Claude API unavailable for validation: {e}")
    if result.get("verdict") == "FAIL":
        for issue in result.get("issues", []):
            print(f"  - {issue}")
        raise PreflightError("Pre-flight validation failed — resolve issues above and re-run")


def run_preflight(form: dict, project_repo_root: str) -> None:
    registry_text, components = _check_registry(form, project_repo_root)
    _check_phase_assignments(form, {c["id"] for c in components})
    summary_path = Path(project_repo_root) / form["engagement_folder"] / "_action-summary.md"
    if not summary_path.exists():
        print("Warning: _action-summary.md not found — all components will initialise at Pending HLD Approval")
    summary_text = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
    _preflight_claude(form, registry_text, summary_text)


# ── Board HTML generation (Issue #55) ────────────────────────────────────────

def _build_prompt(form: dict, registry_text: str, guidelines: str, template: str) -> tuple[str, str]:
    components = parse_registry(registry_text)
    phases_fmt = "\n".join(
        f"{i+1}. {p['name']} ({p['id']}): {p['description']}. "
        f"Components: {', '.join(p['component_ids'])}. Dates: {p['start_date']} to {p['end_date']}"
        for i, p in enumerate(form["phases"])
    )
    comps_fmt = "\n".join(f"- {c['id']}: {c['name']}" for c in components)
    slug = form["project_slug"]
    rendered = template
    for key, val in {
        "design_guidelines": guidelines, "project_name": form["project_name"],
        "project_slug": slug, "board_url": f"http://{_DO_HOST}/dr-pm/{slug}/",
        "phases_formatted": phases_fmt, "components_formatted": comps_fmt,
        "board_ui_notes": form["board_ui_notes"] or "No additional UI notes.",
        "initialized_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }.items():
        rendered = rendered.replace(f"{{{key}}}", val)
    system_block = rendered.split("[/SYSTEM]")[0].replace("[SYSTEM]\n", "").strip()
    user_block = rendered.split("[USER]\n")[1].replace("\n[/USER]", "").strip()
    return system_block, user_block


def generate_board_html(form: dict, registry_text: str) -> str:
    guidelines = (_DRPM_ROOT / "board_html_design_guidelines.md").read_text(encoding="utf-8")
    template = (_DRPM_ROOT / "board_html_prompt_template.txt").read_text(encoding="utf-8")
    system_prompt, user_msg = _build_prompt(form, registry_text, guidelines, template)
    client = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])
    try:
        msg = client.messages.create(model=_MODEL, max_tokens=8192, system=system_prompt,
                                     messages=[{"role": "user", "content": user_msg}])
    except anthropic.AuthenticationError:
        raise BoardGenerationError("CLAUDE_API_KEY invalid — board generation failed")
    except anthropic.APIConnectionError as e:
        raise BoardGenerationError(f"Claude API unreachable: {e}")
    if not msg.content:
        raise BoardGenerationError("Claude returned empty response")
    html = msg.content[0].text
    validate_board_html(html)
    return html


# ── Board deploy (Issue #56) ──────────────────────────────────────────────────

def deploy_board(html: str, project_slug: str) -> None:
    tmp = Path(f"/tmp/dr-pm-init-{project_slug}.html")
    tmp.write_text(html, encoding="utf-8")
    ssh_base = ["ssh", "-i", _SSH_KEY, "-o", "StrictHostKeyChecking=no", f"{_DO_USER}@{_DO_HOST}"]
    ssh_opt = f"ssh -i {_SSH_KEY} -o StrictHostKeyChecking=no"
    remote_dir = f"{_REMOTE_BASE}/{project_slug}"
    try:
        r = subprocess.run(ssh_base + [f"mkdir -p {remote_dir} && chown -R www-data:www-data {remote_dir}"],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            raise DeployError(f"SSH mkdir failed: {r.stderr.decode(errors='replace').strip()}")
        r = subprocess.run(["rsync", "-az", "-e", ssh_opt, str(tmp),
                            f"{_DO_USER}@{_DO_HOST}:{remote_dir}/index.html"],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            raise DeployError(f"rsync failed: {r.stderr.decode(errors='replace').strip()}")
    except subprocess.TimeoutExpired:
        raise DeployError("rsync timed out after 60s")
    finally:
        tmp.unlink(missing_ok=True)


# ── Workflow provisioning (Issue #57) ─────────────────────────────────────────

def _compute_cron(days: list[str], hour: int, timezone: str) -> str:
    offset_hours = int(datetime.now(ZoneInfo(timezone)).utcoffset().total_seconds() / 3600)
    utc_hour = (hour - offset_hours) % 24
    return f"0 {utc_hour} * * {','.join(_DAY_CRON[d] for d in days)}"


_DATA_REQUEST_TEMPLATE = """\
name: Data Request
description: Request data or information needed for component progress
title: "[DATA REQUEST] COMP-ID — brief description"
labels: ["data-request"]
body:
  - type: dropdown
    id: project
    attributes:
      label: Project
      description: Which DR-PM project does this data request belong to?
      options:
{project_options}
    validations:
      required: true
  - type: input
    id: component
    attributes:
      label: Component ID
      description: "Component ID this request blocks (e.g. COMP-PM-004)"
      placeholder: "COMP-PM-004"
    validations:
      required: true
  - type: textarea
    id: description
    attributes:
      label: What data do you need?
    validations:
      required: true
  - type: textarea
    id: source
    attributes:
      label: Suggested source
      placeholder: "e.g. CRM system, client database, stakeholder"
    validations:
      required: false
"""


def _update_issue_template(form: dict, project_repo_root: str) -> None:
    templates_dir = Path(project_repo_root) / ".github" / "ISSUE_TEMPLATE"
    templates_dir.mkdir(parents=True, exist_ok=True)
    dest = templates_dir / "data-request.yml"
    new_option = f"        - \"{form['project_slug']} — {form['project_name']}\""
    if dest.exists():
        content = dest.read_text(encoding="utf-8")
        if form["project_slug"] not in content:
            content = re.sub(
                r'(      options:\n)((?:        - .*\n)*)',
                lambda m: m.group(1) + m.group(2) + new_option + "\n",
                content,
            )
        dest.write_text(content, encoding="utf-8")
    else:
        dest.write_text(
            _DATA_REQUEST_TEMPLATE.replace("{project_options}", new_option),
            encoding="utf-8",
        )


def _write_workflow(form: dict, project_repo_root: str) -> None:
    sched = form["email_schedule"]
    cron = _compute_cron(sched["days"], sched["hour"], sched["timezone"])
    yaml_content = _WORKFLOW_YAML.format(
        project_name=form["project_name"],
        project_slug=form["project_slug"],
        cron_expression=cron,
        engagement_folder=form["engagement_folder"],
    )
    workflows_dir = Path(project_repo_root) / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / "dr-pm-daily.yml").write_text(yaml_content, encoding="utf-8")
    _update_issue_template(form, project_repo_root)


def _set_secrets(form: dict) -> None:
    try:
        secrets = {
            "DO_SSH_KEY": Path("~/.ssh/dr_pm_actions").expanduser().read_text(encoding="utf-8"),
            "CLAUDE_API_KEY": os.environ["CLAUDE_API_KEY"],
            "SMTP_SERVER": os.environ["SMTP_SERVER"],
            "SMTP_PORT": os.environ["SMTP_PORT"],
            "SMTP_USERNAME": os.environ["SMTP_USERNAME"],
            "SMTP_PASSWORD": os.environ["SMTP_PASSWORD"],
            "SMTP_SENDER_ADDRESS": os.environ["SMTP_SENDER_ADDRESS"],
        }
    except KeyError as e:
        print(f"Missing required env var: {e}. Set it before running Create_PPT.")
        sys.exit(1)
    for name, value in secrets.items():
        try:
            subprocess.run(["gh", "secret", "set", name, "--repo", form["repo_url"], "--body", value],
                           check=True, timeout=30, capture_output=True)
        except subprocess.TimeoutExpired:
            raise ProvisionError(f"gh secret set timed out for {name}")
        except subprocess.CalledProcessError as e:
            raise ProvisionError(f"Failed to set secret {name}: {e.stderr.decode(errors='replace').strip()}")


def provision_workflow(form: dict, project_repo_root: str) -> None:
    _write_workflow(form, project_repo_root)
    _set_secrets(form)


# ── Config write (Issue #58) ──────────────────────────────────────────────────

def _git_commit_config(config: dict, root: str, folder: str) -> None:
    config_rel = f"{folder}/reports/dr-pm-config.json"
    for cmd in (
        ["git", "-C", root, "add", config_rel],
        ["git", "-C", root, "commit", "-m", f"chore: initialise DR-PM for {config['project_name']}"],
        ["git", "-C", root, "push"],
    ):
        try:
            subprocess.run(cmd, check=True, timeout=60, capture_output=True)
        except subprocess.CalledProcessError:
            if "push" in cmd:
                print(f"Git push failed. Config saved locally. Push manually: git -C {root} push")
                return
            sys.exit(1)
        except subprocess.TimeoutExpired:
            print("Git command timed out.")
            sys.exit(1)


def write_config(form: dict, project_repo_root: str) -> None:
    slug = form["project_slug"]
    config = {
        "project_name": form["project_name"], "project_slug": slug,
        "repo_url": form["repo_url"], "engagement_folder": form["engagement_folder"],
        "board_url": f"http://{_DO_HOST}/dr-pm/{slug}/",
        "do_host": _DO_HOST,
        "recipients": {"internal": form["recipients_internal"], "client": form["recipients_client"]},
        "email_schedule": form["email_schedule"], "email_enabled": True,
        "phases": [{"id": p["id"], "name": p["name"], "description": p["description"],
                    "start_date": p["start_date"], "end_date": p["end_date"], "component_ids": p["component_ids"]}
                   for p in form["phases"]],
        "nginx_auth_user": "drpm",
        "initialized_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        save_config(config, project_repo_root, form["engagement_folder"])
    except (ConfigValidationError, OSError) as e:
        print(f"Config write failed: {e}")
        sys.exit(1)
    _git_commit_config(config, project_repo_root, form["engagement_folder"])


# ── Summary and main (Issues #59, #60) ───────────────────────────────────────

def _compute_next_run(days: list[str], hour: int, tz_name: str) -> datetime:
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    target_weekdays = sorted(_DAY_WEEKDAY[d] for d in days)
    for delta in range(8):
        candidate = (now + timedelta(days=delta)).replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate.weekday() in target_weekdays and candidate > now:
            return candidate.astimezone(timezone.utc)
    return now.astimezone(timezone.utc)


def print_summary(form: dict, next_run_utc: datetime) -> None:
    slug = form["project_slug"]
    board_url = f"http://{_DO_HOST}/dr-pm/{slug}/"
    password = os.environ.get("DR_PM_BOARD_PASSWORD", "[set DR_PM_BOARD_PASSWORD env var]")
    sched = form["email_schedule"]
    print("\n=== DR-PM Initialization Complete ===\n")
    print(f"Project:    {form['project_name']}")
    print(f"Board URL:  {board_url}")
    print(f"Board auth: user=drpm  password={password}")
    print(f"\nEmail schedule:  {' '.join(sched['days'])} at {sched['hour']:02d}:00 {sched['timezone']}")
    print(f"First scheduled run: {next_run_utc.strftime('%Y-%m-%dT%H:%M:%SZ')} (UTC)")
    print("\nNext steps:")
    print("  1. Share the board URL and password with recipients")
    print("  2. Verify the board loads at the URL above")
    print("  3. Confirm the GitHub Actions workflow is active in the project repo")


def _preflight_and_generate(form: dict, root: str) -> str:
    print("\nRunning pre-flight checks...")
    try:
        run_preflight(form, root)
    except PreflightError as e:
        print(f"Pre-flight failed: {e}")
        sys.exit(1)
    registry_text, _ = _check_registry(form, root)
    print("\nGenerating board HTML (Claude API Call 2)...")
    try:
        html = generate_board_html(form, registry_text)
    except (BoardGenerationError, BoardHTMLValidationError) as e:
        print(f"Board generation failed: {e}")
        sys.exit(1)
    return html


def _run_init(form: dict, root: str) -> None:
    html = _preflight_and_generate(form, root)
    print("\nDeploying board to droplet...")
    try:
        deploy_board(html, form["project_slug"])
    except DeployError as e:
        print(f"Deploy failed: {e}")
        sys.exit(1)
    print("\nProvisioning GitHub Actions workflow...")
    try:
        provision_workflow(form, root)
    except (ProvisionError, FileNotFoundError) as e:
        print(f"Provisioning failed: {e}")
        sys.exit(1)
    print("\nWriting config to project repo...")
    write_config(form, root)


def main() -> None:
    root = str(Path.cwd())
    try:
        form = collect_form()
    except KeyboardInterrupt:
        print("\nInitialization cancelled.")
        sys.exit(0)
    _run_init(form, root)
    sched = form["email_schedule"]
    next_run = _compute_next_run(sched["days"], sched["hour"], sched["timezone"])
    print_summary(form, next_run)


if __name__ == "__main__":
    main()
