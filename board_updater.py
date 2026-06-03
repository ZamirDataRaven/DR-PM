from __future__ import annotations

import json
import os
import re
import subprocess
import warnings
from pathlib import Path

from config_manager import ConfigLoadError, ConfigValidationError, load_config


class BoardUpdateError(Exception):
    pass


class BoardDeployError(Exception):
    pass


_DATA_RE = re.compile(
    r"(window\.DR_PM_DATA\s*=\s*)\{.*?\}(\s*;)",
    re.DOTALL,
)


# ── Payload injection (Issue #27) ─────────────────────────────────────────────

def inject_payload(html: str, payload: dict) -> str:
    matches = _DATA_RE.findall(html)
    if not matches:
        raise BoardUpdateError(
            "window.DR_PM_DATA not found in index.html — board may be corrupted; re-run Create_PPT"
        )
    if len(matches) > 1:
        raise BoardUpdateError(
            "Multiple window.DR_PM_DATA assignments found in index.html — board is malformed"
        )
    repl = json.dumps(payload, indent=2)
    return _DATA_RE.sub(lambda m: m.group(1) + repl + m.group(2), html)


# ── rsync helpers (Issues #28) ────────────────────────────────────────────────

def _rsync_pull(slug: str, do_host: str, do_user: str, ssh_key_path: str, tmp: Path) -> None:
    remote = f"{do_user}@{do_host}:/var/www/dr-pm/{slug}/index.html"
    ssh_opt = f"ssh -i {ssh_key_path} -o StrictHostKeyChecking=no"
    try:
        result = subprocess.run(
            ["rsync", "-az", "-e", ssh_opt, remote, str(tmp)],
            capture_output=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise BoardDeployError("rsync timed out after 60s")
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        if "No such file" in stderr:
            raise BoardUpdateError(
                f"index.html not found at /var/www/dr-pm/{slug}/ — has this project been initialised with Create_PPT?"
            )
        raise BoardUpdateError(
            f"rsync pull failed — check DO_SSH_KEY and droplet availability: {stderr}"
        )


def deploy(local_html_path: Path, project_slug: str, do_host: str, do_user: str, ssh_key_path: str) -> None:
    remote = f"{do_user}@{do_host}:/var/www/dr-pm/{project_slug}/index.html"
    ssh_opt = f"ssh -i {ssh_key_path} -o StrictHostKeyChecking=no"
    try:
        result = subprocess.run(
            ["rsync", "-az", "-e", ssh_opt, str(local_html_path), remote],
            capture_output=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise BoardDeployError("rsync timed out after 60s")
    if result.returncode != 0:
        raise BoardDeployError(f"rsync failed: {result.stderr.decode(errors='replace').strip()}")


# ── Top-level orchestrator (Issue #29) ───────────────────────────────────────

def update(payload: dict, project_repo_root: str, engagement_folder: str) -> None:
    try:
        config = load_config(project_repo_root, engagement_folder)
    except (ConfigLoadError, ConfigValidationError) as e:
        raise BoardUpdateError(f"Config load failed: {e}")
    slug = config["project_slug"]
    do_host = config["do_host"]
    do_user = os.environ.get("DR_PM_DO_USER", "root")
    ssh_key_path = os.environ["DR_PM_SSH_KEY_PATH"]
    tmp = Path(f"/tmp/dr-pm-{slug}-index.html")
    _rsync_pull(slug, do_host, do_user, ssh_key_path, tmp)
    try:
        updated = inject_payload(tmp.read_text(encoding="utf-8"), payload)
        tmp.write_text(updated, encoding="utf-8")
        deploy(tmp, slug, do_host, do_user, ssh_key_path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError as e:
            warnings.warn(f"Could not delete temp file {tmp}: {e}")
