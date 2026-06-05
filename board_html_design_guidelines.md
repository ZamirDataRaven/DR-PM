# Board HTML Design Guidelines — DR-PM Project Progress Tracker

**Version:** v0.1
**Date:** 2026-05-31
**Owner:** Zamir
**Status:** Approved
**Consumed by:** COMP-PM-002 Board HTML Template

These guidelines are the standing design rules Claude uses when generating the per-project HTML board at initialization. They apply to every project. Per-project customisation (colour preferences, layout notes) provided in the admin form's Board UI notes field may override or extend these rules for that project only.

---

## 1. Theme

Light gray only. No dark mode. No responsive breakpoints required — fixed-width desktop layout (minimum 1200px).

No branding. No DR logo, wordmark, or identity marks anywhere on the board.

---

## 2. Self-Containment

The HTML file must be fully self-contained — all CSS in a `<style>` block within the `<head>`, all JavaScript inline in a `<script>` block. No external CDN links, no external font imports, no image src attributes pointing to remote URLs.

---

## 3. Typography

Font stack: `-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif`

| Role | Size | Weight | Color |
| :---- | :---- | :---- | :---- |
| Page title (project name) | 22px | 600 | `#e0e0f0` |
| Section header (zone label) | 13px | 500 | `#7070a0` uppercase, letter-spacing 0.08em |
| Component name (card primary) | 14px | 500 | `#e0e0f0` |
| Component ID (badge) | 11px | 500 | `#7070a0` |
| Issue title | 13px | 400 | `#c0c0d8` |
| Metadata (date, count) | 12px | 400 | `#6060880` |

---

## 4. Color Palette

### Page structure

| Element | Color |
| :---- | :---- |
| Page background | `#f4f4f8` |
| Zone / panel background | `#eaeaf2` |
| Card background | `#ffffff` |
| Card border (default) | `#d0d0e4` |
| Divider / separator | `#dddde8` |
| Primary text | `#1c1c2e` |
| Secondary / muted text | `#7878a8` |

### Workflow step colors

Used as the primary accent on component cards (left border strip, 4px wide) and as phase bar segment colors.
Step labels correspond exactly to the 10 DR framework steps (00–09).
No purple or pink anywhere — accent palette is green → teal → blue → yellow → orange → red → green.

| Workflow step | Color | Hex |
| :---- | :---- | :---- |
| HLD Review | Gray | `#52526e` |
| HLD Decomposition | Emerald green | `#059669` |
| Create Spec | Teal | `#0d9488` |
| Spec to Task | Blue | `#3b82f6` |
| Req Verification | Cyan | `#06b6d4` |
| Coding Assist | Yellow | `#eab308` |
| Unit Test | Orange | `#f97316` |
| Code Review | Red | `#ef4444` |
| Pre-Commit Review | Dark orange | `#ea580c` |
| PR Evidence | Green | `#22c55e` |

### Phase status colors (phase bar segments)

| Phase status | Color |
| :---- | :---- |
| Done (all components at PR Evidence) | `#22c55e` (green) |
| In progress (≥1 component active) | `#059669` (emerald green) |
| TBD (no components started) | `#52526e` (gray) |

### Stream identity badge

Each board displays a stream badge in the page header (next to the project title) identifying which stream it belongs to. Format: small pill, background = primary stream color, white text, `border-radius: 20px`, `font-size: 11px`, `font-weight: 600`.
Stream color assignments: Stream 2 = `#059669` (emerald green). Set per project in board_ui_notes at Create_PPT time.

### Alert border overrides (component cards)

| Condition | Full card border | Width |
| :---- | :---- | :---- |
| Active blocker | `#ef4444` (red) | 2px |
| Active data request | `#eab308` (yellow) | 2px |
| Both present | `#ef4444` (red — takes precedence) | 2px |

---

## 5. Layout — Three Zones

### Page header (above all zones)

