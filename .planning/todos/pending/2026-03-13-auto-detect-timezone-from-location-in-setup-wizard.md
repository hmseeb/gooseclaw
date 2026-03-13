---
created: 2026-03-13T21:04:49.694Z
title: Auto-detect timezone from location in setup wizard
area: ui
files:
  - docker/setup.html
  - docker/gateway.py:2667-2674
---

## Problem

The setup wizard currently requires the user to manually select their timezone from a dropdown. This is friction during onboarding. The browser knows the user's timezone via `Intl.DateTimeFormat().resolvedOptions().timeZone`, so the wizard should auto-detect it and pre-fill the field.

## Solution

In `setup.html`, use the browser's `Intl` API to detect timezone on page load and pre-populate the timezone field. Still allow manual override. The gateway's `apply_config()` at line 2667 already handles writing the timezone to config, so no backend changes needed beyond accepting the auto-detected value.
