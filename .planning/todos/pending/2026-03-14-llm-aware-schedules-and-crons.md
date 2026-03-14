---
created: 2026-03-14T02:52:42.767Z
title: LLM-aware schedules and crons
area: general
files: []
---

## Problem

The system needs a way for the LLM to be aware of scheduled tasks, cron jobs, and time-based automations. Currently schedules/crons exist but the LLM has no visibility into what's scheduled, when things run, or how to reason about timing. This makes it impossible for the LLM to intelligently coordinate with scheduled operations, avoid conflicts, or proactively inform users about upcoming events.

## Solution

Build an LLM-accessible schedule/cron registry that exposes:
- What crons/scheduled jobs exist and their cadence
- When the next run is expected
- What each job does (human-readable descriptions)
- Ability for the LLM to query "what's happening in the next N hours"
- Potentially let the LLM create/modify schedules through a structured interface
