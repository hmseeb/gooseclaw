---
phase: quick-4
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - docker/gateway.py
  - docker/setup.html
  - docker/test_gateway.py
autonomous: true
requirements: [QUICK-4]

must_haves:
  truths:
    - "First-time setup wizard shows a 'Create Password' screen as step 0 before provider selection"
    - "Visiting / or /setup without auth shows a custom styled login page (not browser native popup)"
    - "Correct password sets a session cookie and redirects to the requested page"
    - "All /api/* endpoints return 401 when no valid session cookie or password"
    - "Password is stored as bcrypt-style SHA-256 hash in setup.json under web_auth_token_hash"
    - "GOOSECLAW_RECOVERY_SECRET env var recovery flow resets password (not token)"
    - "No auto-generated token logic remains in codebase"
  artifacts:
    - path: "docker/gateway.py"
      provides: "Password-based auth, login endpoint, login page serving"
      contains: "handle_login_page"
    - path: "docker/setup.html"
      provides: "Password creation step in wizard, password field replaces token field"
      contains: "Create Password"
    - path: "docker/test_gateway.py"
      provides: "Tests for password auth flow"
      contains: "test_password"
  key_links:
    - from: "docker/setup.html"
      to: "/api/auth/login"
      via: "fetch POST with password"
      pattern: "api/auth/login"
    - from: "docker/gateway.py handle_save"
      to: "hash_token"
      via: "password hashing before storage"
      pattern: "web_auth_token_hash.*hash_token"
    - from: "docker/gateway.py check_auth"
      to: "gooseclaw_session cookie"
      via: "cookie-based session verification"
      pattern: "gooseclaw_session"
---

<objective>
Replace the auto-generated auth token system with user-set password authentication.

Purpose: Users currently get a random token they must save. This is confusing and easy to lose. A password they choose themselves is more natural and memorable.

Output: Password-based auth with custom login page, cookie sessions, and password creation during first-time setup.
</objective>

<execution_context>
@/Users/haseeb/.claude/get-shit-done/workflows/execute-plan.md
@/Users/haseeb/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@docker/gateway.py (auth system: lines 910-922, 1795-1870, 6175-6392, 7197-7237, 7396-7408)
@docker/setup.html (wizard steps, auth token UI, dashboard auth token field)
@docker/test_gateway.py (existing test patterns)
</context>

<tasks>