Full-width light bar (`background: #f4f4f8`, `padding: 32px 24px`). Content centered:
- Project name: 22px, 600 weight, `#e0e0f0`, centered
- Generated date + board URL: 13px, `#7070a0`, centered, below project name
- "View Live Board" button: purple `#a855f7`, white text, `border-radius: 6px`, centered, below date line

### Top zone — Phase bar

- Full-width horizontal bar (`width: 100%`, `display: flex`, no `flex-wrap`)
- One segment per phase, ordered left to right
- **Proportional width**: each segment's flex value equals its `component_ids.length`; phases with 0 components get `flex: 1` (minimum visible sliver). Set via JS: `seg.style.flex = String(Math.max(phase.component_ids.length, 1))`
- Segment: rounded pill (`border-radius: 6px`), colored background (phase status color), phase name in white 13px 600 weight, `min-width: 0`
- Active (selected/clicked) segment: full opacity; inactive: 65% opacity
- Clicking a segment filters the middle zone to show only that phase's components; clicking again deselects (shows all)
- Phase count badge (e.g. "4 components") shown inside segment in 11px muted text

### Middle zone — Component grid

- CSS grid, 4 columns by default; Claude may adjust to 3 or 5 based on component count
- Each card: `border-radius: 8px`, background `#1e1e2e`, left border strip 4px in workflow step color, `box-shadow: 0 2px 8px rgba(0,0,0,0.4)`
- Card content (top to bottom):
  1. Component ID badge (top-left, 11px, muted `#7070a0`)
  2. Component name (14px, 500 weight, `#e0e0f0`)
  3. Open/closed task count (12px, muted — e.g. "3 open · 2 closed")
  4. **Current DR step — displayed prominently at the bottom of the card** as a full-width pill/tag: step label text (e.g. "Pending HLD Approval") in the matching step color (see §4), 12px, 500 weight, centered, with a faint background tint of that same color at 15% opacity
- The step label at the bottom must always be visible without interaction — it is not hidden behind a hover or click
- If component has active blocker or data request, override card border per §4 alert rules
- Clicking a card expands an inline task list below the card (accordion style, no modal) showing open and closed issues for that component, with issue number, title, and open/closed status

### Bottom zone — Active issues list

- Two sub-sections: **Blockers** (red `#ef4444` section header accent) and **Data Requests** (yellow `#eab308` section header accent)
- Each entry: `[COMP-ID]` badge + issue title + date opened (relative: "3 days ago")
- Sorted oldest first within each sub-section
- If no blockers: Blockers sub-section shows "No active blockers" in muted text
- If no data requests: Data Requests sub-section shows "No active data requests" in muted text

---

## 6. Data Layer

The board's dynamic data is stored in a JavaScript object (`window.DR_PM_DATA`) in a `<script>` block near the bottom of `<body>`. The daily runner (COMP-PM-005) replaces this object in-place — no other part of the HTML is modified.

```javascript
window.DR_PM_DATA = {
  project: "Project Name",
  generated: "2026-05-31T08:00:00Z",
  board_url: "http://[DO-IP]/dr-pm/[slug]/",
  phases: [
    {
      id: "phase-1",
      name: "Phase Name",
      status: "in_progress",  // "done" | "in_progress" | "tbd"
      component_ids: ["COMP-001", "COMP-004"]  // NOTE: field is component_ids, NOT components
    }
  ],
  components: {
    "COMP-001": {
      name: "Webhook Handler",          // real component name from registry
      phase: "phase-1",
      step: "HLD Decomposition",        // one of the 10 DR step labels (see §4)
      step_color: "#7c3aed",
      has_blocker: false,
      has_data_request: false,
      tasks: {
        open: [{ number: 12, title: "..." }],
        closed: [{ number: 8, title: "..." }]
      }
    }
  },
  blockers: [
    { component_id: "COMP-001", issue_number: 15, title: "...", opened: "2026-05-28" }
  ],
  data_requests: []
};
```

---

*Confidential — Internal Use Only | Data Raven Technologies*
