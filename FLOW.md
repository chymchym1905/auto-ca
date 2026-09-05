# Tower of Fantasy — CA Farm Automation: Flow & Behavior

This is the canonical reference for how the automation drives the three game
windows through the Critical-Abyss (CA) farm loop. It documents the roles, the
step-by-step flow, every game state the detector recognizes, how clicks are
located, and the known OCR quirks. (`CLAUDE.md` has the architecture/module map;
this file is the behavior spec.)

## Roles (three windows)

| Role | Drives | Typical username (config) |
|------|--------|---------------------------|
| `mainhost` | Hosts the main party; presses Go; approves main's join request | e.g. `Klee 1` |
| `main` | Leaves party each loop, then re-requests to join mainhost's party | e.g. `chymchym1905` |
| `althost` | Queues the CA match from the Arena (Priority → Challenge) | e.g. `Klee 2` |

Usernames are entered at capture time and saved to `config.json`. Name-targeted
actions (Request / Approve a specific row) match the configured username.

## Hotkeys

| Hotkey | Purpose |
|--------|---------|
| **Ctrl+Enter** | Capture the focused window as the current role (capture phase). |
| **F8** | Capture an **icon template** from the focused window (drag-to-crop dialog). |

Both are global (work while the game is focused), registered against the app's
HWND, and consume the `WM_HOTKEY` message so they fire exactly once per press.

## One-time setup

1. **Capture windows** — for each role, focus its ToF window, press Ctrl+Enter,
   confirm the screenshot, enter the username. HWND is bound in memory; the
   window **PID** + username are saved to `config.json`.
2. **PID restore** — on later launches, any role whose saved PID still owns a
   visible ToF window is **re-bound automatically** (matched by PID + title), so
   it needs no recapture. Only missing roles are re-captured. If all three
   restore, the app opens the control panel directly.
3. **Icon templates (F8)** — capture the icons OCR cannot read. The capture
   dialog offers the names as **radio buttons** (no typos); pick one:
   - `team_toggle` — the overworld side-panel team/quest toggle button.
   - `priority_entry` — the overworld trigger that opens the Priority panel.
   - `team_lobby_tab` — the **Team Lobby** tab in the Lobby (its "Team" part is
     an icon and OCR confuses it with the screen title "Lobby", so a template is
     more reliable than text).
   - `arena_tile` — the **Arena** tile on the Priority → Challenge screen (it is
     artwork and OCR cannot read the word "Arena").
   - `arena_ca_selected` — the Arena **"The Critical Abyss"** tab when **selected**
     (highlighted). Used to confirm CA is selected before queueing, since Apex
     League and CA read identically under OCR. Capture it with CA already selected.
   Templates are scale-tolerant (matched across the three windows' different
   resolutions) and saved to `templates/<name>.png` + `templates/manifest.json`.

   **Variants:** some buttons have multiple looks — e.g. `priority_entry` can show
   a **red-dot badge**, a **"new" tag**, or appear **plain**. Capture each look:
   tick **"Save as an additional variant"** in the dialog so it is kept as
   `priority_entry#2`, `#3`, … instead of replacing. A lookup matches against all
   variants of a name and any one matching is enough.

## Click behavior

- **Focus + real input**: only one window is driven at a time. The target window
  is brought to the foreground, then a real cursor move + click is sent
  (`pydirectinput`/SendInput), which DirectX/Unreal windows accept. Background
  message injection is *not* used.
- **Alt-hold in overworld**: in the ToF overworld the cursor is locked to camera
  control. Overworld HUD/icon clicks **hold Alt** to free the cursor
  (`hold_alt=True`). Menu screens (lobby, arena, popups) do not need it.
- **OCR-located text clicks**: buttons/tabs with text are found by OCR bounding
  box and clicked at the box center. Name rows are found by locating the
  username, then clicking the Request/Approve button on the same row.
- **Icon-located clicks**: icon-only buttons are found by template match and
  clicked at the match center.

## The farm loop (6 steps)

The loop runs each iteration in this order. Each step focuses the relevant
window, detects its state, and acts. Steps verify transitions with `wait_for`.

