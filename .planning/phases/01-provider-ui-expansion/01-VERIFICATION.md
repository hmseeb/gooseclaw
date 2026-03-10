---
phase: 01-provider-ui-expansion
verified: 2026-03-10T15:00:00Z
status: gaps_found
score: 9/10 must-haves verified
re_verification: false
gaps:
  - truth: "Each provider card displays name, description, pricing hint, and a clickable get-API-key link"
    status: failed
    reason: "renderProviderGrid() renders only name and description. The pricing field is stored in PROVIDERS registry and .card-meta / .card-link CSS classes are defined, but the rendering template in renderProviderGrid() omits both p.pricing and p.keyUrl entirely. No pricing hint text and no 'Get key' link appear on any card at runtime."
    artifacts:
      - path: "docker/setup.html"
        issue: "renderProviderGrid() template string renders card-icon, card-name, card-desc only. p.pricing and p.keyUrl are never interpolated. card-meta and card-link CSS classes are dead (defined but never emitted in HTML output)."
    missing:
      - "Add pricing hint line to card template: <div class=\"card-meta\">${p.pricing}</div>"
      - "Add conditional get-key link to card template for providers with keyUrl (skip for ollama use 'Download', skip for custom entirely): <a class=\"card-link\" href=\"${p.keyUrl}\" target=\"_blank\" rel=\"noopener\">Get key</a>"
human_verification:
  - test: "Open docker/setup.html in browser, inspect step-0 provider grid"
    expected: "Each provider card shows a pricing hint (e.g. 'Pay per token', 'Free tier available') and a small 'Get key' link that opens the provider console in a new tab. Ollama card shows 'Download' link to ollama.com/download. Custom card shows no external link."
    why_human: "renderProviderGrid is JS-rendered at runtime; automated grep on static HTML cannot confirm rendered output in browser"
  - test: "Walk through full 5-step flow with Anthropic provider in browser"
    expected: "Provider grid (step 0) -> API key field with placeholder and console.anthropic.com link (step 1) -> Model pre-filled 'claude-sonnet-4-5' with datalist (step 2) -> Settings with BotFather instructions (step 3) -> Summary showing provider/model/timezone/telegram (step 4)"
    why_human: "End-to-end interactive flow with JS state transitions cannot be fully verified statically"
  - test: "Select Ollama then click through to step 2"
    expected: "Step 2 shows 'must pre-pull' note prominently. Step 1 shows host URL field pre-filled with http://host.docker.internal:11434."
    why_human: "Provider-specific conditional rendering depends on JS runtime state"
---

# Phase 1: Provider UI Expansion Verification Report

