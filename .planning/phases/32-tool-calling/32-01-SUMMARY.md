---
phase: 32-tool-calling
plan: 01
subsystem: voice
tags: [gemini-live, tool-calling, goosed, mcp]

requires:
  - phase: 28-voice-foundation
    provides: _gemini_build_config, _voice_parse_server_message, test_voice.py
provides:
  - "_discover_voice_tools() — queries goosed /config, returns Gemini function declarations + name map"
  - "_voice_execute_tool() — relays tool requests through goosed _do_rest_relay"
  - "_voice_build_tool_response() — builds Gemini toolResponse JSON with SILENT scheduling"
  - "_gemini_build_config() tools parameter — includes functionDeclarations in setup config"
affects: [32-tool-calling]

tech-stack:
  added: []
  patterns: [name-sanitization-for-gemini, silent-scheduling-tool-response]

key-files:
  created: []
  modified:
    - docker/gateway.py
    - docker/tests/test_voice.py

key-decisions:
  - "SILENT scheduling on toolResponse prevents Gemini from double-speaking raw results"
  - "Name sanitization replaces non-alphanumeric chars with underscores for Gemini compatibility"
  - "Tool results truncated to 2000 chars to stay within Gemini context limits"

patterns-established:
  - "Tool discovery: query goosed /config, filter enabled extensions, sanitize names"
  - "Tool execution: build natural language prompt, relay through _do_rest_relay"
  - "Tool response: always send toolResponse even on error to prevent Gemini hang"

requirements-completed: [TOOL-01, TOOL-02, TOOL-03, TOOL-05, TOOL-06]

duration: 4min
completed: 2026-03-27
---

# Plan 32-01: Tool Calling Core Functions Summary

**TDD-built _discover_voice_tools, _voice_execute_tool, _voice_build_tool_response with Gemini function declaration format and SILENT scheduling**

## Performance

- **Duration:** 4 min
- **Started:** 2026-03-27
- **Completed:** 2026-03-27
- **Tasks:** 2 (RED + GREEN)
- **Files modified:** 2

## Accomplishments
- _discover_voice_tools() queries goosed /config and returns Gemini-compatible function declarations for all enabled extensions
- Extension name sanitization (hyphens/dots/spaces to underscores) with reverse mapping for routing
- _voice_execute_tool() relays tool requests through goosed _do_rest_relay with 15s timeout and 2000-char truncation
- _voice_build_tool_response() builds correct Gemini toolResponse JSON with SILENT scheduling
- _gemini_build_config() accepts optional tools parameter for including functionDeclarations
- 15 new tests covering discovery, execution, response, and config tools parameter

## Task Commits

Each task was committed atomically:

1. **Task 1: RED — Failing tests** - `3f6fb81` (test)
2. **Task 2: GREEN — Implementation** - `f066c5a` (feat)

_TDD plan: RED-GREEN cycle, no refactor needed._

## Files Created/Modified
- `docker/gateway.py` - Added _discover_voice_tools, _voice_execute_tool, _voice_build_tool_response; updated _gemini_build_config
- `docker/tests/test_voice.py` - Added TestToolDiscovery (6 tests), TestToolExecution (4 tests), TestToolResponse (3 tests), 2 config tests

## Decisions Made
- SILENT scheduling on toolResponse prevents Gemini from double-speaking raw tool results
- Results truncated to 2000 chars to avoid overwhelming Gemini context
- Graceful fallback: returns empty list on goosed errors or non-200 responses

## Deviations from Plan
None - plan executed exactly as written.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Core functions ready for Plan 02 to wire into relay loop
- _gemini_build_config tools parameter ready for _gemini_connect passthrough

---
*Phase: 32-tool-calling*
*Completed: 2026-03-27*
