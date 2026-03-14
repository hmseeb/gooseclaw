---
created: 2026-03-13T22:31:57.828Z
title: Queue consecutive messages instead of bouncing with Still thinking
area: general
files:
  - docker/gateway.py:366-368
  - docker/gateway.py:5903-5904
---

## Problem

When a user sends a message and the LLM is still processing, sending a second message immediately gets bounced with "Still thinking... send /stop to cancel." The second message is lost. Users expect their messages to queue up and be processed after the current one finishes, similar to how ChatGPT/Claude handle rapid messages.

## Solution

Instead of bouncing with "Still thinking...", queue the message. Options:
1. Buffer consecutive messages and append them to the conversation after the current relay completes
2. Concatenate queued messages into a single relay (e.g. "User also said: ...")
3. At minimum, acknowledge the message ("Got it, I'll get to this after my current response") and process it next

The user lock in `_do_message_relay` (line ~366) has a 2-second timeout. When it fails to acquire, instead of sending the bounce message, store the text in a per-user queue and process after the lock is released.
