#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

from board_html_validator import BoardHTMLValidationError, validate_board_html
from config_manager import ConfigValidationError, validate_config
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
_MODEL = "claude-sonnet-4-6"
_VALID_DAYS = frozenset({"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"})
_DAY_CRON = {"Mon": "1", "Tue": "2", "Wed": "3", "Thu": "4", "Fri": "5", "Sat": "6", "Sun": "0"}
_DAY_WEEKDAY = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
_GITHUB_URL_RE = re.compile(
    r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$"
)

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


# ── GitHub API helpers ────────────────────────────────────────────────────────

def _gh_api_get(owner: str, repo: str, path: str, branch: str) -> tuple[str, str]:
    result = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}/contents/{path}?ref={branch}"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise PreflightError(f"Cannot read {path} from {owner}/{repo}: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    return base64.b64decode(data["content"]).decode("utf-8"), data.get("sha", "")


def _gh_api_put(owner: str, repo: str, path: str, content: str, message: str, sha: str, branch: str) -> None:
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    args = [
        "gh", "api", f"repos/{owner}/{repo}/contents/{path}",
        "-X", "PUT",
        "-f", f"message={message}",
        "-f", f"content={encoded}",
        "-f", f"branch={branch}",
    ]
    if sha:
        args += ["-f", f"sha={sha}"]
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise OSError(f"Cannot write {path} to {owner}/{repo}: {result.stderr.strip()}")


def _gh_api_upsert(owner: str, repo: str, path: str, content: str, message: str, branch: str) -> None:
    try:
        _, sha = _gh_api_get(owner, repo, path, branch)
    except PreflightError:
        sha = ""
    _gh_api_put(owner, repo, path, content, message, sha, branch)


def _parse_action_summary_url(url: str) -> tuple[str, str, str, str]:
    """Parse GitHub blob URL → (owner, repo, branch, full_path)."""
    m = _GITHUB_URL_RE.match(url.strip())
    if not m:
        raise ValueError("Expected https://github.com/owner/repo/blob/branch/path/_action-summary.md")
    owner, repo, branch, path = m.group(1), m.group(2), m.group(3), m.group(4)
    if not path.endswith("_action-summary.md"):
        raise ValueError("URL must point to a file named _action-summary.md")
    return owner, repo, branch, path


# ── Form collection ───────────────────────────────────────────────────────────

def _ask(prompt: str) -> str:
    return input(prompt).strip()


def _ask_loop(prompt: str, validate) -> str:
    while True:
        val = _ask(prompt)
        err = validate(val)
        if err is None:
            return val
        print(f"Invalid: {err}. Please re-enter.")


_BRIEF_EXTRACTION_SYSTEM = """\
You are a DR-PM project configuration extractor.
Given a project brief document and a component registry, extract structured configuration as JSON.
Return ONLY valid JSON — no explanation, no markdown fences — with these exact keys:
{
  "project_name": "Full project name",
  "project_slug": "lowercase-hyphens-only-max-30-chars",
  "recipients_internal": ["email@domain.com"],
  "recipients_client": ["email@domain.com"],
  "email_schedule": {
    "days": ["Sun", "Mon", "Tue", "Wed", "Thu"],
    "hour": 8,
    "timezone": "Asia/Jerusalem"
  },
  "phases": [
    {
      "id": "phase-slug",
      "name": "Phase Name",
      "description": "What this phase covers",
      "start_date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD",
      "component_ids": ["COMP-001", "COMP-002"]
    }
  ],
  "board_ui_notes": ""
}
Rules:
- Extract phases and their component assignments from the brief. If components are mentioned per phase, assign them. If not, split components evenly across phases.
- component_ids must match IDs from the provided component registry exactly.
- If no phases are specified, create two: Detail Design (first half of components) and Build (all components — components appear in all phases they actively belong to).
- Extract dates from the brief; if missing, estimate from today forward.
- days must be from: Mon Tue Wed Thu Fri Sat Sun
- timezone must be a valid IANA timezone name
- project_slug: lowercase, hyphens only, derived from project name
"""


