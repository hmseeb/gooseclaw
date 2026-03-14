---
created: 2026-03-14T16:45:24.158Z
title: Investigate Goose multi-agent spawning with goosed
area: general
files: []
---

## Problem

Need to understand if Goose (goosed) supports spawning multiple agents simultaneously. Specifically:

- Can goosed run multiple agent instances in parallel?
- How does this work with Telegram and other interface integrations?
- If a user sends multiple messages or tasks, does goosed handle them concurrently or sequentially?
- What's the architecture for multi-agent coordination in goosed?

## Solution

1. Research goosed source code and docs for multi-agent/multi-session support
2. Check if Telegram integration supports concurrent conversations or parallel task execution
3. If not natively supported, investigate approaches:
   - Multiple goosed instances behind a router
   - Session multiplexing within a single goosed process
   - Queue-based task distribution across agent workers
   - Custom orchestration layer that spawns/manages multiple goose sessions
4. Document findings and propose architecture if custom solution needed
