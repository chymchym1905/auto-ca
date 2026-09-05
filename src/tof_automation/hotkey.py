"""Global hotkey support for PyQt6 via the Win32 ``RegisterHotKey`` API.

A global hotkey is required because, during the "focus + hotkey" flow, the ToF
window (not our app) holds keyboard focus when the user triggers a capture.
``RegisterHotKey`` delivers a ``WM_HOTKEY`` message regardless of focus; we
catch it with a Qt native event filter and emit a Qt signal.

Important: the native event filter is a *standalone* ``QAbstractNativeEventFilter``
(not also a ``QObject``). PyQt6 does not properly install a native event filter
on an object that multiply-inherits from ``QObject`` and
``QAbstractNativeEventFilter`` — the filter is silently never called. So the
``QObject`` (for the signal) and the filter are kept as separate objects.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
from collections.abc import Callable

from PyQt6.QtCore import QAbstractNativeEventFilter, QObject, pyqtSignal

# Win32 modifier flags for RegisterHotKey.
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
WM_HOTKEY = 0x0312

# Virtual-key codes.
VK_RETURN = 0x0D
VK_F8 = 0x77


class _HotkeyEventFilter(QAbstractNativeEventFilter):
    """Catches ``WM_HOTKEY`` for a given hotkey id and invokes ``on_hotkey``."""

    def __init__(self, on_hotkey: Callable[[], None], hotkey_id: int) -> None:
        super().__init__()
        self._on_hotkey = on_hotkey
        self._id = hotkey_id

    def nativeEventFilter(self, event_type, message):  # noqa: N802 (Qt override)
        if event_type == "windows_generic_MSG":
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == self._id:
                self._on_hotkey()
                # Consume the message. The filter is otherwise consulted at
                # several dispatch points for the same message, which would
                # fire the hotkey multiple times per keypress.
                return True, 0
        return False, 0


class GlobalHotkey(QObject):
    """Registers a single system-wide hotkey and emits :attr:`activated`.

    Default binding is Ctrl+Enter. Call :meth:`install` with the
    ``QApplication`` to hook the native event filter, then :meth:`register`
    (after the main window exists) with the window's HWND.
    """

    activated = pyqtSignal()

    def __init__(
        self,
        modifiers: int = MOD_CONTROL,
        vk: int = VK_RETURN,
        hotkey_id: int = 1,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._modifiers = modifiers
        self._vk = vk
        self._id = hotkey_id
        self._hwnd = 0
        self._registered = False
        self._filter = _HotkeyEventFilter(self.activated.emit, hotkey_id)

    def install(self, app) -> None:
        """Install the native event filter on the given ``QApplication``."""
        app.installNativeEventFilter(self._filter)

    def register(self, hwnd: int = 0) -> bool:
        """Register the hotkey globally. Returns ``True`` on success.

        ``hwnd`` should be the target window's handle (e.g.
        ``int(window.winId())``). Registering against a real window — rather
        than ``NULL`` — makes ``WM_HOTKEY`` arrive as a *window* message, which
        Qt reliably routes to the native event filter.
        """
        if self._registered:
            return True
        self._hwnd = int(hwnd)
        ok = ctypes.windll.user32.RegisterHotKey(
            self._hwnd or None, self._id, self._modifiers, self._vk
        )
        self._registered = bool(ok)
        return self._registered

    def unregister(self) -> None:
        if self._registered:
            ctypes.windll.user32.UnregisterHotKey(self._hwnd or None, self._id)
            self._registered = False
