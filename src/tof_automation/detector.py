"""Game-state classification from OCR text.

Each state is defined by signature phrases that appear on that screen. A frame is
classified by counting how many of a state's signatures fuzzily appear (via
:func:`textmatch.find_phrase`); the state with the most hits wins, ties broken by
summed match score. If nothing matches, the frame is treated as ``overworld``
(the text-sparse default).

Signatures are a starting point authored from ``ref-images/`` and are expected to
be tuned against live captures — keep them distinctive per state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .ocr import OcrResult
from .textmatch import find_phrase, normalize

OVERWORLD = "overworld"
CA_INSTANCE = "ca_instance"


@dataclass(frozen=True)
class State:
    name: str
    signatures: tuple[str, ...]
    min_hits: int = 1
    threshold: float = 0.7
    # Overlays/toasts/drill-ins that sit ON TOP of another recognizable screen
    # get a higher priority so they win over the base screen they cover (e.g.
    # the "Team joined" toast over the lobby, the drilled-in party list over the
    # persistent target-category nav).
    priority: int = 0


# Distinctive tokens are preferred so scoring separates overlapping menus (the
# two Priority tabs, the three Lobby screens). Tokens chosen from live OCR of
# ref-images/ (mode-name tiles OCR poorly, so key off surrounding text instead).
STATES: tuple[State, ...] = (
    State("overworld_menu",
          ("Smart Servant", "Commissary", "Suppressors", "Matrices", "Terminal"),
          min_hits=2),
    State("overworld_chat",
          ("Send", "Whisper", "Recruit", "Message reminder", "Show barrage"),
          min_hits=2),
    # The chat panel's **System** channel (the game log). It is read-only, so it
    # swaps the Send box for 'You cannot post in the channel' — the one phrase
    # that separates it from the other channels, which share the same nav rail
    # ('System' and 'Whisper' are visible on every tab, so they can't be used).
    # Priority 2 to beat overworld_chat, which its nav rail also matches.
    State("chat_system",
          ("You cannot post in the channel",),
          min_hits=1, priority=2),
    State("team_popup",
          ("Create Team", "Find Team"),
          min_hits=2, priority=1),
    State("my_team_empty",
          ("Create Team", "Lobby", "Free Target", "My Team"),
          min_hits=2),
    State("request_list_popup",
          ("Deny All", "Approve", "Ignore"),
          min_hits=2, priority=1),
    State("team_joined",
          ("Team joined",),
          min_hits=1, priority=1),
    State("leave_team_confirm",
          ("Leave the team", "Cancel"),
          min_hits=2, priority=1),
    State("leave_scene_confirm",
          ("Leave the current scene", "Cancel"),
          min_hits=2, priority=2),
    # Inside the CA instance. 'Opponent' (the score header) is unique to it — not
    # in the overworld or lobby. 'Your Team' is deliberately NOT used: it fuzzily
    # collides with the lobby's 'My Team' tab. Priority 1 so it beats a base screen
    # (e.g. team_lobby) that the busy instance HUD can spuriously match; the leave
    # dialog (priority 2) still wins over it.
    State("ca_instance",
          ("Opponent",),
          min_hits=1, priority=1),
    State("team_lobby",
          ("Quit Team", "Go", "My Team", "Lobby"),
          min_hits=2),
    State("find_target_list",
          ("Free Target", "World Exploration", "Roaming Boss", "Joint Operation"),
          min_hits=2),
    State("find_party_list",
          ("Match", "Waiting to join"),
          min_hits=2, priority=1),
    State("arena_screen",
          ("Arena", "Apex League", "Find matches", "Critical Abyss"),
          min_hits=2),
    State("priority_challenge",
          ("Team PVE", "Solo PVE", "No attempts", "Unlimited"),
          min_hits=2),
    State("priority_menu",
          ("Weekly Activity", "Bounty Missions", "Vitality", "Overworld enemies"),
          min_hits=2),
)


@dataclass
class Classification:
    state: str
    confidence: float                      # summed match score of winning state
    hits: dict[str, int] = field(default_factory=dict)   # per-state signature hits


# The CA instance shows a MM:SS countdown in the top-center HUD (counts down from
# 30:00). It's the most reliable signal that we're *inside* the instance rather
# than the overworld (both are text-sparse 3D views). The colon is sometimes lost
# by OCR, so a bare 4-digit MMSS token in the same spot is accepted too.
_TIMER_RE = re.compile(r"(\d{1,2})\s*[:.;]\s*(\d{2})")

# Normalized client region (x0, y0, x1, y1 in 0..1) of the top-center countdown,
# plus how much to upscale that crop before OCR. OCR'ing this tight, enlarged crop
# reads the digits FAR more reliably than the full frame — the full frame loses
# the leading minutes digit (26:06 → 06:06), which the crop recovers. Tuned from
# ref-images and overridable via config.json ("regions": {"ca_timer": [...]}).
CA_TIMER_REGION: tuple[float, float, float, float] = (0.44, 0.0, 0.56, 0.07)
CA_TIMER_UPSCALE = 3

# Normalized client region of the team-lobby **Go** (find-match) button, bottom
# right of the lobby bar. After pressing Go we re-OCR just this crop: on success
# the button changes/disappears, on failure it stays 'Go' (e.g. the CA event is
# unavailable). Cropping tight keeps the short, fuzzy-prone 'Go' token from being
# matched elsewhere on the busy lobby. Overridable via "regions": {"go_button"}.
GO_BUTTON_REGION: tuple[float, float, float, float] = (0.78, 0.85, 0.99, 1.0)

# Same idea for althost's Arena screen: the **Find matches** button (right-hand
# panel, under the mode's PvP tag). Re-OCR'd after clicking to tell "queued" from
# "the server refused" — the refusal that matters is a matching cooldown, which
# althost hits just like mainhost. Overridable via "regions": {"find_match"}.
FIND_MATCH_REGION: tuple[float, float, float, float] = (0.74, 0.58, 1.0, 0.72)

# Normalized client region of the chat panel's message area, used to read the
# System channel (the game log) after a failed Go. Bounded on the left to skip
# the channel nav rail ('World/Crew/Team/…') and at the bottom to skip the
# 'You cannot post in the channel' footer, so only real log lines are returned.
# Overridable via config.json ("regions": {"system_log": [...]}).
SYSTEM_LOG_REGION: tuple[float, float, float, float] = (0.08, 0.03, 0.56, 0.93)

# How many trailing log lines count as "the latest message". More than one
# because a long message wraps onto a second line ('…has occupied the Energy' /
# 'Station.'), and because a refusal prints one line per member and world chatter
# lands in between: in a live log the '<player> is offline.' line was already the
# oldest of four, so a narrower window would have missed the reason entirely.
# Staleness is bounded anyway — the tail is scanned newest-first, so any fresher
# reason still wins.
SYSTEM_LOG_SCAN = 6

# System-log wordings that mean the CA event itself is closed — the only reason
# to stop the loop. Several spellings because the exact string varies by patch
# and OCR mangles short words. Hand-editable.
EVENT_UNAVAILABLE_PHRASES: tuple[str, ...] = (
    "Event unavailable",
    "Event not available",
    "The event is not available",
    "Event has ended",
)

# Per-member conditions that block matchmaking: the queue will not start while a
# member hasn't readied up *or* has gone offline. Both are other players, so the
# loop waits and retries rather than stopping. Matched on the tail only, since
# the lines are per-member and the log keeps every past occurrence.
NOT_READY_PHRASE = "is not ready"
OFFLINE_PHRASE = "is offline"

# Timed server-side blocks — *transient*, so the loop waits them out instead of
# stopping. Two wordings are known: 'Matching cooldown ends in 2 min 25 sec.' and
# 'Matching banned for 5 minutes.' Either one must be present before a duration
# is trusted, so a stray number elsewhere can never be read as a wait.
MATCH_BLOCK_PHRASES: tuple[str, ...] = (
    "Matching cooldown",
    "Matching banned",
    "Matchmaking banned",
)
# Digits+unit are matched separately (either part may be absent: '45 sec',
# '2 min', '5 minutes'), with OCR look-alikes allowed for the units (m1n/mln,
# 5ec). Both spellings of seconds occur: the chat log writes '25 sec', the
# on-screen toast writes '23s.', so a bare 's' counts too.
_COOLDOWN_MIN_RE = re.compile(r"(\d+)\s*(?:m[il1]n(?:ute)?s?\b|m\b)", re.I)
_COOLDOWN_SEC_RE = re.compile(r"(\d+)\s*(?:sec(?:ond)?s?\b|5ec\b|s\b)", re.I)


def _fuzzy_contains(text: str, phrase: str, threshold: float = 0.78) -> bool:
    """True if ``phrase`` appears in ``text`` allowing for OCR noise.

    Log lines can't use :func:`textmatch.find_phrase` (that works on boxed OCR
    words); this is the string-level equivalent — normalize both sides, then
    slide a phrase-sized window over the text and take the best ratio.
    """
    nt, np_ = normalize(text), normalize(phrase)
    if not nt or not np_:
        return False
    if np_ in nt:
        return True
    n = len(np_)
    for size in (n, n + 2):
        for i in range(0, max(1, len(nt) - size + 1)):
            if SequenceMatcher(None, np_, nt[i:i + size]).ratio() >= threshold:
                return True
    return False


def parse_matching_cooldown(text: str) -> int | None:
    """Seconds left on a timed matchmaking block, else ``None``.

    Covers both wordings in :data:`MATCH_BLOCK_PHRASES` — *"Matching cooldown
    ends in 2 min 25 sec."* and *"Matching banned for 5 minutes."* — and requires
    one of them *plus* at least one duration part, so an unrelated line that
    happens to contain a number can't be read as a wait.
    """
    if not any(_fuzzy_contains(text, p) for p in MATCH_BLOCK_PHRASES):
        return None
    mins = _COOLDOWN_MIN_RE.search(text)
    secs = _COOLDOWN_SEC_RE.search(text)
    if not mins and not secs:
        return None
    total = (int(mins.group(1)) * 60 if mins else 0) + (int(secs.group(1)) if secs else 0)
    return total or None


# The '[system]' badge OCRs as 1-2 capitals glued to the first word ('FRAurel…',
# 'SRJin…'). Stripping is only cosmetic (for log lines that name a player), so it
# is deliberately conservative: it fires only when the leftover starts like a
# name (capital + lowercase), never on a lowercase-initial nickname.
_BADGE_RE = re.compile(r"^[^\w]*(?:[A-Z]{2}(?=[a-z])|[A-Z]{1,2}(?=[A-Z][a-z]))")


def strip_system_badge(text: str) -> str:
    """Drop the OCR'd '[system]' badge from the front of a log line."""
    return _BADGE_RE.sub("", text.strip())


