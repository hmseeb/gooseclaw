---
phase: 01-template-engine-and-code-generation
plan: 03
subsystem: codegen
tags: [python, rest-api, http, urllib, mcp, template]

# Dependency graph
requires:
  - phase: 01-01
    provides: generate_extension() function and base_helpers.py.tmpl boilerplate
provides:
  - rest_api.py.tmpl with api_get, api_post, api_put, api_delete MCP tools
  - _build_headers() supporting bearer, api_key_header, and basic auth
  - _make_request() HTTP helper using urllib.request (stdlib)
affects:
  - 02-01 (registration will generate REST API extensions from this template)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Auth type pattern: configurable via ${auth_type} template variable (bearer/api_key_header/basic)"
    - "Custom header pattern: ${auth_header} allows per-service API key header names"
    - "_make_request() centralizes HTTP logic with error handling for HTTPError and URLError"

key-files:
  created:
    - docker/extensions/templates/rest_api.py.tmpl
    - docker/tests/test_rest_api_template.py
  modified: []

key-decisions:
  - "Used urllib.request (stdlib) over requests library — no external dependency per GEN-03"
  - "Three auth patterns: bearer token, API key header, basic auth — covers most REST APIs"
  - "Tool functions accept JSON strings for params/body — MCP tool args must be simple types"

patterns-established:
  - "REST template auth: _build_headers() reads auth_type at generation time, api_key from vault at runtime"
  - "HTTP error handling: HTTPError returns status_code/reason/body dict, URLError returns reason string"

requirements-completed: [TMPL-03]

# Metrics
duration: 3min
completed: 2026-04-01
---

# Phase 1 Plan 03: REST API Template Summary

**Generic REST API MCP server template with GET/POST/PUT/DELETE tools, configurable auth (bearer/API key/basic), urllib-based HTTP, and vault credential reads**

## Performance

- **Duration:** 3 min
- **Started:** 2026-04-01T05:39:00Z
- **Completed:** 2026-04-01T05:42:00Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Created rest_api.py.tmpl with 4 MCP tools: api_get, api_post, api_put, api_delete
- _build_headers() supports bearer token, API key header, and basic auth patterns
- _make_request() centralizes HTTP logic with urllib.request, handles JSON parsing and error reporting
- Configurable auth_type and auth_header via template variables with sensible defaults
- 6 tests covering generation validity, tool presence, vault reads, no-requests check, stdout redirect, auth variations

## Task Commits

Each task was committed atomically:

1. **Task 1: Create REST API template** - `bf7b06f` (feat)
2. **Task 2: Add REST API template generation tests** - `5550759` (test)

## Files Created/Modified
- `docker/extensions/templates/rest_api.py.tmpl` - REST API MCP server template with HTTP tools
- `docker/tests/test_rest_api_template.py` - 6 tests for REST API template generation

## Decisions Made
- urllib.request over requests library (stdlib requirement)
- Three auth patterns cover majority of REST API authentication methods
- JSON string params for MCP tool compatibility (MCP args are simple types)

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- REST API template ready for Phase 2 extension generation
- Template supports any service with API key or bearer token auth
- auth_type and auth_header configurable at generation time via extra_subs

---
*Phase: 01-template-engine-and-code-generation*
*Completed: 2026-04-01*
