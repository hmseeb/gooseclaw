---
created: 2026-03-13T22:33:00.000Z
title: Hide internal file references and tool usage from user-facing LLM output
area: general
files:
  - identity/system.md
  - identity/onboarding.md
  - docker/gateway.py:5500-5530
---

## Problem

The LLM leaks internal details to users during conversation:
1. Shows "[Using tool...]" prefixes in responses (tool call status messages)
2. References internal files like "soul.md", "user.md", "system.md" in its replies
3. Mentions "ONBOARDING_NEEDED" flags and other internal state
4. Says things like "I'll follow the onboarding flow from system.md"

Users should never see implementation details. The bot should feel like a natural conversation, not expose its internal architecture.

## Solution

Two-pronged fix:
1. **Identity files**: Add explicit instructions to system.md/onboarding.md: "NEVER mention file names (soul.md, user.md, system.md), internal flags (ONBOARDING_NEEDED), or implementation details to the user. Keep the magic behind the curtain."
2. **Gateway filtering**: In the streaming relay (_do_rest_relay_streaming), strip or suppress "[Using tool...]" prefixes from text that gets sent to users. These are tool status messages meant for verbose mode only, not user output. Check the verbosity filtering at ~line 5527 and ensure tool status messages are only shown when verbosity is "verbose".