**Phase Goal:** User sees a complete, organized wizard with all 15+ providers, smart model selection, and a clear multi-step setup flow
**Verified:** 2026-03-10T15:00:00Z
**Status:** gaps_found
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| #  | Truth | Status | Evidence |
|----|-------|--------|---------|
| 1  | 23 providers (15+ required) organized into Cloud API, Subscription, Local, Enterprise, Custom categories on step 0 | VERIFIED | PROVIDERS registry has 23 unquoted+quoted keys; CATEGORIES object defines 5 groups; renderProviderGrid() iterates categoryOrder array; called on page load; step-0 uses `<div id="providerGrid">` |
| 2  | Each provider card displays name, description, pricing hint, and clickable get-API-key link | FAILED | renderProviderGrid() template renders only `card-name` and `card-desc`. The `p.pricing` and `p.keyUrl` fields exist in PROVIDERS data, and `.card-meta` / `.card-link` CSS classes are defined, but neither is interpolated into the card template string. Zero cards show pricing or key links at runtime. |
| 3  | All 8 new providers from PROV-03 are present: mistral, xai, deepseek, together, cerebras, perplexity, ollama, azure-openai | VERIFIED | All 8 confirmed present in PROVIDERS registry |
| 4  | After selecting any provider, credentials step shows correct field(s) with format hints and help links | VERIFIED | buildCredFields() reads PROVIDERS[selectedProvider]; uses p.keyPrefix for format hints ("Key should start with `sk-ant-`"); all providers get help links with target="_blank"; special branches for claude-code, github-copilot, ollama/ramalama, lm-studio/docker-model-runner, azure-openai, custom |
| 5  | User progresses through 5 visible steps: Provider -> Credentials -> Model -> Settings -> Confirm | VERIFIED | id="step-0" through id="step-4" and id="step-success" all exist; pnum-0 through pnum-4 and pstep-0 through pstep-4 all present; goToStep() loops i=0..4; all back/forward navigation wired |
| 6  | After selecting a provider, model selection step shows default model pre-filled and suggestions dropdown | VERIFIED | step-2 has `<input id="model" list="modelSuggestions">`; buildModelStep() sets modelInput.value from provider.defaultModel; populates datalist from provider.models array; goToStep(2) calls buildModelStep() |
| 7  | Ollama model step shows note that models must be pre-pulled locally | VERIFIED | buildModelStep() has explicit ollama/ramalama branch rendering `.model-note` div with "ollama pull modelname" instruction |
| 8  | OpenRouter model step shows note about multi-model routing | VERIFIED | buildModelStep() has explicit openrouter branch rendering `.model-note` with "provider/model format" text and link to openrouter.ai/models |
| 9  | Settings step shows BotFather instructions for Telegram bot creation | VERIFIED | step-3 contains `.botfather-instructions` div with numbered steps including @BotFather reference; CSS class defined with accent border-left styling |
| 10 | Confirmation step shows summary of provider, model, timezone, and Telegram status | VERIFIED | buildSummary() reads PROVIDERS[selectedProvider].name, #model value, #timezone value, #telegramToken, #webAuthToken; renders 5-row summary into #configSummary; called by goToStep(4) |

**Score:** 9/10 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `docker/setup.html` | Provider data registry and categorized grid UI (01-01) | VERIFIED | PROVIDERS registry with 23 providers; CATEGORIES with 5 groups; renderProviderGrid() |
| `docker/setup.html` | 5-step wizard flow with model selection, settings, and confirmation (01-02) | VERIFIED | step-0 through step-4; buildModelStep(); buildSummary(); goToStep(0-4+success) |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| PROVIDERS data object | provider-grid DOM rendering | renderProviderGrid function | VERIFIED | renderProviderGrid() iterates Object.entries(PROVIDERS), groups by category, renders category-label + provider-card divs with data-provider attrs |
| provider selection | credential fields | buildCredFields reads PROVIDERS registry | VERIFIED | selectProvider(id) sets selectedProvider then calls buildCredFields(); buildCredFields() reads PROVIDERS[selectedProvider] for name/keyPlaceholder/keyUrl/keyPrefix |
| progress bar | 5 step indicators | goToStep handles indices 0-4 plus success | VERIFIED | goToStep() loops i=0..4, updates pnum-i/pstep-i/pline-i; calls buildModelStep() at n=2, buildSummary() at n=4; 'success' is a named special case |
| model step | PROVIDERS[selectedProvider].models | datalist populated from provider registry | VERIFIED | buildModelStep() reads provider.models array, creates `<option>` elements in #modelSuggestions datalist |
| confirmation summary | all form field values | buildSummary reads selected config | VERIFIED | buildSummary() reads #model, #timezone, #telegramToken, #webAuthToken, and PROVIDERS[selectedProvider] for provider name/icon |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|---------|
| PROV-01 | 01-01 | 15+ providers in categories | SATISFIED | 23 providers in 5 categories |
| PROV-02 | 01-01 | Cards show name, desc, pricing hint, get-key link | BLOCKED | Cards show name+desc only; pricing and key link NOT rendered |
| PROV-03 | 01-01 | 8 new providers: mistral, xai, deepseek, together, cerebras, perplexity, ollama, azure-openai | SATISFIED | All 8 present in registry |
| UX-01 | 01-01 | Step 0 categorized grid (Cloud API / Subscription / Local / Custom) | SATISFIED | renderProviderGrid groups by 5 categories |
| UX-02 | 01-01 | Step 1 creds with inline help link and format hints | SATISFIED | buildCredFields renders keyUrl help links and keyPrefix format hints |
| MODL-01 | 01-02 | Default model shown prominently | SATISFIED | buildModelStep pre-fills input value and shows "Default: `model`" hint text |
| MODL-02 | 01-02 | datalist suggestions per provider | SATISFIED | datalist #modelSuggestions populated from provider.models array |
| MODL-03 | 01-02 | Ollama pre-pull note | SATISFIED | model-note rendered for ollama/ramalama with "ollama pull" instruction |
| MODL-04 | 01-02 | OpenRouter routing note | SATISFIED | model-note rendered for openrouter with provider/model format explanation |
| UX-03 | 01-02 | Step 2 model selection with smart defaults and suggestions | SATISFIED | step-2 exists with pre-filled default and datalist |
| UX-04 | 01-02 | Step 3 optional settings (telegram, timezone, auth token) | SATISFIED | step-3 has timezone, telegramToken, webAuthToken fields |
| UX-05 | 01-02 | Step 4 confirmation summary | SATISFIED | step-4 with buildSummary() rendering configSummary |
| TG-01 | 01-02 | BotFather instructions in wizard | SATISFIED | .botfather-instructions div with numbered steps in step-3 |

