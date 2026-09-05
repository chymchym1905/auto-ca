# Tower of Fantasy Automation

Python toolkit to automate tasks in the **Tower of Fantasy** game on Windows.
The first piece is an interactive **window capturer** that binds three game
windows to roles for later automation steps.

## Environment

- Windows 11, Python 3.12 (`C:\Program Files\Python312\python.exe`).
- Virtualenv lives in `.venv/`. Always run with the venv interpreter:
  ```
  .venv\Scripts\python.exe main.py
  ```
- Install/refresh deps: `.venv\Scripts\python.exe -m pip install -r requirements.txt`

## Run

```
.venv\Scripts\python.exe main.py
```

## Core behavior (confirmed with the user — do not change without asking)

- **Capture method:** *focus + hotkey*. The user focuses the target ToF window
  and presses the global hotkey **Ctrl+Enter**. The app grabs the current
  foreground window and screenshots its on-screen region.
- **Roles:** fixed and in this order — `mainhost`, `main`, `althost`.
- **Confirmation:** after each capture the screenshot is shown and the user must
  Confirm (correct window) or Retry before advancing to the next role.
- **Capture output:** HWNDs live in-memory (`MainWindow._bound`, `role -> hwnd`).
  **Usernames and the last window PID** persist to a hand-editable `config.json`
  (`config.py`, `RoleConfig.pid`).
- **PID restore (skip recapture):** on launch, `MainWindow._try_restore` re-binds
  each role whose saved `pid` still owns a visible ToF window
  (`capturer.find_window_by_pid`, matched by PID + title hint to survive PID
  reuse). HWNDs change per launch but PIDs are stable while the client runs.
  Only roles that fail to restore stay in `_pending_roles` for manual capture; if
  all restore, the app skips capture and opens the control panel directly. PIDs
  are saved on capture-confirm (`get_window_pid`).
