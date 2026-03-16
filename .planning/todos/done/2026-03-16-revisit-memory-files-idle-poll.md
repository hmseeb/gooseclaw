---
created: 2026-03-16T01:32:48.970Z
title: "Revisit memory files idle poll"
area: general
files:
  - ~/.claude/projects/-Users-haseeb/memory/top_of_mind.md
  - ~/.claude/projects/-Users-haseeb/memory/lessons.md
  - ~/.claude/projects/-Users-haseeb/memory/projects.md
---

## Problem

Need a mechanism to periodically re-read memory files (top_of_mind.md, lessons.md, projects.md) during idle periods so Claude stays current with priorities, lessons, and project context without manual prompting. Currently memory is only loaded at session start, so mid-session changes or long sessions can drift out of sync.

## Solution

Set up a 10-minute idle poll that re-reads the three memory files and reports only if something changed since last check. Could be implemented as a CronCreate job or a custom hook. Needs a diffing mechanism to avoid noisy unchanged reports.