def is_not_ready(text: str) -> bool:
    """True if ``text`` is a '<player> is not ready.' system line."""
    return _fuzzy_contains(text, NOT_READY_PHRASE, 0.85)


def is_offline(text: str) -> bool:
    """True if ``text`` is a '<player> is offline.' system line.

    Deliberately an **exact** normalized substring test, not a fuzzy one: the log
    is full of '<player> is online.' and the two differ by two letters, which
    scores ~0.94 under fuzzy matching. Reading 'is online' as 'is offline' would
    make the loop retry ten times and then stop on a perfectly healthy party, so
    the safe error here is to miss an OCR-mangled 'offline' rather than invent
    one. ('offline' cannot appear inside 'isonline', so the test is sound.)
    """
    return "offline" in normalize(text)


def member_block_reason(text: str) -> str | None:
    """Why this line says a member is blocking the queue, or ``None``."""
    if is_not_ready(text):
        return "not ready"
    if is_offline(text):
        return "offline"
    return None


def is_member_blocked(text: str) -> bool:
    """True if ``text`` reports a member who blocks matchmaking."""
    return member_block_reason(text) is not None


def read_toast_verdict(ocr: OcrResult) -> tuple[str, int | None]:
    """Read a matchmaking refusal straight off the screen that refused it.

    ToF puts the reason on the failing screen as a toast — *"Matching cooldown
    ends in 23s."* sits mid-screen on the team lobby while **Go** is still on the
    button. When it's there, it answers the question without leaving the screen,
    so the whole chat-log trip (Esc out, Enter, System, Esc back — ~20s of stolen
    focus) is only needed when the toast has already faded.

    Same verdicts as :func:`classify_system_message`, scanned per OCR line so a
    phrase isn't matched across unrelated screen text.
    """
    for line in ocr.lines:
        seconds = parse_matching_cooldown(line.text)
        if seconds is not None:
            return "cooldown", seconds
    for line in ocr.lines:
        if any(_fuzzy_contains(line.text, p) for p in EVENT_UNAVAILABLE_PHRASES):
            return "unavailable", None
    blocked = sum(1 for line in ocr.lines if is_member_blocked(line.text))
    if blocked:
        return "member_blocked", blocked
    return "unknown", None


