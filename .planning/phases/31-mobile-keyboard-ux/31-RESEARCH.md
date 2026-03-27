# Phase 31: Mobile + Keyboard UX - Research

**Researched:** 2026-03-27
**Domain:** Mobile-responsive voice UI, keyboard shortcuts, text input, Screen Wake Lock API
**Confidence:** HIGH

## Summary

Phase 31 enhances the existing voice.html (880 lines, built in Phase 30) with four capabilities: keyboard shortcuts (Spacebar hold-to-talk, Escape disconnect), text input alongside voice, mobile-first responsive layout, and Screen Wake Lock to keep the phone awake during voice sessions.

The existing codebase is well-positioned for these additions. The voice.html already uses `100dvh` for viewport height, has a basic mobile media query (768px breakpoint), and the mic button is already 80px desktop / 100px mobile with `touch-action: manipulation`. The gateway.py already forwards text WebSocket frames to Gemini (line 9084-9088), so text input requires only browser-side UI work. The Gemini Live API accepts text via `{"realtimeInput": {"text": "..."}}` JSON format, and responds with audio (since responseModalities is set to AUDIO). All four requirements are browser-only changes to voice.html with no gateway modifications needed.

**Primary recommendation:** Add keyboard event listeners, a text input bar, responsive CSS enhancements, and Wake Lock API calls, all within voice.html. No server-side changes required.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| UI-03 | Spacebar hold-to-talk, Escape to disconnect keyboard shortcuts work on desktop | Keyboard event pattern (keydown/keyup with preventDefault), must skip when input focused |
| UI-04 | User can type messages in same interface when voice isn't convenient (text-to-voice switching) | Gemini realtimeInput.text JSON format verified, gateway already forwards text frames |
| UI-05 | Dashboard layout is mobile-first, works great on phone browsers with touch-friendly controls | Existing 768px breakpoint needs expansion, 44px+ tap targets, safe-area-inset for notched phones |
| UI-06 | Screen stays awake during active voice session on mobile (Screen Wake Lock API) | navigator.wakeLock.request('screen') with visibilitychange re-acquire pattern, 95.9% browser support |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Screen Wake Lock API | Baseline 2025 | Keep screen awake during voice | Native browser API, 95.9% global support, no library needed |
| Keyboard Events API | Stable | Hold-to-talk, disconnect shortcuts | Native keydown/keyup events, no library needed |
| Gemini realtimeInput.text | Live API v1beta | Send typed text to Gemini | Already supported by gateway text frame forwarding |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| CSS dvh units | Baseline 2023 | Dynamic viewport height on mobile | Already used in voice.html (line 19) |
| CSS env() safe-area | Supported since iOS 11 | Handle notched phone displays | For bottom controls on iPhone X+ |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Screen Wake Lock API | NoSleep.js (plays hidden video) | Hack that wastes battery, Wake Lock API is the right answer |
| Native keyboard events | Hotkeys.js library | Unnecessary dependency for 2 shortcuts in a single HTML file |

**Installation:**
```bash
# No installation needed. All features use native browser APIs.
# voice.html is a self-contained single HTML file with no dependencies.
```

## Architecture Patterns

### Recommended Changes to voice.html
```
voice.html (existing ~880 lines, adding ~200 lines)
  CSS additions:
  ├── text-input-bar styles
  ├── enhanced mobile breakpoints (safe-area, larger targets)
  └── keyboard hint tooltip styles

  HTML additions:
  ├── text input bar (input + send button)
  └── keyboard shortcut hints (tooltip/label)

  JS additions:
  ├── Keyboard handler (keydown/keyup)
  ├── Text input handler (sendTextMessage)
  ├── Wake Lock manager (request/release/re-acquire)
  └── Focus guard (disable shortcuts when typing)
```

