---
phase: quick-1
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - docker/entrypoint.sh
  - docker/setup.html
  - docker/gateway.py
autonomous: true
requirements: [SETUP-SETTINGS-01, SETUP-SETTINGS-02, SETUP-SETTINGS-03]

must_haves:
  truths:
    - "Env vars set via Railway/Docker override setup.json values on container restart"
    - "Visiting /setup when already configured shows an editable settings dashboard (not the wizard)"
    - "Settings dashboard allows inline editing of provider, model, timezone, telegram token"
    - "Saving dashboard changes calls POST /api/setup/save and restarts the agent"
    - "Switching providers preserves previously-entered API keys in saved_keys"
    - "Switching back to a prior provider auto-fills its saved key"
  artifacts:
    - path: "docker/entrypoint.sh"
      provides: "Env var priority fix in setup.json re-hydration"
      contains: "os.environ.get"
    - path: "docker/setup.html"
      provides: "Settings dashboard UI and saved_keys persistence logic"
      contains: "settings-dashboard"
    - path: "docker/gateway.py"
      provides: "GET /api/setup/config returns saved_keys (masked)"
  key_links:
    - from: "docker/setup.html"
      to: "/api/setup/config"
      via: "fetch on page load to determine wizard vs dashboard mode"
      pattern: "fetch.*api/setup/config"
    - from: "docker/setup.html"
      to: "/api/setup/save"
      via: "POST from dashboard save button"
      pattern: "fetch.*api/setup/save"
    - from: "docker/entrypoint.sh"
      to: "setup.json"
      via: "Python re-hydration with env var priority check"
      pattern: "os.environ.get"
---

<objective>
Fix env var priority in entrypoint.sh, add a settings dashboard to the setup wizard page, and implement provider key persistence across provider switches.

Purpose: Currently, re-visiting /setup after initial config forces a full wizard re-run, setup.json re-hydration overrides Railway env vars, and switching providers loses previously entered keys. These three issues degrade the reconfiguration experience.

Output: Updated entrypoint.sh, setup.html, and gateway.py with all three improvements.
</objective>

<execution_context>
@/Users/haseeb/.claude/get-shit-done/workflows/execute-plan.md
@/Users/haseeb/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@docker/entrypoint.sh (lines 123-151: setup.json re-hydration block)
@docker/gateway.py (full file: apply_config, handle_save, handle_get_config, env_map)
@docker/setup.html (full file: wizard steps 0-4, JS functions, PROVIDERS registry, saveConfig, buildCredFields)
</context>

<tasks>

<task type="auto">
  <name>Task 1: Fix env var priority in entrypoint.sh re-hydration</name>
  <files>docker/entrypoint.sh</files>
  <action>
In the setup.json re-hydration block (lines 128-150), the inline Python script currently exports TELEGRAM_BOT_TOKEN and TZ unconditionally from setup.json. This overrides values set via Railway env vars.

Fix the inline Python script to check `os.environ.get()` before each export, matching the pattern already used in the vault hydration section (lines 193-195).

Specific changes to the Python code inside the eval block:

1. For the provider API key export (line 143): wrap in `if not os.environ.get(env_map[pt]):` check
2. For CLAUDE_CODE_OAUTH_TOKEN (line 141): wrap in `if not os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'):`
3. For TELEGRAM_BOT_TOKEN (lines 144-146): change from:
   ```
   tg = c.get('telegram_bot_token', '')
   if tg:
       print(f'export TELEGRAM_BOT_TOKEN="{tg}"')
   ```
   to:
   ```
   tg = c.get('telegram_bot_token', '')
   if tg and not os.environ.get('TELEGRAM_BOT_TOKEN'):
       print(f'export TELEGRAM_BOT_TOKEN="{tg}"')
   ```

4. For TZ (lines 147-149): same pattern:
   ```
   tz = c.get('timezone', '')
   if tz and not os.environ.get('TZ'):
       print(f'export TZ="{tz}"')
   ```

