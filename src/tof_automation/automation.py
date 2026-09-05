"""CA-farm loop orchestrator.

Drives the three role windows through the 6-step loop. Every decision is made by
detecting each window's current state (OCR -> :mod:`detector`) and every click is
located by label (OCR -> :mod:`actions`), so there are almost no magic pixel
coordinates. Name-targeted steps use the configured usernames.

What is grounded vs. what needs you:
* Grounded in ref-images (buttons are OCR-located): pressing **Go**, **Request**
  on mainhost's party row, **Approve** main's request, selecting **The Critical
  Abyss** and **Find match** in Arena.
* Needs calibration / upcoming screenshots (overworld entry triggers — no text to
  click): opening the team lobby, leaving the party, opening Priority. These are
  isolated in the ``open_*`` / ``leave_party`` hooks below; each logs and returns
  False until wired to the real trigger (a HUD icon click point or keybind).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import actions, detector, ocr, textmatch
from .capturer import CaptureError
from .icons import IconLibrary
from .replay import ReplayRecorder


@dataclass
class RoleWindow:
    role: str
    hwnd: int
    username: str


LogFn = Callable[[str], None]

# Screens that are already part of the team/lobby UI. From any of these the team
# toggle either works or is unnecessary; from anything else that isn't the plain
# overworld (Arena, Priority, chat, a full-screen menu) the toggle icon simply
# isn't on screen, so it must be backed out of first.
TEAM_UI_STATES: tuple[str, ...] = (
    "team_lobby", "my_team_empty", "team_popup", "team_joined",
    "find_target_list", "find_party_list", "request_list_popup",
)


@dataclass
class QueueCheck:
    """Outcome of re-reading a queue button after pressing it.

    ``stuck`` is True when the button never changed, i.e. the press did nothing.
    ``toast`` carries the verdict if the game said why on-screen while we were
    looking (see :func:`detector.read_toast_verdict`) — reading it costs nothing,
    because the same captures are already being taken for the button crop.
    """

    stuck: bool
    toast: tuple[str, int | None] = ("unknown", None)


class CalibrationNeeded(Exception):
    """Raised by an entry-point hook that has no live trigger wired yet."""


class GoUnavailable(Exception):
    """Raised when the CA event itself is closed, so matchmaking can never start.

    Only raised once the **System chat log** has been read and says so (see
    :meth:`Session._diagnose_match_failure`) — a stuck Go button alone is not
    enough, because the server also refuses for transient reasons. This is the
    one verdict that stops the loop: retrying a closed event is pointless.
    """


class MembersBlocked(Exception):
    """Raised when team members are stopping the queue from starting.

    Two wordings, both per-member: '<player> is not ready.' and '<player> is
    offline.' Matchmaking will not start while either holds. They are other
    players, so there is nothing to click — step 2 waits and re-presses Go, the
    same shape as :class:`MatchingCooldown`.
    """

    def __init__(self, count: int, names: list[str] | None = None) -> None:
        super().__init__(f"{count} team member(s) not ready or offline")
        self.count = count
        self.names = names or []


class MatchingCooldown(Exception):
    """Raised when the server put matchmaking on a timed cooldown.

    The System log says 'Matching cooldown ends in 2 min 25 sec.' — a transient
    block (leaving and re-forming teams quickly), not a closed event. The loop
    waits ``seconds`` out and retries instead of stopping.
    """

    def __init__(self, seconds: int) -> None:
        super().__init__(f"matchmaking is on cooldown for {seconds}s")
        self.seconds = seconds


class Session:
    """Holds the three role windows and drives detection + the farm loop."""

    def __init__(
        self,
        windows: dict[str, RoleWindow],
        points: dict[str, list[float]] | None = None,
        icon_lib: IconLibrary | None = None,
        log: LogFn | None = None,
        stop: threading.Event | None = None,
        regions: dict[str, list[float]] | None = None,
        timer_upscale: int = detector.CA_TIMER_UPSCALE,
        replay_duration: float = 300.0,
        go_retry_delay: float = 30.0,
        not_ready_delay: float = 20.0,
        not_ready_retries: int = 10,
        cooldown_retries: int = 5,
        unknown_retries: int = 3,
        chat_settle: float = 1.5,
    ) -> None:
        self.windows = windows
        self.points = points or {}
        self.icons = icon_lib or IconLibrary()
        self.log: LogFn = log or print
        self.stop = stop or threading.Event()
        # Normalized OCR crop regions (e.g. "ca_timer"); resolution-independent.
        self.regions = regions or {}
        self.timer_upscale = timer_upscale
        # How long to sit out a Go failure the System log couldn't explain, so an
        # unexplained refusal retries at a sane pace instead of spinning.
        self.go_retry_delay = go_retry_delay
        # How long to give team members to ready up, and how many times to
        # re-press Go in place before giving up on the run entirely.
        self.not_ready_delay = not_ready_delay
        self.not_ready_retries = not_ready_retries
        # Budgets for the other refusal kinds, per queue press (see
        # _handle_refusal). A timed block is waited out and the button pressed
        # again; an unexplained refusal gets a few quick retries.
        self.cooldown_retries = cooldown_retries
        self.unknown_retries = unknown_retries
        # Pause after closing menus before opening chat — see _open_system_chat.
        self.chat_settle = chat_settle
        # Rolling instant-replay buffer (started/stopped by run_loop only — not
        # dry_run). On a step failure or caught exception it's dumped to
        # replays/<label>/<role>.mp4 so the screen state that caused it can be
        # reviewed after the fact.
        self.replay = ReplayRecorder(windows, duration=replay_duration, log=self.log)
        # Where each role's team toggle was last matched, as normalized client
        # fractions. The HUD does not move within a session, so a position that
        # matched once is a sound fallback for a frame where the template misses
        # (see _locate_team_toggle).
        self._toggle_seen: dict[str, tuple[float, float]] = {}

    # -- primitives ---------------------------------------------------------
    def _win(self, role: str) -> RoleWindow:
        return self.windows[role]

    def detect(self, role: str) -> detector.Classification:
        """Focus the role's window, read it, and classify its state.

        Uses the near-empty-read retry (:func:`actions.read_window_robust`): a
        frame that OCRs to nothing would otherwise classify as ``overworld`` and
        send a step machine down the wrong branch.
        """
        rw = self._win(role)
        actions.focus_window(rw.hwnd)
        return detector.classify(actions.read_window_robust(rw.hwnd))

    def dry_run(self) -> dict[str, str]:
        """Detect every window's current state without clicking. For validation."""
        states: dict[str, str] = {}
        for role, rw in self.windows.items():
            cls = self.detect(role)
            states[role] = cls.state
            self.log(f"[dry-run] {role} ({rw.username}) -> {cls.state} "
                     f"(conf={cls.confidence:.2f})")
        return states

    def wait_for(
        self, role: str, target: str, timeout: float, interval: float = 1.5
    ) -> bool:
        """Poll ``role`` until it reaches ``target`` state or times out."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.stop.is_set():
                return False
            state = self.detect(role).state
            if state == target:
                return True
            self.log(f"  …{role} is '{state}', waiting for '{target}'")
            time.sleep(interval)
        self.log(f"  ✗ timeout: {role} never reached '{target}'")
        return False

    # -- click helpers ------------------------------------------------------
    def _click_icon_or_text(
        self, role: str, icon_name: str, text: str,
        hold_alt: bool = False, text_threshold: float = 0.7,
    ) -> bool:
        """Click a target by icon template if captured, else by OCR text.

        For elements OCR reads poorly (e.g. the Team Lobby tab, whose "Team" is
        an icon and whose title twin is also "Lobby"), capture an icon with F8;
        the text path is only a fallback.
        """
        hwnd = self._win(role).hwnd
        if self.icons.has(icon_name):
            if actions.click_icon(hwnd, self.icons, icon_name, hold_alt=hold_alt):
                return True
        return actions.click_text(hwnd, text, threshold=text_threshold, hold_alt=hold_alt)

    def _log_request_rows(self, role: str, want: str) -> None:
        """Log the row label next to each 'Request' button (diagnostic).

        When the host row can't be found, this shows what names OCR *did* read so
        a username mismatch with ``config.json`` is easy to spot.
        """
        try:
            result = actions.read_window(self._win(role).hwnd)
        except Exception:
            return
        buttons = textmatch.find_all(result, "Request", 0.7)
        if not buttons:
            self.log(f"  ⚠ looking for host '{want}' but no Request rows are visible")
            return
        names = []
        for b in buttons:
            b_cy = b.y + b.h // 2
            # The row label is the line text to the left of this Request button.
            row = [
                w.text for line in result.lines for w in line.words
                if abs((w.y + w.h // 2) - b_cy) < b.h and (w.x + w.w) <= b.x
            ]
            names.append(" ".join(row).strip() or "?")
        self.log(f"  ⚠ host '{want}' not found. Request rows seen: {names}")

    def _click_team_lobby_tab(self, role: str) -> bool:
        """Click the 'Team Lobby' tab on the Lobby screen.

        Prefer the ``team_lobby_tab`` icon template (F8). The text fallback can't
        rely on the label: OCR reads the tab as just 'Lobby' (its 'Team' is an
        icon) and the screen *title* is also 'Lobby', so a plain text click hits
        the title. Instead we pick the 'Lobby' aligned with the tab strip row
        (anchor 'My Team'), which is the tab.
        """
        hwnd = self._win(role).hwnd
        if self.icons.has("team_lobby_tab"):
            if actions.click_icon(hwnd, self.icons, "team_lobby_tab"):
                return True
        return actions.click_text_aligned(hwnd, "Lobby", "My Team")

    # -- overworld entry hooks ----------------------------------------------
    def _locate_team_toggle(self, role: str, tries: int = 3) -> tuple[int, int]:
        """Resolve the team toggle's client-pixel position.

        Four sources, in order: a fresh icon match, the position this role's
        toggle matched at *earlier in this session*, a calibrated point, and
        finally an error.

        The remembered position matters because this template is background
        sensitive. It is the white flag on a plain dark HUD, but once mainhost has
        a party the member list draws **green health bars behind it**, and the
        correlation falls from ~0.86 to ~0.50 — its exact threshold — so the match
        flaps frame to frame. That is a live finding: it stopped a run at
        iteration 4 with 'best score 0.50 < threshold 0.50' while iterations 1-3
        had squeaked through. Since the HUD never moves within a session, a
        position that matched once is a far better answer than giving up, and
        several frames are tried first because the score flaps.
        """
        hwnd = self._win(role).hwnd
        have_icon = self.icons.has("team_toggle")
        best: float | None = None
        if have_icon:
            for attempt in range(tries):
                match, score = actions.find_icon_scored(hwnd, self.icons, "team_toggle")
                if score is not None and (best is None or score > best):
                    best = score
                if match is not None:
                    cw, ch = actions.client_size(hwnd)
                    if cw and ch:
                        self._toggle_seen[role] = (match.cx / cw, match.cy / ch)
                    return match.cx, match.cy
                if attempt + 1 < tries:
                    time.sleep(0.4)  # the score flaps; look at another frame
        seen = self._toggle_seen.get(role)
        if seen:
            best_str = f"{best:.2f}" if best is not None else "n/a"
            self.log(f"  team_toggle didn't match this frame (best {best_str}) — "
                     "using the spot it matched at earlier this session")
            return actions.norm_to_client(hwnd, seen[0], seen[1])
        pt = self.points.get("team_toggle")
        if pt:
            return actions.norm_to_client(hwnd, pt[0], pt[1])
        # Distinguish "no template at all" from "template loaded but didn't match
        # this frame" — the latter is a matching/tuning issue, NOT a missing icon,
        # so don't tell the user to recapture something they already have.
        if have_icon:
            thr = self.icons.templates["team_toggle"].threshold
            best_str = f"{best:.2f}" if best is not None else "n/a"
            raise CalibrationNeeded(
                f"team_toggle template is loaded but did not match {role}'s current "
                f"frame in {tries} tries (best score {best_str} < threshold "
                f"{thr:.2f}), and it has not matched yet this session. The party "
                "member list (green health bars) sits behind this icon and drags "
                "the score down — press F8 on that screen to save a variant "
                "(team_toggle#2), lower the threshold in templates/manifest.json, "
                "or calibrate a point."
            )
        raise CalibrationNeeded(
            "team_toggle not set — capture the icon with F8 or calibrate a point."
        )

    def _click_team_toggle(self, role: str) -> None:
        """Click the team toggle **twice** (0.5s apart) at the same spot.

        From quest mode it takes two clicks to reach the team screen (quest→team
        mode→team screen), and clicking twice reliably gets there from either mode,
        so we always do two rather than detect the mode. We locate the toggle once
        and click that spot both times — its icon looks slightly different once
        team mode is active, so re-matching mid-sequence could fail. Alt is held
        because the overworld cursor is camera-locked.
        """
        hwnd = self._win(role).hwnd
        x, y = self._locate_team_toggle(role)
        actions.click_client(hwnd, x, y, hold_alt=True)
        time.sleep(0.5)
        actions.click_client(hwnd, x, y, hold_alt=True)

    def open_team_lobby(
        self, role: str, targets: tuple[str, ...] = ("team_lobby",),
        attempts: int = 3,
    ) -> bool:
        """Double-click the team toggle until one of ``targets`` is detected.

        ``targets`` defaults to ``team_lobby`` (a player *with* a team); a teamless
        player lands on ``my_team_empty`` instead, so callers that may be teamless
        pass the broader set of lobby states they accept.

        Each attempt double-clicks the toggle (see :meth:`_click_team_toggle`) and
        re-checks; if already on a target screen we click nothing. The toggle is an
        overworld HUD element and is **not** clickable inside the CA instance, so if
        the window is still in the instance we wait it out rather than blind-click.
        """
        for _ in range(attempts):
            if self.stop.is_set():
                return False
            state = self.detect(role).state
            if state in targets:
                return True
            if state == "ca_instance":
                self.log(f"  …{role} still in the CA instance; waiting to exit")
                time.sleep(1.5)
                continue
            if state not in TEAM_UI_STATES and state != "overworld":
                # The toggle is an overworld HUD icon, so it is not on screen
                # while a full-screen menu (Arena, Priority, chat) is up. Trying
                # to click it there raises CalibrationNeeded — which stops the
                # whole loop — so back out to the overworld first. This is what
                # killed a live run: step 5 aborted its iteration, leaving
                # mainhost off the overworld, and step 2 of the next iteration
                # died on 'team_toggle ... best score 0.05'.
                self.log(f"  {role} is on '{state}' — backing out to the "
                         "overworld before using the team toggle")
                self._return_to_overworld(role, presses=3, allow_chat=False)
            self._click_team_toggle(role)
            time.sleep(1.5)
        return self.detect(role).state in targets

    def leave_party(self, role: str) -> bool:
        """Open the team screen, click 'Quit Team', and confirm 'Leave the team?'."""
        if not self.open_team_lobby(role):
            return False
        hwnd = self._win(role).hwnd
        if not actions.click_text(hwnd, "Quit Team", threshold=0.6):
            self.log("  ✗ 'Quit Team' not found on team screen")
            return False
        # Confirmation dialog "Leave the team?" with Cancel / OK.
        if not self.wait_for(role, "leave_team_confirm", timeout=6):
            self.log("  ✗ leave-team confirmation did not appear")
            return False
        # OCR often reads the OK button as '0K'; try both.
        return actions.click_text_any(hwnd, ["OK", "0K"], threshold=0.6)

    def _click_challenge_tab(self, role: str) -> bool:
        """Click the 'Challenge' tab in the Priority side nav.

        Prefer the ``priority_challenge_tab`` icon template (F8) over OCR text —
        the nav icon carries a "NEW" badge that OCR reads inconsistently, and
        this button lives in the same icon-dense area as ``priority_entry``, so
        a dedicated template avoids the two being confused.
        """
        return self._click_icon_or_text(role, "priority_challenge_tab", "Challenge",
                                         text_threshold=0.7)

    def _open_priority(self, role: str) -> bool:
        """Click the overworld Priority entry icon (``priority_entry``, F8).

        The Priority button has several variants (red-dot badge, "new" tag, or
        plain); capture each with F8 and the matcher tries them all.
        """
        if not self.icons.has("priority_entry"):
            raise CalibrationNeeded(
                "priority_entry icon not captured — press F8 on the overworld "
                "Priority button to add it."
            )
        hwnd = self._win(role).hwnd
        m, best = actions.find_icon_scored(hwnd, self.icons, "priority_entry")
        if m is None:
            best_str = f"{best:.2f}" if best is not None else "n/a"
            self.log(f"  ✗ priority_entry icon not found (best={best_str}) — capture "
                     "its variants: red-dot / 'new' tag / plain")
            return False
        self.log(f"  priority_entry match score={m.score:.2f} at ({m.cx},{m.cy}); "
                 "clicking")
        actions.click_client(hwnd, m.cx, m.cy, hold_alt=True)
        return True

    # -- steps --------------------------------------------------------------
    def step1_main_leaves(self) -> bool:
        self.log("Step 1: main leaves the party")
        return self.leave_party("main")

    def step2_mainhost_go(self) -> bool:
        """Open mainhost's team lobby and press Go until matchmaking starts.

        A refusal is retried **here**, by pressing Go again on the same lobby —
        not by failing back to ``run_loop`` and rebuilding the party from step 1.
        Members that aren't ready or are offline, and timed blocks, both just need
        another press once the condition clears; :meth:`_handle_refusal` owns the
        waits and the per-reason budgets. Only a closed event (`GoUnavailable`) or
        members who never become ready stop the loop.
        """
        self.log("Step 2: mainhost opens team lobby and presses Go")
        budget = self._new_budget()
        while not self.stop.is_set():
            if self.detect("mainhost").state != "team_lobby":
                self.open_team_lobby("mainhost")
            if not self.wait_for("mainhost", "team_lobby", timeout=15):
                return False
            if not actions.click_text(self._win("mainhost").hwnd, "Go"):
                self.log("  ✗ Go button not found on the lobby")
                return False
            # Verify matchmaking actually started: on success the button's label
            # becomes 'Matching'. If it still reads 'Go' the press did nothing,
            # and only the game says why (toast, else the System log).
            check = self._go_button_stuck("mainhost")
            if not check.stuck:
                self.log("  ✓ matchmaking started (Go button cleared)")
                return True
            if not self._handle_refusal("mainhost", check.toast, budget):
                return False
        return False

    def _handle_refusal(
        self, role: str, toast: tuple[str, int | None], budget: dict[str, int]
    ) -> bool:
        """Diagnose a refused queue press and decide whether to press again.

        Returns True to press the *same button on the same screen* again (after
        whatever wait the reason calls for), False to give up on this step.
        Raises :class:`GoUnavailable` (event closed) or :class:`MembersBlocked`
        (budget exhausted), both of which stop the loop.

        Retrying happens **inside the step** rather than by failing back to
        ``run_loop``: a refusal only means "this press didn't take", so the fix is
        another press of Go / Find matches — restarting the whole iteration would
        tear down a party that is already formed and queued.

        ``budget`` is a per-press counter dict, so each reason has its own
        allowance and the loop always terminates.
        """
        try:
            # Raises on a known verdict (from the toast, else the System log)
            self._diagnose_match_failure(role, toast)
        except MembersBlocked as exc:
            budget["blocked"] += 1
            if budget["blocked"] > self.not_ready_retries:
                self.log(f"  ✗ still blocked after {self.not_ready_retries} "
                         f"retries (~{self.not_ready_delay * self.not_ready_retries / 60:.0f} min)")
                raise
            who = f" [{', '.join(exc.names)}]" if exc.names else ""
            self.log(f"  {exc.count} member(s) still blocking{who} — waiting "
                     f"{self.not_ready_delay:.0f}s and pressing again "
                     f"(try {budget['blocked']}/{self.not_ready_retries})")
            return not self.stop.wait(self.not_ready_delay)
        except MatchingCooldown as exc:
            budget["cooldown"] += 1
            if budget["cooldown"] > self.cooldown_retries:
                self.log(f"  ✗ still on cooldown after {self.cooldown_retries} "
                         "waits; giving up on this run")
                return False
            wait = exc.seconds + 20  # margin: the message is rounded
            mm, ss = divmod(int(wait), 60)
            self.log(f"  ⏳ matchmaking blocked for {exc.seconds}s — waiting "
                     f"{mm:02d}:{ss:02d} then pressing again "
                     f"(try {budget['cooldown']}/{self.cooldown_retries})")
            return not self.stop.wait(wait)
        # Nothing recognised. Most often a press that didn't register, so retry
        # promptly the first time and back off after that.
        budget["unknown"] += 1
        if budget["unknown"] > self.unknown_retries:
            self.log(f"  ✗ {self.unknown_retries} refusals with no reason given; "
                     "giving up on this run")
            return False
        delay = 3.0 if budget["unknown"] == 1 else self.go_retry_delay
        self.log(f"  no reason in the System log — pressing again in "
                 f"{delay:.0f}s (try {budget['unknown']}/{self.unknown_retries})")
        return not self.stop.wait(delay)

    @staticmethod
    def _new_budget() -> dict[str, int]:
        """Fresh per-step retry counters for :meth:`_handle_refusal`."""
        return {"blocked": 0, "cooldown": 0, "unknown": 0}

    # -- matchmaking-failure diagnosis (System chat log) ---------------------
    def _diagnose_match_failure(
        self, role: str, toast: tuple[str, int | None] = ("unknown", None)
    ) -> str:
        """Read ``role``'s System chat log to find out why matchmaking refused.

        Used by **both** queue buttons — mainhost's lobby **Go** (step 2) and
        althost's Arena **Find matches** (step 5) — because the server refuses
        both the same way, and the cooldown is per account: either window can be
        the one that's blocked.

        A stuck button has (at least) two very different causes: the CA event is
        closed, or the server put matchmaking on a short cooldown. Treating both
        as "closed" stopped the loop over what was only a few minutes' wait, so
        the reason is read from the game itself: back out to the overworld
        (matchmaking only happens there anyway), **Enter** to open chat, click the
        **System** channel, and read the newest lines at the bottom.

        ``toast`` is a verdict the caller already read off the failing screen
        (ToF shows *"Matching cooldown ends in 23s."* right there — see
        ``ref-images/go fail.png``). When it says something, that IS the answer:
        act on it immediately and skip the chat trip entirely, which keeps the
        common case fast and leaves the windows where they were.

        Raises :class:`GoUnavailable` (stop the loop) or
        :class:`MatchingCooldown` (wait it out, then retry); returns ``"unknown"``
        when neither source says anything, which the caller treats as transient.
        The chat panel is always closed again before returning or raising, so the
        next attempt starts from a clean overworld.
        """
        if toast[0] != "unknown":
            self.log(f"  {role} was refused on-screen: {toast[0]}")
            return self._raise_verdict(
                role, toast, source="the on-screen message"
            )
        self.log(f"  {role}'s queue didn't take — reading its System log")
        if not self._open_system_chat(role):
            self.log("  ⚠ couldn't open the System log; treating as transient")
            self._close_chat(role)
            return "unknown"
        messages = self._read_system_log(role)
        for text in messages[-detector.SYSTEM_LOG_SCAN:]:
            self.log(f"    log: {text}")
        if not messages:
            self.log("  ⚠ System log read empty; treating as transient")
        verdict = detector.classify_system_message(messages)
        self._close_chat(role)
        return self._raise_verdict(
            role, verdict, source="the System log", messages=messages
        )

    def _raise_verdict(
        self,
        role: str,
        verdict: tuple[str, int | None],
        source: str,
        messages: list[str] | None = None,
    ) -> str:
        """Turn a verdict into the loop's control flow (or ``"unknown"``).

        ``messages`` are the log lines the verdict came from, used to name which
        members are holding up a 'not ready' queue.
        """
        kind, seconds = verdict
        if kind == "member_blocked":
            names = self._blocking_members(messages or [])
            mine = [n for n in names if n.split(" (")[0] in self.windows]
            if mine:
                # One of OUR accounts is the blocker — worth calling out, since
                # that is something we could act on, unlike a random player who
                # simply hasn't clicked Ready or has logged off.
                self.log(f"  ⚠ our own {', '.join(mine)} is blocking the queue")
            self.log(f"  {source}: {seconds} team member(s) not ready or offline"
                     + (f" ({', '.join(names)})" if names else ""))
            raise MembersBlocked(seconds or 0, names)
        if kind == "unavailable":
            raise GoUnavailable(
                f"{source} says the CA event is unavailable for {role} — "
                "matchmaking cannot start."
            )
        if kind == "cooldown":
            self.log(f"  {source}: matching cooldown, {seconds}s left")
            raise MatchingCooldown(seconds or 0)
        return "unknown"

    def _blocking_members(self, messages: list[str]) -> list[str]:
        """Who the tail's member lines name, each tagged with why.

        Returns entries like ``"JinShynestraR (offline)"`` or ``"main (not
        ready)"``. The '[system]' badge glues onto the first word ('SRJinShynestra
        is not ready.'), so the name can't be split off cleanly — instead each
        configured username is fuzzily looked for inside the line, and anything
        else is reported as the line's own leading words.
        """
        names: list[str] = []
        for text in messages[-detector.SYSTEM_LOG_SCAN:]:
            reason = detector.member_block_reason(text)
            if reason is None:
                continue
            for role, win in self.windows.items():
                if win.username and detector._fuzzy_contains(text, win.username):
                    who = role
                    break
            else:
                # Not one of ours: show the line's own words before the phrase.
                head = text.split(" is ")[0]
                who = detector.strip_system_badge(head) or "?"
            entry = f"{who} ({reason})"
            if entry not in names:
                names.append(entry)
        return names

    def _read_system_log(self, role: str) -> list[str]:
        """OCR the chat panel's message area; oldest line first, newest last."""
        region = tuple(self.regions.get("system_log", detector.SYSTEM_LOG_REGION))
        result = actions.read_window_focused(self._win(role).hwnd)
        return detector.read_system_messages(result, region)

    def _open_system_chat(self, role: str, attempts: int = 5) -> bool:
        """Get ``role`` to the chat panel's System channel.

        A small state machine like steps 3/5, because the starting screen varies:
        back out of whatever menu is up, **Enter** opens the chat panel, and the
        **System** nav item switches it to the game log.

        The System click is **mandatory** — Enter lands on the last-used channel,
        which defaults to World, and a World frame can classify as ``chat_system``
        on its own (the read-only footer isn't exclusive to System). So a
        ``chat_system`` reading is only trusted *after* we have clicked System
        ourselves; otherwise we would happily read World chat as the game log.

        The panel is a menu overlay, so its clicks need no Alt — but if a click
        doesn't land (the overworld cursor lock can still be in effect right after
        Esc) the next pass retries it the other way.
        """
        hwnd = self._win(role).hwnd
        chat_open = ("overworld_chat", "chat_system")
        self._return_to_overworld(role)
        # Let the menu-close animation finish. Coming from a full-screen menu
        # (althost's Arena), an Enter sent too soon is swallowed by the closing
        # screen — and because Enter *toggles* the chat panel, blindly pressing
        # again just closes what the previous press opened.
        time.sleep(self.chat_settle)
        clicked = False
        alt = False
        for attempt in range(attempts):
            if self.stop.is_set():
                return False
            state = self.detect(role).state
            if state not in chat_open:
                self.log(f"  opening chat on {role} (Enter, try {attempt + 1}) "
                         f"from '{state}'")
                actions.press_key(hwnd, "enter")
                # Poll for the panel instead of taking one look: it fades in, and
                # a single early read would report 'overworld' and make us press
                # Enter again — which closes it.
                for _ in range(3):
                    time.sleep(0.8)
                    if self.detect(role).state in chat_open:
                        break
                continue
            if clicked and state == "chat_system":
                return True
            self.log(f"  switching to the System channel"
                     f"{' (alt held)' if alt else ''}")
            if not actions.click_text(hwnd, "System", threshold=0.7, hold_alt=alt):
                alt = not alt  # cursor may still be camera-locked; try the other way
            clicked = True
            time.sleep(1.2)
        return clicked and self.detect(role).state == "chat_system"

    def _close_chat(self, role: str) -> None:
        """Dismiss the chat panel (best effort) so the next step starts clean."""
        actions.press_key(self._win(role).hwnd, "esc")
        self._return_to_overworld(role, presses=2)

    def _return_to_overworld(
        self, role: str, presses: int = 5, allow_chat: bool = True
    ) -> bool:
        """Press Esc until ``role`` is back on the overworld (or chat is up).

        Esc is the universal 'back' in ToF and the number of screens to unwind
        varies (lobby → team screen → overworld), so the state is re-checked
        between presses instead of pressing a fixed number of times.

        ``allow_chat`` counts an open chat panel as arrived — right for the log
        diagnosis (chat only opens over the overworld, and another Esc would just
        close it again), wrong when the caller needs to *click* the overworld HUD,
        which the panel covers.
        """
        hwnd = self._win(role).hwnd
        arrived = ("overworld", "overworld_chat", "chat_system") if allow_chat \
            else ("overworld",)
        for _ in range(presses):
            if self.stop.is_set():
                return False
            if self.detect(role).state in arrived:
                return True
            actions.press_key(hwnd, "esc")
            time.sleep(0.8)
        return self.detect(role).state in arrived

    def _button_stuck(
        self,
        role: str,
        region_key: str,
        default_region: tuple[float, float, float, float],
        label: str,
        threshold: float = 0.8,
        frames: int = 9,
        interval: float = 0.45,
    ) -> QueueCheck:
        """True if a queue button is still showing after we pressed it.

        Reads a tight, upscaled crop of the button's region (resolution-
        independent) so a short, fuzzy-prone token isn't picked up elsewhere on a
        busy screen. **Every** read must still show the label before declaring
        failure, so a single OCR miss can't strand the loop — the first read that
        finds the button gone is taken as success.

        Shared by mainhost's lobby **Go** and althost's Arena **Find matches**:
        both mean "matchmaking was asked for", and both can be refused the same
        way (a matching cooldown), so both are verified and diagnosed alike. What
        *success* looks like differs per button (Go turns into 'Matching', Find
        matches disappears and a 'Matching 00:02' widget takes its place), but
        "the label is gone from its own region" covers both.

        The refusal toast (*"Matching cooldown ends in 23s."*) is on screen for
        barely two seconds, and an OCR-per-read loop samples far too slowly to
        catch it — which is why the first live run learned nothing. So the frames
        are **burst-captured first** (~30ms each) and OCR'd afterwards:

        1. every frame's button crop is read — the first one missing the label
           means the queue started, and we stop there;
        2. only if the label survived *every* frame do we OCR the full frames
           looking for the toast, newest work last so the earliest (most likely)
           frame is examined first.

        Catching the toast is still a bonus: when it's missed the caller falls
        back to the System chat log.
        """
        region = tuple(self.regions.get(region_key, default_region))
        hwnd = self._win(role).hwnd
        if self.stop.is_set():
            return QueueCheck(stuck=False)
        caps = actions.burst_capture(hwnd, frames, interval)
        for cap in caps:
            crop = ocr.recognize_bgra_region(
                cap.image_bgra, cap.width, cap.height, region, self.timer_upscale
            )
            if textmatch.find_phrase(crop, label, threshold) is None:
                return QueueCheck(stuck=False)  # button gone/changed → queued
        # The label survived every frame → the press did nothing. Now spend the
        # OCR budget looking for the reason in the frames we already hold.
        toast: tuple[str, int | None] = ("unknown", None)
        for cap in caps:
            if self.stop.is_set():
                break
            full = ocr.recognize_bgra(cap.image_bgra, cap.width, cap.height)
            toast = detector.read_toast_verdict(full)
            if toast[0] != "unknown":
                self.log(f"  caught the on-screen message: {toast[0]}")
                break
        return QueueCheck(stuck=True, toast=toast)

    def _go_button_stuck(self, role: str, **kw) -> QueueCheck:
        """Re-read mainhost's lobby **Go** button after pressing it.

        On success the button's own label becomes 'Matching' (confirmed with the
        user), so 'Go' disappearing from the crop is the signal.
        """
        return self._button_stuck(
            role, "go_button", detector.GO_BUTTON_REGION, "Go", **kw
        )

    def _find_match_stuck(self, role: str, **kw) -> QueueCheck:
        """Re-read althost's Arena **Find matches** button after pressing it.

        On success the button vanishes outright and a 'Matching 00:02' widget
        appears lower on the screen (ref-images/find match succeed.png), so its
        region simply goes empty.
        """
        return self._button_stuck(
            role, "find_match", detector.FIND_MATCH_REGION, "Find matches",
            threshold=0.7, **kw
        )

    def step3_main_requests(self, max_steps: int = 12) -> bool:
        """Navigate main from wherever it is to the CA party list, then Request.

        After Step 2 took foreground for mainhost, main's Lobby closes and main is
        usually back in overworld (or showing the Create/Find-Team popup). Rather
        than assume a fixed start, this drives a small state machine: each pass
        detects main's state and takes the *one* action appropriate to it, so we
        never blind-click (e.g. hitting the team toggle while a popup is up, which
        just dismisses it). main is teamless here, so the path is
        overworld → team_popup → find_target_list → find_party_list.

        The target list (mixed parties) and the CA-filtered party list look nearly
        identical in text — both keep the left category nav and both have Request
        rows — so they can't be told apart reliably under live OCR (find_party_list
        only reads distinctly once a request is pending). We therefore don't depend
        on that distinction: on *either* list we try to Request the host's row
        first, and only click 'The Critical Abyss' to filter if the host isn't
        visible yet. This self-corrects whichever list we're actually on.
        """
        self.log("Step 3: main finds and requests mainhost's party")
        hwnd = self._win("main").hwnd
        host_name = self._win("mainhost").username

        for _ in range(max_steps):
            if self.stop.is_set():
                return False
            state = self.detect("main").state
            if state in ("find_party_list", "find_target_list"):
                # On either list: Request mainhost's row if it's visible. If not
                # (an unfiltered/mixed list), filter by clicking The Critical Abyss
                # and retry next pass — avoids re-clicking the tab forever when a
                # filtered CA list is misread as the target list.
                if actions.click_row_button(hwnd, host_name, "Request"):
                    return True
                # Host row not found. Log the names sitting next to Request
                # buttons so a username mismatch is obvious (the configured host
                # name must match the on-screen party name).
                self._log_request_rows("main", host_name)
                actions.click_text(hwnd, "The Critical Abyss", threshold=0.7)
            elif state == "team_popup":
                # Overworld Create/Find-Team overlay: take Find Team.
                actions.click_text(hwnd, "Find Team", threshold=0.7)
            elif state == "my_team_empty":
                # Full Lobby on My Team tab: switch to the Team Lobby tab. OCR
                # reads the tab as just 'Lobby' (its 'Team' is an icon) and the
                # screen title is *also* 'Lobby', so a plain text click would hit
                # the title; _click_team_lobby_tab disambiguates by the tab row.
                self._click_team_lobby_tab("main")
            elif state == "ca_instance":
                # Still in the CA run — the team toggle isn't clickable here, so
                # wait for main to exit instead of blind-clicking the HUD.
                self.log("  …main still in the CA instance; waiting to exit")
            elif not self.icons.has("overworld_marker") or self._in_overworld("main"):
                # Confirmed overworld (scanner icon present, or no icon to check
                # with) → open the team panel by double-clicking the toggle.
                # Teamless main lands on team_popup (or my_team_empty), handled on
                # the next pass.
                self._click_team_toggle("main")
            else:
                # Unrecognized frame that is NOT the overworld — almost always a
                # menu screen (e.g. my_team_empty) that OCR misread for one frame.
                # The overworld toggle isn't on screen here, so clicking it is
                # wrong; wait and re-detect instead of blind-clicking the HUD.
                self.log(f"  …main detected as '{state}' (not a step-3 state, not "
                         "overworld); re-checking, not clicking the team toggle")
            time.sleep(1.5)

        self.log("  ✗ main could not reach the CA party list")
        return False

    def step4_mainhost_approves(self, timeout: float = 30) -> bool:
        """Open mainhost's Request List and approve main's join request.

        A state machine like Steps 3/5: each pass detects mainhost's state and acts,
        so it clicks **Request List immediately** (no pre-wait) and retries. The
        popup does not open itself — mainhost sits on its team screen (`team_lobby`)
        with a 'Request List' button (bottom bar, next to Recruit/Go).
        """
        self.log("Step 4: mainhost approves main's request")
        host = self._win("mainhost")
        main_name = self._win("main").username

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.stop.is_set():
                return False
            state = self.detect("mainhost").state
            if state == "request_list_popup":
                # Approve main's row. May fail if the request hasn't propagated to
                # the host yet — keep the popup open and retry on the next pass.
                if actions.click_row_button(host.hwnd, main_name, "Approve"):
                    return True
            elif state == "team_lobby":
                # Open the request list (bottom-bar button).
                actions.click_text(host.hwnd, "Request List", threshold=0.7)
            time.sleep(1.5)

        self.log("  ✗ request list never opened / main's request not approved")
        return False

    def _ensure_ca_selected(self, hwnd: int, attempts: int = 4) -> bool:
        """Make sure the Arena 'The Critical Abyss' tab is selected.

        Apex League and Critical Abyss read identically under OCR (only the tab
        highlight differs), so selection is verified with the ``arena_ca_selected``
        icon template — the highlighted CA tab. We click the tab and re-check until
        the template matches. If the template isn't captured we can't verify, so we
        click once best-effort and warn (capture it with F8 for reliability).
        """
        if not self.icons.has("arena_ca_selected"):
            self.log("  ⚠ no 'arena_ca_selected' template — can't confirm CA is "
                     "selected (capture it with F8 on the highlighted CA tab)")
            actions.click_text(hwnd, "The Critical Abyss", threshold=0.6)
            time.sleep(0.6)
            return True
        for _ in range(attempts):
            if self.stop.is_set():
                return False
            if actions.find_icon(hwnd, self.icons, "arena_ca_selected") is not None:
                return True
            actions.click_text(hwnd, "The Critical Abyss", threshold=0.6)
            time.sleep(0.8)
        return actions.find_icon(hwnd, self.icons, "arena_ca_selected") is not None

    def step5_althost_ca(self, max_steps: int = 14) -> bool:
        """Drive althost overworld → Priority → Challenge → Arena → Find match.

        A state machine like Step 3: each pass detects althost's state and takes
        the one action for it, so it retries until it lands the CA queue and is
        robust to whatever screen althost starts on.

        The **Arena** tile on the Challenge tab is artwork, not OCR-readable text
        (confirmed: OCR finds no "Arena" there), so it is clicked via an icon
        template ``arena_tile`` (capture with F8). The rest are OCR text.
        """
        self.log("Step 5: althost finds CA match in Arena")
        hwnd = self._win("althost").hwnd
        budget = self._new_budget()  # per-reason refusal allowances
        # ``steps`` bounds the *navigation*; a refusal retry gives its step back
        # (see below) so waiting out a cooldown can't exhaust the budget for
        # getting to the Arena screen.
        steps = 0
        while steps < max_steps:
            steps += 1
            if self.stop.is_set():
                return False
            state = self.detect("althost").state
            self.log(f"  althost: {state}")
            if state == "arena_screen":
                # Select The Critical Abyss (left tab), *verify* it took (the click
                # sometimes doesn't register and Apex League stays selected), then
                # queue. Only Find matches once CA is confirmed selected.
                if not self._ensure_ca_selected(hwnd):
                    self.log("  …CA tab not confirmed selected yet; retrying")
                    continue
                # Click via the region crop: full-frame OCR of the Arena screen
                # finds this label only occasionally (3/14 frames in the failed
                # run's replay) and can match a fragment, which clicks the wrong
                # spot. The crop reads it 14/14 at score 1.00.
                region = tuple(self.regions.get(
                    "find_match", detector.FIND_MATCH_REGION))
                clicked = actions.click_text_in_region(
                    hwnd, "Find matches", region, self.timer_upscale, 0.7
                ) or actions.click_text(hwnd, "Find matches", threshold=0.7)
                if clicked:
                    # Verify the queue actually started — the same check mainhost
                    # does after Go. althost hits the matching cooldown too (it is
                    # per account), and a silently-refused Find matches used to
                    # look like success, leaving step 6 to wait out a run that was
                    # never queued.
                    check = self._find_match_stuck("althost")
                    if not check.stuck:
                        self.log("  ✓ althost queued (Find matches cleared)")
                        return True
                    # Refused: wait out whatever the reason calls for and press
                    # Find matches again, right here. Failing the iteration would
                    # restart at step 1 and tear down mainhost's and main's queue,
                    # which is already formed by this point.
                    if not self._handle_refusal("althost", check.toast, budget):
                        return False
                    steps -= 1  # a refusal retry isn't a navigation step
                    continue
            elif state == "priority_challenge":
                # The Arena tile is artwork; OCR can't read it — use the template.
                # Log the match location/score so a weak, mislocated match (which
                # would click the center of the screen instead of the tile) is
                # visible rather than silently retried.
                if self.icons.has("arena_tile"):
                    m, best = actions.find_icon_scored(hwnd, self.icons, "arena_tile")
                    if m is not None:
                        self.log(f"  arena_tile match score={m.score:.2f} at "
                                 f"({m.cx},{m.cy}); clicking")
                        actions.click_client(hwnd, m.cx, m.cy)
                    else:
                        best_str = f"{best:.2f}" if best is not None else "n/a"
                        self.log(f"  ✗ arena_tile no match (best={best_str}); "
                                 "not clicking")
                elif not actions.click_text(hwnd, "Arena", threshold=0.6):
                    self.log("  ✗ Arena tile not found — capture 'arena_tile' "
                             "with F8 on the Priority → Challenge screen")
            elif state == "priority_menu":
                if not self._click_challenge_tab("althost"):
                    self.log("  ✗ Challenge tab not found — capture "
                             "'priority_challenge_tab' with F8 on the Priority "
                             "menu's Challenge nav icon")
            elif state == "ca_instance":
                # Still in the CA run — the Priority entry isn't clickable here, so
                # wait for althost to exit instead of blind-clicking the HUD.
                self.log("  …althost still in the CA instance; waiting to exit")
            elif (
                self.icons.has("priority_challenge_tab")
                and actions.find_icon(hwnd, self.icons, "priority_challenge_tab") is not None
            ):
                # OCR classified this frame as something else (commonly a stale
                # 'overworld' misread of the Priority menu — its signature phrases
                # didn't fuzzy-match this pass), but the Challenge tab icon is
                # visibly on screen. Trust the icon over OCR here rather than
                # retrying priority_entry, which isn't on screen anymore and can
                # never match once we've left the overworld.
                self._click_challenge_tab("althost")
            else:
                # overworld / other: open the Priority panel via its icon.
                self._open_priority("althost")
            time.sleep(1.5)

        self.log("  ✗ althost could not queue the CA match")
        return False

    def _read_ca_status(self, role: str) -> tuple[int | None, str]:
        """Focus ``role``, capture once, return (countdown seconds, state).

        The countdown is read from a tight, upscaled crop of the timer region
        (normalized fractions, resolution-independent) because the full frame
        loses the leading minutes digit (26:06 → 06:06); the same capture's full
        frame is reused for state classification.
        """
        region = tuple(self.regions.get("ca_timer", detector.CA_TIMER_REGION))
        full, crop = actions.read_window_and_region(
            self._win(role).hwnd, region, self.timer_upscale
        )
        remaining = detector.parse_timer(crop)
        if remaining is None:
            remaining = detector.read_ca_timer(full)  # fallback: full-frame scan
        return remaining, detector.classify(full).state

    def _leave_ca_instance(
        self, role: str, attempts: int = 8, known_inside: bool = False
    ) -> bool:
        """Leave the CA instance: click the leave button, confirm the dialog.

        A retry state machine (like steps 3/5): each pass detects the state and
        takes the one action for it, so a click that doesn't register is simply
        retried instead of aborting. The leave button is the textless top-left HUD
        icon (``ca_leave``, F8). The in-instance cursor is camera-locked, so the
        click **holds Alt** (confirmed with user). Success = the dialog was
        dismissed, or we're sustainably out of the instance (also covers the run
        ending on its own).

        ``known_inside`` means the caller has *just* read the countdown and knows
        we're in the instance (step 6, having seen the timer below 27:00). The
        first pass then clicks the leave button straight away instead of
        re-reading the timer — the timer is checked **once**, by the caller.
        """
        hwnd = self._win(role).hwnd
        if not self.icons.has("ca_leave"):
            raise CalibrationNeeded(
                "ca_leave icon not captured — press F8 on the CA-instance leave "
                "button (top-left HUD) to add it."
            )
        out = 0  # consecutive reads showing we're no longer in the instance/dialog
        skip_read = known_inside  # caller already read the timer this instant
        for _ in range(attempts):
            if self.stop.is_set():
                return False
            if skip_read:
                # Trust the caller's fresh read; act on it without re-reading.
                skip_read = False
                remaining, state = 0, "ca_instance"
            else:
                # Use the region-crop countdown as the reliable in-instance signal
                # — the full-frame classifier can mislabel the busy instance HUD
                # as a base screen (e.g. team_lobby), skipping the leave click.
                remaining, state = self._read_ca_status(role)
            if state == "leave_scene_confirm":
                out = 0
                # OCR often reads the OK button as '0K'; try both.
                actions.click_text_any(hwnd, ["OK", "0K"], threshold=0.6)
                time.sleep(1.5)
                r2, s2 = self._read_ca_status(role)
                if s2 != "leave_scene_confirm" and r2 is None:
                    self.log("  ✓ left the instance (confirmed 'Leave the scene?')")
                    return True
            elif remaining is not None or state == "ca_instance":
                out = 0
                # Cursor is camera-locked in the instance → hold Alt to click HUD.
                if actions.click_icon(hwnd, self.icons, "ca_leave", hold_alt=True):
                    self.log("  clicked leave button, waiting for confirmation")
                else:
                    self.log("  ✗ ca_leave button not found on screen")
            else:
                # No countdown and no dialog: we're out. Require two consecutive
                # such reads so a single misread doesn't fake a successful leave.
                out += 1
                if out >= 2:
                    self.log(f"  ✓ out of the instance (state '{state}')")
                    return True
            time.sleep(1.5)
        self.log("  ✗ could not confirm leaving the instance")
        return False

    @staticmethod
    def _timer_below_27(remaining: int) -> bool:
        """Safely test 'countdown < 27:00' against OCR's dropped-leading-digit bug.

        OCR frequently drops the leading (tens) minutes digit — 26:06 reads as
        06:06 — so the absolute value can't be trusted, and trusting it risks
        leaving while the real timer is still ABOVE 27:00 (which strands mainhost).
        The *units* digit, however, reads reliably, and for a countdown
        'remaining < 27:00' ⟺ 'remaining minutes ≤ 26'. A units digit in 1..6
        means the true minutes is 6/16/26 (or 5/15/25 …) — all < 27:00 regardless
        of a dropped tens digit. Units 7/8/9/0 are ambiguous (could be 27/28/29/30,
        i.e. still ≥ 27:00), so we treat those as 'not yet' and keep waiting.
        """
        return 1 <= (remaining // 60) % 10 <= 6

    def _in_overworld(self, role: str) -> bool:
        """True if ``role`` is out of the CA instance and back in the overworld.

        Detected by the overworld-only top-right HUD scanner icon
        (``overworld_marker``): it is present in the overworld and absent in the
        CA combat HUD. This is far more reliable than the OCR timer/classifier —
        the instance HUD and the overworld are both text-sparse 3D views, so the
        OCR signal flapped (a window that had returned kept reading "still in the
        run"). Falls back to the OCR timer+state only if the icon isn't captured.
        """
        hwnd = self._win(role).hwnd
        if self.icons.has("overworld_marker"):
            match, _ = actions.find_icon_scored(hwnd, self.icons, "overworld_marker")
            return match is not None
        remaining, state = self._read_ca_status(role)
        return remaining is None and state == "overworld"

    def _wait_all_overworld(
        self, deadline: float, poll: float = 30, confirm_reads: int = 1
    ) -> bool:
        """Wait until every window is back in the overworld (run finished).

        mainhost leaves the instance early, but main and althost stay until the run
        ends on its own and then auto-return. The loop must not restart until all
        three are back, or step 1 (main leaves party) acts on a window still in the
        instance.

        Every role is re-checked **every** poll — we never permanently mark a role
        "done", because a transient overworld read (e.g. a loading frame) would
        otherwise stick and let us finish while that window is still inside. We
        complete as soon as *all* roles read overworld together (``confirm_reads``
        polls, 1 by default); a role that's really still inside reads
        ``ca_instance`` and resets the streak.
        """
        self.log("  waiting for all instances to return to overworld")
        all_streak = 0
        while time.time() < deadline:
            if self.stop.is_set():
                return False
            still = sorted(r for r in self.windows if not self._in_overworld(r))
            if still:
                all_streak = 0
                self.log(f"  still in the run: {still}")
            else:
                all_streak += 1
                self.log(f"  all read overworld ({all_streak}/{confirm_reads})")
                if all_streak >= confirm_reads:
                    self.log("  all instances back in overworld")
                    return True
            if self.stop.wait(poll):
                return False
        self.log("  ✗ not all instances returned to overworld in time")
        return False

    def step6_wait_overworld(
        self,
        timeout: float = 2400,
        grace: float = 240,
        poll: float = 20,
        confirm_reads: int = 1,
    ) -> bool:
        """Sit through the CA run, make mainhost leave, then wait for all to return.

        For the first ``grace`` seconds (default 4 min) we touch nothing: detection
        focuses a window, which yanks control from players actively fighting inside
        the Critical Abyss, and nothing actionable can happen that early.

        After the grace we poll **mainhost only** (one window, minimal disturbance).
        Matchmaking/loading can be slow, so mainhost may still be in the overworld
        when polling starts — we must wait for it to *enter* the instance (timer
        appears) rather than mistake "not entered yet" for "run finished" (tracked
        by ``entered``). Once inside, its HUD shows a countdown from 30:00; when it
        is safely below 27:00 (see :meth:`_timer_below_27`) — a single read is enough
        (``confirm_reads``, 1 by default) — we click the leave button and confirm
        'Leave the current scene?'. Only mainhost leaves here; main and althost stay until the run
        finishes and auto-return, so we then wait for **all three** to be back in
        the overworld before completing — otherwise the next iteration's step 1
        would act on instances still in the run.
        """
        self.log("Step 6: wait out the CA run, then mainhost leaves near the end")
        deadline = time.time() + timeout
        if grace > 0:
            self.log(f"  …leaving instances alone for {grace:.0f}s "
                     "(players are in the run)")
            if self.stop.wait(min(grace, max(0.0, deadline - time.time()))):
                return False
        pending = 0   # consecutive reads safely below 27:00
        entered = False  # have we seen mainhost inside the instance yet?
        out_streak = 0   # consecutive overworld reads *after* entering
        while time.time() < deadline:
            if self.stop.is_set():
                return False
            remaining, state = self._read_ca_status("mainhost")
            if remaining is not None:
                entered = True
                out_streak = 0
                mm, ss = divmod(remaining, 60)
                if self._timer_below_27(remaining):
                    pending += 1
                    self.log(f"  mainhost timer {mm:02d}:{ss:02d} "
                             f"(below 27:00, {pending}/{confirm_reads})")
                    if pending >= confirm_reads:
                        self.log("  below 27:00 — mainhost leaving")
                        if not self._leave_ca_instance(
                            "mainhost", known_inside=True
                        ):
                            return False
                        # mainhost is out; wait for main/althost to finish and return.
                        return self._wait_all_overworld(deadline)
                else:
                    pending = 0
                    self.log(f"  mainhost timer {mm:02d}:{ss:02d} (still ≥ 27:00)")
            elif state == "overworld":
                pending = 0
                if not entered:
                    # Not in the instance yet — matchmaking/loading still running.
                    self.log("  …waiting for mainhost to enter the CA instance")
                else:
                    # Was inside, now overworld → run ended before we caught 27:00.
                    # Debounced so a transient loading frame mid-run doesn't trip it.
                    out_streak += 1
                    if out_streak >= confirm_reads:
                        self.log("  mainhost back in overworld (run ended early)")
                        return self._wait_all_overworld(deadline)
            else:
                pending = 0
                out_streak = 0
                self.log(f"  …mainhost timer not readable (state '{state}')")
            if self.stop.wait(poll):
                return False
        return False

    # -- loop ---------------------------------------------------------------
    def _dump_replay(self, label: str) -> None:
        """Dump the rolling replay buffer to disk, tagged with ``label``.

        Best-effort: a dump failure (disk full, codec issue) is logged but never
        allowed to crash the loop — the replay is a diagnostic aid, not a step.
        """
        ts = time.strftime("%Y%m%d_%H%M%S")
        try:
            paths = self.replay.dump(f"{ts}_{label}")
        except Exception as exc:
            self.log(f"  ⚠ replay dump failed: {exc!r}")
            return
        if not paths:
            self.log("  (no replay footage buffered yet)")
        for p in paths:
            self.log(f"  replay saved: {p}")

    def run_loop(self, max_iterations: int | None = None) -> None:
        """Run the farm loop until stopped (or ``max_iterations`` reached)."""
        steps = (
            self.step1_main_leaves,
            self.step2_mainhost_go,
            self.step3_main_requests,
            self.step4_mainhost_approves,
            self.step5_althost_ca,
            self.step6_wait_overworld,
        )
        self.replay.start()
        try:
            i = 0
            while not self.stop.is_set():
                i += 1
                self.log(f"=== Loop iteration {i} ===")
                try:
                    for step in steps:
                        if self.stop.is_set():
                            return
                        if not step():
                            self.log(f"  ✗ {step.__name__} did not complete; "
                                     "aborting iteration")
                            self._dump_replay(f"iter{i}_{step.__name__}_fail")
                            # A step-6 failure means mainhost likely never left the
                            # CA instance — the windows are still mid-run, so
                            # restarting at step 1 would act on instances that
                            # aren't in the team screens. Stop instead of looping
                            # into a broken iteration.
                            if step is self.step6_wait_overworld:
                                self.log("  Stopping: instances may still be in "
                                         "the CA run. Fix the leave step, then "
                                         "restart.")
                                return
                            break
                    else:
                        self.log(f"=== Iteration {i} complete ===")
                except GoUnavailable as exc:
                    self.log(f"  ✗ {exc}")
                    self.log("  Stopping: CA is unavailable, so retrying won't help.")
                    self._dump_replay(f"iter{i}_go_unavailable")
                    return
                except MembersBlocked as exc:
                    # Step 2 already retried this every not_ready_delay for
                    # not_ready_retries tries, re-reading the log each time in
                    # case the reason changed. Reaching here means the members
                    # never readied up, so the party is not going to start.
                    who = f" [{', '.join(exc.names)}]" if exc.names else ""
                    self.log(f"  ✗ {exc.count} team member(s) never became "
                             f"ready{who}")
                    self.log("  Stopping: matchmaking cannot start while a team "
                             "member is not ready or offline.")
                    self._dump_replay(f"iter{i}_members_blocked")
                    return
                except MatchingCooldown as exc:
                    # Transient server-side block, not a closed event: wait it out
                    # (plus a margin — the message is rounded to whole seconds)
                    # and let the next iteration retry from step 1.
                    wait = exc.seconds + 20
                    mm, ss = divmod(int(wait), 60)
                    self.log("  ⏳ matchmaking is on cooldown — waiting "
                             f"{mm:02d}:{ss:02d}, then retrying")
                    if self.stop.wait(wait):
                        return
                except CaptureError as exc:  # window vanished / zero-size
                    self.log(f"  ✗ capture error: {exc}")
                    self._dump_replay(f"iter{i}_capture_error")
                except CalibrationNeeded as exc:
                    self.log(f"  ⚠ needs calibration: {exc}")
                    self.log("  Pausing loop — wire the entry-point hook and restart.")
                    self._dump_replay(f"iter{i}_calibration_needed")
                    return
                if max_iterations and i >= max_iterations:
                    return
        finally:
            self.replay.stop()