### Pattern 1: Keyboard Shortcuts with Focus Guard
**What:** Spacebar hold-to-talk and Escape disconnect, disabled when text input is focused
**When to use:** Desktop keyboard interaction
**Example:**
```javascript
// Source: MDN KeyboardEvent docs + hold-to-talk pattern
var spaceHeld = false;

document.addEventListener('keydown', function(e) {
  // Skip shortcuts when typing in text input
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

  if (e.code === 'Space' && !e.repeat) {
    e.preventDefault(); // Prevent page scroll
    if (currentState === STATE.READY) {
      spaceHeld = true;
      startCapture();
    }
  } else if (e.code === 'Escape') {
    e.preventDefault();
    disconnectVoice();
  }
});

document.addEventListener('keyup', function(e) {
  if (e.code === 'Space' && spaceHeld) {
    spaceHeld = false;
    if (currentState === STATE.LISTENING) {
      stopCapture();
    }
  }
});
```

### Pattern 2: Text Input to Gemini via realtimeInput
**What:** Send typed text as JSON through WebSocket, Gemini responds with audio
**When to use:** When user types instead of speaking
**Example:**
```javascript
// Source: Gemini Live API docs (ai.google.dev/api/live)
function sendTextMessage(text) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (!text.trim()) return;

  // Send as JSON text frame (gateway forwards to Gemini as-is)
  var msg = JSON.stringify({
    realtimeInput: {
      text: text
    }
  });
  ws.send(msg);

  // Show in transcript immediately (user sees what they typed)
  addTranscript('user', text);
}
```

### Pattern 3: Screen Wake Lock with Visibility Re-acquire
**What:** Keep screen awake during active voice session, re-acquire after tab switch
**When to use:** Mobile voice sessions
**Example:**
```javascript
// Source: MDN Screen Wake Lock API docs
var wakeLock = null;

async function requestWakeLock() {
  if (!('wakeLock' in navigator)) return; // No-op on unsupported browsers
  try {
    wakeLock = await navigator.wakeLock.request('screen');
    wakeLock.addEventListener('release', function() {
      wakeLock = null;
    });
  } catch (err) {
    // Silently fail (low battery, system settings, etc.)
    console.log('[voice] wake lock denied:', err.message);
  }
}

function releaseWakeLock() {
  if (wakeLock) {
    wakeLock.release();
    wakeLock = null;
  }
}

// Re-acquire when returning to tab (browser releases on visibility change)
document.addEventListener('visibilitychange', function() {
  if (document.visibilityState === 'visible' && currentState !== STATE.DISCONNECTED) {
    requestWakeLock();
  }
});
```

### Pattern 4: Mobile-First Responsive Layout
**What:** Touch-friendly controls with proper sizing and safe areas
**When to use:** Phone browsers
**Example:**
```css
/* Source: WCAG 2.5.5 + Apple HIG tap target guidelines */

/* Text input bar */
#text-input-bar {
  display: flex;
  padding: 8px 16px;
  padding-bottom: calc(8px + env(safe-area-inset-bottom, 0px));
  gap: 8px;
  border-top: 1px solid #1a1a1a;
  flex-shrink: 0;
}

#text-input-bar input {
  flex: 1;
  background: #1e1e1e;
  border: 1px solid #333;
  color: #e0e0e0;
  padding: 12px 16px;
  border-radius: 24px;
  font-size: 16px; /* Prevents iOS zoom on focus */
  outline: none;
}

#text-input-bar button {
  width: 44px;
  height: 44px;
  min-width: 44px;
  border-radius: 50%;
  background: #7c6aef;
  border: none;
  cursor: pointer;
}

/* Mobile controls padding with safe area */
@media (max-width: 768px) {
  #controls {
    padding-bottom: calc(24px + env(safe-area-inset-bottom, 0px));
  }
}
```

