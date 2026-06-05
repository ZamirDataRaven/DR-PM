from __future__ import annotations

import json
import re
from pathlib import Path


class ConfigValidationError(Exception):
    pass


class ConfigLoadError(Exception):
    pass


VALID_DAYS = frozenset({"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"})
_EMAIL_RE = re.compile(r"[^@]+@[^@]+\.[^@]+")


def _check_recipients(recipients, errors: list) -> None:
    if not isinstance(recipients, dict):
        errors.append("'recipients': required dict with 'internal' and 'client' arrays")
        return
    for key in ("internal", "client"):
        lst = recipients.get(key)
        if not isinstance(lst, list):
            errors.append(f"'recipients.{key}': required array of strings")
        elif not all(isinstance(e, str) and _EMAIL_RE.match(e) for e in lst):
            errors.append(f"'recipients.{key}': each element must be a valid email address")


def _check_schedule(schedule, errors: list) -> None:
    if not isinstance(schedule, dict):
        errors.append("'email_schedule': required dict with 'days', 'hour', 'timezone'")
        return
    days = schedule.get("days")
    if not isinstance(days, list) or not days:
        errors.append("'email_schedule.days': required non-empty array")
    elif not all(d in VALID_DAYS for d in days):
        errors.append(f"'email_schedule.days': each element must be one of {sorted(VALID_DAYS)}")
    hour = schedule.get("hour")
    if not isinstance(hour, int) or isinstance(hour, bool) or not 0 <= hour <= 23:
        errors.append("'email_schedule.hour': required int 0–23")
    if not isinstance(schedule.get("timezone"), str) or not schedule["timezone"].strip():
        errors.append("'email_schedule.timezone': required non-empty string")


def _check_phases(phases, errors: list) -> None:
    if not isinstance(phases, list) or not phases:
        errors.append("'phases': required non-empty array of phase objects")
        return
    for i, phase in enumerate(phases):
        if not isinstance(phase, dict):
            errors.append(f"'phases[{i}]': must be an object")
            continue
        for field in ("id", "name", "description", "start_date", "end_date"):
            if not isinstance(phase.get(field), str):
                errors.append(f"'phases[{i}].{field}': required string")
        if not isinstance(phase.get("component_ids"), list):
            errors.append(f"'phases[{i}].component_ids': required array (may be empty)")
        if (all(isinstance(phase.get(d), str) for d in ("start_date", "end_date"))
                and phase["end_date"] < phase["start_date"]):
            errors.append(f"'phases[{i}].end_date': must be >= start_date")


def validate_config(config: dict) -> None:
    errors = []
    for field in ("project_name", "project_slug", "repo_url", "engagement_folder",
                  "board_url", "do_host", "nginx_auth_user", "initialized_at"):
        if not isinstance(config.get(field), str) or not config[field].strip():
            errors.append(f"'{field}': required non-empty string")
    if isinstance(config.get("repo_url"), str) and not config["repo_url"].startswith("https://github.com/"):
        errors.append("'repo_url': must begin with 'https://github.com/'")
    if not isinstance(config.get("email_enabled"), bool):
        errors.append("'email_enabled': required bool")
    _check_recipients(config.get("recipients"), errors)
    _check_schedule(config.get("email_schedule"), errors)
    _check_phases(config.get("phases"), errors)
    if errors:
        raise ConfigValidationError("Config validation failed:\n" + "\n".join(f"  - {e}" for e in errors))


def load_config(project_repo_root: str, engagement_folder: str) -> dict:
    path = Path(project_repo_root) / "DR PM" / engagement_folder / "dr-pm-config.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigLoadError(
            f"dr-pm-config.json not found at {path}. Run Create_PPT to initialise the project."
        )
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigLoadError(f"Invalid JSON in dr-pm-config.json at {path}: {e}")
    validate_config(config)
    return config


def save_config(config: dict, project_repo_root: str, engagement_folder: str) -> None:
    validate_config(config)
    path = Path(project_repo_root) / "DR PM" / engagement_folder / "dr-pm-config.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise OSError(f"Failed to create directory {path.parent}: {e}") from e
    try:
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    except OSError as e:
        raise OSError(f"Failed to write config to {path}: {e}") from e
