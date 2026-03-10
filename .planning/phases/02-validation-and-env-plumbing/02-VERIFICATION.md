---
phase: 02-validation-and-env-plumbing
verified: 2026-03-11T00:00:00Z
status: gaps_found
score: 4/5 must-haves verified
gaps:
  - truth: "Validation errors show provider-specific messages (not generic 'invalid key')"
    status: partial
    reason: "Backend dispatch_validation returns provider-specific messages in the 'message' field, but frontend validateKey() reads 'data.note' not 'data.message' for success display. Success messages always fall back to the generic unicode check + 'connection successful'. Error messages (data.error) work correctly."
    artifacts:
      - path: "docker/setup.html"
        issue: "validateKey() shows data.note as success text (line 2018), but backend never returns a 'note' field — only 'message'. Provider-specific success messages like 'Connected to Anthropic. API key is valid.' are never shown."
      - path: "docker/gateway.py"
        issue: "dispatch_validation returns {valid, message, ...} but no 'note' field. Frontend falls back to static text."
    missing:
      - "Change setup.html validateKey() lines 2018 and 2022 to read: showAlert('success', data.message || data.note || '\\u2713 connection successful')"
      - "This single-line fix resolves the field name mismatch and surfaces provider-specific success messages"
human_verification:
  - test: "Reconfigure flow visual check"
    expected: "After clicking Reconfigure from dashboard: provider card is highlighted, model field is pre-filled, credential fields show masked placeholders, save button is enabled"
    why_human: "DOM manipulation and placeholder rendering cannot be verified from static code analysis"
  - test: "Empty field blocking"
    expected: "Entering credentials step with empty API key and clicking Continue shows an error alert, not advancing to model step"
    why_human: "Requires browser interaction to verify event flow"
  - test: "Save button gating"
    expected: "On a fresh wizard session, save button on step 4 is disabled until Test passes. After passing Test, button enables."
    why_human: "Requires browser to verify disabled state and state transitions"
  - test: "Telegram token format validation"
    expected: "Entering '123badformat' in telegram token field and tabbing away shows inline error. Entering '123456789:ABCDefgh-xyz' clears the error."
    why_human: "Requires browser to verify onblur behavior"
---

# Phase 02: Validation and Env Plumbing Verification Report

**Phase Goal:** Every provider configuration is validated before save, persisted correctly, and restored on container restart without data loss
**Verified:** 2026-03-11T00:00:00Z
**Status:** gaps_found
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Every provider in the PROVIDERS registry has a matching env_map entry in gateway.py | VERIFIED | env_map in gateway.py has exactly 23 entries, confirmed via code inspection |
| 2 | entrypoint.sh rehydration covers all 23 providers | VERIFIED | All 23 provider strings present in entrypoint.sh rehydration block, including azure-openai, litellm, github-copilot, and local providers |
| 3 | User cannot save config with empty or malformed API key — save button is gated behind validation | VERIFIED | saveBtn has `disabled` attribute in HTML (line 1049), updateSaveBtn() syncs state from validationPassed, advanceFromCredentials() blocks empty fields per provider |
| 4 | After container restart, all previously configured env vars are restored | VERIFIED | entrypoint.sh rehydration block + gateway.py main() both call apply_config from setup.json; field_map in apply_config handles non-standard field names |
| 5 | Validation errors show provider-specific messages (not generic "invalid key") | PARTIAL | Error messages (data.error) are provider-specific and displayed correctly. Success messages fail: backend returns field "message" but frontend reads "data.note" — specific success text is never shown, falling back to generic "connection successful" |

