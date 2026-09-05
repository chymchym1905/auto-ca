"""Input actions against a game window: focus, click, and OCR-located clicks.

Per the confirmed design, clicks use *focus + real input*: the target window is
brought to the foreground, then a real OS cursor move + click is sent with
``pydirectinput`` (SendInput), which DirectX/Unreal windows accept. Only one
window is driven at a time.

OCR boxes are in captured-frame pixels, and the frame is the window's client
area (see ``capturer.get_client_region``), so a box's coordinates are already
client-relative — converted to screen with ``ClientToScreen`` before clicking.
"""

from __future__ import annotations

import ctypes
import time

import pydirectinput
import win32con
import win32gui

from . import icons, ocr, textmatch
from .capturer import capture_window

# pydirectinput safety: disable the move-to-corner failsafe (legitimate clicks
# can land near screen edges) and keep its built-in pause small.
pydirectinput.FAILSAFE = False
pydirectinput.PAUSE = 0.02

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# Windows blocks SetForegroundWindow for a process that hasn't received recent
# user input (the "foreground lock timeout"). That's why focusing works for the
# first loop — right after the user clicks "Start CA loop" — then silently fails
# on later loops: the window never comes to front, so the capture grabs a stale
# or wrong frame and OCR misreads it. Setting the timeout to 0 lets us foreground
# at will. It's a per-session system setting; best-effort (ignore failures).
_SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
_SPIF_SENDCHANGE = 0x2


def _disable_foreground_lock() -> None:
    try:
        _user32.SystemParametersInfoW(
            _SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.c_void_p(0), _SPIF_SENDCHANGE
        )
    except Exception:
        pass


_disable_foreground_lock()


def _force_foreground(hwnd: int) -> None:
    """Foreground a window even across thread/foreground-lock restrictions.

    Attaches our thread's input to **both** the currently-foreground window's
    thread and the target's thread — attaching to the foreground thread is what
    actually lets SetForegroundWindow succeed when another process owns focus.
    """
    cur_thread = _kernel32.GetCurrentThreadId()
    fg = _user32.GetForegroundWindow()
    fg_thread = _user32.GetWindowThreadProcessId(fg, None) if fg else 0
    target_thread = _user32.GetWindowThreadProcessId(hwnd, None)
    attached = [t for t in {fg_thread, target_thread} if t and t != cur_thread]
    for t in attached:
        _user32.AttachThreadInput(cur_thread, t, True)
    try:
        _user32.BringWindowToTop(hwnd)
        _user32.ShowWindow(hwnd, win32con.SW_SHOW)
        _user32.SetForegroundWindow(hwnd)
    finally:
        for t in attached:
            _user32.AttachThreadInput(cur_thread, t, False)


def focus_window(hwnd: int, settle: float = 0.30, attempts: int = 4) -> bool:
    """Bring ``hwnd`` to the foreground; verify and retry. Returns success.

    The capture path grabs the window's screen region, so it only reads the right
    content if the window is *actually* foreground. We therefore don't trust a
    single SetForegroundWindow call (Windows may ignore it) — we verify with
    GetForegroundWindow and retry, escalating to the thread-attach path, before
    settling. A False return means focus could not be confirmed; callers reading
    a frame should treat what they captured as unreliable.
    """
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    if win32gui.GetForegroundWindow() == hwnd:
        time.sleep(settle)
        return True
    for attempt in range(attempts):
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        if win32gui.GetForegroundWindow() != hwnd:
            _force_foreground(hwnd)
        if win32gui.GetForegroundWindow() == hwnd:
            time.sleep(settle)
            return True
        time.sleep(0.12 * (attempt + 1))
    return win32gui.GetForegroundWindow() == hwnd


