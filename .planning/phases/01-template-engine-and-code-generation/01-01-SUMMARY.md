---
phase: 01-template-engine-and-code-generation
plan: 01
subsystem: codegen
tags: [python, string-template, mcp, code-generation, vault]

# Dependency graph
requires: []
provides:
  - generate_extension() function rendering .py.tmpl templates via string.Template
  - list_templates() for discovering available service templates
  - base_helpers.py.tmpl with _vault_get(), stdout redirect, FastMCP init
  - docker/extensions/ package structure
affects:
  - 01-02 (email template depends on generator + base_helpers)
  - 01-03 (REST API template depends on generator + base_helpers)
  - 02-01 (registration will call generate_extension)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "string.Template.safe_substitute for .py.tmpl rendering — no Jinja2 dependency"
    - "base_helpers.py.tmpl concatenated before service template — shared boilerplate pattern"
    - "Monkeypatch TEMPLATES_DIR and OUTPUT_BASE_DIR for testable generation paths"

key-files:
  created:
    - docker/extensions/__init__.py
    - docker/extensions/generator.py
    - docker/extensions/templates/base_helpers.py.tmpl
    - docker/tests/test_generator.py
  modified: []

key-decisions:
  - "Used string.Template over Jinja2 — stdlib only, sufficient for variable substitution in Python source"
  - "base_helpers uses $$ escaping for literal dollars in f-strings within templates"
  - "Generator module exposes TEMPLATES_DIR and OUTPUT_BASE_DIR as module-level vars for test monkeypatching"

patterns-established:
  - "Template rendering: base_helpers.py.tmpl + service.py.tmpl concatenated, then safe_substitute"
  - "Test isolation: monkeypatch TEMPLATES_DIR and OUTPUT_BASE_DIR to tmp_path fixtures"

requirements-completed: [TMPL-01, TMPL-04, TMPL-05, GEN-01, GEN-02, GEN-03]

# Metrics
duration: 3min
completed: 2026-04-01
---

# Phase 1 Plan 01: Generator Engine Summary

**string.Template-based code generation engine producing standalone MCP server .py files from .py.tmpl templates with vault credential reads and stdout redirect**

## Performance

- **Duration:** 3 min
- **Started:** 2026-04-01T05:30:00Z
- **Completed:** 2026-04-01T05:38:00Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments
- Created docker/extensions/ package with generator.py engine using string.Template.safe_substitute
- Created base_helpers.py.tmpl with _vault_get() subprocess vault reader, sys.stdout redirect, FastMCP init
- generate_extension() renders templates to /data/extensions/{name}/server.py with executable permissions
- list_templates() discovers .py.tmpl files excluding base_helpers
- 9 unit tests covering generation, syntax validity, vault patterns, idempotency, permissions

## Task Commits

Each task was committed atomically:

1. **Task 1: Create generator module with string.Template engine** - `445d8f4` (feat)
2. **Task 2: Add unit tests for generator** - `0b6d654` (test)

## Files Created/Modified
- `docker/extensions/__init__.py` - Package init
- `docker/extensions/generator.py` - Template rendering engine with generate_extension() and list_templates()
- `docker/extensions/templates/base_helpers.py.tmpl` - Shared boilerplate: vault helper, stdout redirect, FastMCP
- `docker/tests/test_generator.py` - 9 unit tests for generator module

## Decisions Made
- Used string.Template (stdlib) over Jinja2 per research recommendation — variable substitution is all we need
- Module-level TEMPLATES_DIR and OUTPUT_BASE_DIR constants for easy test monkeypatching
- base_helpers concatenated before service template so shared code appears once at top

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Generator engine ready for 01-02 (email template) and 01-03 (REST API template)
- base_helpers.py.tmpl provides all shared boilerplate — service templates just add tools
- Test fixtures established for template testing pattern (monkeypatch dirs)

---
*Phase: 01-template-engine-and-code-generation*
*Completed: 2026-04-01*
