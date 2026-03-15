---
created: 2026-03-15T23:30:00.000Z
title: "Parallel session orchestrator MCP extension"
area: general
files: []
---

## Problem

Goose is single-process, single-agent. It can't fan out work to multiple parallel workers. But goosed already supports multiple concurrent sessions via `/agent/start` with session-scoped isolation. This capability is untapped from the bot's perspective.

## Solution

Build an MCP extension that lets the bot spawn N parallel goose sessions, each with its own task, then collect and merge results.

**Core tools:**
- `parallel_run(tasks: list[str], timeout: int)` — spawn a session per task, wait for all, return merged results
- `session_status(session_ids: list[str])` — check progress of spawned sessions

**Architecture:**
- MCP extension (stdio) wraps goosed's `/agent/start` REST API
- Each spawned session gets a focused system prompt + task
- Extension polls sessions until complete or timeout
- Results aggregated and returned as single tool response

**Use cases:**
- "find me 3 cool AI tools" — 3 parallel research sessions
- "check all my integrations are working" — parallel health checks
- "summarize these 5 articles" — fan-out summarization
- any task that's embarrassingly parallel

**Key constraint:** goosed has max session concurrency limits. extension should respect them and queue excess tasks.

**Research done:** Goose confirmed single-process, sessions provide isolation not process spawning. Gateway threading handles bot concurrency. This extension would be a new orchestration layer on top of existing session multiplexing.
