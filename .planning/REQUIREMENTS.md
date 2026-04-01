# Requirements: Auto-Generated MCP Extensions

**Defined:** 2026-04-01
**Core Value:** Credentials in vault automatically become fast, direct tool access

## v1 Requirements

### Template Engine

- [x] **TMPL-01**: Jinja2-based template system renders single-file Python MCP servers
- [x] **TMPL-02**: Email template (IMAP/SMTP) - read, search, send emails
- [x] **TMPL-03**: REST API template - generic authenticated API calls (API key, bearer token)
- [x] **TMPL-04**: Templates read credentials from vault at runtime via `secret get` CLI
- [x] **TMPL-05**: All generated servers redirect stdout to stderr (MCP protocol safety)

### Code Generation

- [x] **GEN-01**: Generator takes template name + vault credential keys and produces a working MCP server .py file
- [x] **GEN-02**: Generated files stored on /data/extensions/ volume (survive redeploys)
- [x] **GEN-03**: Each generated extension is a standalone file with no external dependencies beyond stdlib + mcp SDK

### Registration

- [x] **REG-01**: Generated extensions registered in goosed config.yaml automatically
- [x] **REG-02**: Registry file (/data/extensions/registry.json) tracks all generated extensions
- [x] **REG-03**: Boot loader in entrypoint.sh restores generated extensions from registry on container start
- [x] **REG-04**: Goosed restart after registration to load new extension

### Credential Detection

- [x] **DET-01**: AI detects when user provides credentials in chat (app passwords, API keys, tokens)
- [x] **DET-02**: User confirmation before vaulting (never auto-vault without consent)
- [x] **DET-03**: AI classifies credential type and selects appropriate template
- [x] **DET-04**: End-to-end flow: user drops cred → confirm → vault → generate → register → available

### Validation

- [x] **VAL-01**: Generated .py files pass ast.parse() syntax check before registration
- [x] **VAL-02**: Health check after registration (extension responds to basic MCP ping)
- [x] **VAL-03**: Auto-disable extension after 3 consecutive startup failures

## v2 Requirements

### Advanced Templates

- **ADV-01**: Calendar template (CalDAV) - list/create events (requires caldav pip package)
- **ADV-02**: OAuth device flow template for Google, GitHub, Slack
- **ADV-03**: OpenAPI-to-MCP auto-generation from API specs

### Management

- **MGT-01**: List/delete/regenerate extensions via chat commands
- **MGT-02**: Hot-reload extensions without full goosed restart
- **MGT-03**: Extension version tracking and rollback

## Out of Scope

| Feature | Reason |
|---------|--------|
| Browser-based OAuth consent UI | Complex frontend, device flow sufficient for v1 |
| Extension marketplace/sharing | Single-user system |
| LLM-generated arbitrary code | Security risk, templates only |
| Auto pip install at runtime | Container security, pre-install in Dockerfile |
| Multi-user credential isolation | Single-user platform |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| TMPL-01 | Phase 1 | Complete |
| TMPL-02 | Phase 1 | Complete |
| TMPL-03 | Phase 1 | Complete |
| TMPL-04 | Phase 1 | Complete |
| TMPL-05 | Phase 1 | Complete |
| GEN-01 | Phase 1 | Complete |
| GEN-02 | Phase 1 | Complete |
| GEN-03 | Phase 1 | Complete |
| REG-01 | Phase 2 | Complete |
| REG-02 | Phase 2 | Complete |
| REG-03 | Phase 2 | Complete |
| REG-04 | Phase 2 | Complete |
| DET-01 | Phase 3 | Complete |
| DET-02 | Phase 3 | Complete |
| DET-03 | Phase 3 | Complete |
| DET-04 | Phase 3 | Complete |
| VAL-01 | Phase 3 | Complete |
| VAL-02 | Phase 3 | Complete |
| VAL-03 | Phase 3 | Complete |

**Coverage:**
- v1 requirements: 19 total
- Mapped to phases: 19
- Unmapped: 0

---
*Requirements defined: 2026-04-01*
