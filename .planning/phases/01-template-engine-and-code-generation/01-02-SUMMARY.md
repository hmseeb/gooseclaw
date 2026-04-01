---
phase: 01-template-engine-and-code-generation
plan: 02
subsystem: codegen
tags: [python, email, imap, smtp, mcp, template]

# Dependency graph
requires:
  - phase: 01-01
    provides: generate_extension() function and base_helpers.py.tmpl boilerplate
provides:
  - email_imap.py.tmpl with email_search, email_read, email_send MCP tools
  - IMAP4_SSL and SMTP_SSL connection helpers reading credentials from vault
  - Email header decoding utility
affects:
  - 02-01 (registration will generate email extensions from this template)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "IMAP connection helper pattern: _get_imap_connection() reads host/user/password from vault"
    - "SMTP connection helper pattern: _get_smtp_connection() reads host/user/password from vault"
    - "Connection cleanup in finally blocks for all tool functions"

key-files:
  created:
    - docker/extensions/templates/email_imap.py.tmpl
    - docker/tests/test_email_template.py
  modified: []

key-decisions:
  - "Used IMAP4_SSL (port 993) and SMTP_SSL (port 465) as defaults — covers Fastmail, Gmail, most modern providers"
  - "email_search uses OR-combined IMAP SEARCH across subject, from, and text fields"
  - "email_read falls back to stripped HTML if no text/plain part available"

patterns-established:
  - "Service template structure: email-specific imports at top, connection helpers, @mcp.tool() functions, if __name__ entry point"
  - "Vault reads inside connection helpers, not in tool functions directly — single point of credential access"

requirements-completed: [TMPL-02]

# Metrics
duration: 3min
completed: 2026-04-01
---

# Phase 1 Plan 02: Email IMAP/SMTP Template Summary

**Email MCP server template with IMAP search/read and SMTP send tools, vault-based credential reads, and stdlib-only imports (imaplib, smtplib, email)**

## Performance

- **Duration:** 3 min
- **Started:** 2026-04-01T05:39:00Z
- **Completed:** 2026-04-01T05:42:00Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Created email_imap.py.tmpl with 3 MCP tools: email_search, email_read, email_send
- IMAP search across subject/from/text with configurable folder and limit
- Email read with text/plain preference and HTML fallback stripping
- SMTP send via SMTP_SSL with vault-based sender address
- All connections created via helper functions reading from vault, closed in finally blocks
- 5 tests covering generation validity, tool presence, vault reads, stdlib deps, stdout redirect

## Task Commits

Each task was committed atomically:

1. **Task 1: Create email IMAP/SMTP template** - `d0b2f1d` (feat)
2. **Task 2: Add email template generation tests** - `c410bb3` (test)

## Files Created/Modified
- `docker/extensions/templates/email_imap.py.tmpl` - Email MCP server template with search/read/send
- `docker/tests/test_email_template.py` - 5 tests for email template generation

## Decisions Made
- Used IMAP4_SSL/SMTP_SSL as defaults (modern provider standard)
- OR-combined IMAP SEARCH for flexible email discovery
- HTML stripping as fallback when no plaintext part exists

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Email template ready for Phase 2 extension generation and registration
- Template uses vault_prefix pattern compatible with credential detector

---
*Phase: 01-template-engine-and-code-generation*
*Completed: 2026-04-01*