**Orphaned requirements check:** No additional Phase 1 requirement IDs found in REQUIREMENTS.md that are not claimed by a plan.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `docker/setup.html` | renderProviderGrid() function | CSS classes `.card-meta` and `.card-link` defined in `<style>` but never emitted in JS template strings | Warning | PROV-02 / Truth 2 fails — pricing hints and get-key links are completely absent from rendered cards |

No TODO/FIXME/placeholder comments found. No empty return stubs. No console.log-only implementations.

### Human Verification Required

#### 1. Provider card content in browser

**Test:** Open `docker/setup.html` as a local file in a browser and inspect the step-0 provider grid.
**Expected:** Each card shows a pricing hint (e.g. "Pay per token", "Free tier available") and a small "Get key" link opening the provider console. Ollama card shows "Download" link. Custom card shows no external link.
**Why human:** renderProviderGrid is executed at runtime; static grep confirmed the template does NOT emit pricing or keyUrl, so this test is expected to FAIL — it documents the gap visually.

#### 2. Full 5-step wizard flow (Anthropic)

**Test:** Load setup.html in browser, select Anthropic, click through all 5 steps.
**Expected:** Step 0 grid with categories -> Step 1 API key field with "sk-ant-api03-..." placeholder and console.anthropic.com link -> Step 2 model pre-filled "claude-sonnet-4-5" with datalist -> Step 3 settings with BotFather numbered instructions -> Step 4 summary card showing all values -> Save button on step 4 only.
**Why human:** End-to-end JS state machine with multiple dynamic rendering functions; full interactive flow validation.

#### 3. Ollama special flow

**Test:** Select Ollama, proceed to step 1 and step 2.
**Expected:** Step 1 shows Host URL field pre-filled with "http://host.docker.internal:11434". Step 2 shows "must pre-pull" warning note.
**Why human:** Provider-specific conditional branching with pre-filled input values.

### Gaps Summary

One gap blocks full goal achievement for PROV-02 and the phase-level success criterion "Each provider card displays its name, description, pricing hint, and a clickable 'get API key' link."

**Root cause:** The `renderProviderGrid()` function was implemented with a minimal card template that renders `card-icon`, `card-name`, and `card-desc` only. The plan required `card-meta` (pricing) and `card-link` (get-key) to also be rendered. The CSS for both classes was correctly added, and the data (`p.pricing`, `p.keyUrl`) exists in every PROVIDERS entry, but the template string in `renderProviderGrid()` simply omits them.

This is a wiring gap: the data and styles exist, the rendering function does not wire them into the output.

**Fix scope:** Small — add two lines to the `renderProviderGrid()` card template in `docker/setup.html`. No data changes needed, no CSS changes needed.

All other 9/10 truths are fully verified. The 5-step wizard, model selection, credentials, BotFather instructions, and confirmation summary are all correctly implemented and wired.

---

_Verified: 2026-03-10T15:00:00Z_
_Verifier: Claude (gsd-verifier)_