def click_client(
    hwnd: int, cx: int, cy: int, settle: float = 0.20, hold_alt: bool = False
) -> None:
    """Click at client-area coordinates ``(cx, cy)`` of ``hwnd``.

    ``hold_alt`` holds Alt down for the whole move+click. In the ToF overworld
    the cursor is locked to camera control; holding Alt frees it so HUD elements
    can be clicked. Menu screens (lobby, arena, etc.) do not need it.

    Timing matters for the overworld: the game frees the cursor a beat *after*
    Alt goes down, so we wait before moving — clicking too soon lands as a
    camera/world click (the screen centre) instead of on the HUD icon, which is
    why an overworld icon (e.g. the Priority entry) often needed many retries to
    'take'. We also settle briefly after the move so the cursor is on target
    before the click.
    """
    sx, sy = win32gui.ClientToScreen(hwnd, (int(cx), int(cy)))
    if hold_alt:
        pydirectinput.keyDown("alt")
        time.sleep(0.15)  # let the game release the camera-locked cursor
    try:
        pydirectinput.moveTo(sx, sy)
        time.sleep(0.06)  # ensure the cursor has arrived before clicking
        pydirectinput.click()
    finally:
        if hold_alt:
            pydirectinput.keyUp("alt")
    time.sleep(settle)


def press_key(hwnd: int, key: str, presses: int = 1, settle: float = 0.6) -> None:
    """Focus ``hwnd`` and send ``key`` as real key presses.

    For the keyboard-only paths that have no clickable target: **Esc** to back
    out of a menu and **Enter** to open the chat panel. Sent through
    ``pydirectinput`` (SendInput scancodes) for the same reason clicks are —
    DirectX/Unreal windows ignore posted key messages.
    """
    focus_window(hwnd)
    for _ in range(presses):
        pydirectinput.press(key)
        time.sleep(0.12)
    time.sleep(settle)


def client_size(hwnd: int) -> tuple[int, int]:
    """Width and height of ``hwnd``'s client area, in pixels."""
    _, _, cw, ch = win32gui.GetClientRect(hwnd)
    return cw, ch


def norm_to_client(hwnd: int, nx: float, ny: float) -> tuple[int, int]:
    """Convert a normalized client position ``(nx, ny)`` (0..1) to client pixels."""
    _, _, cw, ch = win32gui.GetClientRect(hwnd)
    return int(nx * cw), int(ny * ch)


def click_norm(
    hwnd: int, nx: float, ny: float, hold_alt: bool = False, settle: float = 0.20
) -> None:
    """Click at a normalized client position ``(nx, ny)`` in 0..1 fractions.

    Used for calibrated icon buttons (no text to OCR). ``hold_alt`` for overworld
    HUD icons (camera-locked cursor).
    """
    cx, cy = norm_to_client(hwnd, nx, ny)
    click_client(hwnd, cx, cy, settle=settle, hold_alt=hold_alt)


def read_window(hwnd: int) -> ocr.OcrResult:
    """Capture the window's client area and OCR it (no focusing)."""
    cap = capture_window(hwnd)
    return ocr.recognize_bgra(cap.image_bgra, cap.width, cap.height)


def read_window_focused(hwnd: int) -> ocr.OcrResult:
    """Focus the window first (so it's on top), then capture and OCR it."""
    focus_window(hwnd)
    return read_window(hwnd)


def read_window_robust(hwnd: int, min_lines: int = 5, upscale: int = 2) -> ocr.OcrResult:
    """Read the window, retrying upscaled when the frame comes back near-empty.

    WinRT OCR sometimes returns **nothing at all** for a busy, artwork-heavy
    frame at its native size — the Arena screen does this, and consecutive
    captures of the same static screen flip between 0 and 17 lines. A frame that
    reads empty classifies as ``overworld`` (the text-sparse default), which sent
    step 5 clicking the overworld Priority icon while the Arena screen was up.

    The same frame upscaled 2× reads fine (verified: 7/7 previously-misread
    frames, including ``ref-images/find match succeed.png``, go from 0-3 lines and
    'overworld' to 10-24 lines and 'arena_screen'). The retry only costs anything
    on the rare bad read, since a healthy screen clears ``min_lines`` first try.
    """
    cap = capture_window(hwnd)
    result = ocr.recognize_bgra(cap.image_bgra, cap.width, cap.height)
    if len(result.lines) >= min_lines:
        return result
    retry = ocr.recognize_bgra_region(
        cap.image_bgra, cap.width, cap.height, (0.0, 0.0, 1.0, 1.0), upscale
    )
    # Boxes come back in upscaled pixels, but so do width/height, so every
    # position test in the detector (which is ratio-based) still holds.
    return retry if len(retry.lines) > len(result.lines) else result


