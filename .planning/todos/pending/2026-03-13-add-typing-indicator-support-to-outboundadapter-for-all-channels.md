---
created: 2026-03-13T22:45:00.000Z
title: Add typing indicator support to OutboundAdapter for all channels
area: general
files:
  - docker/gateway.py:5307-5308
  - docker/gateway.py:408-417
  - docker/gateway.py:228-260
---

## Problem

Typing indicators are hardcoded to Telegram's `_send_typing_action()` API. Custom channel integrations (Slack, Discord, web, etc.) get no typing feedback while the LLM is processing. The `OutboundAdapter` base class and `ChannelCapabilities` have no `send_typing` method.

## Solution

1. Add `send_typing(chat_id)` to `OutboundAdapter` base class (no-op default)
2. Override in `TelegramOutboundAdapter` to call `_send_typing_action`
3. Replace all hardcoded `_send_typing_action(bot_token, chat_id)` calls in relay loops with `adapter.send_typing(chat_id)`
4. Custom channel plugins can then implement their own typing (Slack typing event, Discord POST /channels/{id}/typing, web SSE, etc.)
5. Add `typing` bool to `ChannelCapabilities` so relay loop can skip it for channels that don't support it