**Score:** 4/5 truths verified (1 partial)

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `docker/gateway.py` | Complete env_map for all 23 providers, apply_config handles all provider types, dispatch_validation for all providers | VERIFIED | env_map has 23 entries; apply_config has field_map for AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, LITELLM_HOST, OLLAMA_HOST, GITHUB_TOKEN; dispatch_validation handles all 23 providers; apply_config called at startup (main()) and on save (handle_save()) |
| `docker/entrypoint.sh` | Complete rehydration env_map matching gateway.py | VERIFIED | Python rehydration block covers all 23 providers: single-key providers via env_map dict, plus explicit branches for claude-code, azure-openai, litellm, github-copilot, and local providers with OLLAMA_HOST |
| `docker/setup.html` | Frontend validation gating, format hints, pre-fill on reconfigure | VERIFIED (with gap) | validationPassed, getFormatHint(), validateFormat(), prefillWizard(), startReconfigure(), advanceFromCredentials() all present and wired; gap in validateKey() using data.note instead of data.message |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| docker/setup.html validateKey() | /api/setup/validate | fetch POST with provider_type + credentials | WIRED | Line 2009: `fetch('/api/setup/validate', ...)` sends flat payload with provider-specific field names (ollama_host, azure_endpoint, etc.) |
| docker/setup.html saveConfig() | /api/setup/save | fetch POST only after validation gate | WIRED | saveBtn starts disabled; updateSaveBtn() enables only when validationPassed=true; saveConfig() at line 2838 |
| docker/gateway.py handle_validate() | dispatch_validation() | routes to provider-specific validator | WIRED | Line 1425: `result = dispatch_validation(provider, credentials)` |
| docker/entrypoint.sh rehydration | docker/gateway.py env_map | env_map variable names must match | WIRED | Both files use identical env var names (ANTHROPIC_API_KEY, AZURE_OPENAI_API_KEY, etc.); field_map provides setup.json->env_var translation in apply_config |
| docker/gateway.py apply_config() | docker/gateway.py env_map | apply_config iterates env_map for env var setting | WIRED | Line 881-885: `for env_var in env_map.get(provider_type, [])` with field_map lookup |
| docker/gateway.py dispatch_validation() | Frontend success display | data.message field | BROKEN | Backend returns `{valid, message}` but frontend reads `data.note` (line 2018). Provider-specific success text never displayed. |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| PROV-04 | 02-01 | Each provider has correct env var mapping in gateway.py and entrypoint.sh | SATISFIED | env_map has 23 entries in both files; field_map in apply_config handles azure_key, azure_endpoint, litellm_host, ollama_host |
| PROV-05 | 02-01 | Each provider has a sensible default model | SATISFIED | default_models dict in gateway.py has 23 entries covering all providers |
| PROV-06 | 02-02, 02-03 | Each provider has a working validation endpoint | SATISFIED | dispatch_validation handles all 23 providers; real API calls for anthropic/google/openai-compat; format-only for avian/ovhcloud; skip_validation for claude-code/github-copilot; host check for local providers |
| CRED-01 | 02-02 | API key field rejects empty input before save | SATISFIED | advanceFromCredentials() blocks empty API keys with provider-specific checks; reconfigure path allows bypass only when dashboardConfig.{field}_set is true |
| CRED-02 | 02-02 | API key format validated per provider (prefix check, length check) | SATISFIED | validateFormat() in setup.html checks URL format for local providers, non-empty for claude-code/azure, prefix warnings for standard providers via PROVIDERS[].keyPrefix |
| CRED-03 | 02-02 | Save disabled until tested or explicitly skipped | SATISFIED | saveBtn disabled in HTML; updateSaveBtn() gates on validationPassed; local providers and github-copilot auto-set validationPassed; reconfigure auto-sets validationPassed |
| CRED-04 | 02-02, 02-03 | Validation errors show specific messages | PARTIAL | Error messages (data.error) are provider-specific and displayed. Success messages broken: frontend reads data.note but backend returns data.message; specific success text silently lost |
| CRED-05 | 02-02, 02-03 | Claude-code shows clear instructions since remote validation is impossible | SATISFIED | buildCredFields() for claude-code shows explicit instructions including `claude setup-token` command and subscription link; dispatch_validation returns skip_validation=True with actionable message |
| ENV-01 | 02-01 | entrypoint.sh reads ALL provider env vars from setup.json on restart | SATISFIED | Python rehydration block at line 130-192 covers all 23 providers with correct field->env var mappings |
| ENV-02 | 02-01 | gateway.py calls apply_config on startup when setup.json exists | SATISFIED | main() at line 1700: `apply_config(setup)` called before starting goose web |
| ENV-03 | 02-01 | All new providers (mistral, xai, deepseek, etc.) are in env_map in both files | SATISFIED | All 8 new providers confirmed present in both gateway.py env_map and entrypoint.sh rehydration |
| ENV-04 | 02-01 | PATH includes ~/.local/bin for claude CLI | SATISFIED | entrypoint.sh line 18: `export PATH="$HOME_DIR/.local/bin:$PATH"` present and not commented |
| UX-07 | 02-02 | Reconfigure pre-fills form with existing values (secrets masked) | SATISFIED | prefillWizard() handles all provider types; secrets shown as "already set" placeholder; azure_endpoint and ollama_host are pre-filled as non-secret values; validationPassed auto-set to true |
| TG-02 | 02-02 | Telegram token format validated (digits:alphanumeric) | SATISFIED | validateTelegramFormat() function at line 2136 uses regex `/^\d+:[A-Za-z0-9_-]+$/`; shown inline on blur via onblur="validateTelegramFormat(this)" on telegramToken input |

