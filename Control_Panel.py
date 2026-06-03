#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import zoneinfo
from pathlib import Path

import anthropic

from config_manager import ConfigLoadError, ConfigValidationError, load_config, save_config, VALID_DAYS
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+")


# ── Subcommand actions ────────────────────────────────────────────────────────

def action_status(config: dict) -> None:
    sched = config.get("email_schedule", {})
    days = " ".join(sched.get("days", []))
    hour = sched.get("hour", 0)
    tz = sched.get("timezone", "")
    enabled = str(config.get("email_enabled", False)).lower()
    internal = ", ".join(config["recipients"]["internal"]) or "(none)"
    client = ", ".join(config["recipients"]["client"]) or "(none)"
    print(f"=== DR-PM Config — {config.get('project_slug', '')} ===")
    print(f"Project:        {config.get('project_name', '')}")
    print(f"Board URL:      {config.get('board_url', '')}")
    print(f"Email enabled:  {enabled}")
    print(f"Schedule:       {days} at {hour:02d}:00 {tz}")
    print(f"Recipients (internal): {internal}")
    print(f"Recipients (client):   {client}")
    print("Phases:")
    for p in config.get("phases", []):
        comp_ids = ", ".join(p.get("component_ids", []))
        print(f"  {p.get('id','')}  {p.get('name','')}  ({p.get('start_date','')} → {p.get('end_date','')})  [{comp_ids}]")
    print(f"Initialized:    {config.get('initialized_at', '')}")


def action_toggle_email(config: dict, project_repo_root: str, engagement_folder: str) -> None:
    config["email_enabled"] = not config["email_enabled"]
    try:
        save_config(config, project_repo_root, engagement_folder)
    except (ConfigValidationError, OSError) as e:
        print(f"Error saving config: {e}")
        sys.exit(1)
    state = "ENABLED" if config["email_enabled"] else "DISABLED"
    print(f"Email sending: {state}")


def action_edit_recipients(config: dict, project_repo_root: str, engagement_folder: str, action: str, recipient_type: str, email: str) -> None:
    if not _EMAIL_RE.match(email):
        print(f"Invalid email address: {email}")
        sys.exit(1)
    lst = config["recipients"][recipient_type]
    if action == "add":
        if email in lst:
            print("Already present — no change.")
            return
        lst.append(email)
        msg = f"Added {email} to {recipient_type} recipients."
    else:
        if email not in lst:
            print("Address not found — no change.")
            return
        lst.remove(email)
        msg = f"Removed {email} from {recipient_type} recipients."
    try:
        save_config(config, project_repo_root, engagement_folder)
    except (ConfigValidationError, OSError) as e:
        print(f"Error saving config: {e}")
        sys.exit(1)
    print(f"Done. {msg}")


def action_edit_schedule(config: dict, project_repo_root: str, engagement_folder: str, days: list[str], hour: int, timezone: str) -> None:
    for d in days:
        if d not in VALID_DAYS:
            print(f"Invalid day: {d}. Allowed: Mon Tue Wed Thu Fri Sat Sun")
            sys.exit(1)
    if not 0 <= hour <= 23:
        print("Hour must be 0–23")
        sys.exit(1)
    if timezone not in zoneinfo.available_timezones():
        print(f"Unknown timezone: {timezone}. Use IANA timezone names (e.g. Asia/Jerusalem)")
        sys.exit(1)
    config["email_schedule"] = {"days": days, "hour": hour, "timezone": timezone}
    try:
        save_config(config, project_repo_root, engagement_folder)
    except (ConfigValidationError, OSError) as e:
        print(f"Error saving config: {e}")
        sys.exit(1)
    print(f"Schedule updated: {' '.join(days)} at {hour:02d}:00 {timezone}")
    print("Note: also update the cron expression in dr-pm-daily.yml in the project repo.")


def action_refresh(config: dict) -> None:
    repo_url = config.get("repo_url", "")
    workflow_file = f"dr-pm-{config.get('project_slug', 'project')}-daily.yml"
    try:
        subprocess.run(
            ["gh", "workflow", "run", workflow_file, "--repo", repo_url],
            capture_output=True, timeout=30, check=True,
        )
    except FileNotFoundError:
        print("gh CLI not found. Install from https://cli.github.com and authenticate.")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("gh CLI timed out after 30s.")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace").strip()
        print(f"gh CLI failed: {stderr}. Run 'gh auth login' and retry.")
        sys.exit(1)
    print(f"Manual refresh triggered. Check: {repo_url}/actions")


# ── Config update from brief file ────────────────────────────────────────────

_UPDATE_EXTRACTION_SYSTEM = """\
You are a DR-PM project configuration extractor.
Given a project brief and a component registry, extract updated configuration as JSON.
Return ONLY valid JSON with these exact keys:
{
  "recipients_internal": ["email@domain.com"],
  "recipients_client": ["email@domain.com"],
  "email_schedule": {"days": ["Sun","Mon","Tue","Wed","Thu"], "hour": 8, "timezone": "Asia/Jerusalem"},
  "phases": [
    {
      "id": "phase-slug",
      "name": "Phase Name",
      "description": "Description",
      "start_date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD",
      "component_ids": ["COMP-001", "COMP-002"]
    }
  ]
}
Rules:
- component_ids must match IDs from the provided registry exactly
- days must be from: Mon Tue Wed Thu Fri Sat Sun
- timezone must be a valid IANA timezone name
- Return JSON only, no explanation
"""