<task type="auto">
  <name>Task 1: Add password auth backend + login page endpoint in gateway.py</name>
  <files>docker/gateway.py, docker/test_gateway.py</files>
  <action>
  **Backend changes in gateway.py:**

  1. Add a new `/api/auth/login` POST endpoint in `do_POST`:
     - Accepts JSON `{"password": "..."}` body
     - Rate-limited via `auth_limiter`
     - Verifies password against stored hash using `verify_token(password, stored_hash)` (existing function works fine for this)
     - On success: returns 200 with `Set-Cookie: gooseclaw_session=...; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000` and `{"success": true}`
     - On failure: returns 401 `{"error": "Invalid password"}`
     - First boot (no setup.json): returns 400 `{"error": "No password configured yet"}`

  2. Add a new `/login` GET route in `do_GET`:
     - Serves an inline HTML login page (similar to how error pages work, but styled to match the gooseclaw dark theme from setup.html CSS variables)
     - Page has: password input, submit button, error message area, "Lost password?" link to `/setup?recover`
     - Form submits via fetch to `/api/auth/login`, on success sets cookie from response and redirects to `/`
     - The login page HTML should be stored as a constant string `LOGIN_HTML` near the top of the handler section

  3. Modify auth flow for `handle_setup_page` (around line 6175):
     - When `load_setup()` exists and user is not authenticated and not recovery mode:
       - Instead of sending 401 with WWW-Authenticate Basic header, redirect to `/login`

  4. Modify `handle_admin_page` (around line 6228):
     - When `_check_local_or_auth()` fails:
       - Instead of current behavior, redirect to `/login`

  5. Modify the root `/` route (line 5998):
     - When configured but not authenticated: redirect to `/login` instead of relying on Basic Auth

  6. Remove auto-generated token logic from `handle_save` (around line 6352-6355):
     - Delete the block: `if not plaintext_token and not os.environ.get("GOOSE_WEB_AUTH_TOKEN"): plaintext_token = secrets.token_urlsafe(24)`
     - The password MUST come from the user via the setup wizard. If no password provided and no existing hash and no env var, return 400 error: `{"success": false, "errors": ["Password is required"]}`
     - Keep the hashing logic: when `web_auth_token` (now the password) is provided, hash it and store as `web_auth_token_hash`
     - Remove `resp["auth_token"] = plaintext_token` from the response (no more showing tokens)

  7. Update `handle_auth_recover` (line 7199):
     - Change response message from "Auth token reset" to "Password reset"
     - The recovery flow now generates a temporary random password (keep `secrets.token_urlsafe(24)`) and tells the user to change it in settings
     - Response: `{"success": true, "temporary_password": new_token, "message": "Password reset. Use this temporary password to log in, then change it in settings."}`

  8. Update `_check_local_or_auth` (line 6915):
     - When remote and not authenticated: redirect to `/login` instead of sending 401 with WWW-Authenticate

  9. Keep `check_auth` function working as-is for cookie verification. The Basic Auth path in `check_auth` can remain for backward compatibility (API clients may use it), but the browser flow should use the login page.

  10. Remove `GOOSE_WEB_AUTH_TOKEN` env var support from `get_auth_token()`. The only auth sources should be:
      - `setup.json web_auth_token_hash` (new hashed password)
      - `setup.json web_auth_token` (legacy plaintext, for migration)

  **Tests in test_gateway.py:**

  Add a `TestPasswordAuth` class with:
  - `test_login_endpoint_success`: POST /api/auth/login with correct password returns 200 + Set-Cookie
  - `test_login_endpoint_wrong_password`: returns 401
  - `test_login_endpoint_no_password_configured`: returns 400 on first boot
  - `test_save_requires_password_on_first_setup`: handle_save without password returns 400 error
  - `test_save_with_password_hashes_and_stores`: handle_save with password stores hash, no plaintext
  - `test_no_auto_generated_token`: verify handle_save does NOT generate token when password is blank on first setup
  - `test_recovery_returns_temporary_password`: verify recovery response has temporary_password field
  - `test_login_page_served`: GET /login returns 200 with HTML containing password input
  </action>
  <verify>
    <automated>cd /Users/haseeb/nix-template && python3 -m pytest docker/test_gateway.py -x -q 2>&1 | tail -5</automated>
  </verify>
  <done>
  - POST /api/auth/login validates password against stored hash and sets session cookie
  - GET /login serves custom styled login page
  - Unauthenticated browser requests redirect to /login (not Basic Auth popup)
  - handle_save requires user-provided password on first setup (no auto-generation)
  - Recovery endpoint returns temporary_password instead of auth_token
  - All existing 463+ tests still pass plus new password auth tests
  </done>
</task>