### Anti-Patterns to Avoid
- **Keyboard shortcuts active during text input:** Pressing spacebar while typing would trigger talk mode instead of typing a space. Always check `e.target.tagName` before handling shortcuts.
- **Font-size below 16px on mobile inputs:** iOS Safari auto-zooms on input focus if font-size < 16px. Use `font-size: 16px` minimum.
- **Using 100vh instead of 100dvh on mobile:** Already avoided in current code (line 19 uses `100dvh`), but be careful not to regress.
- **Forgetting to re-acquire wake lock after visibility change:** Browser releases wake lock when tab becomes hidden. Must re-acquire on `visibilitychange` to `visible`.
- **Not preventing spacebar default on keydown:** Must call `preventDefault()` on `keydown`, not `keyup`. The scroll happens on keydown.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Screen wake lock | Hidden video/audio hack (NoSleep.js pattern) | `navigator.wakeLock.request('screen')` | Native API has 95.9% support, clean release semantics, battery-friendly |
| iOS safe area handling | Fixed pixel padding guesses | `env(safe-area-inset-bottom)` | Varies by device, CSS env() is the standard approach |
| Mobile viewport height | JavaScript window.innerHeight calculations | CSS `dvh` units | Already used in voice.html, handles iOS Safari toolbar correctly |

**Key insight:** All four requirements use native browser APIs. No libraries needed. The single-HTML-file constraint is easily met because these are all vanilla JS + CSS additions.

## Common Pitfalls

### Pitfall 1: Spacebar Triggers Click on Focused Button
**What goes wrong:** If the mic button has focus, pressing spacebar triggers both the keyboard shortcut AND a click event on the button (double-firing).
**Why it happens:** Browsers fire click events on focused buttons when spacebar is pressed (accessibility feature).
**How to avoid:** Either blur the mic button after click, or in the keyboard handler check if the mic button has focus and skip the shortcut.
**Warning signs:** Double state transitions, startCapture called twice.

### Pitfall 2: Text Input Empty After Send on Mobile
**What goes wrong:** On some mobile browsers, clearing input value doesn't work if the input still has focus and IME is composing.
**Why it happens:** Mobile keyboard composition events (CJK, autocomplete) hold onto the input value.
**How to avoid:** Blur the input before clearing, or use `setTimeout` to clear after blur completes.
**Warning signs:** Previous message text stays in input after send.

### Pitfall 3: Wake Lock Fails Silently
**What goes wrong:** Wake lock request fails but app doesn't notice, screen still dims.
**Why it happens:** Low battery mode, power saver, or unsupported browser. The API throws but many implementations don't catch.
**How to avoid:** Always wrap in try/catch. Don't show error to user (it's a nice-to-have). Log for debugging.
**Warning signs:** Screen dims during active voice session on mobile.

### Pitfall 4: Keyboard Event Repeat Property
**What goes wrong:** Holding spacebar fires multiple keydown events, causing startCapture to be called repeatedly.
**Why it happens:** Keyboard auto-repeat. `keydown` fires every ~30ms while key is held.
**How to avoid:** Check `e.repeat === true` and ignore repeated keydown events. Or use a `spaceHeld` flag.
**Warning signs:** Console spam of "startCapture" calls, audio glitches.

### Pitfall 5: Gateway Text Frame Forwarding Assumes Valid JSON
**What goes wrong:** If browser sends malformed JSON text frame, gateway forwards it to Gemini which rejects it.
**Why it happens:** Gateway line 9084-9088 forwards text frames as-is without validation.
**How to avoid:** Construct the JSON on the browser side and `JSON.stringify` before sending. Gateway doesn't need changes.
**Warning signs:** Gemini drops connection after text message sent.

### Pitfall 6: Gemini Responds with Audio to Text Input
**What goes wrong:** User types a message, expects a text reply, but Gemini speaks the response because responseModalities is AUDIO.
**Why it happens:** The config sets `responseModalities: ["AUDIO"]`. Text input goes through realtimeInput, and Gemini responds with audio regardless of input modality.
**How to avoid:** This is actually CORRECT behavior for a voice dashboard. The user types when voice isn't convenient, but they still hear the response. The transcript shows the text. Make sure the status text changes to "Speaking" so the user knows audio is coming.
**Warning signs:** None, this is expected behavior. Document it clearly in the UI.

## Code Examples