def _extract_from_brief(brief_content: str, registry_text: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])
    user_msg = f"COMPONENT REGISTRY:\n{registry_text}\n\nPROJECT BRIEF:\n{brief_content}"
    try:
        msg = client.messages.create(
            model=_MODEL, max_tokens=2048,
            system=_BRIEF_EXTRACTION_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        return json.loads(msg.content[0].text)
    except json.JSONDecodeError:
        raise PreflightError("Claude returned malformed JSON from brief extraction")
    except anthropic.AuthenticationError:
        raise PreflightError("CLAUDE_API_KEY invalid")
    except anthropic.APIConnectionError as e:
        raise PreflightError(f"Claude API unreachable: {e}")


def collect_form() -> dict:
    print("=== DR-PM Initialization ===\n")

    url = _ask_loop(
        "_action-summary.md GitHub URL: ",
        lambda v: None if ("github.com" in v and "_action-summary.md" in v)
                  else "must be a GitHub URL ending in _action-summary.md",
    )
    try:
        owner, repo, branch, summary_path = _parse_action_summary_url(url)
    except ValueError as e:
        print(f"Invalid URL: {e}")
        sys.exit(1)
    engagement_folder = str(Path(summary_path).parent)
    repo_url = f"https://github.com/{owner}/{repo}"

    print(f"\nRepo:              {repo_url}")
    print(f"Engagement folder: {engagement_folder}")
    print(f"Branch:            {branch}\n")

    brief_path = _ask_loop(
        "Path to project brief file (any format — text, JSON, etc.): ",
        lambda v: None if Path(v).exists() else f"File not found: {v}",
    )
    brief_content = Path(brief_path).read_text(encoding="utf-8")

    print("\nReading component registry...")
    try:
        registry_text, _ = _gh_api_get(owner, repo, f"{engagement_folder}/01-decomposition/component-registry-v0.1.md", branch)
    except PreflightError as e:
        print(f"Could not read registry: {e}")
        sys.exit(1)

    print("Extracting configuration from brief (Claude)...")
    try:
        extracted = _extract_from_brief(brief_content, registry_text)
    except PreflightError as e:
        print(f"Extraction failed: {e}")
        sys.exit(1)

    print(f"\nExtracted:")
    print(f"  Project:   {extracted.get('project_name','')} / slug: {extracted.get('project_slug','')}")
    print(f"  Internal:  {', '.join(extracted.get('recipients_internal', [])) or '(none)'}")
    print(f"  Client:    {', '.join(extracted.get('recipients_client', [])) or '(none)'}")
    sched = extracted.get("email_schedule", {})
    print(f"  Schedule:  {' '.join(sched.get('days', []))} at {sched.get('hour','')}:00 {sched.get('timezone','')}")
    for p in extracted.get("phases", []):
        n = len(p.get("component_ids", []))
        print(f"  Phase:     {p['name']} {p.get('start_date','')} → {p.get('end_date','')} ({n} components)")

    confirm = _ask("\nProceed? (Enter to confirm, n to cancel): ")
    if confirm.lower() == "n":
        print("Cancelled. Edit the brief file and re-run.")
        sys.exit(0)

    board_ui_notes = _ask("Board UI notes (Enter to skip): ")

    return {
        "project_name": extracted["project_name"],
        "project_slug": extracted["project_slug"],
        "repo_url": repo_url,
        "engagement_folder": engagement_folder,
        "_owner": owner, "_repo": repo, "_branch": branch,
        "recipients_internal": extracted.get("recipients_internal", []),
        "recipients_client": extracted.get("recipients_client", []),
        "email_schedule": extracted["email_schedule"],
        "phases": extracted["phases"],
        "board_ui_notes": board_ui_notes or extracted.get("board_ui_notes", ""),
    }


# ── Pre-flight validation ─────────────────────────────────────────────────────

def _check_registry(form: dict) -> tuple[str, list[dict]]:
    owner, repo, branch = form["_owner"], form["_repo"], form["_branch"]
    reg_path = f"{form['engagement_folder']}/01-decomposition/component-registry-v0.1.md"
    try:
        text, _ = _gh_api_get(owner, repo, reg_path, branch)
    except PreflightError as e:
        raise PreflightError(f"Component registry not found: {e}")
    components = parse_registry(text)
    if not components:
        raise PreflightError(f"Registry not parseable at {reg_path}")
    return text, components


def _preflight_claude(form: dict, registry_text: str, summary_text: str) -> None:
    client = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])
    system = ("You are a DR framework project validator. Given a component registry, action summary, "
              "and phases definition, return JSON only with keys 'verdict' (PASS or FAIL) and 'issues' (array of strings).")
    user = (f"Registry:\n{registry_text}\n\nAction summary:\n{summary_text}\n\n"
            f"Phases:\n{json.dumps(form['phases'], indent=2)}")
    try:
        msg = client.messages.create(model=_MODEL, max_tokens=1024, system=system,
                                     messages=[{"role": "user", "content": user}])
        raw = msg.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw.strip())
        # Extract first JSON object if there's a preamble
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        result = json.loads(m.group() if m else raw)
    except (json.JSONDecodeError, AttributeError):
        result = {"verdict": "FAIL", "issues": ["Claude returned malformed validation response"]}
    except (anthropic.AuthenticationError, anthropic.APIConnectionError) as e:
        raise PreflightError(f"Claude API unavailable for validation: {e}")
    if result.get("verdict") == "FAIL":
        for issue in result.get("issues", []):
            print(f"  - {issue}")
        raise PreflightError("Pre-flight validation failed — resolve issues above and re-run")


