# Feature Landscape: Multi-Channel & Multi-Bot Support

**Domain:** AI agent gateway with multi-platform messaging and multi-bot routing
**Researched:** 2026-03-13
**Existing system:** GooseClaw v1.0 with Telegram, channel plugin system, notification bus, job engine

## Table Stakes

Features users expect when a platform claims "multi-channel" and "multi-bot." Missing any of these and the system feels half-baked compared to Matterbridge, Hubot adapters, or Matrix bridges.

| Feature | Why Expected | Complexity | Depends On | Notes |
|---------|--------------|------------|------------|-------|
| Shared command router | Every multi-channel framework (Hubot, Botpress, Rasa) routes /help /stop /clear identically across channels. Currently these are hardcoded in the telegram poll loop only. Channel plugins have zero command support. | Medium | Existing CHANNEL dict contract | Extract commands from telegram poll loop into a shared router that ChannelRelay calls. The CHANNEL contract already has `poll` which receives `relay_fn`. Commands need to be interceptable before relay. |
| Per-user session locks for all channels | Telegram has `_telegram_chat_locks` preventing concurrent relays per user. Channel plugins via ChannelRelay have zero concurrency protection. Two messages from same user = two simultaneous goose web relays = garbled output. | Low | ChannelRelay class | Add a lock dict to ChannelRelay keyed by user_id, same pattern as `_get_chat_lock()`. Nearly copy-paste from telegram code. |
| Cancellation support for all channels | Telegram has /stop with active relay tracking (`_telegram_active_relays`). Channel plugins have no way to cancel. A stuck goose response blocks the user indefinitely. | Medium | Per-user session locks, shared command router | ChannelRelay needs to track active WebSocket refs per user (like `_telegram_active_relays`). /stop command in shared router closes the socket and sets cancelled flag. |
| Multi-bot telegram instances | Users expect one gateway to run N telegram bots, each with its own personality/provider/model. The current code has a single `_telegram_running` bool and single `TELEGRAM_BOT_TOKEN`. Running two bots = impossible. | High | Per-channel model routing (exists), setup.json schema changes | Each bot needs its own poll thread, session store, pair codes, and active relay tracking. The 409 Conflict constraint means each token MUST have exactly one poller. This is the hardest table-stakes feature. |
| Per-bot provider/model config | When you have multiple bots, each needs its own LLM. The current `channel_routes` system maps channel names to model IDs, but doesn't support `telegram:bot1` vs `telegram:bot2`. | Medium | Multi-bot instances, existing models array + channel_routes | Extend channel_routes to support bot-scoped keys like `telegram:sales_bot`. Each bot's poll thread passes its route key to `_relay_to_goose_web`. |
| Channel plugin command registration | Plugins should declare what commands they support beyond the defaults. Discord might want /image, Slack might want /thread. The CHANNEL dict has no `commands` field. | Low | Shared command router | Add optional `commands` key to CHANNEL dict: `[{"name": "image", "description": "Generate image", "handler": fn}]`. Router merges plugin commands with global commands. |
| Notification bus channel targeting for plugins | `notify_all(text, channel="slack")` already works for loaded plugins. But the job engine's `notify_channel` field and cron scheduler only know about hardcoded channel names: "web", "telegram", "cron", "memory". Plugin channels are excluded from setup validation and UI. | Low | Existing notification bus, setup.json schema | Make `valid_channels` dynamic: read from `_loaded_channels` keys + hardcoded builtins. Update validation in `_validate_config()` and `handle_set_routes()`. |
| Typing/activity indicators for plugins | Telegram sends "typing..." via `sendChatAction`. Users on any platform expect to see the bot is working. Channel plugins have no way to signal activity. | Low | CHANNEL dict contract | Add optional `typing` callback to CHANNEL dict: `typing(user_id) -> None`. ChannelRelay calls it before relaying. Platforms that don't support it just skip. |

## Differentiators

Features that set GooseClaw apart from generic bridges (Matterbridge) or chatbot frameworks (Botpress). Not expected, but valued.

