# Changelog

## Unreleased

- Changed OCR fallback routing so fast dialogue captures use Windows OCR only;
  an unusable full-window Windows OCR result now triggers one same-frame
  RapidOCR PP-OCRv6-small ONNX fallback, with the old 2x focused pass removed
  from the automatic flow. ONNX Runtime is preloaded before WinRT OCR to avoid
  a Windows DLL initialization-order failure when the fallback is needed.
- Added the `configure_game_layout` MCP tool and persisted per-session layout
  profiles for dialogue regions, speaker/choice regions, and game-specific OCR
  marker pairs. The parser no longer infers current-game symbols from a window
  title or hard-coded bracket sets; fast dialogue captures project the profile
  into their local coordinates before parsing.
- Added the `configure_game_timing` MCP tool. Games with typewriter animation
  can opt into local bottom-dialogue text-hash settling; a timeout stops
  autoplay before another click instead of treating a partial line as ready.
  The default fixed path remains unchanged for fast, animation-free profiles.
- Expanded the README with a first-use, new-game, and cross-Agent handoff
  checklist, including full-window validation before enabling fast dialogue OCR.

## [0.1.0] - 2026-08-14

- Added the local MCP server for visual-novel observation, input, and session
  recording.
- Added structured dialogue, choice, scene, action, and story-variable data.
- Added full-desktop capture and complete-window background capture on Windows.
- Added local Windows OCR with compact processed text for MCP consumers.
- Added an MIT license and initial open-source packaging metadata.
- Optimized Windows capture with bulk channel conversion and fast PNG encoding;
  bound games now use complete-window capture automatically.
- Reduced default autoplay waits to 0.15 seconds for dialogue and 0.25 seconds
  for choices, while keeping per-call wait overrides available.
- Prevented game focus from restoring normal/maximized/fullscreen windows into
  a smaller mode; minimized windows are the only ones restored. Added a safe
  input-queue fallback for Windows focus restrictions and arrow-key aliases.
- Made mouse injection fail loudly when `SetCursorPos` or any `SendInput`
  mouse event is rejected instead of reporting a false successful click.
- Added an explicit absolute mouse-move event before each click so games that
  depend on raw mouse movement see the same move-then-click sequence as a
  physical pointer.
- Added local `advance_game` input verification based on the bottom dialogue
  box OCR region; scene/background changes are no longer treated as proof that
  a click or key was consumed.
- Added local settings-page detection; settings controls are excluded from story
  data and high-level autoplay clicks an OCR-located “return to game” button.
  The general parser exposes `screen_type=settings`, manual recording ignores
  those controls, and it never uses ESC as a generic settings recovery key.
- Added an opt-in background input route using `PostMessageW`: `background_press_key`,
  `background_click`, `advance_game(background=true)`, and
  `select_choice(background=true)` queue input without activating the game or
  moving the real cursor. Results distinguish a Windows-accepted message
  (`queued=true`) from actual game consumption; Win32-message-only games are
  supported, while Raw Input/DirectInput paths may ignore it.
- Added a bounded `SendMessageTimeoutW` delivery mode through `delivery="send"`
  / `background_input_method="send"` for engines that do not consume the
  asynchronous queue promptly; it still reports system-level delivery rather
  than claiming that the game applied the action.
- Added `background_scroll` for background `WM_MOUSEWHEEL` testing, including
  screen-coordinate wheel messages and a client-area mouse-move prelude.
- Changed background `advance_game` to send a window-center left click directly;
  it no longer sends the ineffective background SPACE path.
- Reused the most recent local bottom-dialogue OCR snapshot during advancement
  when it is fresh, and reduced the default post-input wait to `0.05` seconds
  to remove an unnecessary pre-input capture from the fast path.
- Treats a transient blank dialogue panel during a fade as “not verified” and
  performs one short resample instead of falsely reporting a successful advance.
- Added an explicit Windows touch-injection alternative through
  `click_screen(input_method="touch")`.
- Enabled Windows per-monitor DPI awareness before capture and input so a
  scaled 2560x1600 desktop is not incorrectly reported as 1707x1067 and
  window/screen coordinates remain consistent.
- Added normalized/pixel `ocr_region` parameters to OCR and autoplay tools;
  region filtering leaves the saved full screenshot unchanged and gives
  Senren*Banka a default lower dialogue-box profile.
- Added a two-stage window capture path: the first window frame establishes the
  complete geometry, while later OCR/advance/choice calls use a fast dialogue
  region frame and automatically fall back to a complete `PrintWindow` frame
  when the region is blank, occluded, or otherwise unusable. Region metadata is
  kept separate from full-window coordinates so background clicks do not drift.
- Reused a persistent local Windows OCR worker, asyncio loop, and OCR engine so
  MCP calls do not recreate the WinRT/COM OCR environment for every dialogue.
- Added `play_until_choice`, which keeps OCR, structured recording, and advance
  clicks local until a real game choice appears, then returns one compact batch
  to Codex with safety limits for blank OCR, settings pages, and max steps.
- Capped the default returned dialogue batch at 6000 story characters so long
  routes stop locally and can be resumed without flooding the Codex context.
- Added a full-window choice probe after the fast bottom-dialogue capture stays
  blank; centered unprefixed button rows such as `说实话` and `敷衍过去` are
  detected locally without sending another blind click.
- Added a repeated-dialogue guard: after multiple delivered clicks with no
  bottom-text change, the MCP probes for choices before sending another click.
- Improved dialogue parsing for variable-length name labels wrapped in
  `〖〗`, `【】`, or `[]`, including OCR-inserted spaces between name
  characters; ambiguous `『...』` rows now use following-row and OCR-position
  context, while ordinary single-row `『...』` and `「...」` remain dialogue.
