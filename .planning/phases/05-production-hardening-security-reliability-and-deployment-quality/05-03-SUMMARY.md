---
phase: 05-production-hardening
plan: 03
subsystem: infra
tags: [security, shell-injection, auth, hashing, sha256, eval, shlex]

requires:
  - phase: quick-1-01
    provides: entrypoint.sh with os.environ.get guards and gateway.py with auth token handling

provides:
  - Safe config rehydration in entrypoint.sh using mktemp+source+shlex.quote (no eval injection)
  - Vault variable name sanitization for YAML keys with hyphens/spaces
  - SHA-256 token hashing in setup.json (plaintext never stored on disk)
  - Gateway-owns-all-auth architecture with internal random token for goose web subprocess

affects:
  - 05-04
  - 05-05
  - 05-06

tech-stack:
  added: [hashlib (stdlib, already available)]
  patterns:
    - mktemp+source instead of eval "$(python3 -c ...)" for safe shell variable export
    - shlex.quote() for all externally-sourced values in shell scripts
    - re.sub sanitization of YAML keys to valid shell variable names
    - SHA-256 hash-before-store for auth tokens (stdlib only, no bcrypt)
    - gateway-owns-all-auth -- gateway verifies user, proxies with internal token

key-files:
  created: []
  modified:
    - docker/entrypoint.sh
    - docker/gateway.py

key-decisions:
  - "mktemp+source instead of eval -- writes Python output to temp file then sources it; safer, debuggable, temp file cleaned up immediately"
  - "shlex.quote() for all values -- prevents shell injection even if setup.json contains special characters like semicolons, backticks, dollar signs"
  - "re.sub(r'[^A-Z0-9_]', '_', env_name) for vault variable names -- YAML keys with hyphens (my-service) produce invalid shell vars (GOOSECLAW_MY-SERVICE_KEY); sanitize to underscores"
  - "SHA-256 not bcrypt -- Python stdlib only constraint, auth tokens are high-entropy random strings (not passwords), timing attacks not a concern for this use case"
  - "gateway-owns-all-auth -- goose web gets internal random token; users never know it; gateway authenticates users against stored hash then proxies with internal token"
  - "Backward compatible -- old setup.json with plaintext web_auth_token still works (get_auth_token returns is_hashed=False); hashed on next save"
  - "Expanded provider env_map in entrypoint.sh -- added mistral, xai, deepseek, together, cerebras, perplexity, avian, venice, ovhcloud (was only 5 providers, now 14)"

patterns-established:
  - "Safe shell rehydration: mktemp + python3 shlex.quote > file + source file + rm file"
  - "Auth tuple pattern: get_auth_token() -> (stored, is_hashed); check_auth() handles both formats"
  - "Internal token pattern: subprocess gets random token; proxy layer injects it; user-facing auth is separate"

requirements-completed: [SEC-06, SEC-07]

duration: 15min
completed: 2026-03-11
---

# Phase 05 Plan 03: Security Hardening -- Eval Injection and Auth Token Hashing Summary

**Eliminated eval shell injection in entrypoint.sh via mktemp+shlex.quote pattern, and implemented SHA-256 token hashing with gateway-owns-all-auth architecture so plaintext secrets never touch disk**

## Performance

- **Duration:** ~15 min
- **Started:** 2026-03-11T00:00:00Z
- **Completed:** 2026-03-11T00:15:00Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Replaced both `eval "$(python3 -c ...)"` blocks in entrypoint.sh with safe mktemp+source+shlex.quote pattern, eliminating the critical shell injection vector where a malicious setup.json value could execute arbitrary commands
- Added re.sub sanitization for vault GOOSECLAW_* variable names -- YAML keys with hyphens/spaces previously produced invalid shell variable names
- Expanded provider env_map coverage in entrypoint.sh from 5 providers to 14 (adding mistral, xai, deepseek, together, cerebras, perplexity, avian, venice, ovhcloud)
- SHA-256 hashing of auth tokens before storage in setup.json -- plaintext never written to disk, only displayed once to user in setup response
- Gateway-owns-all-auth: goose web subprocess receives random internal token; gateway authenticates users against hash, then proxies with internal token (users never see internal token)
- Full backward compatibility: old plaintext web_auth_token in setup.json still works until next save

## Task Commits

Each task was committed atomically:

1. **Task 1: Replace eval injection with safe export mechanism in entrypoint.sh** - `5acb1b3` (fix)
2. **Task 2: Hash auth tokens and implement gateway-owns-all-auth architecture** - `bc99521` (feat)

**Plan metadata:** (docs commit - see final_commit)

## Files Created/Modified
- `docker/entrypoint.sh` - Replaced eval+python3 with mktemp+source+shlex.quote; expanded provider env_map; added vault variable name sanitization
- `docker/gateway.py` - Added hashlib + hash_token/verify_token; modified get_auth_token() to return (token, is_hashed) tuple; handle_save() hashes before store; start_goose_web() uses internal random token; proxy_to_goose() injects internal token into Authorization header

## Decisions Made
- SHA-256 over bcrypt: Python stdlib only constraint applies (no pip installs). Auth tokens are high-entropy random strings (secrets.token_urlsafe), not user-chosen passwords. SHA-256 is sufficient.
- Gateway-owns-all-auth is required because: after hashing, passing the stored hash to `goose web --auth-token` would break auth (user sends plaintext, goose web has hash, they would never match). The internal token solves this cleanly.
- mktemp+source over eval: easier to debug (file content visible), same security properties once values are properly quoted, temp file cleaned up immediately after sourcing.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing Critical] Masked web_auth_token_hash in /api/setup/config response**
- **Found during:** Task 2 (gateway.py changes)
- **Issue:** handle_get_config() masked "web_auth_token" but not "web_auth_token_hash" -- the hash field is new and wasn't in the masking list; exposing it could aid offline cracking attempts
- **Fix:** Added "web_auth_token_hash" to the masking list in handle_get_config()
- **Files modified:** docker/gateway.py
- **Verification:** Field now masked in config response
- **Committed in:** bc99521 (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (Rule 2 - missing critical security masking)
**Impact on plan:** Necessary for completeness. No scope creep.

## Issues Encountered
None -- plan executed smoothly.

## User Setup Required
None - no external service configuration required. Changes are transparent to users (backward compatible).

## Next Phase Readiness
- entrypoint.sh and gateway.py are now secure against the two most critical injection/disclosure vulnerabilities
- Ready for Phase 05-04 (remaining security hardening items)
- Existing deployments: on next /api/setup/save call, token gets hashed automatically

---
*Phase: 05-production-hardening*
*Completed: 2026-03-11*