- **UI framework:** PyQt6.
- **Packaging:** single Windows executable via PyInstaller. Build with
  ```
  .venv\Scripts\python.exe build.py
  ```
  which runs `tof-auto-ca.spec` and then stages the runtime files beside the exe:
  `dist\ToF-Auto-CA.exe` + `dist\templates\` + `dist\config.json` (~102 MB,
  windowed, no console). **Ship the whole `dist\` folder** — `config.json`,
  `templates/` and `replays/` deliberately stay *outside* the bundle because they
  are hand-edited and written at runtime, which a one-file bundle's temp
  extraction dir cannot support (`config.config_path()`, `icons.templates_dir()`
  and `replay.replays_dir()` all switch on `sys.frozen`).
  `winsdk` is a namespace package whose WinRT submodules are imported lazily, so
  the spec lists them explicitly in `hiddenimports` — without that, OCR fails only
  at runtime. Verify a build with `ToF-Auto-CA.exe --selftest`: it checks the
  runtime paths, loads the icon templates and runs a real OCR round-trip, writing
  `selftest.txt` beside the exe (exit code 0 = healthy).

## Automation flow (CA farm loop) — confirmed with user

> Full behavior spec — every step, state, tab, button, and OCR quirk — is in
> **[FLOW.md](FLOW.md)**. The summary below is the high level.

Three windows by role drive a 6-step loop (reference screenshots in `ref-images/`):
1. **main** opens team screen → **Quit Team** → confirm **"Leave the team?" → OK**
   (OCR reads OK as `0K`; `click_text_any(["OK","0K"])`). Lands on the Lobby
   **My Team** tab (`my_team_empty`, shows CREATE TEAM).
2. **mainhost** opens team menu → team lobby → presses **Go** (find match). After
   pressing, it verifies the Go button cleared (`_go_button_stuck`, OCR crop of
   `detector.GO_BUTTON_REGION`). If it still reads "Go" the press did nothing, and
   **the reason is read from the game's System chat log** rather than assumed
   (`_diagnose_match_failure`): Esc out to the overworld → **Enter** (chat opens
   on the last-used channel, World by default) → **click System** (mandatory) →
   OCR the newest log lines. (ToF also toasts the reason on the
   failing screen for ~2s; `_button_stuck` **burst-captures 9 frames without
   OCR** and reads them afterwards to catch it, since an OCR-per-read loop
   samples too slowly. Live runs show the System log does not always carry the
   cooldown, so the two sources back each other up.)
   Only *"Event unavailable"* raises
   `GoUnavailable` and stops the loop; a timed block (*"Matching cooldown ends
   in N min M sec"* / *"Matching banned for N minutes"*) raises `MatchingCooldown`
   and `run_loop` waits it out and retries; *"&lt;player&gt; is not ready."* / *"is offline"*
   raise `MembersBlocked` — matching is blocked until every member is ready and
   online, so step 2 re-presses Go every 20s for 10 tries and the loop **stops**
   if they never clear. **Retries always re-press the same button in place**
   (`_handle_refusal`, shared with step 5's Find matches) and never restart the
   iteration from step 1: a refusal just means that press didn't take, and a
   restart would tear down an already-formed party.
3. **main** clicks the **Team Lobby** tab (tab strip is [Team Lobby] [Nearby
   Teams] [Nearby Players] [My Team]; match the full "Team Lobby" since the title
   is also "Lobby") → `find_target_list` → click **The Critical Abyss** →
   `find_party_list` → **Request** the row named after **mainhost**.
4. **mainhost** → request-list popup → **Approve** the request named after **main**.
5. **althost** → Priority → Challenge → Arena → pick Critical Abyss → **Find
   matches**, then verifies the queue started (`_find_match_stuck`, OCR crop of
   `detector.FIND_MATCH_REGION`). The matching cooldown is **per account**, so
   althost hits it too: a stuck button runs the same
   `_diagnose_match_failure("althost")` as step 2.
6. Sit through the run untouched for the first 4 min (players are playing), then
   poll **mainhost only**; once its in-instance countdown drops below 27:00, click
   the leave button (`ca_leave` icon) → confirm **"Leave the current scene?" → OK**.
   Only mainhost leaves; main/althost return on their own. Loop restarts.

- **Name matching:** the correct party/request row is found by username. Usernames
  come from `config.json`; on-screen names are read with **WinRT OCR**
  (`winsdk`/Windows.Media.Ocr) — chosen over Tesseract/EasyOCR because it ships
  with Windows, needs no bundled binary, and keeps the PyInstaller build small.
- **Click delivery:** *focus + real input* — bring the target window to the
  foreground, move the OS cursor and click, then move to the next window. Chosen
  because DirectX/Unreal windows usually ignore background `PostMessage` input.

## State detection + actions (built: OCR-driven, not template matching)

Detection and click-targeting are **OCR-driven**, since nearly every ToF UI
element is text-labeled. This is resolution-independent and needs no per-account
templates or OpenCV (lighter PyInstaller bundle). Modules:

- **`ocr.py`** — WinRT OCR wrapper. `recognize_bgra(bytes, w, h)` → `OcrResult`
  (lines/words with pixel bounding boxes). In-memory: BGRA → PNG (PIL) →
  `InMemoryRandomAccessStream` → `BitmapDecoder` → OCR. No temp files.
- **`textmatch.py`** — fuzzy matching (OCR is imperfect: `ritiC91 Abyss`).
  `find_phrase` (best fuzzy match + box), `find_all` (every match, for repeated
  button labels), `normalize` (lower + alnum only), `SequenceMatcher` ratios.
- **`detector.py`** — `classify(OcrResult)` scores each `State` by how many
  signature phrases fuzzily appear; highest (priority, hits, score) wins; no
  match → `overworld`. `priority` lets overlays (toasts/popups/drill-ins) win
  over the base screen they cover. **Validated 17/17 on `ref-images/`.** States:
  overworld, overworld_menu, overworld_chat, chat_system, team_popup,
  team_lobby, request_list_popup, team_joined, find_target_list,
  find_party_list, arena_screen, priority_menu, priority_challenge. Signatures
  are tuned from ref-images and will need live re-tuning. Also holds the
  **System-log** readers behind the Go-failure diagnosis:
  `read_system_messages` (regroups OCR fragments into log lines inside
  `SYSTEM_LOG_REGION`), `parse_matching_cooldown` ("2 min 25 sec" → 145) and
  `classify_system_message` → `("unavailable"|"cooldown"|"unknown", secs)`.
- **OCR robustness (Arena screen):** full-frame OCR of artwork-heavy screens is
  unreliable (0-17 lines across identical frames), so `actions.read_window_robust`
  retries a near-empty read on a 2× upscale (used by `Session.detect`), and
  `actions.click_text_in_region` locates a button in an upscaled crop and maps the
  box back to client pixels — more findable *and* better centred than a full-frame
  match. See FLOW.md step 5.
- **`actions.py`** — focus + real input. `focus_window` (SetForegroundWindow +
  AttachThreadInput fallback), `click_client` (ClientToScreen → pydirectinput),
  `click_text` (OCR-locate a label and click), `click_row_button` (find a
  username's row, click its Request/Approve), `press_key` (Esc to back out of a
  menu, Enter to open chat — the keyboard-only paths). **`hold_alt`**: in the ToF
  overworld the cursor is camera-locked, so HUD clicks must hold Alt to free it;
  menu screens do not. Overworld-entry clicks pass `hold_alt=True`.
- **`automation.py`** — `Session(windows, points, …)` with `dry_run()`,
  `wait_for`, six step methods, and `run_loop`.
  - `open_team_lobby(role)`: the side-panel team toggle is an **icon** (no text),
    located via the `team_toggle` template (or normalized point fallback). Rather
    than detect quest-vs-team mode, `_click_team_toggle` **double-clicks the same
    spot (Alt held, 0.5s apart)** — two clicks reach the team screen from quest
    mode (quest→team mode→team screen) and are harmless from team mode — then
    `open_team_lobby` re-checks until `team_lobby` (or another lobby state) shows.
  - `leave_party(role)`: opens the team screen then OCR-clicks **`Quit Team`**
    (+ a best-effort confirm: Confirm/Quit/Yes/OK). Avoids needing the leave
    *icon*.
  - Steps 3/5 are OCR-driven **state machines** (`step3_main_requests`,
    `step5_althost_ca`): each pass detects the window's state and takes the one
    action for it, so they self-correct and tolerate any start screen. Step 5's
    **Arena** tile is artwork (OCR can't read "Arena"), so it uses an `arena_tile`
    icon template; `_open_priority` clicks the overworld `priority_entry` icon.

### Icon templates (`icons.py`) — scale-tolerant matching

Icon-only / visual elements (team toggle, Priority entry, Step-6 match-found)
are matched with OpenCV. The three windows can run at **different resolutions**,
so matching is scale-tolerant: each template stores `src_h` (height of the frame
it was cropped from); at match time the base scale is `frame_h / src_h` and a
±18% range is searched, restricted to a normalized search `region` (avoids
false positives in the icon-dense HUD). Validated: 0.72×/1.35× resizes match at
~0.97 at the correct center. `actions.click_icon` focuses → captures BGR →
`IconLibrary.find` → clicks the match center (Alt for overworld icons).

Templates live in `templates/` next to the exe: one PNG per icon + `manifest.json`
(`{name: {file, src_h, region, threshold}}`), hand-editable.

**Variants:** a button with several looks (e.g. `priority_entry` — red-dot badge /
"new" tag / plain) is stored as multiple templates named `<base>#<n>`
(`priority_entry`, `priority_entry#2`, …). `IconLibrary.find`/`find_scored` match
against **all** variants of a base (`base_name()` strips the `#n`) and return the
best passing one — any variant matching is enough. `has(base)`/`variants(base)`/
`next_variant_name(base)` are the variant-aware helpers; automation uses
`icons.has(name)` instead of `name in templates`.