<task type="auto">
  <name>Task 2: Update setup.html wizard and dashboard for password-based auth</name>
  <files>docker/setup.html</files>
  <action>
  **Wizard changes:**

  1. Rename wizard step 3 "Optional Settings" auth field:
     - Change label from "Web Auth Token" to "Password"
     - Change `id="webAuthToken"` to `id="webPassword"` (update ALL JS references)
     - Change input type from `type="text"` to `type="password"`
     - Add a confirm password field below it: `id="webPasswordConfirm"` with `type="password"`
     - Change placeholder from "auto-generated if blank" to "choose a password for web access"
     - Change hint from "set a memorable token or leave blank for a random one" to "required. you'll use this to log in."
     - Add client-side validation: passwords must match, minimum 4 characters
     - Show inline error if passwords don't match when navigating to step 4

  2. On first setup (not reconfigure), make password required:
     - In the step 3 -> step 4 navigation, check password is filled
     - Show error "Password is required" if empty on first setup
     - During reconfigure, "leave blank to keep current" behavior stays

  3. Update `buildSummary()` function (around line 2760):
     - Change "Auth Token" row to "Password"
     - Value: password set ? "Set" : "Not set" (never show "Auto-generated")

  4. Update `saveConfig()` function (around line 4244):
     - Change `web_auth_token: document.getElementById('webAuthToken')?.value || ''` to use new `webPassword` id
     - Add password confirmation check before save
     - Remove the `handle web_auth_token: keep existing if blank during reconfigure` comment, update to reference password

  **Dashboard changes (step-dashboard):**

  5. In the dashboard field for auth token (around line 1803):
     - Change label from "Auth Token" to "Password"
     - Change `id="dfi-authtoken"` placeholder from "leave blank to keep current" to "enter new password"
     - Change display value from showing dots or "Auto-generated" to just "Set" (password is always user-set now)

  6. In `showDashboard()` function (around line 3217):
     - Update the auth token section to show "Password" label
     - Display "Set" instead of dots

  7. In `saveDashboardChanges()` function:
     - Map the authtoken edit field value to `web_auth_token` in the config (this is the password now)

  **Success screen changes:**

  8. Remove the token display box from step-success (lines 1686-1693):
     - Delete the `tokenBox` div entirely (no more "save this token" display)
     - Delete related JS: `document.getElementById('savedToken').textContent = data.auth_token` etc.
     - The success screen should just show startup progress + "Open Chat" button

  **Recovery page:**

  9. Update recovery text (around line 1830):
     - Change "Lost your auth token?" to "Lost your password?"
     - Change "Enter the recovery secret to get a new one" to "Enter the recovery secret to reset your password"
     - Update the success message to show: "Password reset! Your temporary password: {password}. Log in and change it in settings."
     - Change `data.auth_token` references to `data.temporary_password`
  </action>
  <verify>
    <automated>cd /Users/haseeb/nix-template && python3 -m pytest docker/test_gateway.py -x -q 2>&1 | tail -5</automated>
    <manual>Visit /setup on first boot -- should show password field in step 3 (required). Visit / when not authed -- should show custom login page, not browser popup.</manual>
  </verify>
  <done>
  - Setup wizard step 3 has "Password" + "Confirm Password" fields instead of "Web Auth Token"
  - Password is required on first setup, optional on reconfigure
  - Success screen no longer shows a saved token
  - Dashboard shows "Password: Set" instead of "Auth Token: dots"
  - Recovery page references passwords, not tokens
  - buildSummary shows "Password: Set" instead of "Auth Token: Auto-generated"
  - All HTML references to webAuthToken updated to webPassword
  </done>
</task>

</tasks>

<verification>
- `python3 -m pytest docker/test_gateway.py -x -q` -- all tests pass (463+ existing + new password tests)
- `grep -c "auto-generated if blank\|token_urlsafe(24)" docker/setup.html` returns 0 (no auto-gen references in HTML)
- `grep -c "GOOSE_WEB_AUTH_TOKEN" docker/gateway.py` returns 0 (env var removed)
- `grep -c "webAuthToken" docker/setup.html` returns 0 (all references renamed)
- `grep "handle_login_page\|/api/auth/login\|LOGIN_HTML" docker/gateway.py` shows login page infrastructure exists
</verification>

<success_criteria>
- First-time users create a password during setup (no auto-generated tokens)
- Unauthenticated visits to / or /setup show custom login page
- Cookie-based session after successful login
- All API endpoints require auth (unchanged behavior, different mechanism for browsers)
- Recovery flow resets password via GOOSECLAW_RECOVERY_SECRET
- Zero references to auto-generated token system in user-facing code
</success_criteria>

<output>
After completion, create `.planning/quick/4-replace-auto-generated-auth-token-with-u/4-SUMMARY.md`
</output>
