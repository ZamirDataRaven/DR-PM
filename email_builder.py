from __future__ import annotations

import os
import smtplib
import time
import warnings
from collections import Counter
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic

from config_manager import ConfigLoadError, ConfigValidationError, load_config


class EmailBuildError(Exception):
    pass


class EmailSendError(Exception):
    pass


_NARRATIVE_MODEL = "claude-sonnet-4-6"
_NARRATIVE_MAX_TOKENS = 500
_FONT = "font-family:Arial,Helvetica,sans-serif"

_NARRATIVE_SYSTEM = (
    "You are a concise project status writer for a software delivery team. "
    "Write a 2–3 sentence plain-text narrative summarising today's project progress. "
    "Be factual and specific. Reference actual numbers and component names from the data. "
    "Do not use bullet points, headers, or markdown. Output plain text only."
)

_STATUS_COLORS = {"done": "#22c55e", "in_progress": "#a855f7", "tbd": "#9ca3af"}
# Canonical source: data_collector.STEP_COLORS — kept local to avoid cross-module path dependency
STEP_COLORS = {
    "Pending HLD Approval": "#52526e", "Pending Spec Approval": "#a855f7",
    "In Development": "#eab308", "Unit Test": "#f97316",
    "Review": "#ef4444", "PR Evidence": "#22c55e",
}


# ── Issue row (Issue #34) ─────────────────────────────────────────────────────

def _format_issue_row(issue: dict, issue_type: str) -> str:
    style = (
        "background-color:#fef2f2;border-left:3px solid #ef4444"
        if issue_type == "blocker"
        else "background-color:#fefce8;border-left:3px solid #eab308"
    )
    td = f'style="padding:8px;{_FONT};font-size:13px"'
    return (
        f'<tr style="{style}">'
        f'<td {td}>[{issue.get("comp_id", "")}]</td>'
        f'<td {td}>#{issue.get("issue_number", "")} {issue.get("title", "")}</td>'
        f'<td {td}>{issue.get("opened", "")}</td>'
        f'</tr>'
    )


# ── Email body helpers (Issue #36) ────────────────────────────────────────────

def _section(title: str, content: str) -> str:
    hdr = (
        f'<tr><td style="background:#f5f5f5;padding:8px 24px">'
        f'<p style="{_FONT};font-size:11px;font-weight:600;color:#666;'
        f'text-transform:uppercase;letter-spacing:0.08em;margin:0">{title}</p></td></tr>'
    )
    return hdr + f'<tr><td style="padding:16px 24px">{content}</td></tr>'


def _phase_table(phases: list, components: dict) -> str:
    rows = ""
    for p in phases:
        step_counts: dict = {}
        for comp_id in p.get("component_ids", []):
            step = components.get(comp_id, {}).get("step", "Unknown")
            step_counts[step] = step_counts.get(step, 0) + 1
        step_parts = " &nbsp;·&nbsp; ".join(
            f'<span style="color:{STEP_COLORS.get(s,"#666")};font-weight:600">{c}× {s}</span>'
            for s, c in sorted(step_counts.items())
        ) or '<span style="color:#999">No components</span>'
        rows += (
            f'<tr><td style="padding:8px 0;{_FONT};font-size:14px;font-weight:600;vertical-align:top;width:180px">{p.get("name","")}</td>'
            f'<td style="padding:8px 0;{_FONT};font-size:13px">{step_parts}</td></tr>'
        )
    return f'<table width="100%" style="border-collapse:collapse">{rows or "<tr><td>No phases</td></tr>"}</table>'


def _step_table(components: dict) -> str:
    counts = Counter(c.get("step", "Unknown") for c in components.values())
    rows = ""
    for step, count in sorted(counts.items()):
        color = STEP_COLORS.get(step, "#333")
        rows += f'<tr><td style="padding:4px 0;{_FONT};font-size:14px;color:{color}">{step}</td><td style="padding:4px 0;{_FONT};font-size:14px;font-weight:600;text-align:right">{count}</td></tr>'
    return f'<table width="100%" style="border-collapse:collapse">{rows or "<tr><td>No components</td></tr>"}</table>'


def _advances_html(advances: list) -> str:
    if not advances:
        return f'<p style="{_FONT};font-size:14px;color:#666;margin:0">None</p>'
    items = "".join(
        f'<li style="{_FONT};font-size:14px;color:#333;margin:4px 0"><strong>{a["comp_id"]}</strong> → <strong>{a["new_step"]}</strong></li>'
        for a in advances
    )
    return f'<ul style="margin:0;padding-left:20px">{items}</ul>'


def _issues_table(issues: list, issue_type: str) -> str:
    if not issues:
        return f'<p style="{_FONT};font-size:14px;color:#666;margin:0">None</p>'
    th = f'style="padding:8px;{_FONT};font-size:12px;font-weight:600;text-align:left;background:#f5f5f5"'
    rows = "".join(_format_issue_row(i, issue_type) for i in issues)
    return (
        f'<table width="100%" style="border-collapse:collapse">'
        f'<tr><th {th}>Component</th><th {th}>Issue</th><th {th}>Opened</th></tr>'
        f'{rows}</table>'
    )


