# Requirements: GooseClaw Setup Wizard v2

**Defined:** 2026-03-10
**Core Value:** A user with zero DevOps knowledge can deploy and configure GooseClaw correctly on the first try

## v1 Requirements

### Provider Support

- [x] **PROV-01**: Wizard offers 15+ providers organized into categories (Cloud API, Subscription, Local, Custom)
- [x] **PROV-02**: Each provider card shows name, description, pricing hint, and "get API key" link
- [x] **PROV-03**: New providers added: mistral, xai, deepseek, together, cerebras, perplexity, ollama, azure-openai
- [x] **PROV-04**: Each provider has correct env var mapping in gateway.py and entrypoint.sh
- [x] **PROV-05**: Each provider has a sensible default model
- [x] **PROV-06**: Each provider has a working validation endpoint

### Credential Validation

- [x] **CRED-01**: API key field rejects empty input before save
- [x] **CRED-02**: API key format is validated per provider (prefix check, length check)
- [x] **CRED-03**: "Test" button is mandatory or strongly gated (save disabled until tested or explicitly skipped)
- [x] **CRED-04**: Validation errors show specific messages (not just "invalid key")
- [x] **CRED-05**: Claude-code shows clear instructions since remote validation is impossible

### Model Selection

- [x] **MODL-01**: Each provider shows its recommended default model prominently
- [x] **MODL-02**: Model field uses datalist/suggestions with valid models per provider
- [x] **MODL-03**: Ollama shows note that models must be pre-pulled locally
- [x] **MODL-04**: OpenRouter shows note about multi-model routing

### Gateway Resilience

- [ ] **GATE-01**: Health check thread monitors goose web process and auto-restarts on crash
- [ ] **GATE-02**: Auto-restart uses exponential backoff (not infinite fast loop)
- [x] **GATE-03**: Web UI shows actual error message when goose web fails (not "refresh in a few seconds")
- [x] **GATE-04**: goose web stderr is captured and available for debugging
- [x] **GATE-05**: Gateway proxy returns goose web error details to the browser

### Env Var Rehydration

- [x] **ENV-01**: entrypoint.sh reads ALL provider env vars from setup.json on restart (not just 5)
- [x] **ENV-02**: gateway.py calls apply_config on startup when setup.json exists
- [x] **ENV-03**: All new providers (mistral, xai, deepseek, etc.) are in env_map in both files
- [x] **ENV-04**: PATH includes ~/.local/bin for claude CLI

### UX Flow

- [x] **UX-01**: Step 0 shows providers in categorized grid (Cloud API / Subscription / Local / Custom)
- [x] **UX-02**: Step 1 shows credentials with inline help link and format hints
- [x] **UX-03**: Step 2 shows model selection with smart defaults and suggestions
- [x] **UX-04**: Step 3 shows optional settings (telegram, timezone, auth token)
- [x] **UX-05**: Step 4 shows confirmation summary of what was configured
- [ ] **UX-06**: After save, shows real-time startup status (checking config, starting goose, ready/error)
- [x] **UX-07**: Reconfigure pre-fills form with existing values (secrets masked)

### Telegram

- [x] **TG-01**: Wizard shows BotFather instructions for creating a bot
- [x] **TG-02**: Telegram token format is validated (digits:alphanumeric)
- [ ] **TG-03**: Pairing code is shown in the web UI after setup completes (not just logs)

### Advanced Settings

- [ ] **ADV-01**: Optional "Advanced" toggle reveals lead/worker multi-model settings
- [ ] **ADV-02**: Lead provider, model, and turn count configurable
- [ ] **ADV-03**: Advanced settings write to config.yaml correctly (GOOSE_LEAD_PROVIDER, etc.)

### Auth Recovery

- [x] **AUTH-01**: If user is locked out (lost auth token), there's a recovery path
- [x] **AUTH-02**: Recovery mechanism works without SSH access to container

## v2 Requirements

### Provider Profiles
- **PROF-01**: Save multiple provider configurations
- **PROF-02**: Quick-switch between saved profiles

### OAuth Flows
- **OATH-01**: OpenRouter OAuth device flow
- **OATH-02**: GitHub Copilot device flow authentication

### Extensions
- **EXT-01**: Enable/disable goose extensions from wizard
- **EXT-02**: Add custom MCP servers from wizard

## Out of Scope

| Feature | Reason |
|---------|--------|
| Mobile-responsive wizard | Railway dashboard is desktop, users configure from desktop |
| Custom extension management | goose web handles this natively |
| Full planner/subagent multi-model UI | Too complex, lead/worker covers 90% of multi-model use cases |
| Automatic model discovery from API | Adds latency to wizard load, static suggestions are good enough |
| Provider cost calculator | Nice to have but not core to configuration |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| PROV-01 | Phase 1 | Complete |
| PROV-02 | Phase 1 | Complete |
| PROV-03 | Phase 1 | Complete |
| PROV-04 | Phase 2 | Pending |
| PROV-05 | Phase 2 | Pending |
| PROV-06 | Phase 2 | Complete |
| CRED-01 | Phase 2 | Complete |
| CRED-02 | Phase 2 | Complete |
| CRED-03 | Phase 2 | Complete |
| CRED-04 | Phase 2 | Complete |
| CRED-05 | Phase 2 | Complete |
| MODL-01 | Phase 1 | Complete |
| MODL-02 | Phase 1 | Complete |
| MODL-03 | Phase 1 | Complete |
| MODL-04 | Phase 1 | Complete |
| GATE-01 | Phase 3 | Pending |
| GATE-02 | Phase 3 | Pending |
| GATE-03 | Phase 3 | Complete |
| GATE-04 | Phase 3 | Complete |
| GATE-05 | Phase 3 | Complete |
| ENV-01 | Phase 2 | Pending |
| ENV-02 | Phase 2 | Pending |
| ENV-03 | Phase 2 | Pending |
| ENV-04 | Phase 2 | Pending |
| UX-01 | Phase 1 | Complete |
| UX-02 | Phase 1 | Complete |
| UX-03 | Phase 1 | Complete |
| UX-04 | Phase 1 | Complete |
| UX-05 | Phase 1 | Complete |
| UX-06 | Phase 3 | Pending |
| UX-07 | Phase 2 | Complete |
| TG-01 | Phase 1 | Complete |
| TG-02 | Phase 2 | Complete |
| TG-03 | Phase 3 | Pending |
| ADV-01 | Phase 4 | Pending |
| ADV-02 | Phase 4 | Pending |
| ADV-03 | Phase 4 | Pending |
| AUTH-01 | Phase 3 | Complete |
| AUTH-02 | Phase 3 | Complete |

**Coverage:**
- v1 requirements: 39 total
- Mapped to phases: 39
- Unmapped: 0

---
*Requirements defined: 2026-03-10*
*Last updated: 2026-03-10 after roadmap creation*