def read_system_messages(
    ocr: OcrResult,
    region: tuple[float, float, float, float] = SYSTEM_LOG_REGION,
) -> list[str]:
    """Return the System-channel log lines, oldest first (newest last).

    OCR splits one on-screen log line into several ``Line`` fragments (a coloured
    span like 'Energy Station' comes back separately), so fragments are regrouped
    into rows by their y position and ordered left-to-right within a row. Words
    outside ``region`` — the nav rail, the world behind the panel, the footer —
    are dropped. The '[system]' badge glues onto the first word ('FRMatching...'),
    which is left as-is: every consumer matches fuzzily.
    """
    if not ocr.width or not ocr.height:
        return []
    left, right = region[0] * ocr.width, region[2] * ocr.width
    top, bottom = region[1] * ocr.height, region[3] * ocr.height
    frags: list[tuple[int, int, str]] = []  # (y, x, text)
    for line in ocr.lines:
        ws = [
            w for w in line.words
            if left <= w.x + w.w / 2 <= right and top <= w.y + w.h / 2 <= bottom
        ]
        if not ws:
            continue
        text = " ".join(w.text for w in ws).strip()
        if text:
            frags.append((min(w.y for w in ws), min(w.x for w in ws), text))
    if not frags:
        return []
    frags.sort()
    tol = max(6, int(0.012 * ocr.height))  # same-row fragments share a y band
    rows: list[list[tuple[int, int, str]]] = [[frags[0]]]
    for frag in frags[1:]:
        if frag[0] - rows[-1][0][0] <= tol:
            rows[-1].append(frag)
        else:
            rows.append([frag])
    return [" ".join(t for _, _, t in sorted(row, key=lambda f: f[1])) for row in rows]


