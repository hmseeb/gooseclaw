# gooseclaw

> personal AI agent in 5 minutes. deploy on railway, chat on telegram or web.

gooseclaw is a personal AI agent built on [Goose](https://github.com/block/goose) by Block. it runs on Railway, talks to you on Telegram and via a web UI, and learns who you are over time.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/new/template/TEMPLATE_CODE)

## what you get

- **web UI + telegram bot** — chat from your browser or phone
- **interactive onboarding** on first message (no config files to edit)
- **persistent memory** that survives redeploys (Railway volume + optional git backup)
- **30+ LLM providers** supported (Claude, GPT, Gemini, Groq, self-hosted, etc.)
- **scheduled tasks** — morning briefings, daily summaries, or anything you ask for

## quick start

### 1. create a telegram bot

open Telegram, find [@BotFather](https://t.me/BotFather), send `/newbot`, follow the prompts. copy the bot token.

### 2. choose your LLM provider

| option | what you need | cost |
|--------|---------------|------|
| Claude subscription | run `claude setup-token` locally, copy the token | your existing sub |
| API key | get a key from Anthropic, OpenAI, Google, etc. | pay-per-use |
| Custom endpoint | any OpenAI-compatible URL | depends |

### 3. deploy on railway

click the deploy button above, or:

1. fork this repo
2. connect it to Railway
3. add a volume mounted at `/data`
4. set environment variables (see below)
5. deploy

### 4. pair your telegram

after deployment, check Railway logs for a 6-character pairing code. send it to your bot on Telegram. this is a one-time step.

### 5. say hello

message your bot. on first contact, it'll walk you through a quick setup to learn your name, preferences, and communication style. after that, it's your personal agent.

## environment variables

### required

| variable | description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | bot token from @BotFather |

### LLM provider (pick one set)

**Claude subscription:**

| variable | description |
|----------|-------------|
| `CLAUDE_SETUP_TOKEN` | token from `claude setup-token` command |

**API key:**

| variable | description |
|----------|-------------|
| `GOOSE_PROVIDER` | provider name: `anthropic`, `openai`, `google`, `groq`, `openrouter` |
| `GOOSE_API_KEY` | API key for the provider |

**Custom endpoint:**

| variable | description |
|----------|-------------|
| `CUSTOM_PROVIDER_URL` | OpenAI-compatible API endpoint |
| `CUSTOM_PROVIDER_MODEL` | model name (default: `gpt-4`) |
| `CUSTOM_PROVIDER_KEY` | API key (if needed) |

### optional

| variable | default | description |
|----------|---------|-------------|
| `GOOSE_MODEL` | provider default | model override |
| `TZ` | `UTC` | timezone |
| `GOOSE_WEB_AUTH_TOKEN` | auto-generated | auth token for web UI (set for stable token across deploys) |
| `GITHUB_PAT` | — | GitHub PAT for git-based state persistence |
| `GITHUB_REPO` | — | repo for git persistence (e.g. `username/my-agent`) |

## how it works

```
┌──────────────────────────────────────────┐
│           Railway Container               │
│                                           │
│  entrypoint.sh                            │
│  ├── goose web (chat UI on $PORT)         │
│  ├── goose gateway (telegram bot)         │
│  ├── goose schedule (cron jobs)           │
│  └── persist loop (git push, optional)    │
│                                           │
│  /data/ (Railway volume)                  │
│  ├── identity/                            │
│  │   ├── soul.md       ← personality      │
│  │   ├── user.md       ← your profile     │
│  │   ├── tools.md      ← capabilities     │
│  │   ├── memory.md     ← learned facts    │
│  │   ├── heartbeat.md  ← proactive rules  │
│  │   └── journal/      ← daily logs       │
│  ├── recipes/          ← scheduled tasks  │
│  ├── config/           ← goose config     │
│  └── sessions/         ← session state    │
└──────────────────────────────────────────┘
```

### identity architecture

inspired by [OpenClaw](https://github.com/openclaw)'s bootstrap pattern. identity files are loaded every session so the agent maintains continuity of self.

| file | purpose | who writes |
|------|---------|------------|
| `soul.md` | personality, values, communication style | onboarding, then you |
| `user.md` | your name, role, timezone, preferences | onboarding, then you |
| `tools.md` | platform info | template |
| `memory.md` | long-term facts learned over time | agent |
| `heartbeat.md` | proactive behaviors | onboarding, then you |
| `journal/` | daily session logs | agent |

### onboarding

on first message, the agent detects that identity files haven't been configured and starts an interactive Q&A:

1. what's your name?
2. what do you do?
3. what timezone are you in?
4. how should I talk to you?
5. what do you want help with?

answers are written to `soul.md`, `user.md`, and `heartbeat.md`. subsequent messages use the populated identity.

### persistence

identity state persists two ways:

- **Railway volume** (`/data`): survives redeploys, always active
- **Git auto-push** (optional): commits `memory.md`, `journal/`, `soul.md`, `user.md` to your fork every 5 minutes

## local development

```bash
# clone
git clone https://github.com/hmseeb/gooseclaw.git
cd gooseclaw

# copy env
cp .env.example .env
# edit .env with your values

# build and run
docker build -t gooseclaw .
docker run --env-file .env -p 8080:8080 -v gooseclaw-data:/data gooseclaw
```

## customization

### edit identity files directly

after onboarding, you can ask the agent to update its identity:

- "update your personality to be more formal"
- "add to your memory that I prefer python over javascript"
- "change your communication style to be more concise"

### edit files on the volume

if you have Railway CLI access:

```bash
railway shell
cat /data/identity/soul.md
# edit as needed
```

### fork and customize

for deeper customization, fork this repo and modify:

- `identity/persistent-instructions.md` (always-on agent instructions)
- `identity/heartbeat.md` (proactive behavior definitions)

## credits

- **[Goose](https://github.com/block/goose)** by Block. the AI agent framework that powers everything
- **[Railway](https://railway.com)** for one-click container deployment
- Identity architecture inspired by **[OpenClaw](https://github.com/openclaw)**

## license

MIT