Add `import os` at the top of the inline Python script (before the json import). Note: `os` is already available since the fix uses `os.environ.get`.
  </action>
  <verify>
    <automated>grep -c "os.environ.get" /Users/haseeb/nix-template/docker/entrypoint.sh | grep -q "^[3-9]" && echo "PASS: env var checks present" || echo "FAIL: missing env var checks"</automated>
    <manual>Read the re-hydration block and confirm every export is gated by an os.environ.get check</manual>
  </verify>
  <done>All setup.json re-hydration exports (provider API key, CLAUDE_CODE_OAUTH_TOKEN, TELEGRAM_BOT_TOKEN, TZ) are gated by os.environ.get checks so Railway/Docker env vars take priority.</done>
</task>

<task type="auto">
  <name>Task 2: Add settings dashboard and provider key persistence</name>
  <files>docker/setup.html, docker/gateway.py</files>
  <action>
**Part A: Settings Dashboard in setup.html**

Add a settings dashboard that displays when the user is already configured. The existing `fetch('/api/setup/config')` call on page load already checks `data.configured`. Extend this logic:

1. Add a new `<div class="step" id="step-dashboard">` section in the HTML (after the success screen div, before `</div><!-- container -->`). The dashboard should have:
   - Same styling vocabulary as existing wizard (use .summary-container, .field classes, etc.)
   - A title: "Settings" with subtitle "manage your agent configuration"
   - A summary display area showing: Provider (name + icon), Model, Timezone, Telegram status, Auth Token status
   - Each field should have an "Edit" button/link that toggles it to an inline input
   - A "Save Changes" button (class btn-primary) and a "Re-run Setup Wizard" link (class btn-ghost)
   - A "Back to Chat" link

2. Modify the `fetch('/api/setup/config')` handler (around line 1267-1274 of setup.html) to:
   - When `data.configured` is true: hide the wizard steps and progress bar, show `step-dashboard` instead
   - Populate the dashboard fields from `data.config`
   - Pre-fill the `savedKeys` JS object from `data.config.saved_keys` if present

3. Add JS functions:
   - `showDashboard(config)`: Renders the dashboard with current config values. Each field shows value as text with an "edit" icon/button. Clicking edit replaces text with an input pre-filled with current value.
   - `saveDashboardChanges()`: Collects all current values (edited or not), merges with saved_keys, and POSTs to `/api/setup/save`. On success, show a brief success message and update displayed values.
   - `switchToWizard()`: Hides dashboard, shows wizard steps (resets to step 0). This is the "Re-run Setup Wizard" escape hatch.

4. Add CSS for the dashboard:
   - `.dashboard-field`: flex row with label, value, edit button
   - `.dashboard-field .field-value`: displays current value
   - `.dashboard-field .field-edit`: hidden by default, shown when editing
   - `.dashboard-field.editing .field-value`: hidden
   - `.dashboard-field.editing .field-edit`: shown
   - Keep the same dark theme, card styling, accent colors as existing wizard

**Part B: Provider Key Persistence in setup.html**

1. Add a JS variable: `let savedKeys = {};`

2. In `saveConfig()` function: before POSTing, merge the current key into savedKeys:
   - `savedKeys[selectedProvider] = currentKeyValue` (extract from the appropriate input)
   - Add `config.saved_keys = savedKeys` to the POST payload

3. In `buildCredFields()` function: after rendering fields, check if `savedKeys[selectedProvider]` exists and auto-fill the input:
   - For standard providers: set `document.getElementById('apiKey').value = savedKeys[selectedProvider]`
   - For claude-code: set claudeToken value
   - For custom: set customUrl/customKey values (store as object)
   - For ollama/local: set ollamaHost value

4. In the initial config load (`fetch('/api/setup/config')`): if `data.config.saved_keys` exists, set `savedKeys = data.config.saved_keys`