def _gh_api_get_registry(repo_url: str, engagement_folder: str) -> str:
    owner_repo = repo_url.replace("https://github.com/", "").rstrip("/")
    path = f"{engagement_folder}/01-decomposition/component-registry-v0.1.md"
    result = subprocess.run(
        ["gh", "api", f"repos/{owner_repo}/contents/{path}?ref=main"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise ConfigLoadError(f"Cannot read registry from GitHub: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    return base64.b64decode(data["content"]).decode("utf-8")


def action_update_config(config: dict, project_repo_root: str, engagement_folder: str, brief_path: str) -> None:
    brief_content = Path(brief_path).read_text(encoding="utf-8")
    print("Reading component registry from GitHub...")
    try:
        registry_text = _gh_api_get_registry(config["repo_url"], engagement_folder)
    except ConfigLoadError as e:
        print(f"Error: {e}")
        sys.exit(1)
    print("Extracting updated configuration from brief (Claude)...")
    try:
        client = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])
        msg = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=2048,
            system=_UPDATE_EXTRACTION_SYSTEM,
            messages=[{"role": "user", "content": f"REGISTRY:\n{registry_text}\n\nBRIEF:\n{brief_content}"}],
        )
        extracted = json.loads(msg.content[0].text)
    except json.JSONDecodeError:
        print("Error: Claude returned malformed JSON")
        sys.exit(1)
    except anthropic.AuthenticationError:
        print("Error: CLAUDE_API_KEY invalid")
        sys.exit(1)

    print(f"\nExtracted:")
    print(f"  Internal:  {', '.join(extracted.get('recipients_internal', [])) or '(none)'}")
    print(f"  Client:    {', '.join(extracted.get('recipients_client', [])) or '(none)'}")
    sched = extracted.get("email_schedule", {})
    print(f"  Schedule:  {' '.join(sched.get('days', []))} at {sched.get('hour', '')}:00 {sched.get('timezone', '')}")
    for p in extracted.get("phases", []):
        print(f"  Phase:     {p['name']} {p.get('start_date','')} → {p.get('end_date','')} ({len(p.get('component_ids',[]))} components)")

    confirm = input("\nApply these changes? (Enter to confirm, n to cancel): ").strip()
    if confirm.lower() == "n":
        print("Cancelled.")
        sys.exit(0)

    config["recipients"] = {
        "internal": extracted.get("recipients_internal", config["recipients"]["internal"]),
        "client": extracted.get("recipients_client", config["recipients"]["client"]),
    }
    if "email_schedule" in extracted:
        config["email_schedule"] = extracted["email_schedule"]
    if "phases" in extracted:
        config["phases"] = extracted["phases"]

    try:
        save_config(config, project_repo_root, engagement_folder)
    except (ConfigValidationError, OSError) as e:
        print(f"Error saving config: {e}")
        sys.exit(1)
    print("Config updated. Push changes: git -C <project-repo-root> add -A && git push")


# ── CLI setup and dispatch ────────────────────────────────────────────────────

def _setup_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DR-PM Control Panel")
    parser.add_argument("--project-repo-root", required=True)
    parser.add_argument("--engagement-folder", required=True)
    sub = parser.add_subparsers(dest="subcommand", required=True)
    sub.add_parser("status")
    sub.add_parser("toggle-email")
    sub.add_parser("refresh")
    for name in ("add-recipient", "remove-recipient"):
        p = sub.add_parser(name)
        p.add_argument("--type", dest="recipient_type", required=True, choices=["internal", "client"])
        p.add_argument("--email", required=True)
    p_s = sub.add_parser("set-schedule")
    p_s.add_argument("--days", required=True)
    p_s.add_argument("--hour", type=int, required=True)
    p_s.add_argument("--tz", required=True)
    p_u = sub.add_parser("update-config")
    p_u.add_argument("--brief", required=True, help="Path to updated project brief file")
    return parser


def _dispatch(args: argparse.Namespace, config: dict, root: str, folder: str) -> None:
    cmd = args.subcommand
    if cmd == "status":
        action_status(config)
    elif cmd == "toggle-email":
        action_toggle_email(config, root, folder)
    elif cmd in ("add-recipient", "remove-recipient"):
        action_edit_recipients(config, root, folder, cmd.split("-")[0], args.recipient_type, args.email)
    elif cmd == "set-schedule":
        action_edit_schedule(config, root, folder, args.days.split(","), args.hour, args.tz)
    elif cmd == "refresh":
        action_refresh(config)
    elif cmd == "update-config":
        action_update_config(config, root, folder, args.brief)


def main() -> None:
    args = _setup_parser().parse_args()
    root, folder = args.project_repo_root, args.engagement_folder
    try:
        config = load_config(root, folder)
    except (ConfigLoadError, ConfigValidationError) as e:
        print(f"Error loading config: {e}")
        sys.exit(1)
    _dispatch(args, config, root, folder)


if __name__ == "__main__":
    main()
