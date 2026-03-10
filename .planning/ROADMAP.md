# Roadmap: GooseClaw Setup Wizard v2

## Overview

Transform the existing GooseClaw setup wizard from a 7-provider basic setup into a polished 15+ provider onboarding experience with bulletproof validation, resilient gateway management, and advanced multi-model support. This is a brownfield project -- all phases modify existing files (setup.html, gateway.py, entrypoint.sh), not build from scratch. Four phases deliver: expanded provider UI, backend validation plumbing, gateway resilience with live feedback, and advanced settings for power users.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Provider UI Expansion** - Redesign wizard with 15+ providers in categories, model selection, and full setup flow steps
- [ ] **Phase 2: Validation and Env Plumbing** - Every provider validates credentials, maps env vars correctly, rehydrates on restart, and pre-fills on reconfigure
- [ ] **Phase 3: Gateway Resilience and Live Feedback** - goose web is monitored, auto-restarted, errors surfaced to user, real-time startup status, and auth recovery
- [ ] **Phase 4: Advanced Multi-Model Settings** - Lead/worker multi-model configuration for power users

## Phase Details

### Phase 1: Provider UI Expansion
**Goal**: User sees a complete, organized wizard with all 15+ providers, smart model selection, and a clear multi-step setup flow
**Depends on**: Nothing (first phase)
**Requirements**: PROV-01, PROV-02, PROV-03, MODL-01, MODL-02, MODL-03, MODL-04, UX-01, UX-02, UX-03, UX-04, UX-05, TG-01
**Success Criteria** (what must be TRUE):
  1. User sees 15+ providers organized into Cloud API, Subscription, Local, and Custom categories on step 0
  2. Each provider card displays its name, description, pricing hint, and a clickable "get API key" link
  3. After selecting a provider, user sees model selection with a sensible default pre-filled and a suggestions dropdown
  4. User progresses through provider -> credentials -> model -> optional settings -> confirmation summary, with all five steps visible
  5. Telegram step shows BotFather instructions for creating a bot
**Plans:** 2 plans

Plans:
- [ ] 01-01-PLAN.md -- Provider data registry, categorized card grid, and dynamic credential fields for all 15+ providers
- [ ] 01-02-PLAN.md -- Expand to 5-step flow with model selection, BotFather instructions, and confirmation summary

### Phase 2: Validation and Env Plumbing
**Goal**: Every provider configuration is validated before save, persisted correctly, and restored on container restart without data loss
**Depends on**: Phase 1
**Requirements**: PROV-04, PROV-05, PROV-06, CRED-01, CRED-02, CRED-03, CRED-04, CRED-05, ENV-01, ENV-02, ENV-03, ENV-04, UX-07, TG-02
**Success Criteria** (what must be TRUE):
  1. User cannot save config with empty or malformed API key -- save button is gated behind validation
  2. Each provider's validation test shows specific success/failure messages (not generic "invalid key")
  3. Claude-code provider shows clear manual instructions since remote validation is impossible
  4. After container restart, all previously configured env vars are restored and goose starts with correct provider/model
  5. When reconfiguring, form fields are pre-filled with existing values (API keys masked)
**Plans**: TBD

Plans:
- [ ] 02-01: TBD
- [ ] 02-02: TBD
- [ ] 02-03: TBD

### Phase 3: Gateway Resilience and Live Feedback
**Goal**: goose web crashes are handled automatically, users see real-time status and actual errors, and locked-out users can recover access
**Depends on**: Phase 2
**Requirements**: GATE-01, GATE-02, GATE-03, GATE-04, GATE-05, UX-06, TG-03, AUTH-01, AUTH-02
**Success Criteria** (what must be TRUE):
  1. If goose web crashes, it auto-restarts with exponential backoff -- user sees it recover without manual intervention
  2. After clicking save, user sees real-time startup status (checking config -> starting goose -> ready/error) instead of "refresh in a few seconds"
  3. When goose web fails to start, the actual error message from stderr is shown in the browser UI
  4. Telegram pairing code is displayed in the web UI after setup completes (not buried in logs)
  5. A user who lost their auth token can regain access without SSH into the container
**Plans**: TBD

Plans:
- [ ] 03-01: TBD
- [ ] 03-02: TBD

### Phase 4: Advanced Multi-Model Settings
**Goal**: Power users can configure lead/worker multi-model setups without leaving the wizard
**Depends on**: Phase 2
**Requirements**: ADV-01, ADV-02, ADV-03
**Success Criteria** (what must be TRUE):
  1. An "Advanced" toggle on the settings step reveals lead/worker multi-model configuration fields
  2. User can set a separate lead provider, lead model, and turn count
  3. Advanced settings are correctly written to config.yaml (GOOSE_LEAD_PROVIDER, GOOSE_LEAD_MODEL, GOOSE_LEAD_TURN_COUNT)
**Plans**: TBD

Plans:
- [ ] 04-01: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 1 -> 2 -> 3 -> 4
(Phase 4 depends on Phase 2, not Phase 3, so it could run in parallel with Phase 3 if desired)

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Provider UI Expansion | 0/2 | Planning complete | - |
| 2. Validation and Env Plumbing | 0/3 | Not started | - |
| 3. Gateway Resilience and Live Feedback | 0/2 | Not started | - |
| 4. Advanced Multi-Model Settings | 0/1 | Not started | - |