def classify_system_message(messages: list[str]) -> tuple[str, int | None]:
    """Read why matchmaking refused, from the tail of the System log.

    Returns ``("cooldown", seconds)``, ``("unavailable", None)``,
    ``("member_blocked", how_many_members)`` or ``("unknown", None)``. Scanned
    newest-first over the last :data:`SYSTEM_LOG_SCAN` lines so a stale 'Event
    unavailable' from an earlier attempt can't override a fresher notice.

    Member lines are counted rather than returned on the first hit, because the
    game logs one per member ("A is not ready." / "B is offline.") and knowing
    how many are holding the queue up is worth reporting.
    """
    tail = messages[-SYSTEM_LOG_SCAN:]
    for text in reversed(tail):
        seconds = parse_matching_cooldown(text)
        if seconds is not None:
            return "cooldown", seconds
        if any(_fuzzy_contains(text, p) for p in EVENT_UNAVAILABLE_PHRASES):
            return "unavailable", None
        if is_member_blocked(text):
            return "member_blocked", sum(1 for t in tail if is_member_blocked(t))
    return "unknown", None



def parse_timer(ocr: OcrResult) -> int | None:
    """Parse a MM:SS countdown from an OCR result of just the timer crop.

    No position filtering — the crop *is* the timer region — so this joins the
    crop's tokens and extracts the time, tolerawhting a split ('26:' '06') or a
    mangled separator ('26.06'). Returns total seconds, or ``None``.
    """
    text = " ".join(w.text for line in ocr.lines for w in line.words)
    m = _TIMER_RE.search(text)
    if m:
        mm, ss = int(m.group(1)), int(m.group(2))
    else:
        digits = re.sub(r"\D", "", text)
        if len(digits) not in (3, 4):
            return None
        mm, ss = int(digits[:-2]), int(digits[-2:])
    if ss >= 60 or mm > 30:  # CA countdown starts at 30:00
        return None
    return mm * 60 + ss