### Complete Keyboard Handler
```javascript
// Source: MDN KeyboardEvent + voice dashboard state machine
var spaceHeld = false;

document.addEventListener('keydown', function(e) {
  // Skip when typing in text input
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA'
      || e.target.isContentEditable) return;

  if (e.code === 'Space') {
    e.preventDefault();
    if (e.repeat) return; // Ignore auto-repeat

    if (currentState === STATE.DISCONNECTED) {
      // First press connects
      connectVoice();
    } else if (currentState === STATE.READY) {
      spaceHeld = true;
      startCapture();
    } else if (currentState === STATE.SPEAKING) {
      // Barge-in: interrupt AI and start talking
      spaceHeld = true;
      handleInterruption();
      startCapture();
    }
  } else if (e.code === 'Escape') {
    e.preventDefault();
    if (currentState !== STATE.DISCONNECTED) {
      disconnectVoice();
    }
  }
});

document.addEventListener('keyup', function(e) {
  if (e.code === 'Space' && spaceHeld) {
    spaceHeld = false;
    if (currentState === STATE.LISTENING) {
      stopCapture();
    }
  }
});
```

### Complete Text Input Handler
```javascript
// Source: Gemini Live API realtimeInput.text format
function sendTextMessage(text) {
  if (!text || !text.trim()) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;

  var msg = JSON.stringify({
    realtimeInput: { text: text.trim() }
  });
  ws.send(msg);

  // Add to transcript as user message
  addTranscript('user', text.trim());

  // Clear input
  var input = document.getElementById('text-input');
  if (input) input.value = '';
}
```

### Complete Wake Lock Manager
```javascript
// Source: MDN Screen Wake Lock API
var wakeLock = null;

async function requestWakeLock() {
  if (!('wakeLock' in navigator)) return;
  try {
    wakeLock = await navigator.wakeLock.request('screen');
    wakeLock.addEventListener('release', function() {
      wakeLock = null;
    });
  } catch (err) {
    console.log('[voice] wake lock request failed:', err.message);
  }
}

function releaseWakeLock() {
  if (wakeLock) {
    wakeLock.release();
    wakeLock = null;
  }
}

// Re-acquire on tab return
document.addEventListener('visibilitychange', function() {
  if (document.visibilityState === 'visible'
      && currentState !== STATE.DISCONNECTED
      && wakeLock === null) {
    requestWakeLock();
  }
});
```

