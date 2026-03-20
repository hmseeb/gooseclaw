---
created: 2026-03-20T09:53:51.485Z
title: "Rename channels to plugins in UI"
area: ui
files:
  - docker/admin.html
  - docker/gateway.py
  - identity/system.md
---

## Problem

"Channels" section on the dashboard shows homebase (clock-in script) and voice-transcriber alongside whatsapp. These are utilities, not communication channels. The label is confusing — users expect "channels" to mean messaging platforms only.

## Solution

Rename "CHANNELS" to "PLUGINS" in the dashboard UI (admin.html), log messages in gateway.py, and identity docs (system.md). Keep the `/data/channels/` directory path and `CHANNEL` dict contract unchanged for backward compatibility. Purely cosmetic rename — labels only, no code logic changes.
