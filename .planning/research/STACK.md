# Technology Stack

**Project:** GooseClaw Auto-Generated MCP Extensions
**Researched:** 2026-04-01

## Recommended Stack

### Core Framework: MCP Server Generation

| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| `mcp` (Python SDK) | 1.26.0 | MCP server runtime, FastMCP decorator API | Already pinned in requirements.txt. Provides `mcp.server.fastmcp.FastMCP` which is the exact pattern used by existing knowledge and memory extensions. Zero new dependencies. |
| Python | 3.10+ | Runtime for generated servers | Matches existing container Python version. MCP SDK requires 3.10+. |
| Jinja2 | 3.1.6 | Template engine for code generation | Battle-tested, zero-dependency templating. Generates Python source files from service-specific templates. Already a transitive dep via MCP SDK's starlette. |
| PyYAML | 6.0.2 | Template manifest and vault reading | Already in requirements.txt. Used for vault.yaml credential loading and extension config generation. |

### Credential Detection and Validation

| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| `re` (stdlib) | builtin | Credential pattern detection | Regex-based detection of API keys, app passwords, OAuth tokens in chat messages. No external dep needed. |
| Pydantic | >=2.12.0 | Template config validation, credential schema | Already a transitive dependency of `mcp` SDK. Use for validating template parameters and credential structures before generation. |

### Service-Specific Libraries (for generated extensions)

| Library | Version | Purpose | Why |
|---------|---------|---------|-----|
| `imaplib` / `smtplib` (stdlib) | builtin | Email (IMAP/SMTP) template | Standard library. Zero additional deps. Sufficient for read/search/send email via app passwords. |
| `caldav` | >=3.0.0 | Calendar (CalDAV) template | Mature Python CalDAV client. Requires Python 3.10+. Handles iCal parsing internally. |
| `httpx` | >=0.27.1 | REST API template (authenticated HTTP) | Already a dependency of `mcp` SDK. Async-capable, HTTP/2 support, modern replacement for `requests`. Use for generic API key / bearer token integrations. |

### Code Generation Infrastructure

| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| Jinja2 | 3.1.6 | Template rendering | Renders Python MCP server source from `.j2` templates. Supports conditionals, loops, includes. NOT Cookiecutter/Copier because we generate at runtime, not as a CLI scaffold. |
| `importlib` / `subprocess` (stdlib) | builtin | Generated server validation | Syntax-check generated code before writing to disk. `python3 -c "import ast; ast.parse(code)"` for validation. |

### Storage and Persistence

| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| Filesystem (`/data/extensions/`) | n/a | Store generated extension code | Matches existing `/data` volume pattern on Railway. Survives redeploys. Each extension gets its own directory. |
| YAML (`config.yaml`) | via PyYAML 6.0.2 | Register extensions with goosed | Existing pattern. Extensions are appended to `config.yaml` under `extensions:` key with `type: stdio`. |
| `vault.yaml` | via PyYAML 6.0.2 | Credential storage | Existing vault system at `/data/secrets/vault.yaml`. Generated extensions read credentials via environment variables hydrated from vault at boot. |

## What NOT to Use

| Category | Rejected | Why Not |
|----------|----------|---------|
| Code generation | Cookiecutter / Copier | These are CLI scaffolding tools for creating new projects. We need runtime code generation within a running application. Jinja2 templates rendered programmatically is the right abstraction. |
| Code generation | `mcp-codegen` | Generates from YAML specs, but overkill. We need simple template rendering, not a full codegen pipeline. Also immature (LOW confidence on stability). |
| Code generation | Standalone FastMCP (3.x) | The built-in `mcp.server.fastmcp` from the `mcp` package is sufficient. Adding standalone `fastmcp` 3.x introduces a second dependency with potential version conflicts. The existing extensions already use `mcp.server.fastmcp`. |
| HTTP client | `requests` | No async support. `httpx` is already a dependency of the MCP SDK and supports both sync and async. |
| HTTP client | `aiohttp` | Different API surface. `httpx` already present, no reason to add another HTTP library. |
| Email | `imapclient` | Adds a dependency for marginal benefit. `imaplib` (stdlib) is sufficient for the email operations we need (search, fetch, parse). The generated template will handle the parsing boilerplate. |
| Template format | OpenAPI-to-MCP generators | Our templates are service-type-specific (email, calendar, REST), not OpenAPI-driven. The template approach is simpler and more predictable than trying to parse arbitrary OpenAPI specs. |
| Validation | JSON Schema directly | Pydantic is already available (MCP SDK dep) and provides better Python DX than raw jsonschema validation. |

## Alternatives Considered

