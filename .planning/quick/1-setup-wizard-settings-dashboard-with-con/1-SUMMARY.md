---
phase: quick-1
plan: "01"
subsystem: setup-wizard
tags: [setup, dashboard, settings, provider-keys, env-vars]
dependency_graph:
  requires: []
  provides: [settings-dashboard, provider-key-persistence, env-var-priority]
  affects: [docker/setup.html, docker/gateway.py, docker/entrypoint.sh]
tech_stack:
  added: []
  patterns: [savedKeys-in-memory-persistence, inline-edit-dashboard, os.environ.get-priority]
key_files:
  created: []
  modified:
    - docker/entrypoint.sh
    - docker/setup.html
    - docker/gateway.py
decisions:
  - "Dashboard shows on /setup when configured, wizard shown only for re-run; avoids forcing re-run wizard on every settings change"
  - "savedKeys stored in-memory JS object + persisted in setup.json saved_keys field; survives provider switches and page reloads"
  - "Dashboard inline edit: per-field edit buttons toggle .editing class; only fields in .editing state contribute to save payload"
  - "Credential field in dashboard uses single input (password/text) rather than replicating full buildCredFields complexity"
metrics:
  duration: "~15 min"
  completed_date: "2026-03-10"
  tasks_completed: 2
  files_modified: 3
---

# Quick Task 1: Setup Wizard Settings Dashboard with Provider Key Persistence — Summary

**One-liner:** Settings dashboard on /setup for already-configured agents, per-field inline editing, and savedKeys persistence across provider switches in the wizard.

## What Was Built

Three coordinated improvements to the setup/configuration experience:

### Task 1: Env Var Priority Fix (entrypoint.sh)

The re-hydration Python block that exports env vars from setup.json on container restart was changed to check `os.environ.get()` before each export. This means Railway/Docker env vars take priority over stored setup.json values. All four export paths gated:

- `CLAUDE_CODE_OAUTH_TOKEN` (claude-code provider)
- Provider API key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.)
- `TELEGRAM_BOT_TOKEN`
- `TZ`

### Task 2: Settings Dashboard + Provider Key Persistence (setup.html + gateway.py)

**Settings Dashboard:**
- `showDashboard(config)` renders when `GET /api/setup/config` returns `configured: true`
- Hides wizard progress bar and reconfig banner; shows `step-dashboard` div
- Displays Provider (icon + name), Credentials (masked), Model, Timezone, Telegram status, Auth Token status
- Each field has an `edit` button that activates `.editing` class, showing inline input/select
- `saveDashboardChanges()` collects only edited-field values, merges with current config, POSTs to `/api/setup/save`
- On success: collapses editing fields, updates displayed values, shows brief success banner
- "Re-run Setup Wizard" button calls `switchToWizard()` which restores wizard UI from step 0

**Provider Key Persistence:**
- `let savedKeys = {}` JS variable added at page level
- `buildCredFields()` now auto-fills the credential input from `savedKeys[selectedProvider]` after rendering
- `saveConfig()` saves current key into `savedKeys[selectedProvider]` before POSTing; includes `config.saved_keys = savedKeys` in payload
- `fetch('/api/setup/config')` on page load pre-fills `savedKeys` from `data.config.saved_keys`
- Handles all provider variants: string keys (standard, claude-code, ollama), object keys (azure-openai, custom)

**gateway.py saved_keys masking:**
- `handle_get_config()` now iterates `saved_keys` dict in the safe copy and applies same masking logic (first 6 + "..." + last 4 for values > 12 chars) to each value

## Commits

| Task | Commit | Description |
|------|--------|-------------|
| Task 1 | 7eea990 | fix(quick-1-01): gate setup.json re-hydration exports behind os.environ.get checks |
| Task 2 | 16ddef7 | feat(quick-1-01): add settings dashboard and provider key persistence |

## Deviations from Plan

None - plan executed exactly as written.

## Success Criteria Check

- [x] entrypoint.sh re-hydration block has os.environ.get guards on all 4 export paths
- [x] /setup shows settings dashboard when already configured
- [x] Dashboard allows editing individual fields without re-running wizard
- [x] Provider API keys persist across provider switches via saved_keys
- [x] Existing wizard flow is unchanged for first-time setup

## Self-Check: PASSED

- docker/entrypoint.sh: FOUND
- docker/setup.html: FOUND
- docker/gateway.py: FOUND
- 1-SUMMARY.md: FOUND
- commit 7eea990: FOUND
- commit 16ddef7: FOUND