def read_window_and_region(
    hwnd: int,
    region: tuple[float, float, float, float],
    upscale: int = 3,
) -> tuple[ocr.OcrResult, ocr.OcrResult]:
    """Focus, capture once, and OCR both the full frame and a sub-region.

    Returns ``(full, region_ocr)``. One capture serves both the state
    classification (full frame) and a precise reading of a small element (the
    upscaled region crop), avoiding two focus/capture round-trips per poll.
    """
    focus_window(hwnd)
    cap = capture_window(hwnd)
    full = ocr.recognize_bgra(cap.image_bgra, cap.width, cap.height)
    sub = ocr.recognize_bgra_region(
        cap.image_bgra, cap.width, cap.height, region, upscale
    )
    return full, sub


def burst_capture(hwnd: int, count: int, interval: float = 0.45) -> list:
    """Focus once, then grab ``count`` frames ``interval`` apart, OCR'ing none.

    For catching something that is only on screen for a moment (the *"Matching
    cooldown ends in 23s."* toast). Capturing is cheap (~30ms) while OCR is not
    (~0.5s a frame), so a read-then-OCR loop samples the screen far too slowly to
    see a 2-second toast. Grabbing raw frames first and OCR'ing them afterwards
    covers the same wall-clock window at several times the sample rate.
    """
    focus_window(hwnd)
    frames = []
    for i in range(count):
        if i:
            time.sleep(interval)
        frames.append(capture_window(hwnd))
    return frames


def capture_bgr(hwnd: int):
    """Capture the window's client area as an OpenCV BGR array."""
    cap = capture_window(hwnd)
    return icons.bgra_to_bgr(cap.image_bgra, cap.width, cap.height)


def find_icon(hwnd: int, library: "icons.IconLibrary", name: str) -> "icons.IconMatch | None":
    """Focus, capture, and locate icon ``name`` in the window."""
    focus_window(hwnd)
    return library.find(name, capture_bgr(hwnd))


def find_icon_scored(
    hwnd: int, library: "icons.IconLibrary", name: str
) -> "tuple[icons.IconMatch | None, float | None]":
    """Focus, capture, and locate icon ``name``, also returning the best score.

    One capture; the score is for diagnostics on a near miss (see
    :meth:`icons.IconLibrary.find_scored`)."""
    focus_window(hwnd)
    return library.find_scored(name, capture_bgr(hwnd))


def click_icon(
    hwnd: int, library: "icons.IconLibrary", name: str, hold_alt: bool = False
) -> bool:
    """Focus, locate icon ``name`` by template, and click its center."""
    match = find_icon(hwnd, library, name)
    if match is None:
        return False
    click_client(hwnd, match.cx, match.cy, hold_alt=hold_alt)
    return True


def click_text(
    hwnd: int, query: str, threshold: float = 0.7, hold_alt: bool = False
) -> bool:
    """Focus, read, and click the label best matching ``query``. Returns success.

    Set ``hold_alt`` for overworld HUD clicks (cursor is camera-locked there).
    """
    focus_window(hwnd)
    result = read_window(hwnd)
    match = textmatch.find_phrase(result, query, threshold)
    if match is None:
        return False
    click_client(hwnd, *match.center, hold_alt=hold_alt)
    return True


