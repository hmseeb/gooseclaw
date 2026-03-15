---
created: 2026-03-13T22:40:00.000Z
title: Support file attachments and multi-image messages in channels
area: general
files:
  - docker/gateway.py:5850-5900
  - docker/gateway.py:3810-3815
---

## Problem

Two issues with file/media handling in Telegram (and likely other channels):

1. **Non-image files ignored**: zip, tar, docx, pdf, etc. are not processed or forwarded to the LLM. Modern LLMs can handle these (Claude reads PDFs, code files, etc.), but the relay pipeline only handles images currently.

2. **Multi-image messages split**: When a user sends multiple screenshots in a single Telegram message (media group), only the first image is captured. The second arrives as a separate message rather than being grouped together. This behavior needs confirmation/tracking but likely stems from Telegram's media_group_id not being handled to batch images into a single relay.

## Solution

1. **File support**: Extend the media download/relay pipeline to handle document file types. Download the file from Telegram, detect MIME type, and pass to goose as appropriate content blocks (base64 for images, text extraction for documents, or file references for archives).

2. **Media group batching**: When Telegram sends a media_group_id, buffer all items in the group (wait ~1s for stragglers) before relaying them as a single message with multiple content blocks. The media_group_id field in Telegram updates identifies which messages belong together.
