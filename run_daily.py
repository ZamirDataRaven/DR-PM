#!/usr/bin/env python3
"""DR-PM daily orchestrator — invoked by dr-pm-daily.yml."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from data_collector import collect, DataCollectorError
from board_updater import update, BoardUpdateError, BoardDeployError
from email_builder import send, EmailBuildError, EmailSendError


def _log(msg: str) -> None:
    print(f"[DR-PM {datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def main() -> None:
    project_repo_root = os.environ["DR_PM_PROJECT_REPO_ROOT"]
    engagement_folder = os.environ["DR_PM_ENGAGEMENT_FOLDER"]
    github_token = os.environ["GITHUB_TOKEN"]

    _log("=== DR-PM daily run starting ===")
    exit_code = 0

    # Step 1 — Data collection (blocking: board and email cannot run without data)
    try:
        _log("Step 1: Data collection")
        payload, delta = collect(project_repo_root, engagement_folder, github_token)
        _log("Step 1: complete")
    except DataCollectorError as e:
        _log(f"Step 1 FAILED: {e}")
        _log("Aborting — board and email skipped.")
        sys.exit(1)

    # Step 2 — Board update (non-blocking for email: email still sends even if board fails)
    try:
        _log("Step 2: Board update")
        update(payload, project_repo_root, engagement_folder)
        _log("Step 2: complete")
    except (BoardUpdateError, BoardDeployError) as e:
        _log(f"Step 2 FAILED: {e}")
        _log("Board update failed — continuing to email.")
        exit_code = 1  # mark run as failed but do not abort email

    # Step 3 — Email send (always attempted if data collection succeeded)
    try:
        _log("Step 3: Email send")
        send(payload, delta, project_repo_root, engagement_folder)
        _log("Step 3: complete")
    except (EmailBuildError, EmailSendError) as e:
        _log(f"Step 3 FAILED: {e}")
        exit_code = 1

    _log(f"=== DR-PM daily run complete (exit {exit_code}) ===")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