### Text Input HTML
```html
<!-- Below #controls, above </body> -->
<div id="text-input-bar">
  <input type="text" id="text-input" placeholder="Type a message..."
         autocomplete="off" enterkeyhint="send">
  <button id="text-send-btn" onclick="sendTextFromInput()" aria-label="Send message">
    <svg viewBox="0 0 24 24" width="20" height="20" fill="#fff">
      <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
    </svg>
  </button>
</div>

<script>
function sendTextFromInput() {
  var input = document.getElementById('text-input');
  sendTextMessage(input.value);
}

document.getElementById('text-input').addEventListener('keydown', function(e) {
  if (e.code === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendTextFromInput();
  }
});
</script>
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| NoSleep.js (hidden video hack) | Screen Wake Lock API | Baseline 2025 (March) | Native, battery-friendly, clean release |
| `100vh` on mobile | `dvh` viewport units | Baseline 2023 | Already used in voice.html |
| Fixed padding for notched phones | `env(safe-area-inset-*)` | iOS 11+ (2017) | Standard way to handle notch/home indicator |
| `keypress` event | `keydown` + `e.code` | Long established | `keypress` deprecated, `e.code` is layout-independent |

**Deprecated/outdated:**
- `keypress` event: Deprecated. Use `keydown`/`keyup` with `e.code` (layout-independent) or `e.key` (character-based).
- `e.keyCode`: Legacy numeric codes. Use `e.code` ('Space', 'Escape') instead.

## Open Questions

1. **Should text input be visible when disconnected?**
   - What we know: Text requires an active WebSocket to Gemini. No connection = can't send text.
   - What's unclear: Should the input be hidden/disabled when disconnected, or always visible?
   - Recommendation: Show the input always, but disable it and show placeholder "Connect to start typing" when disconnected. Reduces visual jank from showing/hiding elements.

2. **Should spacebar connect AND start capture in one gesture?**
   - What we know: Current onMicClick does connect on first tap, then requires second tap to start capture.
   - What's unclear: Should holding spacebar do both (connect + auto-start capture when ready)?
   - Recommendation: First spacebar press connects. Once connected and READY, holding spacebar activates capture. This matches the existing two-step flow and avoids unexpected behavior.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 |
| Config file | docker/pytest.ini |
| Quick run command | `cd docker && python -m pytest tests/test_voice.py -x -q` |
| Full suite command | `cd docker && python -m pytest tests/ -x -q --timeout=30` |
| Estimated runtime | ~5 seconds |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| UI-03 | Keyboard shortcuts (spacebar, escape) in voice.html | static-analysis | `cd docker && python -m pytest tests/test_voice.py -x -q -k keyboard` | no, Wave 0 gap |
| UI-04 | Text input bar exists, sendTextMessage function present | static-analysis | `cd docker && python -m pytest tests/test_voice.py -x -q -k text_input` | no, Wave 0 gap |
| UI-05 | Mobile-responsive CSS (44px+ targets, safe-area, breakpoints) | static-analysis | `cd docker && python -m pytest tests/test_voice.py -x -q -k mobile` | no, Wave 0 gap |
| UI-06 | Wake Lock API usage in voice.html | static-analysis | `cd docker && python -m pytest tests/test_voice.py -x -q -k wake_lock` | no, Wave 0 gap |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task, run: `cd docker && python -m pytest tests/test_voice.py -x -q`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work` runs
- **Estimated feedback latency per task:** ~3 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] `docker/tests/test_voice.py` (append new test classes) -- static analysis tests for UI-03 (keyboard shortcuts in HTML), UI-04 (text input elements), UI-05 (mobile CSS patterns), UI-06 (wake lock API calls)

Note: These are all static-analysis tests that parse voice.html content, following the existing pattern in `TestVoiceDashboardFile`. No integration tests needed because the features are pure browser-side JS/CSS. Tests verify the HTML file contains required patterns (event listeners, CSS rules, API calls).

## Sources

### Primary (HIGH confidence)
- [MDN Screen Wake Lock API](https://developer.mozilla.org/en-US/docs/Web/API/Screen_Wake_Lock_API) - Full API usage, error handling, visibility change pattern
- [Gemini Live API Reference](https://ai.google.dev/api/live) - realtimeInput.text JSON format, clientContent vs realtimeInput differences
- [Gemini Live API WebSocket Guide](https://ai.google.dev/gemini-api/docs/live-api/get-started-websocket) - Raw WebSocket text input format
- [MDN KeyboardEvent](https://developer.mozilla.org/en-US/docs/Web/API/Element/keydown_event) - keydown/keyup events, repeat property, preventDefault
- [Can I Use: Screen Wake Lock](https://caniuse.com/wake-lock) - Browser support: iOS Safari 16.4+, Chrome 85+, Firefox 126+, 95.9% global

### Secondary (MEDIUM confidence)
- [WCAG 2.5.5 Target Size](https://blog.logrocket.com/ux-design/all-accessible-touch-target-sizes/) - 44x44pt minimum tap targets (Apple HIG), 48x48dp (Android)
- [web.dev viewport units](https://web.dev/blog/viewport-units) - dvh/svh/lvh viewport unit guide
- [Jan Kollars: Preventing Space Scrolling](https://www.jankollars.com/posts/preventing-space-scrolling/) - preventDefault on keydown to stop spacebar scroll

### Tertiary (LOW confidence)
- None. All findings verified with official sources.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - All native browser APIs with official MDN documentation
- Architecture: HIGH - Existing voice.html patterns clear, gateway already supports text frames
- Pitfalls: HIGH - Well-documented browser quirks with known solutions
- Gemini text input: HIGH - Verified realtimeInput.text format from official API reference

**Research date:** 2026-03-27
**Valid until:** 2026-04-27 (30 days, all APIs are stable/baseline)