| Feature | Value Proposition | Complexity | Depends On | Notes |
|---------|-------------------|------------|------------|-------|
| Cross-channel session continuity | User starts a conversation on Telegram, continues on Discord. Same goose session, same context. No other self-hosted AI gateway does this well. Matterbridge bridges messages, not AI sessions. | High | Shared user identity system | Requires a user identity layer mapping platform-specific IDs to a unified user. Complex because users don't have accounts. Could use pairing codes: pair Telegram chat 123 and Discord user 456 to the same GooseClaw user. Defer to post-MVP. |
| Per-bot personality/system prompt | Each telegram bot gets its own system prompt (soul.md). @SalesBot talks like a salesperson, @SupportBot talks like tech support. Same gateway, different brains. | Medium | Multi-bot instances, identity/soul.md per bot | Extend bot config with `system_prompt` or `soul_path`. Pass to goose web session creation. Compelling for businesses running multiple bots. |
| Hot-add/remove bots without restart | Add a new telegram bot token through the API and it starts polling immediately. No container restart needed. Like channel plugin hot-reload but for bots. | Medium | Multi-bot instances | Pattern already exists: `_reload_channels()` unloads all and reloads from disk. Apply same to bot instances. Useful for operators who iterate on bot configs. |
| Channel-aware memory writer | The memory writer currently only tracks Telegram chats. With multi-channel, it should learn from ALL channels. User teaches something on Discord, goose remembers it on Telegram. | Medium | Memory writer (exists), cross-channel user identity | The current `_memory_writer_loop` hardcodes `_telegram_sessions_lock`. Needs to iterate all channel sessions. Medium complexity because session ID formats differ by channel. |
| Webhook mode for telegram bots | Current polling is fine for 1-3 bots. At 5+ bots, each running a 30s long-poll thread, resource usage climbs. Webhook mode lets Telegram push updates, which is the recommended approach for multi-bot setups per Telegram's own docs. | High | Multi-bot instances, HTTPS termination (Railway handles this) | Requires registering webhook URLs with Telegram, handling incoming POST requests in the gateway HTTP server. Significant refactor of the poll loop. Defer unless bot count > 5 becomes common. |
| Plugin marketplace / community plugins | Ship with example channel plugins (Discord, Slack, WhatsApp). Users drop a .py file and it works. The infrastructure exists, but there are zero example plugins. | Low per plugin | Channel plugin system (exists) | Write 2-3 reference plugins. Discord is the easiest (discord.py is mature). This is more content than code. |
| Admin dashboard for channel status | Web UI showing: which channels are loaded, per-channel message counts, last message time, error rates. Currently only `/api/channels` returns JSON. | Medium | Channel plugin system, setup.html | Add a channels section to the web dashboard. Shows each channel's health, last activity, and config link. Nice-to-have for operators. |

## Anti-Features

Features to explicitly NOT build. These are traps that look appealing but add complexity without proportional value.

| Anti-Feature | Why Avoid | What to Do Instead |
|--------------|-----------|-------------------|
| Cross-channel message bridging | GooseClaw is an AI agent gateway, not a chat bridge. Bridging messages between Telegram and Discord is Matterbridge's job. Adding it conflates two very different concerns and makes the codebase unmaintainable. | Keep channels as independent AI conversation endpoints. Each channel talks to goose independently. If users want bridging, tell them to run Matterbridge alongside GooseClaw. |
| Platform-specific rich UI in the abstraction layer | Telegram has inline keyboards, Discord has embeds, Slack has blocks. Trying to abstract these into a unified "rich message" format is a rabbit hole. Botpress tried and it's their most complained-about feature. | Keep the abstraction text-only. Let channel plugins handle their own platform-specific formatting in their `send` function. The goose response is always text. |
| OAuth/SSO for channel authentication | Adding OAuth flows for Slack workspace install, Discord bot OAuth, etc. These are complex per-platform and the single-HTML-file constraint makes them painful. | Stick with API tokens and pairing codes. Users paste a bot token in setup.json or env var. Simple, works everywhere, no OAuth dance. |
| Unified chat history across channels | Storing all channel messages in a central database so users can see cross-channel history. This is a full product (like Mattermost) and way beyond scope. | Each channel's session data stays in its own session file. The memory writer extracts learnings, but raw history stays channel-local. |
| Automatic channel plugin discovery | Auto-downloading plugins from a registry, auto-updating, dependency resolution. This is npm/pip territory and adds massive attack surface. | Manual .py file drops in /data/channels/. Documented in README. Keep it dead simple. |
| Per-message provider switching | Letting users change the LLM provider mid-conversation via a command like `/model gpt-4`. Goose sessions are tied to a provider. Switching mid-session either breaks the session or requires a /clear. | Provider is set at bot/channel level, changed through setup UI. If they want a different model, /clear and reconfigure. Don't pretend seamless switching works when it doesn't. |

## Feature Dependencies

```
Shared command router
  |-- Cancellation support for all channels (needs /stop in router)
  |-- Channel plugin command registration (extends router)
  |
Per-user session locks (ChannelRelay)
  |-- Cancellation support (needs lock to know what to cancel)
  |
Multi-bot telegram instances
  |-- Per-bot provider/model config (each bot routes to its model)
  |-- Per-bot personality/system prompt (each bot has its soul)
  |-- Hot-add/remove bots (lifecycle management)
  |
Notification bus dynamic channels
  (independent, can ship anytime)
  |
Typing indicators for plugins
  (independent, can ship anytime)
```

