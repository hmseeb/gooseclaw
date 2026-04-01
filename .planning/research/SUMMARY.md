# Project Research Summary

**Project:** GooseClaw Auto-Generated MCP Extensions
**Domain:** AI agent platform extension auto-generation from user credentials
**Researched:** 2026-04-01
**Confidence:** HIGH

## Executive Summary

GooseClaw needs a system where users drop credentials into chat and get working MCP tool extensions back automatically. The proven approach is template-based code generation, not LLM-written code. Pre-authored Jinja2 templates for each service type (email, calendar, REST API) get rendered with vault key references and registered as stdio MCP servers. The AI's role is narrow: detect credential type, select template, confirm with user. The generated code never touches raw credentials; it reads them from the vault at runtime. This pattern already exists in the codebase (knowledge/server.py, memory/server.py) and extends naturally.

The stack is remarkably lean: only two new pip packages (Jinja2, caldav). Everything else is already installed or stdlib. The architecture adds three new modules to the docker/ directory (detector, generator, registry) without modifying gateway.py's core relay logic. Generated extensions live on the /data persistent volume and survive redeploys via a registry.json manifest that entrypoint.sh reads on boot.

The biggest risks are: (1) credentials leaking into generated source files instead of staying in vault, (2) config.yaml race conditions during extension registration (a documented existing problem), and (3) goosed restarts killing active sessions. All three have proven mitigations in the codebase already. A critical discovery: goosed re-reads config.yaml on every API call, which may eliminate the need for restarts entirely. This needs validation but could remove the worst UX friction.

## Key Findings

### Recommended Stack

The stack maximizes reuse of existing dependencies. The MCP Python SDK (1.26.0) already provides FastMCP, Pydantic, and httpx. Only two new packages are needed.

**Core technologies:**
- **mcp SDK 1.26.0 (FastMCP)**: MCP server runtime for generated extensions. Already in use, zero migration risk.
- **Jinja2 3.1.6**: Template rendering for code generation. Chosen over string.Template for conditionals/loops, over Cookiecutter/Copier because generation happens at runtime not CLI.
- **PyYAML 6.0.2**: Config and vault YAML reading. Already installed.
- **Pydantic 2.12+**: Config validation and credential schemas. Already a transitive dep of mcp SDK.
- **httpx 0.27+**: HTTP client for REST API template. Already a transitive dep of mcp SDK.
- **caldav 3.0+**: CalDAV client for calendar template. Only needed for Phase 5 calendar support.
- **Python stdlib (imaplib, smtplib, re, ast)**: Email template and credential detection. Zero new deps.

**Key stack decision:** Runtime Jinja2 rendering, not static scaffolding. Extensions are single Python files generated during chat, not project structures created via CLI.

### Expected Features

**Must have (table stakes):**
- Credential detection in chat (regex for known prefixes + LLM classification)
- Vault storage with auto-derived service.key paths
- Template system with email (IMAP/SMTP) and generic REST API templates
- AI-driven template selection with user confirmation
- Jinja2 code generation producing FastMCP servers
- Extension registration in config.yaml with goosed restart
- Boot-time auto-registration from /data/extensions/
- Persistent storage on /data volume
- Works on both voice and text channels (automatic via goosed routing)

**Should have (differentiators):**
- Zero-config credential detection (paste key, get confirmation, done)
- Credential validation before generation (IMAP login test, API health check)
- Extension health checking post-registration
- Calendar template (CalDAV)
- Extension management (list, remove, update)
- Auto service detection from credential format (prefix-to-service mapping)
- Template preview / dry-run

**Defer (v2+):**
- OAuth device flow (complex, unlocks Google/GitHub/Slack)
- Hot-reload without restart (investigate goosed config re-read behavior first)
- OpenAPI-to-MCP generation (powerful but complex)
- Extension rollback

### Architecture Approach

The system adds three new modules alongside existing code, not inside it. Gateway.py (already 12,000+ lines) imports and calls these modules rather than growing further. Each generated extension runs as an isolated stdio MCP server process, matching how knowledge and mem0-memory already work. A registry.json file on the persistent volume is the single source of truth for what auto-generated extensions exist, decoupled from the fragile config.yaml preserve/restore cycle.

