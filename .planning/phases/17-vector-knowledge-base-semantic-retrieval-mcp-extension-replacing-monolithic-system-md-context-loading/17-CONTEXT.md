# Phase 17: Vector Knowledge Base - Context

**Gathered:** 2026-03-15
**Status:** Ready for planning

<domain>
## Phase Boundary

Replace the monolithic system.md (~22KB loaded at session start via .goosehints) with a semantic retrieval MCP extension. The bot queries a vector knowledge base on-demand instead of having all procedures, API docs, and schemas dumped into context. EVOLVING files (soul.md, user.md) and MOIM (turn-rules.md) remain unchanged. The vector store also replaces memory.md as the unified knowledge persistence layer.

</domain>

<decisions>
## Implementation Decisions

### Content lifecycle
- Deploy-time full re-index: wipe and rebuild the vector store during container startup (entrypoint.sh)
- No hot reload, no diff-based updates. Clean slate each deploy
- EVOLVING files (soul.md, user.md) stay in .goosehints, not vectorized
- LOCKED files (system.md, schemas/, onboarding.md) get vectorized

### Unified knowledge layer (replaces memory.md)
- Vector store absorbs memory.md's role entirely
- Typed chunks with metadata: each chunk gets a type tag (fact, procedure, preference, integration, schema)
- Immediate vector write: bot calls an MCP tool to upsert a chunk instantly at runtime
- Cross-references between chunks: chunks can link to related chunks (e.g., "this integration uses this credential")
- Exact key lookup supported alongside semantic search
- One-time migration: import memory.md contents as typed chunks, then remove memory.md from .goosehints. Clean break

### Retrieval triggers
- Explicit tool call: bot decides when to call knowledge_search(), not auto-injected per turn
- Matches existing pattern (bot already calls context7, exa on-demand)
- Slim .goosehints remains: identity files (soul.md, user.md) stay loaded at session start
- MOIM stays: turn-rules.md continues injecting per-turn via tom extension. Critical rules must never depend on retrieval
- Top 3-5 chunks per retrieval query
- Similarity scores returned with each chunk so bot can judge match quality

### Claude's Discretion
- Knowledge chunking strategy (how system.md is split into retrievable pieces)
- Vector store and embedding model choice (local vs cloud, which model)
- MCP extension implementation details (tool naming, argument design)
- Chunk size optimization
- How cross-references are stored and traversed

</decisions>

<specifics>
## Specific Ideas

- system.md is ~22KB / ~408 lines / ~6,000 tokens. Not huge, but wasteful when most of it is irrelevant to any given turn
- Current architecture: .goosehints @file inlines everything at session start. MOIM injects turn-rules.md per-turn
- The bot should treat knowledge_search() like it treats context7 or exa: call it when it needs to look something up
- Runtime-written chunks (replacing memory.md) should survive re-index. Deploy-time re-index only wipes "system" namespace, not "runtime" namespace
- Similarity scores let the bot decide if a match is good enough or if it needs to refine its query

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

</deferred>

---

*Phase: 17-vector-knowledge-base-semantic-retrieval-mcp-extension-replacing-monolithic-system-md-context-loading*
*Context gathered: 2026-03-15*
