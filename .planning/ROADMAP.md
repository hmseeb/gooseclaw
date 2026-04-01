# Roadmap: Auto-Generated MCP Extensions

## Overview

Transform GooseClaw from a platform where integrations require manual extension setup into one where dropping credentials in chat automatically produces fast, direct MCP tool access. The journey moves from building the code generation engine, to making generated extensions loadable and persistent, to wiring up the user-facing credential detection and safety nets.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [x] **Phase 1: Template Engine and Code Generation** - string.Template engine produces valid, secure MCP server files from vault credential references (completed 2026-04-01)
- [ ] **Phase 2: Extension Registration and Boot Lifecycle** - Generated extensions register with goosed and survive container restarts
- [ ] **Phase 3: Credential Detection, Validation, and End-to-End Flow** - Users drop credentials in chat and get working tool extensions back automatically

## Phase Details

### Phase 1: Template Engine and Code Generation
**Goal**: A working generation pipeline that takes a template name and vault keys and produces a valid, standalone Python MCP server file on the persistent volume
**Depends on**: Nothing (first phase)
**Requirements**: TMPL-01, TMPL-02, TMPL-03, TMPL-04, TMPL-05, GEN-01, GEN-02, GEN-03
**Success Criteria** (what must be TRUE):
  1. Running the generator with "email" template and vault keys produces a .py file in /data/extensions/ that passes ast.parse() and imports without error
  2. Running the generator with "rest-api" template produces a .py file that starts as a valid MCP server process (responds to stdio)
  3. Generated files read credentials from vault at runtime via `secret get` CLI, never contain hardcoded credential values
  4. All generated servers redirect stdout to stderr so MCP JSON-RPC protocol is not corrupted
  5. Generated files have no external dependencies beyond stdlib and the mcp SDK
**Plans:** 3 plans

Plans:
- [x] 01-01-PLAN.md -- Generator engine: string.Template renderer, base_helpers boilerplate, vault helper, unit tests
- [x] 01-02-PLAN.md -- Email IMAP/SMTP template: search, read, send tools with vault credential reads
- [x] 01-03-PLAN.md -- REST API template: generic authenticated HTTP (GET/POST/PUT/DELETE) with bearer/API key auth

### Phase 2: Extension Registration and Boot Lifecycle
**Goal**: Generated MCP server files become live goosed extensions that persist across container restarts without user intervention
**Depends on**: Phase 1
**Requirements**: REG-01, REG-02, REG-03, REG-04
**Success Criteria** (what must be TRUE):
  1. After generation, the new extension appears in goosed config.yaml and is callable as a tool within the same session
  2. A registry.json file on /data/extensions/ tracks all generated extensions with their metadata
  3. After container restart, all previously generated extensions are restored and available as tools without any user action
  4. Goosed is restarted (or config reloaded) after registration so the new extension is immediately usable
**Plans:** 2 plans

Plans:
- [ ] 02-01-PLAN.md -- Registry module: CRUD operations for /data/extensions/registry.json with atomic writes, unit tests
- [ ] 02-02-PLAN.md -- Config writer + boot loader: gateway.py registration flow, entrypoint.sh registry injection, integration tests

### Phase 3: Credential Detection, Validation, and End-to-End Flow
**Goal**: Users paste credentials in chat and the system detects, confirms, vaults, generates, validates, and registers a working extension automatically
**Depends on**: Phase 2
**Requirements**: DET-01, DET-02, DET-03, DET-04, VAL-01, VAL-02, VAL-03
**Success Criteria** (what must be TRUE):
  1. When a user pastes an API key or app password in chat, the AI detects it and asks for confirmation before vaulting
  2. After user confirms, the system vaults the credential, selects the correct template, generates the extension, and registers it, all without manual steps
  3. Generated .py files are syntax-checked (ast.parse) before registration, preventing broken extensions from being loaded
  4. After registration, a health check confirms the extension responds to MCP ping before declaring success
  5. Extensions that fail to start 3 consecutive times are automatically disabled

**Plans:** 2 plans

Plans:
- [ ] 03-01-PLAN.md -- Validation module: ast.parse syntax check, MCP health check, 3-strike auto-disable with failure tracking
- [ ] 03-02-PLAN.md -- Credential detection + E2E pipeline: regex detector, template classifier, gateway endpoint, validation gates in registration

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Template Engine and Code Generation | 3/3 | Complete | 2026-04-01 |
| 2. Extension Registration and Boot Lifecycle | 0/2 | Planning complete | - |
| 3. Credential Detection, Validation, and End-to-End Flow | 0/2 | Planning complete | - |