**Major components:**
1. **Credential Detector** (docker/extensions/detector.py) -- regex pre-filter + LLM classification. Returns structured DetectedCredential, never stores credentials itself.
2. **Extension Generator** (docker/extensions/generator.py) -- renders Jinja2 templates with vault key references, writes server.py to /data/extensions/{service}/, updates registry.json.
3. **Extension Registry** (docker/extensions/registry.py) -- JSON-based manifest at /data/extensions/registry.json. Tracks what was generated, from which vault keys, when. Read by entrypoint.sh on boot.
4. **Template Registry** (docker/extensions/templates/*.py.tmpl) -- pre-authored FastMCP server skeletons. Ship with the container image, versioned with codebase.
5. **Boot Loader** (entrypoint.sh extension) -- reads registry.json, validates server.py + vault keys exist, injects into config.yaml before goosed starts.

**Key data flows:**
- Generation: user message -> detector -> vault store -> generator -> registry -> config.yaml -> goosed restart -> tools available
- Execution: tool call -> goosed -> stdio -> generated server.py -> vault read at runtime -> external service -> response
- Boot: entrypoint.sh -> read registry.json -> validate -> inject config.yaml -> start goosed

### Critical Pitfalls

1. **Credentials leaked into generated source code** -- Templates must NEVER interpolate credential values. Use vault env var injection (existing pattern from _inject_vault_secrets_into_env) or runtime `secret get` subprocess. Post-generation lint grep for credential patterns. Fix in Phase 1; getting this wrong means rewriting every template.

2. **config.yaml race condition on registration** -- Multiple concurrent writers (apply_config, pairings, extension sync) with no file locking. Mitigate with separate registry.json as source of truth, merge into config.yaml only during controlled events (boot, explicit restart). Fix in Phase 2.

3. **Goosed restart kills active sessions** -- Registering extensions requires restart, which terminates all MCP servers and in-flight sessions. Mitigate with user confirmation before restart, deferred registration to idle periods, and batch registrations. Fix in Phase 2.

4. **AI selects wrong template** -- Credential types are ambiguous (~15-20% misclassification without guardrails). Mitigate with mandatory user confirmation, template manifests declaring required fields, and ast.parse() validation of generated code. Fix in Phase 1 + Phase 4.

5. **Stdout corruption in generated extensions** -- Any print() or library logging to stdout breaks MCP JSON-RPC over stdio. Every template must redirect logging to stderr. Fix in Phase 1 (template boilerplate).

6. **Credential detection false positives** -- Hex strings, UUIDs, code variables look like credentials. Use high-specificity patterns for known formats only, always confirm with user before vaulting. Fix in Phase 3.

## Implications for Roadmap

Based on research, suggested phase structure:

### Phase 1: Template System and Code Generation Pipeline
**Rationale:** Everything depends on templates existing and generating valid code. This is the foundation. Can be tested entirely without user-facing detection logic by manually triggering generation.
**Delivers:** Working template engine, email (IMAP/SMTP) template, REST API template, generated server validation (ast.parse + subprocess health check), registry.json schema.
**Addresses:** Template system, code generation, persistent storage, base template boilerplate (stderr logging, vault reads).
**Avoids:** Credential leaks (vault-read pattern established day 1), stdout corruption (template boilerplate), template injection (SandboxedEnvironment + input validation).

### Phase 2: Extension Registration and Boot Lifecycle
**Rationale:** Generated code is useless until it is registered with goosed and survives reboots. This phase makes extensions actually loadable. Depends on Phase 1 producing valid server.py files.
**Delivers:** Config.yaml writer integration, goosed restart with user confirmation, entrypoint.sh boot loader reading registry.json, extension survive reboot verification.
**Addresses:** Extension registration, boot-time auto-registration, config.yaml management.
**Avoids:** Config.yaml race condition (single-writer pattern + registry.json as source of truth), session interruption (deferred/confirmed restart).

### Phase 3: Credential Detection and AI Integration
**Rationale:** The user-facing piece comes after the pipeline is proven. You can manually test generation + registration in Phases 1-2. Now add the "drop credentials in chat" UX.
**Delivers:** Regex credential detection for top 20 service prefixes, LLM-based classification for ambiguous cases, vault auto-storage, AI template selection with user confirmation, end-to-end flow from chat message to working extension.
**Addresses:** Credential detection, AI template selection, vault storage with auto-derived paths, zero-config UX.
**Avoids:** False positives (high-specificity patterns + mandatory confirmation), wrong template selection (user confirms before generation).

### Phase 4: Validation, Health Checks, and Polish
**Rationale:** Once the core pipeline works end-to-end, add the safety nets. Credential validation catches bad passwords before generating broken extensions. Health checks catch registration failures.
**Delivers:** Pre-generation credential validation (IMAP login test, API health check), post-registration health verification, extension management tools (list, remove, update), template preview/dry-run, auto service detection from prefix.
**Addresses:** Credential validation, extension health checking, extension management CRUD, auto service detection, template preview.
**Avoids:** Wrong template selection (validation catches mismatches), extension crash loops (health check + auto-disable after 3 failures), orphaned extensions (boot-time validation).

### Phase 5: Additional Templates and Advanced Features
**Rationale:** New templates are pure content work once the pipeline is stable. CalDAV adds calendar support. OAuth device flow unlocks Google/GitHub/Slack. Each template is independent.
**Delivers:** Calendar template (CalDAV), multi-credential extension support, OAuth device flow (if pursued), OpenAPI-to-MCP generation (if pursued).
**Addresses:** Calendar template, multi-credential extensions, OAuth support, service coverage expansion.
**Avoids:** Dependency issues (caldav pip package added to Docker image with this phase, not before).

### Phase Ordering Rationale

- **Templates before detection:** The generation pipeline must produce valid, secure code before any user-facing detection triggers it. Manual testing in Phases 1-2 proves the pipeline works without the complexity of chat-based detection.
- **Registration before detection:** A generated extension that cannot register is useless. Solving config.yaml management and boot persistence before adding the chat UX avoids shipping a broken end-to-end experience.
- **Detection after pipeline:** Credential detection is the most complex user-facing piece (regex, LLM classification, false positive handling). Isolating it to Phase 3 lets the team focus on getting the mechanical pipeline right first.
- **Validation as a separate phase:** Health checks and credential validation are safety nets. They improve reliability but are not required for the core flow to function. Shipping without them is acceptable for early testing.
- **Templates last:** Adding new service templates is the easiest work once the pipeline exists. Each template is independent and testable in isolation.

### Research Flags

Phases likely needing deeper research during planning:
- **Phase 2:** config.yaml race condition mitigation needs careful analysis of all existing writers (apply_config, pairings, extension sync). The goosed config re-read behavior (does it pick up new extensions without restart?) needs empirical validation.
- **Phase 3:** Credential detection accuracy vs false positive rate requires testing with real user messages. LLM classification prompt engineering needed.
- **Phase 5 (OAuth):** OAuth device flow (RFC 8628) is well-documented but integration with vault token refresh requires design work.

Phases with standard patterns (skip research-phase):
- **Phase 1:** Jinja2 templating and FastMCP server patterns are well-documented. Existing codebase provides exact patterns to follow (knowledge/server.py, memory/server.py).
- **Phase 4:** Health checking and extension CRUD are straightforward file/process operations.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | Only 2 new deps. Everything else verified in existing codebase. PyPI versions confirmed. |
| Features | HIGH | Requirements clear from PROJECT.md. Existing extension pattern is well-understood. |
| Architecture | HIGH | Follows existing GooseClaw patterns exactly. No novel architecture decisions. |
| Pitfalls | HIGH | Critical pitfalls grounded in documented codebase issues (config.yaml race condition, goosed restart). Security patterns from MCP ecosystem research. |

**Overall confidence:** HIGH

### Gaps to Address

- **goosed config re-read without restart:** The comment at gateway.py line 3390-3392 suggests goosed re-reads config.yaml on every API call. If true, this eliminates the restart requirement entirely. Needs empirical validation in Phase 2. Test: add extension to config.yaml while goosed is running, check if tools appear without restart.
- **caldav library stability in container:** caldav 3.x is marked MEDIUM confidence. Needs testing in the Docker image environment before Phase 5 calendar template work begins.
- **Credential detection accuracy:** No existing data on false positive rates for the regex approach in real chat messages. Phase 3 should include a test suite with 20+ non-credential strings and 10+ real credential formats.
- **Extension count scaling:** Architecture assumes 1-15 extensions. No data on goosed behavior with 10+ stdio extension subprocesses. Monitor memory and boot time once real usage data exists.
- **string.Template vs Jinja2 disagreement:** ARCHITECTURE.md suggests string.Template (stdlib) while STACK.md recommends Jinja2. Recommendation: use Jinja2. Templates will need conditionals (optional SMTP config, optional TLS settings) that string.Template cannot handle. The dependency is already transitively installed via MCP SDK's starlette.

## Sources

### Primary (HIGH confidence)
- GooseClaw codebase: gateway.py (config management, relay, race condition docs), entrypoint.sh (boot sequence, extension registration), knowledge/server.py + memory/server.py (FastMCP patterns), scripts/secret.sh (vault implementation)
- [MCP Python SDK on PyPI](https://pypi.org/project/mcp/) -- v1.26.0, FastMCP built-in
- [MCP Python SDK GitHub](https://github.com/modelcontextprotocol/python-sdk) -- dependency tree, pyproject.toml
- [Jinja2 on PyPI](https://pypi.org/project/Jinja2/) -- v3.1.6
- [Goose extension docs](https://block.github.io/goose/docs/getting-started/using-extensions/) -- stdio config format
- [MCP Email Server](https://github.com/ai-zerolab/mcp-email-server) -- reference IMAP/SMTP MCP implementation

### Secondary (MEDIUM confidence)
- [caldav on PyPI](https://pypi.org/project/caldav/) -- v3.x, Python 3.10+
- [secrets-patterns-db](https://github.com/mazen160/secrets-patterns-db) -- 1600+ credential regex patterns
- [MCP security best practices - Doppler](https://www.doppler.com/blog/mcp-server-credential-security-best-practices)
- [MCP Hot-Reload (mcp-hmr)](https://pypi.org/project/mcp-hmr/) -- v0.0.3.3
- [Goose recipe system](https://www.pulsemcp.com/building-agents-with-goose) -- YAML workflow packaging
- [Chronos MCP (CalDAV)](https://github.com/democratize-technology/chronos-mcp) -- CalDAV reference implementation
- [openapi-mcp-generator](https://github.com/harsha-iiiv/openapi-mcp-generator) -- OpenAPI-to-MCP generation

### Tertiary (LOW confidence)
- goosed config re-read behavior (inferred from gateway.py comment, needs empirical validation)
- Extension count scaling limits (estimated from Python process memory, no real-world data)

---
*Research completed: 2026-04-01*
*Ready for roadmap: yes*
