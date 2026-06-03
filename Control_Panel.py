#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import zoneinfo

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