5. In the dashboard's provider change flow: when user selects a different provider in the dashboard, auto-fill the key from savedKeys if available.

**Part C: Gateway.py updates for saved_keys**

1. In `handle_get_config()`: when masking secrets in the safe copy, also mask values inside `saved_keys` dict (same masking logic: first 6 + "..." + last 4 for values > 12 chars).

2. In `handle_save()`: the saved_keys field passes through naturally since save_setup just writes the full config dict. No changes needed for save. But ensure apply_config does NOT crash if saved_keys is present (it should already be fine since apply_config only reads specific keys).

**Dashboard layout structure:**
```
+----------------------------------+
|  gooseclaw /settings             |
|  manage your agent configuration |
+----------------------------------+
|                                  |
|  Provider    [Anthropic]  [edit] |
|  Model       [claude-s..]  [edit] |
|  Timezone    [America/..]  [edit] |
|  Telegram    [Configured]  [edit] |
|  Auth Token  [Custom]      [edit] |
|                                  |
|  [Save Changes]  [Re-run Wizard] |
|                [Back to Chat]    |
+----------------------------------+
```

When "edit" is clicked on Provider, show a dropdown of all providers (reuse PROVIDERS registry). When Provider changes, auto-fill the key from savedKeys. When "edit" is clicked on Model, show a text input with datalist from PROVIDERS[provider].models.

IMPORTANT: The dashboard should reuse the existing PROVIDERS registry, CATEGORIES, buildCredFields patterns -- do NOT duplicate provider metadata. The dashboard edit mode for Provider should show a simple `<select>` populated from PROVIDERS, not the full card grid.
  </action>
  <verify>
    <automated>grep -c "step-dashboard" /Users/haseeb/nix-template/docker/setup.html | grep -q "^[1-9]" && grep -c "savedKeys" /Users/haseeb/nix-template/docker/setup.html | grep -q "^[2-9]" && grep -c "saved_keys" /Users/haseeb/nix-template/docker/gateway.py | grep -q "^[1-9]" && echo "PASS: dashboard and saved_keys present" || echo "FAIL: missing components"</automated>
    <manual>
1. With no setup.json: visit /setup and confirm the wizard still works normally (5 steps)
2. Complete wizard, then revisit /setup -- should show settings dashboard, not wizard
3. On dashboard: click edit on a field, change it, save -- confirm changes persist
4. In wizard: pick provider A, enter key, go back, pick provider B, go back to A -- key should auto-fill
5. Check that switching providers in dashboard auto-fills saved keys
    </manual>
  </verify>
  <done>
- Settings dashboard renders when setup.json exists, showing all config fields with inline editing
- Dashboard save button POSTs to /api/setup/save and triggers agent restart
- "Re-run Setup Wizard" link switches back to full wizard flow
- savedKeys object persists API keys across provider switches in both wizard and dashboard
- gateway.py masks saved_keys values in GET /api/setup/config response
  </done>
</task>

</tasks>

<verification>
1. Env var priority: set TELEGRAM_BOT_TOKEN env var, create setup.json with different token, run the re-hydration Python snippet -- env var should win
2. Dashboard mode: with setup.json present, GET /setup shows dashboard instead of wizard
3. Dashboard editing: edit any field, save, reload /setup -- changes persist
4. Key persistence: configure with Anthropic key, switch to OpenAI in wizard, switch back to Anthropic -- key auto-fills
5. Wizard still works: delete setup.json, visit /setup -- full wizard flow unchanged
</verification>

<success_criteria>
- entrypoint.sh re-hydration block has os.environ.get guards on all 4 export paths
- /setup shows settings dashboard when already configured
- Dashboard allows editing individual fields without re-running wizard
- Provider API keys persist across provider switches via saved_keys
- Existing wizard flow is unchanged for first-time setup
</success_criteria>

<output>
After completion, create `.planning/quick/1-setup-wizard-settings-dashboard-with-con/1-SUMMARY.md`
</output>