**Capture flow (F8 hotkey, id=2):** focus the ToF window showing the icon, press
**F8** → grabs the frame → `CropDialog` (drag-to-box; name via **radio buttons**
from `icons.KNOWN_ICONS`; an **"additional variant"** checkbox keeps an existing
capture and saves a new `<base>#n`) → `IconLibrary.add` saves the grayscale crop +
a padded normalized search region. `hotkey.py` is parameterized by `hotkey_id` so
F8 and Ctrl+Enter coexist.

`automation.Session` prefers an icon template, falling back to a calibrated
point: `team_toggle` (icon or `config.points` fallback), `priority_entry` (icon).

### Calibrated points (legacy fallback for `team_toggle`)

`config.json` `points: {name: [nx, ny]}` (normalized client fractions). Set via
the **Ctrl+Enter** hotkey: "Calibrate team button" → hover the toggle → Ctrl+Enter
→ `MainWindow._record_calibration`. Used only if no `team_toggle` icon exists.

## UI: automation control panel

After capture finishes, `MainWindow` reveals a panel (log + "Detect states
(dry-run)" / "Start CA loop" / "Stop"). Automation runs on a `threading.Thread`;
the worker touches the UI only via `log_signal` / `worker_done` (Qt-thread-safe).
`closeEvent` sets the stop event.

## Layout

```
main.py                       # entry point: builds QApplication, installs hotkey
src/tof_automation/
  capturer.py                 # ROLES, CaptureResult, capture_window() (mss + win32gui)
  hotkey.py                   # GlobalHotkey: Win32 RegisterHotKey via Qt native event filter
  ui.py                       # MainWindow: role-by-role capture/confirm flow
requirements.txt
```

## Key implementation notes

- **Foreground capture** uses `win32gui.GetForegroundWindow()`, then `mss`
  grabs the window's **client region** on screen. This is reliable for DirectX
  game windows (where `PrintWindow` often returns black) because the user has
  focused the window so it is on top.
- **Client-area capture (no title bar; dynamic-resolution safe).** Region is
  computed in `capturer.get_client_region()` as:
  ```python
  _, _, cw, ch = win32gui.GetClientRect(hwnd)        # (0,0,w,h) of client area
  left,  top    = win32gui.ClientToScreen(hwnd, (0, 0))
  right, bottom = win32gui.ClientToScreen(hwnd, (cw, ch))
  ```
  `GetClientRect` excludes the title bar and borders; `ClientToScreen` maps the
  client corners to absolute screen coordinates. It is recomputed on every
  capture, so a window that resizes itself at runtime (dynamic resolution) is
  always grabbed at its current size with no chrome. *Do NOT use `GetWindowRect`*
  — that includes the title bar. Technique adapted from
  `D:\Software\nte-auto-fishing-script` (`get_window_client_region`), which uses
  the same `GetClientRect` + `ClientToScreen` pair.
- **Global hotkey** is needed because the ToF window — not our app — holds focus
  at capture time. `RegisterHotKey` delivers `WM_HOTKEY` regardless of focus;
  `GlobalHotkey.nativeEventFilter` catches it and emits a Qt signal.
- **BGRA → QPixmap:** `mss` returns BGRA bytes, which match Qt's `Format_RGB32`
  in little-endian memory — no channel swap needed. `_bgra_to_pixmap` copies the
  image to detach from the temporary buffer.

## Conventions

- `src/` layout; `main.py` inserts `src` onto `sys.path` before importing.
- Ask the user before changing any confirmed core behavior above.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