### Step 1 — `main` leaves the party
1. `open_team_lobby("main")`: **double-click** the **team toggle** (icon
   `team_toggle`, Alt held, 0.5s between clicks) and re-check until the **team
   screen** (`team_lobby`) is detected.
   - From quest mode it takes two clicks (quest → team mode → team screen), so we
     always click twice (same spot, located once) rather than detect the mode;
     from team mode the second click is harmless.
2. Click **Quit Team** (OCR text).
3. Confirmation dialog **"Leave the team?"** appears (state `leave_team_confirm`,
   buttons Cancel / OK). Click **OK** — OCR often reads it as `0K`, so the action
   tries both (`click_text_any(["OK","0K"])`).
4. End state: Lobby on the **My Team** tab showing **CREATE TEAM**
   (`my_team_empty`).

### Step 2 — `mainhost` presses Go
1. `open_team_lobby("mainhost")` → `team_lobby`.
2. Click **Go** (OCR text) to start matchmaking / open the party for requests.
3. **Verify it took** (`_go_button_stuck`): re-OCR a tight crop of the Go-button
   region (`detector.GO_BUTTON_REGION`, bottom-right of the lobby bar) a few times.
   On success the button changes/disappears; if it still reads **Go** across every
   read, the press did nothing. Failure requires *unanimous* reads so one OCR miss
   can't strand the loop.
