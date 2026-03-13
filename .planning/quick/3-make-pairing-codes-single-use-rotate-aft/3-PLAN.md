---
phase: quick-3
plan: 01
type: tdd
wave: 1
depends_on: []
files_modified:
  - docker/test_gateway.py
  - docker/gateway.py
autonomous: true
requirements: [QUICK-3]
must_haves:
  truths:
    - "After a successful pair, the old pairing code can never be reused"
    - "After a successful pair, a new pairing code is immediately available"
    - "The new code is different from the consumed code"
  artifacts:
    - path: "docker/gateway.py"
      provides: "BotInstance._check_pairing regenerates code after match"
      contains: "generate_pair_code"
    - path: "docker/test_gateway.py"
      provides: "Tests verifying single-use + rotation behavior"
  key_links:
    - from: "BotInstance._check_pairing"
      to: "BotInstance.generate_pair_code"
      via: "called after successful match"
      pattern: "self\\.generate_pair_code\\(\\)"
---

<objective>
Make pairing codes single-use with automatic rotation. After a successful pair, immediately regenerate the pairing code so the old code can never be reused by another user.

Purpose: Prevent unauthorized access if a pairing code leaks. Currently _check_pairing sets pair_code to None after match, but never generates a replacement. This means the bot has no active code until someone manually hits /api/telegram/pair. The fix: call self.generate_pair_code() right after consuming the old code.

Output: Updated _check_pairing that auto-rotates, with TDD tests proving the behavior.
</objective>

<execution_context>
@/Users/haseeb/.claude/get-shit-done/workflows/execute-plan.md
@/Users/haseeb/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@docker/gateway.py (BotInstance._check_pairing at ~line 343, generate_pair_code at ~line 275)
@docker/test_gateway.py (existing test_check_pairing_consumes_code at ~line 2693)
</context>

<tasks>

<task type="auto">
  <name>Task 1: RED - Add failing tests for pairing code rotation</name>
  <files>docker/test_gateway.py</files>
  <action>
Add two new test methods to the existing test class that contains test_check_pairing_consumes_code (around line 2693):

1. test_check_pairing_rotates_code_after_match:
   - Create BotInstance("test", "tok"), set pair_code = "XYZ789"
   - Call _check_pairing(chat_id="999", text="XYZ789")
   - Assert result is True
   - Assert bot.pair_code is NOT None (new code was generated)
   - Assert bot.pair_code != "XYZ789" (it's a different code)
   - Assert len(bot.pair_code) == 6 (valid format)

2. test_old_pairing_code_rejected_after_use:
   - Create BotInstance("test", "tok"), set pair_code = "ABC123"
   - Call _check_pairing(chat_id="111", text="ABC123") -- succeeds
   - Save the new pair_code
   - Call _check_pairing(chat_id="222", text="ABC123") -- old code
   - Assert second result is False (old code rejected)

Also update the existing test_check_pairing_consumes_code (line 2693-2699) which currently asserts pair_code is None after match. Change that assertion: instead of assertIsNone(bot.pair_code), assert bot.pair_code is not None and bot.pair_code != "XYZ789" (since code now rotates instead of being consumed to None).

Run tests to confirm the NEW tests fail (RED) and the updated existing test also fails. The existing behavior sets pair_code to None, so asserting not-None will fail.
  </action>
  <verify>
    <automated>cd /Users/haseeb/nix-template && python -m pytest docker/test_gateway.py::TestPollLoopInternals::test_check_pairing_rotates_code_after_match docker/test_gateway.py::TestPollLoopInternals::test_old_pairing_code_rejected_after_use -x 2>&1 | tail -5</automated>
    <manual>Tests should FAIL because _check_pairing currently sets pair_code to None</manual>
  </verify>
  <done>Two new failing tests exist proving the rotation behavior is not yet implemented. Existing test updated to expect rotation instead of None.</done>
</task>

<task type="auto">
  <name>Task 2: GREEN - Implement pairing code rotation in _check_pairing</name>
  <files>docker/gateway.py</files>
  <action>
In BotInstance._check_pairing (line 343-355), after a successful match, instead of just setting self.pair_code = None, call self.generate_pair_code() which both generates a new code AND sets self.pair_code to it.

Current code (line 351-353):
```python
if current_code and text.upper() == current_code.upper():
    with self.pair_lock:
        self.pair_code = None
    return True
```

Change to:
```python
if current_code and text.upper() == current_code.upper():
    self.generate_pair_code()
    return True
```

This works because generate_pair_code() already acquires pair_lock internally and sets self.pair_code to the new code. The old code is replaced atomically. The print statement in generate_pair_code already logs the new code.

Do NOT change the poll loop (line 524 area) or any other code. The _check_pairing method is the single point of change.
  </action>
  <verify>
    <automated>cd /Users/haseeb/nix-template && python -m pytest docker/test_gateway.py -k "pairing" -x -v 2>&1 | tail -20</automated>
    <manual>All pairing-related tests pass including the two new rotation tests</manual>
  </verify>
  <done>All pairing tests pass. _check_pairing now auto-rotates the code after successful match. Old codes are immediately invalid. Full test suite passes with no regressions.</done>
</task>

</tasks>

<verification>
Run the full test suite to verify no regressions:
```bash
cd /Users/haseeb/nix-template && python -m pytest docker/test_gateway.py -x -q
```
All tests pass including the new rotation tests.
</verification>

<success_criteria>
- After successful _check_pairing, bot.pair_code is a new 6-char alphanumeric code (not None)
- The old code is rejected on subsequent _check_pairing calls
- Full test suite passes with zero regressions
- No changes outside _check_pairing method (single point of change)
</success_criteria>

<output>
After completion, create `.planning/quick/3-make-pairing-codes-single-use-rotate-aft/3-SUMMARY.md`
</output>