## MVP Recommendation

Prioritize (in this order):

1. **Shared command router** - Extract /help /stop /clear /compact from the telegram poll loop into a reusable module. This is the foundation for everything else. Without it, every new channel re-implements command handling from scratch. Medium effort, massive unlock.

2. **Per-user session locks in ChannelRelay** - Near copy-paste from the existing telegram code. Without this, channel plugins are unusable under any real load. Two messages from the same user = race condition. Low effort, critical fix.

3. **Cancellation support via ChannelRelay** - Track active WebSocket refs per user in ChannelRelay, hook into shared command router's /stop. Without cancellation, a stuck goose response locks the user forever. Medium effort, required for usability.

4. **Dynamic notification bus channel validation** - Make `valid_channels` in config validation read from loaded plugins. Low effort, enables job engine and cron to target any channel.

5. **Multi-bot telegram instances** - The headline feature. Requires refactoring telegram globals into per-bot instances. High effort but this is what v2 is about.

6. **Per-bot provider/model config** - Extends existing `channel_routes` with bot-scoped keys. Medium effort, meaningless without multi-bot.

Defer to post-MVP:
- **Cross-channel session continuity**: Requires a user identity layer that doesn't exist. High complexity, niche use case.
- **Webhook mode for telegram**: Only matters at scale (5+ bots). Current polling works fine for 1-3.
- **Per-bot personality/system prompt**: Nice-to-have, not blocking. Can be added incrementally after multi-bot works.
- **Plugin marketplace**: Content work, not blocking architecture. Ship reference plugins as examples when ready.
- **Typing indicators**: Nice UX polish, zero priority vs functional parity.

## Platform-Specific Considerations

### Telegram Multi-Bot Constraints
- Each bot token MUST have exactly ONE getUpdates poller. Two pollers = 409 Conflict error.
- Current code uses module-level globals (`_telegram_running`, `_telegram_sessions`, `_telegram_active_relays`). Multi-bot requires converting these to per-instance state.
- Pattern: Create a `TelegramBot` class that encapsulates all per-bot state. The gateway holds a dict of `{bot_name: TelegramBot}`.
- Pairing codes need to be per-bot. Each bot has its own paired chat list.

### Discord Channel Plugin Pattern
- Discord has guilds (servers) with channels. Session isolation should be per-guild-per-channel, not per-user.
- Discord supports slash commands natively. The shared command router should let Discord register its commands with Discord's API.
- Discord bots connect via WebSocket gateway (discord.py handles this). The CHANNEL.poll pattern maps cleanly.

### Slack Channel Plugin Pattern
- Slack uses workspace-scoped bot tokens. One token = one workspace.
- Slack's Events API uses webhooks (POST to your server), not polling. The CHANNEL contract may need a `webhook_handler` option alongside `poll`.
- Slack has threaded conversations. Session isolation should be per-thread when threads are used, per-channel otherwise.

### WhatsApp Channel Plugin Pattern
- WhatsApp Business API requires a registered phone number and Meta Business verification.
- Messages expire after 24h unless user initiated. This affects notification delivery.
- WhatsApp supports buttons and lists, but these should stay in the plugin's `send` function, not the abstraction layer.

## Sources

- [Matterbridge architecture](https://github.com/42wim/matterbridge) - Multi-protocol bridge design, connector patterns (HIGH confidence)
- [Hubot adapter pattern](https://hubot.github.com/docs/) - Channel adapter abstraction, command routing (HIGH confidence)
- [Matrix bridge types](https://matrix.org/docs/older/types-of-bridging/) - Puppeted vs relay bridging, appservice architecture (HIGH confidence)
- [Telegram Bot API](https://core.telegram.org/bots/api) - getUpdates 409 conflict, webhook vs polling tradeoffs (HIGH confidence)
- [Telegram multi-bot polling constraint](https://github.com/yagop/node-telegram-bot-api/issues/550) - One poller per token, 409 behavior (HIGH confidence)
- [Botpress multi-channel](https://botpress.com/blog/botpress-vs-rasa) - Table stakes for channel abstraction layers (MEDIUM confidence)
- [Rasa custom connectors](https://rasa.com/docs/reference/channels/custom-connectors/) - Channel plugin contract design (MEDIUM confidence)
- GooseClaw gateway.py source code - Existing channel plugin system, ChannelRelay class, telegram implementation, notification bus (HIGH confidence, primary source)
