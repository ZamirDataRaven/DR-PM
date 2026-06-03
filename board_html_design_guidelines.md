# Board HTML Design Guidelines — DR-PM Project Progress Tracker

**Version:** v0.1
**Date:** 2026-05-31
**Owner:** Zamir
**Status:** Approved
**Consumed by:** COMP-PM-002 Board HTML Template

These guidelines are the standing design rules Claude uses when generating the per-project HTML board at initialization. They apply to every project. Per-project customisation (colour preferences, layout notes) provided in the admin form's Board UI notes field may override or extend these rules for that project only.

---

## 1. Theme

Dark only. No light mode. No responsive breakpoints required — fixed-width desktop layout (minimum 1200px).

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
| Page background | `#0f0f1a` |
| Zone / panel background | `#16162a` |
| Card background | `#1e1e2e` |
| Card border (default) | `#2a2a3e` |
| Divider / separator | `#252538` |

### Workflow step colors

Used as the primary accent on component cards (left border strip, 4px wide) and as phase bar segment colors.

| Workflow step | Color | Hex |
| :---- | :---- | :---- |
| Pending HLD Approval | Gray | `#52526e` |
| Pending Spec Approval | Purple | `#a855f7` |
| In Development | Yellow | `#eab308` |
| Unit Test | Orange | `#f97316` |
| Review | Red-pink | `#ef4444` |
| PR Evidence | Green | `#22c55e` |

### Phase status colors (phase bar segments)

| Phase status | Color |
| :---- | :---- |
| Done (all components at PR Evidence) | `#22c55e` (green) |
| In progress (≥1 component active) | `#a855f7` (purple) |
| TBD (no components started) | `#52526e` (gray) |

### Alert border overrides (component cards)

| Condition | Full card border | Width |
| :---- | :---- | :---- |
| Active blocker | `#ef4444` (red) | 2px |
| Active data request | `#eab308` (yellow) | 2px |
| Both present | `#ef4444` (red — takes precedence) | 2px |

---

## 5. Layout — Three Zones

### Top zone — Phase bar

- Full-width horizontal bar
- One segment per phase, ordered left to right
- Segment: rounded pill (`border-radius: 6px`), colored background (phase status color), phase name in white 13px 600 weight
- Active (selected/clicked) segment: full opacity; inactive: 65% opacity
- Clicking a segment filters the middle zone to show only that phase's components; clicking again deselects (shows all)
- Phase count badge (e.g. "4 components") shown inside segment in 11px muted text

### Middle zone — Component grid

- CSS grid, 4 columns by default; Claude may adjust to 3 or 5 based on component count
- Each card: `border-radius: 8px`, background `#1e1e2e`, left border strip 4px in workflow step color, `box-shadow: 0 2px 8px rgba(0,0,0,0.4)`
- Card content: component ID badge (top-left), component name (primary), current workflow step label (colored text matching step color, 12px)
- If component has active blocker or data request, override card border per §4 alert rules
- Clicking a card expands an inline task list below the card (accordion style, no modal) showing open and closed issues for that component

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
      components: ["COMP-PM-001", "COMP-PM-004"]
    }
  ],
  components: {
    "COMP-PM-001": {
      name: "Create_PPT",
      phase: "phase-1",
      step: "In Development",
      step_color: "#eab308",
      has_blocker: false,
      has_data_request: false,
      tasks: {
        open: [{ number: 12, title: "...", labels: [] }],
        closed: [{ number: 8, title: "..." }]
      }
    }
  },
  blockers: [
    { component_id: "COMP-PM-001", issue_number: 15, title: "...", opened: "2026-05-28" }
  ],
  data_requests: []
};
```

---

*Confidential — Internal Use Only | Data Raven Technologies*