def read_ca_timer(ocr: OcrResult) -> int | None:
    """Return the CA-instance countdown in seconds, or ``None`` if not present.

    Looks for a ``MM:SS`` token in the top-center HUD band so the other top-left
    timer (a separate counter) and stray numbers don't get picked up.
    """
    if not ocr.width or not ocr.height:
        return None
    best: tuple[float, int] | None = None  # (distance-to-center, seconds)
    center_x = ocr.width / 2
    for w in ocr.words:
        t = w.text.strip()
        m = _TIMER_RE.search(t)
        if m:
            mm, ss = int(m.group(1)), int(m.group(2))
        else:
            digits = re.sub(r"\D", "", t)
            if len(digits) == 4:
                mm, ss = int(digits[:2]), int(digits[2:])
            else:
                continue
        if ss >= 60 or mm > 30:  # CA countdown starts at 30:00
            continue
        wcx = w.x + w.w / 2
        wcy = w.y + w.h / 2
        if wcy > 0.20 * ocr.height:                 # top HUD band only
            continue
        if not (0.38 * ocr.width <= wcx <= 0.62 * ocr.width):  # center only
            continue
        dist = abs(wcx - center_x)
        if best is None or dist < best[0]:
            best = (dist, mm * 60 + ss)
    return None if best is None else best[1]


def _state_hits(ocr: OcrResult, state: State) -> tuple[int, float]:
    hits = 0
    score = 0.0
    for phrase in state.signatures:
        m = find_phrase(ocr, phrase, state.threshold)
        if m is not None:
            hits += 1
            score += m.score
    return hits, score


def classify(ocr: OcrResult) -> Classification:
    """Classify a frame's OCR result into a game state."""
    per_state: dict[str, int] = {}
    # Selection key: (priority, hits, score). Priority lets overlay states win
    # over the base screen they cover even with fewer signature hits.
    best_key: tuple[int, int, float] = (-1, 0, 0.0)
    best_name = OVERWORLD
    for state in STATES:
        hits, score = _state_hits(ocr, state)
        per_state[state.name] = hits
        if hits >= state.min_hits:
            key = (state.priority, hits, score)
            if key > best_key:
                best_key = key
                best_name = state.name
    # Safety net: a text-sparse instance frame where 'Opponent' wasn't read would
    # fall through to overworld — but a top-center countdown means we're actually
    # inside the CA instance. Only override the overworld default (NOT a real base
    # screen like the lobby, which can carry an incidental timer-like token).
    if best_name == OVERWORLD and read_ca_timer(ocr) is not None:
        return Classification(state=CA_INSTANCE, confidence=1.0, hits=per_state)
    return Classification(state=best_name, confidence=best_key[2], hits=per_state)