def click_text_any(
    hwnd: int, queries: list[str], threshold: float = 0.7, hold_alt: bool = False
) -> bool:
    """Click the first of several candidate labels found (one capture/OCR).

    Useful for labels OCR mangles (e.g. 'OK' often reads as '0K') — pass both.
    """
    focus_window(hwnd)
    result = read_window(hwnd)
    for query in queries:
        match = textmatch.find_phrase(result, query, threshold)
        if match is not None:
            click_client(hwnd, *match.center, hold_alt=hold_alt)
            return True
    return False


def click_text_in_region(
    hwnd: int,
    query: str,
    region: tuple[float, float, float, float],
    upscale: int = 3,
    threshold: float = 0.7,
    hold_alt: bool = False,
) -> bool:
    """Locate ``query`` inside a normalized region crop and click it.

    Same idea as :func:`click_text`, but the OCR runs on an upscaled crop of just
    the button instead of the whole frame. On screens where full-frame OCR is
    unreliable this is the difference between finding the label almost never and
    always: across 14 consecutive Arena frames, the full frame matched "Find
    matches" 3 times, the crop 14/14 at score 1.00. It also clicks a *better*
    point, since a fuzzy full-frame match can land on a fragment of the label.

    The match's box is in upscaled-crop pixels, so it is divided by ``upscale``
    and offset by the crop's origin to get back to client coordinates.
    """
    focus_window(hwnd)
    cap = capture_window(hwnd)
    sub = ocr.recognize_bgra_region(
        cap.image_bgra, cap.width, cap.height, region, upscale
    )
    match = textmatch.find_phrase(sub, query, threshold)
    if match is None:
        return False
    px0 = max(0, int(region[0] * cap.width))
    py0 = max(0, int(region[1] * cap.height))
    mx, my = match.center
    click_client(hwnd, px0 + mx // upscale, py0 + my // upscale, hold_alt=hold_alt)
    return True


def click_text_aligned(
    hwnd: int,
    query: str,
    anchor: str,
    query_threshold: float = 0.6,
    anchor_threshold: float = 0.7,
    hold_alt: bool = False,
) -> bool:
    """Click the ``query`` label sitting on the same row as ``anchor``.

    Disambiguates a label that appears more than once where only its row position
    tells the right one apart. Concretely: the **Team Lobby tab** reads as just
    'Lobby' under OCR (its 'Team' is an icon) and collides with the screen's
    'Lobby' *title* above it — a plain text click lands on the title. Passing
    query='Lobby' with anchor='My Team' (another tab on the same strip) picks the
    'Lobby' aligned with the tab row instead, then the leftmost on ties (the tab).
    """
    focus_window(hwnd)
    result = read_window(hwnd)
    anchor_match = textmatch.find_phrase(result, anchor, anchor_threshold)
    if anchor_match is None:
        return False
    anchor_cy = anchor_match.y + anchor_match.h // 2
    candidates = textmatch.find_all(result, query, query_threshold)
    if not candidates:
        return False
    best = min(candidates, key=lambda m: (abs((m.y + m.h // 2) - anchor_cy), m.x))
    click_client(hwnd, *best.center, hold_alt=hold_alt)
    return True


def click_row_button(
    hwnd: int,
    name: str,
    button: str,
    name_threshold: float = 0.6,
    button_threshold: float = 0.7,
) -> bool:
    """Click the ``button`` on the row whose text matches ``name``.

    Used for name-targeted actions: find the player's username, then click the
    button (e.g. ``Request`` / ``Approve``) on the same row — the one closest in
    vertical position, preferring a button to the right of the name.
    """
    focus_window(hwnd)
    result = read_window(hwnd)
    name_match = textmatch.find_phrase(result, name, name_threshold)
    if name_match is None:
        return False
    buttons = textmatch.find_all(result, button, button_threshold)
    if not buttons:
        return False

    name_cy = name_match.y + name_match.h // 2
    # Prefer same row (small |dy|); tie-break toward a button right of the name.
    best = min(
        buttons,
        key=lambda b: (abs((b.y + b.h // 2) - name_cy), 0 if b.x >= name_match.x else 1),
    )
    click_client(hwnd, *best.center)
    return True