| Category | Recommended | Alternative | Why Not |
|----------|-------------|-------------|---------|
| Template engine | Jinja2 | Python f-strings / string.Template | Jinja2 handles conditionals, loops, and includes cleanly. f-strings become unreadable for multi-file code generation. string.Template lacks control flow. |
| Template engine | Jinja2 | Mako | Less popular, more complex syntax, no advantage for this use case. |
| Extension format | stdio MCP servers | Streamable HTTP MCP servers | Existing extensions all use stdio. Goosed launches them as child processes. Stdio is simpler (no port management, no auth needed for local IPC). |
| Credential flow | Vault + env vars | Direct file reads in extensions | Vault hydration at boot is the existing pattern. Extensions get credentials via environment variables. Consistent, secure, no file permission issues. |
| Generated code storage | `/data/extensions/` | SQLite / database | Files are simpler, debuggable (cat the generated code), and match the existing `/data` volume pattern. No need for a database layer. |

## Dependency Impact Analysis

### New dependencies to add to requirements.txt

```
Jinja2>=3.1.6
caldav>=3.0.0
```

That's it. Two new packages. Everything else is already present:
- `mcp[cli]==1.26.0` (provides FastMCP, pydantic, httpx, starlette)
- `PyYAML==6.0.2` (vault and config reading)
- Python stdlib provides `imaplib`, `smtplib`, `re`, `ast`, `subprocess`

### Installation

```bash
# Add to requirements.txt
pip3 install --no-cache-dir Jinja2>=3.1.6 caldav>=3.0.0
```

### Already available (no install needed)

```
mcp[cli]==1.26.0        # FastMCP, pydantic >=2.12, httpx >=0.27.1
PyYAML==6.0.2           # Config and vault
Python stdlib            # imaplib, smtplib, re, ast, json, subprocess
```

## Key Architecture Decision: Runtime Jinja2 vs Static Templates

The core decision is: **generate Python source files from Jinja2 templates at runtime, not scaffold projects with Cookiecutter/Copier.**

Rationale:
1. **Runtime context matters.** The AI picks a template based on credential type and user intent. This happens during a chat conversation, not a CLI session.
2. **No project structure needed.** Generated extensions are single Python files (like `knowledge/server.py` and `memory/server.py`). No `pyproject.toml`, no package structure, no entry points.
3. **Vault integration.** Credentials come from vault environment variables, not template prompts. The generator reads vault state and injects the right env var names.
4. **Hot registration.** After generation, the extension must be registered in goosed's config.yaml and (ideally) loaded without a full restart.

## Confidence Assessment

| Decision | Confidence | Rationale |
|----------|------------|-----------|
| `mcp` SDK 1.26.0 with built-in FastMCP | HIGH | Already in use, verified on PyPI, matches existing extensions exactly |
| Jinja2 for code generation | HIGH | Industry standard for Python code generation, stable API, verified 3.1.6 on PyPI |
| Stdio transport for generated extensions | HIGH | All existing GooseClaw extensions use stdio, goosed config format confirmed |
| `httpx` for REST API template | HIGH | Already a dependency of `mcp` SDK, verified in pyproject.toml |
| Pydantic for config validation | HIGH | Already a dependency of `mcp` SDK (>=2.12.0), verified |
| `caldav` for calendar template | MEDIUM | Actively maintained (3.x released March 2026), but not yet tested in this codebase |
| `imaplib`/`smtplib` for email template | MEDIUM | Standard library, well-documented, but IMAP parsing requires careful error handling |
| Vault env var hydration for credentials | HIGH | Existing pattern confirmed in entrypoint.sh, already handles custom keys |

## Sources

- [MCP Python SDK on PyPI](https://pypi.org/project/mcp/) - v1.26.0, verified 2026-04-01
- [MCP Python SDK GitHub](https://github.com/modelcontextprotocol/python-sdk) - FastMCP built-in, pyproject.toml dependencies
- [FastMCP standalone on PyPI](https://pypi.org/project/fastmcp/) - v3.2.0, but we use built-in version
- [Jinja2 on PyPI](https://pypi.org/project/Jinja2/) - v3.1.6, released 2025-03-05
- [caldav on PyPI](https://pypi.org/project/caldav/) - v3.x, Python 3.10+
- [Python imaplib docs](https://docs.python.org/3/library/imaplib.html) - stdlib
- [Goose custom extensions](https://block.github.io/goose/docs/tutorials/custom-extensions/) - stdio config format
- GooseClaw codebase: `docker/entrypoint.sh` (extension registration), `docker/knowledge/server.py` and `docker/memory/server.py` (existing FastMCP patterns), `docker/requirements.txt` (current deps)