def run_preflight(form: dict) -> str:
    """Runs all preflight checks. Returns registry_text for reuse by board generation."""
    registry_text, _ = _check_registry(form)
    owner, repo, branch = form["_owner"], form["_repo"], form["_branch"]
    summary_api_path = f"{form['engagement_folder']}/_action-summary.md"
    try:
        summary_text, _ = _gh_api_get(owner, repo, summary_api_path, branch)
    except PreflightError:
        print("Warning: _action-summary.md not found — all components will initialise at Pending HLD Approval")
        summary_text = ""
    _preflight_claude(form, registry_text, summary_text)
    return registry_text


# ── Board HTML generation ─────────────────────────────────────────────────────

def _build_prompt(form: dict, registry_text: str, guidelines: str, template: str) -> tuple[str, str]:
    components = parse_registry(registry_text)
    phases_fmt = "\n".join(
        f"{i+1}. {p['name']} ({p['id']}): {p['description']}. "
        f"DR steps {p['step_range'][0]}–{p['step_range'][1]}. Dates: {p['start_date']} to {p['end_date']}"
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


_DRPM_REPO_OWNER = "ZamirDataRaven"
_DRPM_REPO_NAME = "DR-PM"
_DRPM_REPO_BRANCH = "main"


def _read_drpm_file(filename: str) -> str:
    content, _ = _gh_api_get(_DRPM_REPO_OWNER, _DRPM_REPO_NAME, filename, _DRPM_REPO_BRANCH)
    return content


def generate_board_html(form: dict, registry_text: str) -> str:
    try:
        guidelines = _read_drpm_file("board_html_design_guidelines.md")
        template = _read_drpm_file("board_html_prompt_template.txt")
    except PreflightError as e:
        raise BoardGenerationError(f"Could not read template files from DR-PM repo: {e}")
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


# ── Board deploy ──────────────────────────────────────────────────────────────

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


# ── Workflow provisioning ─────────────────────────────────────────────────────

def _compute_cron(days: list[str], hour: int, timezone: str) -> str:
    offset_hours = int(datetime.now(ZoneInfo(timezone)).utcoffset().total_seconds() / 3600)
    utc_hour = (hour - offset_hours) % 24
    return f"0 {utc_hour} * * {','.join(_DAY_CRON[d] for d in days)}"


def _update_issue_template(form: dict) -> None:
    owner, repo, branch = form["_owner"], form["_repo"], form["_branch"]
    template_path = ".github/ISSUE_TEMPLATE/data-request.yml"
    new_option = f"        - \"{form['project_slug']} — {form['project_name']}\""
    try:
        content, sha = _gh_api_get(owner, repo, template_path, branch)
        if form["project_slug"] not in content:
            content = re.sub(
                r'(      options:\n)((?:        - .*\n)*)',
                lambda m: m.group(1) + m.group(2) + new_option + "\n",
                content,
            )
        _gh_api_put(owner, repo, template_path, content,
                    f"chore: add {form['project_slug']} to data-request template", sha, branch)
    except PreflightError:
        content = _DATA_REQUEST_TEMPLATE.replace("{project_options}", new_option)
        _gh_api_put(owner, repo, template_path, content,
                    f"chore: create data-request template for {form['project_slug']}", "", branch)


def _write_workflow(form: dict) -> None:
    sched = form["email_schedule"]
    cron = _compute_cron(sched["days"], sched["hour"], sched["timezone"])
    yaml_content = _WORKFLOW_YAML.format(
        project_name=form["project_name"],
        project_slug=form["project_slug"],
        cron_expression=cron,
        engagement_folder=form["engagement_folder"],
    )
    owner, repo, branch = form["_owner"], form["_repo"], form["_branch"]
    workflow_file = f"dr-pm-{form['project_slug']}-daily.yml"
    _gh_api_upsert(owner, repo, f".github/workflows/{workflow_file}", yaml_content,
                   f"chore: provision DR-PM daily workflow for {form['project_slug']}", branch)
    _update_issue_template(form)


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


def provision_workflow(form: dict) -> None:
    _write_workflow(form)
    _set_secrets(form)


# ── Config write ──────────────────────────────────────────────────────────────

def write_config(form: dict) -> None:
    slug = form["project_slug"]
    config = {
        "project_name": form["project_name"], "project_slug": slug,
        "repo_url": form["repo_url"], "engagement_folder": form["engagement_folder"],
        "board_url": f"http://{_DO_HOST}/dr-pm/{slug}/",
        "do_host": _DO_HOST,
        "recipients": {"internal": form["recipients_internal"], "client": form["recipients_client"]},
        "email_schedule": form["email_schedule"], "email_enabled": True,
        "phases": [{"id": p["id"], "name": p["name"], "description": p["description"],
                    "start_date": p["start_date"], "end_date": p["end_date"],
                    "component_ids": p["component_ids"]}
                   for p in form["phases"]],
        "nginx_auth_user": "drpm",
        "initialized_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        validate_config(config)
    except ConfigValidationError as e:
        print(f"Config validation failed: {e}")
        sys.exit(1)
    owner, repo, branch = form["_owner"], form["_repo"], form["_branch"]
    config_path = f"DR PM/{form['engagement_folder']}/dr-pm-config.json"
    try:
        _gh_api_upsert(owner, repo, config_path, json.dumps(config, indent=2),
                       f"chore: initialise DR-PM for {config['project_name']}", branch)
    except OSError as e:
        print(f"Config write failed: {e}")
        sys.exit(1)


# ── Summary and main ──────────────────────────────────────────────────────────

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


def _preflight_and_generate(form: dict) -> str:
    print("\nRunning pre-flight checks...")
    try:
        registry_text = run_preflight(form)
    except PreflightError as e:
        print(f"Pre-flight failed: {e}")
        sys.exit(1)
    print("\nGenerating board HTML (Claude API Call 2)...")
    try:
        html = generate_board_html(form, registry_text)
    except (BoardGenerationError, BoardHTMLValidationError) as e:
        print(f"Board generation failed: {e}")
        sys.exit(1)
    return html


def _run_init(form: dict) -> None:
    html = _preflight_and_generate(form)
    print("\nDeploying board to droplet...")
    try:
        deploy_board(html, form["project_slug"])
    except DeployError as e:
        print(f"Deploy failed: {e}")
        sys.exit(1)
    print("\nProvisioning GitHub Actions workflow...")
    try:
        provision_workflow(form)
    except (ProvisionError, FileNotFoundError) as e:
        print(f"Provisioning failed: {e}")
        sys.exit(1)
    print("\nWriting config to project repo...")
    write_config(form)


def main() -> None:
    try:
        form = collect_form()
    except KeyboardInterrupt:
        print("\nInitialization cancelled.")
        sys.exit(0)
    _run_init(form)
    sched = form["email_schedule"]
    next_run = _compute_next_run(sched["days"], sched["hour"], sched["timezone"])
    print_summary(form, next_run)


if __name__ == "__main__":
    main()