**Orphaned requirements from REQUIREMENTS.md traceability table assigned to Phase 2:** None — all 14 requirement IDs are covered by the three plans.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| gateway.py | 1307 | Comment uses word "placeholder" | Info | Code comment only; not a stub — describes masking behavior |
| entrypoint.sh | 129 | `REHYDRATE_FILE` contains string "XXXXXX" — matches FIXME pattern | Info | mktemp template, not a TODO; safe and intentional |
| entrypoint.sh | 282-287 | "todo" matches TODO pattern | Info | YAML config fragment for goose "todo" extension — not a code TODO |

No blocker anti-patterns found. All flagged matches are false positives from grep pattern matching on legitimate code.

### Human Verification Required

#### 1. Reconfigure Flow Visual Check

**Test:** Log in to a configured instance, open setup wizard, click "Reconfigure" from dashboard
**Expected:** Provider card is highlighted with correct provider, model field shows existing model, credential fields show "already set" placeholder (not masked value), Save button is already enabled
**Why human:** DOM state and CSS class toggling requires browser interaction

#### 2. Empty Field Blocking

**Test:** Fresh wizard session, select any API key provider (e.g., Anthropic), click "Continue" on step 1 without entering a key
**Expected:** Error alert shown, wizard does not advance to step 2
**Why human:** Requires browser event interaction to verify advanceFromCredentials() blocking

#### 3. Save Button Gating

**Test:** Fresh wizard session, complete provider selection, skip testing credentials, navigate to step 4 (confirmation)
**Expected:** Save button is visually disabled; note "Test your credentials before saving" visible
**Why human:** Visual disabled state and note visibility require browser inspection

#### 4. Telegram Token Format Validation

**Test:** Navigate to settings step (step 3), type "invalidformat" in telegram token field, tab out
**Expected:** Inline error "Invalid format. Expected: digits:alphanumeric" appears below the field
**Why human:** onblur event behavior requires browser interaction

---

## Gaps Summary

One gap blocks full CRED-04 compliance: the frontend's `validateKey()` function reads `data.note` for displaying success messages (lines 2018, 2022 in setup.html), but `dispatch_validation()` in gateway.py returns results with a `message` field — there is no `note` field in any backend response.

**Effect:** When a user tests credentials and the test passes, they see a generic "connection successful" tick instead of the provider-specific message (e.g., "Connected to Anthropic. API key is valid." or "Connected to Azure OpenAI. Credentials are valid."). This is not shown at all.

**Error path is unaffected:** `data.error` is correctly read and displayed for failures.

**Fix is a one-line change** in `setup.html`:
- Line 2018: `showAlert('success', data.note || '\u2713 connection successful')` → `showAlert('success', data.message || data.note || '\u2713 connection successful')`
- Line 2022: `showAlert('info', data.note || '\u2713 no validation needed for this provider')` → `showAlert('info', data.message || data.note || '\u2713 no validation needed for this provider')`

The phase goal ("every provider configuration is validated before save, persisted correctly, and restored on container restart") is substantially achieved. The env plumbing (ENV-01 through ENV-04) and persistence/rehydration are fully working. The validation gate (CRED-01, CRED-02, CRED-03) is enforced. The gap affects only the display quality of success feedback, not the functional correctness of validation gating.

---

_Verified: 2026-03-11T00:00:00Z_
_Verifier: Claude (gsd-verifier)_
