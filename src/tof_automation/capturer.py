"""Window capture core for Tower of Fantasy automation.

Identifies game windows by the "focus + hotkey" method: the user focuses the
target ToF window and presses a global hotkey; the foreground window is grabbed
and its on-screen region is screenshotted via ``mss``.

Capture results are held in memory only (no persistence) for the current run.
"""

from __future__ import annotations

from dataclasses import dataclass

import mss
import win32gui
import win32process

# The three fixed roles, in capture order.
ROLES: tuple[str, ...] = ("mainhost", "main", "althost")

# Substring used to sanity-check that the focused window is actually the game.
TOF_TITLE_HINT = "tower of fantasy"


@dataclass
class CaptureResult:
    """A single captured window bound to a role.

    Attributes:
        role: One of ``ROLES``.
        hwnd: Win32 window handle of the captured window.
        title: Window title text at capture time.
        rect: ``(left, top, right, bottom)`` of the client area, screen coords.
        image_bgra: Raw BGRA pixel bytes of the captured region.
        width / height: Pixel dimensions of ``image_bgra``.
    """

    role: str
    hwnd: int
    title: str
    rect: tuple[int, int, int, int]
    image_bgra: bytes
    width: int
    height: int

    @property
    def looks_like_tof(self) -> bool:
        return TOF_TITLE_HINT in self.title.lower()


class CaptureError(Exception):
    """Raised when a window cannot be captured."""


def get_foreground_window() -> int:
    """Return the handle of the current foreground window."""
    return win32gui.GetForegroundWindow()


def get_window_title(hwnd: int) -> str:
    return win32gui.GetWindowText(hwnd)


def get_window_pid(hwnd: int) -> int:
    """Return the process id owning ``hwnd``."""
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    return pid


def find_window_by_pid(pid: int, title_hint: str = TOF_TITLE_HINT) -> int | None:
    """Find a visible top-level window owned by ``pid`` matching ``title_hint``.

    Used to re-bind a role to its game window across launches: the HWND changes
    each run but the process id is stable while the client stays open. The title
    check guards against PID reuse after the original process exits.
    """
    hint = title_hint.lower()
    matches: list[int] = []

    def _cb(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        if get_window_pid(hwnd) != pid:
            return True
        if hint in win32gui.GetWindowText(hwnd).lower():
            matches.append(hwnd)
        return True

    win32gui.EnumWindows(_cb, None)
    return matches[0] if matches else None


def get_client_region(hwnd: int) -> tuple[int, int, int, int]:
    """Return the window's *client area* in screen coordinates.

    Uses ``GetClientRect`` (the rendered content size, excluding the title bar
    and borders) and maps its corners to the screen with ``ClientToScreen``.
    This is re-read on every capture, so a window using dynamic resolution — one
    that resizes itself at runtime — is always captured at its current size with
    no title bar. Technique adapted from ``nte-auto-fishing-script``.

    Returns ``(left, top, right, bottom)`` screen coordinates.
    """
    # GetClientRect yields (0, 0, width, height) relative to the client area.
    _, _, cw, ch = win32gui.GetClientRect(hwnd)
    left, top = win32gui.ClientToScreen(hwnd, (0, 0))
    right, bottom = win32gui.ClientToScreen(hwnd, (cw, ch))
    return left, top, right, bottom


def capture_window(hwnd: int) -> CaptureResult:
    """Grab the on-screen client region of ``hwnd`` and return a partial result.

    Only the client area (rendered game content) is captured — the title bar and
    window borders are excluded. ``role`` is set to an empty string here; the
    caller assigns the role once the user confirms the capture. Raises
    :class:`CaptureError` for invalid or zero-sized windows.
    """
    if not hwnd or not win32gui.IsWindow(hwnd):
        raise CaptureError("No valid window is focused.")

    title = get_window_title(hwnd)
    left, top, right, bottom = get_client_region(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise CaptureError(f"Window client area has zero size ({width}x{height}).")

    with mss.mss() as sct:
        shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
        image_bgra = bytes(shot.raw)
        width, height = shot.width, shot.height

    return CaptureResult(
        role="",
        hwnd=hwnd,
        title=title,
        rect=(left, top, right, bottom),
        image_bgra=image_bgra,
        width=width,
        height=height,
    )
