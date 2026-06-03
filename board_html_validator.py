from __future__ import annotations

import re


class BoardHTMLValidationError(Exception):
    pass


_ZONE_MARKERS = ("<!-- phase-bar -->", "<!-- component-grid -->", "<!-- issues-list -->")
_EXTERNAL_LINK_RE = re.compile(r'<link[^>]+href=["\']?http|<script[^>]+src=["\']?http')


def validate_board_html(html: str) -> None:
    errors = []
    stripped = html.strip()
    if not stripped.startswith("<!DOCTYPE html"):
        errors.append("HTML does not begin with <!DOCTYPE html>")
    if not stripped.endswith("</html>"):
        errors.append("HTML does not end with </html> — may be truncated")
    if "window.DR_PM_DATA" not in html:
        errors.append("window.DR_PM_DATA not found — data layer missing")
    if "<style>" not in html:
        errors.append("<style> block not found — CSS must be inline")
    if "<script>" not in html:
        errors.append("<script> block not found — JS must be inline")
    ext_match = _EXTERNAL_LINK_RE.search(html)
    if ext_match:
        errors.append(f"External link found — board must be self-contained: {ext_match.group()[:80]}")
    for marker in _ZONE_MARKERS:
        if marker not in html:
            errors.append(f"Zone marker missing: {marker}")
    if errors:
        raise BoardHTMLValidationError(
            "Board HTML validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )
