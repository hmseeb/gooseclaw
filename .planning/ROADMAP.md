# Roadmap: GooseClaw

## Milestones

- [x] **v1.0 Setup Wizard** — Phases 1-5 (shipped 2026-03-11)
- [x] **v2.0 Multi-Channel & Multi-Bot** — Phases 6-10 (shipped 2026-03-13)
- [x] **v3.0 Rich Media & Channel Flexibility** — Phases 11-17 (shipped 2026-03-15)
- [x] **v4.0 Production Hardening** — Phases 18-21 (shipped 2026-03-16)
- [ ] **v5.0 mem0 Memory Layer** — Phases 22-25 (in progress)

## Phases

<details>
<summary>v1.0 Setup Wizard (Phases 1-5) — SHIPPED 2026-03-11</summary>

- [x] Phase 1: Provider UI Expansion (2/2 plans)
- [x] Phase 2: Validation and Env Plumbing (3/3 plans)
- [x] Phase 3: Gateway Resilience and Live Feedback (2/2 plans)
- [x] Phase 4: Advanced Multi-Model Settings (1/1 plan)
- [x] Phase 5: Production Hardening (6/6 plans)

</details>

<details>
<summary>v2.0 Multi-Channel & Multi-Bot (Phases 6-10) — SHIPPED 2026-03-13</summary>

- [x] Phase 6: Shared Infrastructure Extraction (3/3 plans)
- [x] Phase 7: Channel Plugin Parity (3/3 plans)
- [x] Phase 8: Notification Channel Targeting (1/1 plan)
- [x] Phase 9: Multi-Bot Core (3/3 plans)
- [x] Phase 10: Multi-Bot Lifecycle (1/1 plan)

</details>

<details>
<summary>v3.0 Rich Media & Channel Flexibility (Phases 11-17) — SHIPPED 2026-03-15</summary>

- [x] Phase 11: Channel Contract v2 (2/2 plans)
- [x] Phase 12: Inbound Media Pipeline (2/2 plans)
- [x] Phase 13: Relay Protocol Upgrade (2/2 plans)
- [x] Phase 14: Outbound Rich Media (2/2 plans)
- [x] Phase 15: Reference Channel Plugin (1/1 plan)
- [x] Phase 16: Watcher Engine (3/3 plans)
- [x] Phase 17: Vector Knowledge Base (3/3 plans)

</details>

<details>
<summary>v4.0 Production Hardening (Phases 18-21) — SHIPPED 2026-03-16</summary>

- [x] Phase 18: Security Foundations (4/4 plans) — shell injection, PBKDF2, secret leak, body limits, headers
- [x] Phase 19: Test Infrastructure and Coverage (4/4 plans) — 103 tests, HTTP endpoints, scripts, e2e
- [x] Phase 20: Infrastructure Hardening (3/3 plans) — logging, shutdown watchdog, dependency pinning
- [x] Phase 21: End-to-End Validation (1/1 plan) — Docker container integration test

</details>

### v5.0 mem0 Memory Layer (In Progress)

**Milestone Goal:** Replace flat chromadb vector search with mem0's vector + knowledge graph memory system as a self-hosted MCP extension. Zero new Railway services. Neo4j runs in-container.

- [x] **Phase 22: mem0 MCP Server + Config** - Standalone mem0 MCP extension with ChromaDB backend and shared config module (completed 2026-03-19)
- [x] **Phase 23: Gateway Memory Writer Migration** - Replace manual extraction pipeline with mem0.add() and identity routing (completed 2026-03-19)
- [x] **Phase 24: ChromaDB Migration + Cleanup** - Migrate existing memories to mem0, deprecate runtime collection (completed 2026-03-19)
- [x] **Phase 25: Neo4j Knowledge Graph** - In-container Neo4j with graph-augmented memory search (completed 2026-03-19)

## Phase Details

