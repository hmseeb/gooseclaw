---
created: 2026-03-14T02:30:37.261Z
title: Remove delivery notification after cron or scheduled job sends message
area: general
files:
  - docker/gateway.py
---

## Problem

When a cron job or scheduled task runs and sends a message to a channel (via `notify`), the system sends a delivery confirmation like "[cron delivered]" or similar status text to the channel. This is noisy and exposes internal implementation details to the user. The user should just see the message content, not the delivery metadata.

## Solution

Find where the delivery notification/confirmation is sent after cron/scheduled job output is relayed to channels. Remove or suppress the delivery status message so only the actual content reaches the user. Check the `notify` CLI, job engine execution path, and any post-delivery callbacks in gateway.py.