4. **Diagnose the failure** (`_diagnose_match_failure`, shared with althost's
   step 5) — a stuck Go button does **not** by itself mean CA is closed; the
   server also refuses transiently, so the reason is read from the game rather
   than assumed. Two sources, in order:

   **(a) The on-screen toast — caught by burst capture.** ToF prints the reason
   on the failing screen itself (`ref-images/go fail.png`: *"Matching cooldown
   ends in 23s."* mid-screen while **Go** is still on the button — it OCRs
   cleanly and `read_toast_verdict` parses it). The toast is up for barely two
   seconds, and an OCR-per-read loop samples far too slowly to see it (a full
   frame costs ~0.5s to OCR, a capture ~30ms). So `_button_stuck`
   **burst-captures 9 frames ~0.45s apart with no OCR at all**, then reads them
   afterwards:
   1. each frame's *button crop* is OCR'd — the first frame missing the label
      means the queue started, and nothing further is read;
   2. only when the label survived every frame are the *full* frames OCR'd,
      oldest first, looking for the toast.

   That way the expensive OCR is spent only on a real failure, while the sample
   rate during the toast's brief life is ~2 frames/second. Catching it is still a
   bonus — when it's missed the flow falls through to (b).

   **(b) The System chat log — the reliable source.** The log keeps the message
   permanently, so this is what the flow is built around:
   - `_return_to_overworld` — press **Esc** until mainhost is out of the menus
     (state re-checked between presses; matchmaking only happens in the overworld
     anyway).
   - Press **Enter** → the chat panel opens on the last-used channel, which
     defaults to **World**. The panel fades in, so the state is polled for a few
     seconds rather than read once — Enter *toggles* the panel, and pressing it
     again on a too-early "still overworld" reading just closes it (this is what
     made the first live run fail on althost, coming out of the Arena screen;
     `chat_settle` also gives the menu-close animation time to finish first).
   - Click the **System** nav item (left rail: World / Crew / Team / Whisper /
     Recruit / Current / System) → state `chat_system`, recognised by its
     read-only footer *"You cannot post in the channel"* (the nav rail itself is
     identical on every channel, so it can't be the signature). **The click is
     mandatory**: Enter never lands on System by itself, and that footer is not
     exclusive to System, so a `chat_system` reading is only trusted *after* we
     have clicked System ourselves — otherwise World chat could be read as the
     game log.
   - `detector.read_system_messages` OCRs the message area
     (`SYSTEM_LOG_REGION`, left rail and footer excluded) and rebuilds the log
     lines — OCR splits one on-screen line into fragments (a coloured span like
     *Energy Station* comes back separately), so fragments are regrouped by their
     y band and ordered by x. Newest message is at the **bottom**.
   - `detector.classify_system_message` scans the last `SYSTEM_LOG_SCAN` (4) lines
     **newest-first** — more than one line because a long message wraps, and
     newest-first so a stale *Event unavailable* from an earlier attempt can't
     outvote a fresher cooldown notice. Matching is fuzzy at the string level
     (`_fuzzy_contains`) because the `[system]` badge glues onto the first word
     (`FRMatching...`) and OCR mangles units (`mln`, `5ec`).
   - **Verdicts:**
     - *"Event unavailable"* (`EVENT_UNAVAILABLE_PHRASES`) → raise `GoUnavailable`
       → `run_loop` **stops**. This is the only case that stops the loop.
     - a **timed block** — *"Matching cooldown ends in 2 min 25 sec."* or
       *"Matching banned for 5 minutes."* (`MATCH_BLOCK_PHRASES` +
       `parse_matching_cooldown`) →
       raise `MatchingCooldown(seconds)` → wait `seconds + 20` (margin: the
       message is rounded) and press the button again, up to `cooldown_retries`
       (5) times.
     - *"&lt;player&gt; is not ready."* or *"&lt;player&gt; is offline."*
       (`member_block_reason`) → raise `MembersBlocked(count, names)`. **The game
       refuses to match while any member is unready or offline**, and they are
       other players — there is nothing to click. One line is logged per member,
       so the count is taken over the scanned tail, each name is tagged with its
       reason (`JinShynestraR (offline)`), and if one of **our own** accounts is
       the blocker it is called out separately (that would be actionable, unlike
       a random player). Retried every `not_ready_delay` (20 s) for
       `not_ready_retries` (10) tries; if they never clear, the exception escapes
       and **`run_loop` stops** — that party is not going to start.

       > `is offline` is matched as an **exact** normalized substring, never
       > fuzzily: the log is full of *"&lt;player&gt; is online."*, and the two
       > score ~0.94 against each other, so a fuzzy test would read a healthy
       > party as blocked and stop the script. Missing an OCR-mangled "offline"
       > is the safe direction to err.
     - anything else → `"unknown"`: retried `unknown_retries` (3) times — the
       first after 3 s, since an unexplained refusal is usually a press that
       didn't register, then backing off to `go_retry_delay` (30 s).

   **Every retry re-presses the same button on the same screen**; it never fails
   back to `run_loop` to restart from step 1. A refusal only means "this press
   didn't take", so the fix is another press of Go / Find matches — restarting the
   iteration would tear down a party that is already formed and queued, and drop
   the windows on screens steps 1-2 don't expect. `_handle_refusal` owns this for
   both steps: run the diagnosis, wait whatever the reason calls for, and return
   whether to press again, with a separate budget per reason (`not_ready_retries`
   10 / `cooldown_retries` 5 / `unknown_retries` 3) so the loop always terminates.
   Only two outcomes stop the whole loop: a closed event, and members who never
   become ready. The System log is re-read on every try, so a *changed* reason
   takes over immediately.
   - The chat panel is closed (`_close_chat`) before returning **or** raising, so
     the retry starts from a clean overworld.

   *Verified live on mainhost:* team lobby → Esc → Enter → System → 30 log lines
   read → verdict. **A live run also showed the log can be silent about a
   cooldown** (only world events were in it), which is why the burst-captured
   toast matters and why an unexplained refusal retries rather than stops. Verified on `ref-images/go fail.png`: toast → `("cooldown",
   23)` while the Go crop still reads "Go" → stuck → wait 43s → retry.

### Step 3 — `main` requests to join mainhost's party
`main` lost foreground while Step 2 drove `mainhost`, and ToF closes the Lobby
when its window is backgrounded — so `main` is **not** at a fixed start here: it
is usually back in `overworld` (or showing the Create/Find-Team popup). Step 3 is
therefore a small **state machine**: each pass detects `main`'s state and takes
the *one* action for it, so it never blind-clicks (e.g. hitting the team toggle
while a popup is up just dismisses it back to overworld — the original bug).
`main` is teamless here, so the path is
`overworld → team_popup → find_target_list → find_party_list`:

| Detected state | Action |
|----------------|--------|
| `overworld` / other | Click the **team toggle** (Alt held; same locate-once spot as `open_team_lobby`) to open the team panel. |
| `team_popup` | Click **Find Team** (the Create/Find-Team overlay). |
| `my_team_empty` | Click the **Team Lobby** tab — prefer the `team_lobby_tab` icon template (its "Team" is an icon and the screen *title* is also "Lobby", so OCR is unreliable), falling back to "Team Lobby" text. |
| `find_target_list` | Click **The Critical Abyss** (left category). |
| `find_party_list` | Click **Request** on the row whose name matches the **mainhost** username — done. |

The tab strip on the lobby is **[Team Lobby] [Nearby Teams] [Nearby Players]
[My Team]**; the CA teams list (`find_party_list`) shows e.g. Klee 1 / Klee 2,
each with a Request and a Match button.

### Step 4 — `mainhost` approves main's request
A **state machine** (like Steps 3/5): each pass detects mainhost's state and acts,
so it opens the list **immediately** (no pre-wait) and retries, up to ~30s.

| Detected state | Action |
|----------------|--------|
| `team_lobby` | Click the **Request List** button (bottom bar, next to Recruit / Go; red badge when requests pending) to open the popup. |
| `request_list_popup` | Click **Approve** on the row matching the **main** username. If main's request hasn't propagated yet the row is absent — the popup stays open and it retries next pass. |

main then joins; mainhost shows a **"Team joined."** toast (`team_joined`).

### Step 5 — `althost` queues the CA match
A **state machine** (like Step 3): each pass detects althost's state and takes
the one action for it, retrying until the CA match is queued.

| Detected state | Action |
|----------------|--------|
| `overworld` / other | Click the **Priority** entry (icon `priority_entry`, Alt held). |
| `priority_menu` | Click the **Challenge** tab (OCR text). |
| `priority_challenge` | Click the **Arena** tile — it is **artwork, not OCR-readable** (OCR finds no "Arena" here), so it uses the **`arena_tile`** icon template (F8). |
| `arena_screen` | Click **The Critical Abyss** (left tab) and **verify it's selected**, then **Find matches** (reads as "Find matches"/"Find match") — done. |

> **CA-selection check:** Apex League and The Critical Abyss read **identically**
> under OCR (only the tab highlight differs), so the click that selects CA can
> silently fail to register and leave **Apex League** queued instead. Step 5
> verifies selection with the **`arena_ca_selected`** icon template (the
> *highlighted* CA tab), re-clicking the tab until it matches before queueing. If
> that template isn't captured it falls back to a best-effort single click + a
> warning — capture it with F8 (on the screen with CA already selected) for
> reliability.

- **Verify the queue took** (`_find_match_stuck`) — the same check mainhost does
  after Go, because **the matching cooldown is per account and althost hits it
  too**. Re-OCR a tight crop of `detector.FIND_MATCH_REGION` (right-hand panel,
  under the mode's PvP tag). What success looks like differs per button, but
  "the label left its own region" covers both (confirmed with the user):
  - **Go** → the button's label *changes to* "Matching".
  - **Find matches** → the button *disappears* and a `Matching 00:02` widget
    takes its place lower on the screen (`ref-images/find match succeed.png`).

  Validated on the ref-images: "Find matches" reads at score 1.00 before the
  press and is absent after it. If the label is still there across every read the
  press did nothing, so hand it to the shared `_handle_refusal("althost", …)` —
  the same policy step 2 uses — which waits out the reason and **presses Find
  matches again, right here**. A refusal retry gives its step back (`steps -= 1`)
  so waiting out a cooldown can't exhaust the navigation budget. Without the check
  a refused Find matches looked like success and step 6 sat out a 40-minute run
  that was never queued; without the in-place retry, one refusal tore down
  mainhost's and main's queue and dropped them on screens step 1/2 don't expect.

> **The `team_toggle` template is background-sensitive.** It is the white flag on
> a plain dark HUD, but once mainhost has a party the member list draws **green
> health bars behind it**, and the match falls from ~0.86 (measured live, no party
> visible) to ~0.50 — its exact threshold — so it flaps pass/fail frame to frame.
> That stopped a live run at iteration 4 (`best score 0.50 < threshold 0.50`)
> while iterations 1-3 had squeaked through. `_locate_team_toggle` therefore tries
> **3 frames** and, failing those, reuses **the position the toggle matched at
> earlier in the session** (`Session._toggle_seen`, normalized) — the HUD never
> moves, so a spot that matched once beats stopping the loop. `CalibrationNeeded`
> is now raised only when it has *never* matched this session. Capturing a variant
> with F8 while the party bars are up (`team_toggle#2`) fixes it at the source.

> **Recovering the team toggle.** `open_team_lobby` now backs out to the
> overworld (`_return_to_overworld(..., allow_chat=False)`) whenever the window
> is on a screen that is neither the plain overworld nor part of the team UI
> (`TEAM_UI_STATES`). The toggle is an overworld HUD icon, so clicking for it on
> the Arena/Priority/chat screens raises `CalibrationNeeded`, which **stops the
> whole loop** — observed live as *"team_toggle ... best score 0.05"* in step 2
> of the iteration after step 5 aborted.

> **The Arena screen OCRs erratically — two mitigations.** Consecutive captures of
> this static screen come back with 0, 3, 8, 12 or 17 lines (artwork-heavy
> background; `ref-images/find match succeed.png` reads **zero lines** at native
> size). Measured on the replay of a failed run, 5 of 14 frames classified as
> `overworld` instead of `arena_screen`, and full-frame OCR located "Find matches"
> in only **3 of 14** frames.
>
> 1. **`actions.read_window_robust`** (used by `Session.detect`): a read returning
>    fewer than `min_lines` (5) is retried on a 2× upscale of the same frame. That
>    rescued 7/7 previously-misread frames — all went from 0-3 lines/`overworld` to
>    10-24 lines/`arena_screen`. A healthy screen never pays for it.
> 2. **`actions.click_text_in_region`**: the button is located in an upscaled crop
>    (14/14 at score 1.00) instead of the full frame, and the match box is mapped
>    back through the crop origin and scale. It can also click a *better* point:
>    on the 1081x749 ref-images, measured against the button's true bounds
>    (`x 846..994, y 485..536`, found by red-pixel thresholding), the full-frame
>    path clicks **(951, 486)** — one pixel inside the top border — while the crop
>    path clicks **(933, 506)**, dead centre. **But this is resolution-dependent
>    and did not explain the failed run:** live-tested at 1080x720 the two paths
>    agree within a pixel ((946, 478) vs (946, 477)) and the click queues
>    correctly. The cause of that run's dead click is still unknown — a server
>    refusal (cooldown) whose toast we missed remains the best candidate, which
>    is what the burst-capture toast scan now exists to catch.

### Step 6 — sit through the run, then mainhost leaves, then loop
- **Grace (first 4 min):** touch nothing — no detection, no focusing. Players are
  actively moving/fighting inside the instance and focusing a window would steal
  their control. (`grace=240`.)
- **After grace:** poll **mainhost only** (one window → minimal disturbance). Its
  HUD shows a countdown from 30:00; on the **first** read that is safely
  **below 27:00** (`confirm_reads=1`), click the leave button and confirm
  **"Leave the current scene?" → OK**.
- **Only mainhost is made to leave.** main and althost stay until the run finishes
  on its own and auto-return. After mainhost leaves, step 6 then **waits for all
  three** to be back in the overworld (slow poll, single confirming read) before
  completing — otherwise the next iteration's step 1 would act on a window still in the run.
- **overworld vs ca_instance** is decided purely by the top-center countdown: timer
  present → `ca_instance`; timer gone + no menu match → `overworld`. "Returned to
  overworld" = countdown gone for that window.
- **Leave button** is a top-left HUD icon (no text) → `ca_leave` icon template
  (F8, capture inside the instance). The instance cursor is camera-locked, so the
  click holds Alt. The timer is read **once**: `_leave_ca_instance(...,
  known_inside=True)` trusts step 6's fresh below-27:00 read and clicks the leave
  icon on its first pass instead of re-reading the countdown (later passes still
  re-detect, so a missed click is retried).
- **Timer read = region crop, not full frame.** OCR'ing the whole frame loses the
  leading minutes digit (`26:06` → `06:06`). Instead the countdown is read from a
  tight, upscaled crop of the normalized `ca_timer` region (`detector.parse_timer`
  on `ocr.recognize_bgra_region`), which recovers the real value. The region is
  resolution-independent (0..1 fractions) and overridable via `config.json`
  `regions.ca_timer`; default in `detector.CA_TIMER_REGION`.
- **Never-early safety net.** Even if a read is wrong, leaving while the true timer
  is still ≥ 27:00 strands mainhost, so the `< 27:00` test (`_timer_below_27`)
  trusts only the reliably-read **units** minutes digit: units 1–6 ⇒ true minutes
  is 26/16/6… (all < 27:00); units 7/8/9/0 are ambiguous (could be 27/28/29/30) so
  it keeps waiting. Debounced over consecutive reads.

## State catalog (detector)

States are classified from OCR text by signature phrases (fuzzy). The state with
the most signature hits wins; `priority` lets an overlay win over the base screen
it covers; no match → `overworld`. Validated on `ref-images/`.

| State | Identified by (signatures) | Notes |
|-------|----------------------------|-------|
| `overworld` | (default / no menu match) | Text-sparse base scene. |
| `overworld_menu` | Smart Servant, Commissary, Suppressors, Matrices, Terminal | Hexagon main menu. |
| `overworld_chat` | Send, Whisper, Recruit, Message reminder, Show barrage | Chat panel. |
| `team_popup` | Create Team **and** Find Team | Overworld create/find popup (needs both). |
| `my_team_empty` | Create Team, Lobby, Free Target, My Team | Lobby, My Team tab, after leaving. |
| `team_lobby` | Quit Team, Go, My Team, Lobby | The team screen (with a party). |
| `chat_system` | You cannot post in the channel | Chat panel on the read-only **System** channel (the game log). Priority 2 — the nav rail also matches `overworld_chat`. |
| `find_target_list` | Free Target, World Exploration, Roaming Boss, Joint Operation | Team Lobby tab list. |
| `find_party_list` | Match **and** Waiting to join | Drilled into a category (CA teams). |
| `request_list_popup` | Deny All, Approve, Ignore (overlay) | The request modal. |
| `leave_team_confirm` | Leave the team, Cancel (overlay) | "Leave the team?" dialog. |
| `team_joined` | Team joined (overlay) | Join-success toast. |
| `priority_menu` | Weekly Activity, Bounty Missions, Vitality, Overworld enemies | Priority → Featured tab. |
| `priority_challenge` | Team PVE, Solo PVE, No attempts, Unlimited | Priority → Challenge tab. |
| `arena_screen` | Arena, Apex League, Find matches, Critical Abyss | Arena match screen. |
| `leave_scene_confirm` | Leave the current scene, Cancel (overlay, priority 2) | "Leave the current scene?" dialog (in-instance); beats `ca_instance`. |
| `ca_instance` | **Opponent** (priority 1); fallback: top-center MM:SS countdown over an otherwise-`overworld` frame | Inside the CA instance. 'Opponent' is unique to it; 'Your Team' is avoided (collides with the 'My Team' tab). Beats a base screen the busy HUD spuriously matches. |

## OCR quirks & disambiguation (matter for clicking)

- **`OK` → `0K`**: the leave-team OK button often OCRs as zero-K. Click via
  `click_text_any(["OK","0K"])`.
- **`Team Lobby` vs `Lobby`**: the screen title and a tab both contain "Lobby",
  and the tab's "Team" is an icon — at low resolution OCR for "Team Lobby" can
  match the title instead. Use the **`team_lobby_tab` icon template** (F8).
- **`Nearby Teams` vs `Nearby Players`**: two adjacent tabs both start with
  "Nearby". The join tab is **Team Lobby** (not Nearby Teams) — see Step 3.
- **CA name variants**: "The Critical Abyss" sometimes OCRs as e.g. `ritiC91
  Abyss`; fuzzy matching handles it.
- **Usernames**: matched fuzzily (e.g. `chymchyml 905` ≈ `chymchym1905`).

## Files

- `config.json` (next to the exe) — per-role `username`, `pid`, `window_title_hint`;
  `points` (normalized icon fallbacks). Hand-editable.
- `templates/` — `<name>.png` per icon + `manifest.json`
  (`{name: {file, src_h, region, threshold}}`). Hand-editable.

## Known caveats / live-tuning notes

- Signature phrases and click labels are tuned from `ref-images/` (low-res) and
  should be re-checked against live captures via **Detect states (dry-run)**.
- `team_toggle` looks slightly different in quest vs team mode (active-tab
  highlight), so the template can fail to re-match on the second click. It is the
  same screen position in both modes, so `_click_team_toggle` locates it **once**
  and clicks that same spot **twice** (0.5s apart) to guarantee reaching the team
  screen from quest mode. Capture the template in quest mode (the state it must
  match from).
- Step 6's in-match
  visual states still need screenshots / icon templates.