### Phase 22: mem0 MCP Server + Config
**Goal**: Bot can store, search, and manage memories through MCP tools during conversations
**Depends on**: Nothing (first phase of v5.0)
**Requirements**: MEM-01, MEM-02, MEM-03, MEM-04, MEM-05, MEM-06, CFG-01, CFG-02, CFG-03, CFG-04
**Success Criteria** (what must be TRUE):
  1. User can ask the bot "remember that I prefer TypeScript over JavaScript" and the bot stores it via memory_add tool
  2. User can ask "what do you know about my coding preferences?" and the bot retrieves relevant memories via memory_search
  3. User can ask the bot to forget something and it removes the memory via memory_delete
  4. User can ask "what memories do you have about me?" and get a full list via memory_list
  5. mem0 extraction uses a cheap model automatically (not the user's expensive main model) with zero additional API key setup
**Plans**: 2 plans

Plans:
- [ ] 22-01-PLAN.md — Dependencies + shared config module (CFG-01 through CFG-04)
- [ ] 22-02-PLAN.md — mem0 MCP server with 6 tools + extension registration (MEM-01 through MEM-06)

### Phase 23: Gateway Memory Writer Migration
**Goal**: Gateway automatically feeds conversation content to mem0 after each session, replacing the manual extraction pipeline
**Depends on**: Phase 22
**Requirements**: GW-01, GW-02, GW-03, GW-04
**Success Criteria** (what must be TRUE):
  1. After a conversation ends, facts mentioned by the user are automatically extracted and stored in mem0 without user action
  2. Memory extraction runs in the background and never blocks or slows the user's next message (timeout-protected)
  3. Stable identity traits (name, role, preferences, communication style) route to user.md, not mem0. Knowledge (projects, facts, events) routes to mem0 only. No duplication.
  4. Contradictions are resolved automatically. If user says "I switched to Rust" after previously storing "I prefer TypeScript", the old memory updates.
**Plans**: 2 plans

Plans:
- [ ] 23-01-PLAN.md — Test scaffold for mem0 gateway integration (refactor chromadb tests, add mem0/timeout/identity tests)
- [ ] 23-02-PLAN.md — Replace manual extraction with mem0.add(), split identity/knowledge prompts, remove dead chromadb code

### Phase 24: ChromaDB Migration + Cleanup
**Goal**: Existing runtime memories migrate to mem0 and the old extraction pipeline is fully removed
**Depends on**: Phase 23
**Requirements**: MIG-01, MIG-02, MIG-03, MIG-04
**Success Criteria** (what must be TRUE):
  1. After migration, all previously stored runtime memories are searchable through mem0 tools
  2. Migration runs once and a sentinel file prevents accidental re-runs on container restart
  3. The old chromadb runtime collection is no longer written to or read from (system docs collection untouched)
  4. Migration inserts directly into mem0's store without re-extracting through LLM (no token burn on existing data)
**Plans**: 2 plans

Plans:
- [ ] 24-01-PLAN.md — Migration script (runtime -> mem0), sentinel guard, entrypoint integration (MIG-01, MIG-02, MIG-04)
- [ ] 24-02-PLAN.md — System-only knowledge server cleanup, indexer cleanup, test updates (MIG-03)

### Phase 25: Neo4j Knowledge Graph
**Goal**: Bot understands entity relationships (not just flat facts) through graph-augmented memory search
**Depends on**: Phase 24
**Requirements**: GRAPH-01, GRAPH-02, GRAPH-03, GRAPH-04
**Success Criteria** (what must be TRUE):
  1. Neo4j starts automatically inside the container via entrypoint, persists data on /data volume, requires zero user configuration
  2. When user mentions relationships ("Alice is my manager", "Project X uses React"), entities and relationships are extracted and stored in the graph
  3. Memory search results are augmented with graph context. Asking about "Alice" also surfaces her relationship to user's projects.
  4. User can explore entity relationships through MCP tools (memory_entities, memory_relations)
**Plans**: 2 plans

Plans:
- [ ] 25-01-PLAN.md — Neo4j in-container install, entrypoint startup, mem0 graph_store config (GRAPH-01, GRAPH-02)
- [ ] 25-02-PLAN.md — Graph-augmented search + memory_entities/memory_relations MCP tools (GRAPH-03, GRAPH-04)

### Phase 26: Fallback Provider System
**Goal**: When primary LLM provider fails (rate limit, timeout, 5xx), auto-switch to next provider in user-configured fallback chain for both main LLM and mem0 extraction
**Depends on**: Phase 25
**Requirements**: FB-01, FB-02, FB-03, FB-04, FB-05, FB-06, FB-07, FB-08, FB-09
**Success Criteria** (what must be TRUE):
  1. When the primary provider returns a retriable error (429, 5xx, timeout), the system automatically tries the next fallback provider
  2. User can configure fallback provider chains for both main LLM and mem0 extraction in the setup wizard and dashboard
  3. Fallback chains support drag-to-reorder for priority ordering
  4. Primary provider is always tried first on each new message (fallback is transient, not sticky)
  5. Only providers with saved API keys are available as fallback options
**Plans**: 3 plans

Plans:
- [x] 26-01-PLAN.md — Test scaffold + error classification + validation + config persistence (FB-01, FB-04, FB-05, FB-09)
- [x] 26-02-PLAN.md — Setup wizard + dashboard UI + entrypoint rehydration (FB-06, FB-07, FB-08)
- [x] 26-03-PLAN.md — Main LLM fallback wiring + mem0 fallback wiring + verification (FB-02, FB-03, FB-09)

## Progress

**Execution Order:**
Phases execute in numeric order: 22 → 23 → 24 → 25 → 26

| Milestone | Phases | Plans | Status | Shipped |
|-----------|--------|-------|--------|---------|
| v1.0 Setup Wizard | 1-5 | 14 | Complete | 2026-03-11 |
| v2.0 Multi-Channel | 6-10 | 11 | Complete | 2026-03-13 |
| v3.0 Rich Media | 11-17 | 15 | Complete | 2026-03-15 |
| v4.0 Hardening | 18-21 | 12 | Complete | 2026-03-16 |
| v5.0 mem0 Memory | 22-25 | 8 | Complete | 2026-03-20 |
| v5.1 Fallback | 26 | 3 | Complete | 2026-03-25 |

**Total: 26 phases, 63 plans shipped across 6 milestones.**