def _email_header(pname: str, board_url: str, today: str) -> str:
    return (
        f'<tr><td style="background:#1e1e2e;padding:24px;text-align:center">'
        f'<p style="color:#e0e0f0;{_FONT};font-size:18px;font-weight:600;margin:0 0 8px">{pname}</p>'
        f'<p style="color:#a0a0c0;{_FONT};font-size:13px;margin:0 0 16px">{today}</p>'
        f'<a href="{board_url}" style="background:#a855f7;color:#fff;{_FONT};font-size:13px;padding:8px 20px;text-decoration:none;border-radius:4px">View Live Board</a>'
        f'</td></tr>'
    )


def build_email_body(payload: dict, delta: dict, config: dict) -> str:
    pname = config.get("project_name", "")
    board_url = config.get("board_url", "")
    today = payload.get("generated", "")[:10]
    body = (
        _email_header(pname, board_url, today)
        + _section("Phase Status", _phase_table(payload.get("phases", []), payload.get("components", {})))
        + _section("Component Progress", _step_table(payload.get("components", {})))
        + _section("New Progress (Last 24 Hours)", _advances_html(delta.get("advances", [])))
        + _section("Active Blockers", _issues_table(payload.get("blockers", []), "blocker"))
        + _section("Data Requests", _issues_table(payload.get("data_requests", []), "data-request"))
    )
    return (
        f'<table width="100%" style="background:#f5f5f5;margin:0;padding:0">'
        f'<tr><td align="center" style="padding:24px 0">'
        f'<table width="600" style="background:#ffffff;border-collapse:collapse">{body}</table>'
        f'</td></tr></table>'
    )


# ── Claude narrative (Issue #35) ──────────────────────────────────────────────

def _build_prompt(payload: dict, delta: dict) -> str:
    phases = "\n".join(
        f"{p['name']}: {p.get('status','tbd')} ({len(p.get('component_ids',[]))} components)"
        for p in payload.get("phases", [])
    )
    steps = Counter(c.get("step", "Unknown") for c in payload.get("components", {}).values())
    step_text = "\n".join(f"{n} at {s}" for s, n in steps.items()) or "None"
    advances = "\n".join(
        f"{a['comp_id']} advanced to {a['new_step']}" for a in delta.get("advances", [])
    ) or "None"
    return (
        f"Project: {payload.get('project', '')}\nDate: {payload.get('generated', '')[:10]}\n\n"
        f"Phase summary:\n{phases}\n\nComponent progress:\n{step_text}\n\n"
        f"New progress in last 24 hours:\n{advances}\n\n"
        f"Active blockers: {len(payload.get('blockers', []))}\n"
        f"Active data requests: {len(payload.get('data_requests', []))}"
    )


def _call_claude_narrative(client: anthropic.Anthropic, prompt: str):
    for attempt in range(2):
        try:
            return client.messages.create(
                model=_NARRATIVE_MODEL,
                max_tokens=_NARRATIVE_MAX_TOKENS,
                system=_NARRATIVE_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.AuthenticationError:
            raise EmailBuildError("CLAUDE_API_KEY invalid — narrative not generated")
        except anthropic.APIConnectionError:
            if attempt == 0:
                time.sleep(30)
            else:
                raise EmailBuildError("Claude API unreachable after retry")


def write_narrative(payload: dict, delta: dict) -> str:
    client = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])
    msg = _call_claude_narrative(client, _build_prompt(payload, delta))
    if not msg or not msg.content:
        raise EmailBuildError("Claude returned empty narrative")
    text = msg.content[0].text
    if msg.stop_reason == "max_tokens":
        warnings.warn("Claude narrative truncated at max_tokens")
        text = text + " [...]"
    return text


# ── SMTP send (Issue #37) ─────────────────────────────────────────────────────

def _smtp_send(subject: str, html_body: str, recipients: list[str]) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_SENDER_ADDRESS"]
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(os.environ["SMTP_SERVER"], int(os.environ["SMTP_PORT"]), timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
        failed = server.sendmail(os.environ["SMTP_SENDER_ADDRESS"], recipients, msg.as_string())
        if failed:
            for addr, err in failed.items():
                warnings.warn(f"Email failed to {addr}: {err}")


def send_email(subject: str, html_body: str, recipients: list[str]) -> None:
    for attempt in range(2):
        try:
            _smtp_send(subject, html_body, recipients)
            return
        except smtplib.SMTPAuthenticationError:
            raise EmailSendError("SMTP auth failed — check SMTP_USERNAME and SMTP_PASSWORD")
        except smtplib.SMTPRecipientsRefused as e:
            raise EmailSendError(f"One or more recipients rejected: {e.recipients}")
        except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, TimeoutError, OSError) as e:
            if attempt == 0:
                time.sleep(30)
            else:
                raise EmailSendError(f"SMTP connection failed after retry: {e}")


# ── Orchestrator (Issue #38) ──────────────────────────────────────────────────

def send(payload: dict, delta: dict, project_repo_root: str, engagement_folder: str) -> None:
    try:
        config = load_config(project_repo_root, engagement_folder)
    except (ConfigLoadError, ConfigValidationError) as e:
        raise EmailBuildError(f"Config load failed: {e}")
    if not config.get("email_enabled", True):
        warnings.warn("Email disabled — skipping send")
        return
    recipients = config["recipients"]["internal"] + config["recipients"]["client"]
    if not recipients:
        warnings.warn("No recipients configured — skipping email send")
        return
    html_body = build_email_body(payload, delta, config)
    today = payload.get("generated", "")[:10]
    send_email(f"DR-PM | {config['project_name']} | {today}", html_body, recipients)
