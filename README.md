# gooseclaw

> personal AI agent in 5 minutes. deploy on railway, chat on telegram or web.

gooseclaw is a personal AI agent built on [Goose](https://github.com/block/goose) by Block. it runs on Railway, talks to you on Telegram and via a web UI, and learns who you are over time.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/AD_ArJ?referralCode=Rnr2IU)

## what you get

- **web UI + telegram bot** — chat from your browser or phone
- **interactive onboarding** on first message (no config files to edit)
- **persistent memory** that survives redeploys (Railway volume + optional git backup)
- **30+ LLM providers** supported (Claude, GPT, Gemini, Groq, self-hosted, etc.)
- **scheduled tasks** — morning briefings, daily summaries, or anything you ask for

## quick start

### 1. deploy on railway

click the deploy button above. no environment variables needed.

or manually: fork this repo, connect to Railway, add a volume mounted at `/data`, deploy.

### 2. run the setup wizard

visit your Railway URL. the setup wizard walks you through:

1. **pick a provider** (Claude, Anthropic, OpenAI, Google, Groq, OpenRouter, or custom endpoint)
2. **enter credentials** with one-click validation
3. **optional settings** (model override, timezone, Telegram, auth token)

the agent starts automatically after setup.

### 3. say hello

on first message, the agent runs a quick onboarding Q&A to learn your name, preferences, and communication style. after that, it's your personal agent.

### 4. (optional) add telegram

want a Telegram bot too? create one with [@BotFather](https://t.me/BotFather), add the token in the setup wizard or set `TELEGRAM_BOT_TOKEN` in Railway, and redeploy. check logs for the pairing code. one-time step.

## environment variables

the setup wizard handles provider configuration, so no env vars are required for a basic deploy. for advanced use or CI/CD, you can set these instead:

### LLM provider (alternative to setup wizard)

| variable | description |
|----------|-------------|
| `CLAUDE_SETUP_TOKEN` | Claude subscription token from `claude setup-token` |
| `GOOSE_PROVIDER` + `GOOSE_API_KEY` | API key provider (`anthropic`, `openai`, `google`, `groq`, `openrouter`) |
| `CUSTOM_PROVIDER_URL` | any OpenAI-compatible endpoint (+ `CUSTOM_PROVIDER_MODEL`, `CUSTOM_PROVIDER_KEY`) |

### optional

| variable | default | description |
|----------|---------|-------------|
| `GOOSE_MODEL` | provider default | model override |
| `TZ` | `UTC` | timezone |
| `GOOSE_WEB_AUTH_TOKEN` | auto-generated | stable auth token for web UI across deploys |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token from @BotFather |
| `GITHUB_PAT` | — | GitHub PAT for git-based state persistence |
| `GITHUB_REPO` | — | repo for git persistence (e.g. `username/my-agent`) |

## how it works

```
┌──────────────────────────────────────────┐
│           Railway Container               │
│                                           │
│  gateway.py (reverse proxy on $PORT)      │
│  ├── /setup       → setup wizard          │
│  ├── /api/setup/* → config API            │
│  └── /*           → goose web (port 3001) │
│                                           │
│  entrypoint.sh                            │
│  ├── gateway.py (setup + proxy)           │
│  ├── goose web (chat UI)                  │
│  ├── goose gateway (telegram bot)         │
│  └── persist loop (git push, optional)    │
│                                           │
│  /data/ (Railway volume)                  │
│  ├── identity/                            │
│  │   ├── soul.md       ← personality      │
│  │   ├── user.md       ← your profile     │
│  │   ├── tools.md      ← capabilities     │
│  │   ├── memory.md     ← learned facts    │
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
| `journal/` | daily session logs | agent |

### onboarding

on first message, the agent detects that identity files haven't been configured and starts an interactive Q&A:

1. what's your name?
2. what do you do?
3. what timezone are you in?
4. how should I talk to you?
5. what do you want help with?

answers are written to `soul.md` and `user.md`. subsequent messages use the populated identity.

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

## credits

- **[Goose](https://github.com/block/goose)** by Block. the AI agent framework that powers everything
- **[Railway](https://railway.com)** for one-click container deployment
- Identity architecture inspired by **[OpenClaw](https://github.com/openclaw)**

## license

MIT
