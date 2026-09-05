"""Interactive PyQt6 UI for capturing Tower of Fantasy windows.

Walks the user through the three fixed roles (mainhost, main, althost) one at a
time. For each role the user focuses the target ToF window and presses the
global hotkey (Ctrl+Shift+C); the app screenshots the foreground window, shows
it, and asks the user to confirm it is the correct window for that role.

Confirmed results are kept in memory (``MainWindow.results``) for the current
run only.
"""

from __future__ import annotations

import threading
import time

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import cv2

from . import icons
from .automation import RoleWindow, Session
from .capturer import (
    ROLES,
    CaptureError,
    CaptureResult,
    capture_window,
    find_window_by_pid,
    get_foreground_window,
    get_window_pid,
)
from .config import Config
from .cropdialog import CropDialog
from .hotkey import VK_F8, GlobalHotkey
from .icons import IconLibrary

ICON_HOTKEY_LABEL = "F8"

HOTKEY_LABEL = "Ctrl+Enter"


def _bgra_to_pixmap(image_bgra: bytes, width: int, height: int) -> QPixmap:
    """Convert raw BGRA bytes (mss order) into a QPixmap.

    BGRA in little-endian memory matches Qt's ``Format_RGB32`` (0xffRRGGBB),
    so no channel swizzling is needed.
    """
    image = QImage(image_bgra, width, height, QImage.Format.Format_RGB32)
    return QPixmap.fromImage(image.copy())  # copy() detaches from the bytes buffer


class MainWindow(QMainWindow):
    # Worker threads talk to the UI only via signals (thread-safe updates).
    log_signal = pyqtSignal(str)
    worker_done = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ToF Window Capturer")
        self.resize(900, 720)

        # In-memory results: role -> CaptureResult (HWNDs are not persisted).
        self.results: dict[str, CaptureResult] = {}
        # role -> bound HWND (from a fresh capture or restored via saved PID).
        self._bound: dict[str, int] = {}
        # Persistent config (usernames + last PIDs per role); loaded/saved to disk.
        self.config = Config.load()
        # Roles still needing manual capture (filled after PID restore in __init__).
        self._pending_roles: list[str] = list(ROLES)
        self._role_index = 0
        self._pending: CaptureResult | None = None
        # Set once all roles are captured/restored (disables capture-hotkey path).
        self._capture_done = False

        # --- Widgets ---
        self.role_label = QLabel()
        self.role_label.setStyleSheet("font-size: 22px; font-weight: bold;")
        self.role_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.instruction_label = QLabel()
        self.instruction_label.setWordWrap(True)
        self.instruction_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.instruction_label.setStyleSheet("font-size: 14px;")

        self.preview_label = QLabel("No capture yet.")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(640, 420)
        self.preview_label.setStyleSheet(
            "background: #1e1e1e; color: #888; border: 1px solid #444;"
        )

        self.title_label = QLabel("")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setStyleSheet("color: #aaa; font-size: 12px;")

        # In-game username for this role (used later to match the right
        # party/request row by name). Persisted to config.json.
        self.username_caption = QLabel()
        self.username_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("In-game username for this role")
        username_row = QHBoxLayout()
        username_row.addStretch(1)
        username_row.addWidget(self.username_caption)
        username_row.addWidget(self.username_edit, stretch=2)
        username_row.addStretch(1)
        self._username_row = username_row

        self.confirm_btn = QPushButton("✓ Confirm — this is correct")
        self.confirm_btn.setEnabled(False)
        self.confirm_btn.clicked.connect(self._on_confirm)

        self.retry_btn = QPushButton("↺ Retry capture")
        self.retry_btn.setEnabled(False)
        self.retry_btn.clicked.connect(self._on_retry)

        button_row = QHBoxLayout()
        button_row.addWidget(self.retry_btn)
        button_row.addWidget(self.confirm_btn)

        # --- Automation control panel (hidden until capture completes) ---
        self.dryrun_btn = QPushButton("🔍 Detect states (dry-run)")
        self.dryrun_btn.clicked.connect(self._on_dry_run)
        self.start_btn = QPushButton("▶ Start CA loop")
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn = QPushButton("■ Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        control_row = QHBoxLayout()
        control_row.addWidget(self.dryrun_btn)
        control_row.addWidget(self.start_btn)
        control_row.addWidget(self.stop_btn)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")

        self.control_panel = QWidget()
        control_layout = QVBoxLayout(self.control_panel)
        control_layout.setContentsMargins(0, 0, 0, 0)
        control_layout.addLayout(control_row)
        control_layout.addWidget(self.log_view, stretch=1)
        self.control_panel.hide()

        # Worker thread state for automation.
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.log_signal.connect(self._append_log)
        self.worker_done.connect(self._on_worker_done)

        layout = QVBoxLayout()
        layout.addWidget(self.role_label)
        layout.addWidget(self.instruction_label)
        layout.addWidget(self.preview_label, stretch=1)
        layout.addWidget(self.title_label)
        layout.addLayout(username_row)
        layout.addLayout(button_row)
        layout.addWidget(self.control_panel, stretch=1)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        # --- Global hotkeys ---
        # Ctrl+Enter: window (role) capture.
        self.hotkey = GlobalHotkey()
        self.hotkey.activated.connect(self._on_hotkey)
        # F8: icon template capture (drag-to-crop the focused window).
        self.icon_hotkey = GlobalHotkey(modifiers=0, vk=VK_F8, hotkey_id=2)
        self.icon_hotkey.activated.connect(self._on_icon_hotkey)

        # Icon template library (loaded from templates/ next to the exe).
        self.icon_lib = IconLibrary().load()
        names = ", ".join(self.icon_lib.templates) or "none"
        icon_msg = (f"Loaded {len(self.icon_lib.templates)} icon template(s) "
                    f"from {self.icon_lib.dir}: {names}")

        # Try to re-bind roles from saved PIDs so they need no recapture.
        restore_msgs = self._try_restore()
        if self._pending_roles:
            self._update_role_view()
        else:
            self._finish(announce=False)  # all restored — straight to the panel
        self._append_log(icon_msg)
        for msg in restore_msgs:
            self._append_log(msg)

    def _try_restore(self) -> list[str]:
        """Re-bind roles whose saved PID still has a live ToF window.

        Returns log messages (deferred until the log view exists). Roles that are
        restored are removed from ``self._pending_roles``.
        """
        msgs: list[str] = []
        for role in ROLES:
            pid = self.config.roles[role].pid
            if not pid:
                continue
            hint = self.config.roles[role].window_title_hint
            hwnd = find_window_by_pid(pid, hint)
            if hwnd:
                self._bound[role] = hwnd
                self._pending_roles.remove(role)
                msgs.append(f"Restored {role} from PID {pid} (hwnd={hwnd}) — no recapture.")
            else:
                msgs.append(f"{role}: saved PID {pid} not found; needs recapture.")
        return msgs

    # -- Lifecycle ----------------------------------------------------------
    def install_hotkeys(self, app) -> None:
        self.hotkey.install(app)
        self.icon_hotkey.install(app)

    def register_hotkeys(self) -> bool:
        # Register against this window's HWND so WM_HOTKEY is delivered as a
        # window message that Qt routes to the native event filter.
        hwnd = int(self.winId())
        ok1 = self.hotkey.register(hwnd)
        ok2 = self.icon_hotkey.register(hwnd)
        return ok1 and ok2

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._stop_event.set()  # ask any running worker to stop
        self.hotkey.unregister()
        self.icon_hotkey.unregister()
        super().closeEvent(event)

    # -- Role flow ----------------------------------------------------------
    @property
    def _current_role(self) -> str:
        return self._pending_roles[self._role_index]

    def _update_role_view(self) -> None:
        """Switch to the current role: reset capture and prefill its username."""
        role = self._current_role
        self.role_label.setText(
            f"Capture role: {role.upper()}  "
            f"({self._role_index + 1}/{len(self._pending_roles)})"
        )
        self.instruction_label.setText(
            f"Click/focus the Tower of Fantasy window for <b>{role}</b>, "
            f"then press <b>{HOTKEY_LABEL}</b> to capture it."
        )
        self.username_caption.setText(f"{role} username:")
        self.username_edit.setText(self.config.roles[role].username)
        self._reset_capture()

    def _reset_capture(self) -> None:
        """Clear only the captured screenshot (keeps the typed username)."""
        self.preview_label.setText("No capture yet.")
        self.preview_label.setPixmap(QPixmap())
        self.title_label.setText("")
        self.confirm_btn.setEnabled(False)
        self.retry_btn.setEnabled(False)
        self._pending = None

    def _on_hotkey(self) -> None:
        hwnd = get_foreground_window()
        # Ignore the hotkey if our own window is focused.
        if hwnd == int(self.winId()):
            QMessageBox.information(
                self,
                "Focus the game first",
                "This capturer window is focused. Click the ToF window, then press "
                f"{HOTKEY_LABEL}.",
            )
            return

        if self._capture_done:
            return  # nothing to capture once all roles are done

        try:
            result = capture_window(hwnd)
        except CaptureError as exc:
            QMessageBox.warning(self, "Capture failed", str(exc))
            return

        self._pending = result
        self._show_pending(result)

    def _on_icon_hotkey(self) -> None:
        """F8: capture the focused window and crop an icon template from it."""
        hwnd = get_foreground_window()
        if hwnd == int(self.winId()):
            QMessageBox.information(
                self, "Focus the game first",
                f"Focus the ToF window showing the icon, then press {ICON_HOTKEY_LABEL}.",
            )
            return
        try:
            cap = capture_window(hwnd)
        except CaptureError as exc:
            QMessageBox.warning(self, "Icon capture failed", str(exc))
            return

        pixmap = _bgra_to_pixmap(cap.image_bgra, cap.width, cap.height)
        dialog = CropDialog(pixmap, parent=self)
        if dialog.exec() != CropDialog.DialogCode.Accepted:
            return
        rect = dialog.selection_in_image()
        name = dialog.icon_name
        if rect is None or not name:
            QMessageBox.warning(
                self, "Icon not saved", "Draw a box around the icon and select its name."
            )
            return

        # Keep an existing capture and add a new variant (<base>#n) if requested
        # (e.g. priority_entry's red-dot / 'new' tag / plain looks).
        if dialog.as_variant:
            name = self.icon_lib.next_variant_name(name)

        # Crop the grayscale template and a padded normalized search region.
        frame_bgr = icons.bgra_to_bgr(cap.image_bgra, cap.width, cap.height)
        x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        gray = cv2.cvtColor(frame_bgr[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)
        region = self._padded_region(x, y, w, h, cap.width, cap.height)
        self.icon_lib.add(name, gray, src_h=cap.height, region=region)
        self._append_log(
            f"Saved icon '{name}' ({w}x{h}) from {cap.width}x{cap.height} "
            f"-> templates/{name}.png"
        )

    @staticmethod
    def _padded_region(x, y, w, h, fw, fh):
        """Normalized search region: the selection expanded by ~half its size."""
        px, py = w * 0.5, h * 0.5
        rx = max(0.0, (x - px) / fw)
        ry = max(0.0, (y - py) / fh)
        rw = min(1.0 - rx, (w + 2 * px) / fw)
        rh = min(1.0 - ry, (h + 2 * py) / fh)
        return (round(rx, 4), round(ry, 4), round(rw, 4), round(rh, 4))

    def _show_pending(self, result: CaptureResult) -> None:
        pixmap = _bgra_to_pixmap(result.image_bgra, result.width, result.height)
        scaled = pixmap.scaled(
            self.preview_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview_label.setPixmap(scaled)
        warn = "" if result.looks_like_tof else "  ⚠ title doesn't look like ToF"
        self.title_label.setText(f'Window: "{result.title}"  [{result.width}×{result.height}]{warn}')
        self.confirm_btn.setEnabled(True)
        self.retry_btn.setEnabled(True)

    def _on_retry(self) -> None:
        self._reset_capture()

    def _on_confirm(self) -> None:
        if self._pending is None:
            return
        username = self.username_edit.text().strip()
        if not username:
            QMessageBox.warning(
                self,
                "Username required",
                f"Enter the in-game username for {self._current_role} before confirming.",
            )
            self.username_edit.setFocus()
            return

        role = self._current_role
        result = self._pending
        result.role = role
        self.results[role] = result
        self._bound[role] = result.hwnd
        self.config.roles[role].username = username
        # Remember the window's PID so this role can be re-bound next launch.
        self.config.roles[role].pid = get_window_pid(result.hwnd)

        self._role_index += 1
        if self._role_index >= len(self._pending_roles):
            self._finish()
        else:
            self._update_role_view()

    def _finish(self, announce: bool = True) -> None:
        try:
            saved_path = self.config.save()
            save_note = f"Saved usernames + PIDs to {saved_path}"
        except OSError as exc:
            save_note = f"⚠ Could not save config: {exc}"

        lines = []
        for role in ROLES:
            hwnd = self._bound.get(role)
            if hwnd is None:
                continue
            src = "restored" if role not in self.results else "captured"
            lines.append(
                f"• {role}: \"{self.config.roles[role].username}\"  "
                f"(hwnd={hwnd}, pid={self.config.roles[role].pid}, {src})"
            )
        if announce:
            QMessageBox.information(
                self,
                "Windows ready",
                "Bound windows for this run:\n\n" + "\n".join(lines) + f"\n\n{save_note}",
            )
        # Disable capture controls; reveal the automation panel.
        self._capture_done = True
        self.role_label.setText("✓ All roles captured")
        self.instruction_label.setText(
            "Capture complete. Run a dry-run to verify state detection, "
            "then start the loop."
        )
        self.username_caption.setText("")
        self.username_edit.setEnabled(False)
        self.confirm_btn.setEnabled(False)
        self.retry_btn.setEnabled(False)
        self.preview_label.hide()
        self.title_label.hide()
        self.control_panel.show()

    # -- Automation ---------------------------------------------------------
    def _append_log(self, text: str) -> None:
        """Append to the log view, stamping every line with the wall clock.

        The loop runs for hours, so a bare line can't be placed in time (how long
        did matchmaking take? when did the timer cross 27:00?). Stamping here —
        the single choke point every message passes through, worker messages
        included via ``log_signal`` — keeps the timestamp out of the call sites.
        Each physical line gets its own stamp so multi-line messages stay aligned.
        """
        ts = time.strftime("%H:%M:%S")
        for line in text.splitlines() or [""]:
            self.log_view.appendPlainText(f"[{ts}] {line}")

    def _build_session(self) -> Session:
        windows = {
            role: RoleWindow(
                role=role,
                hwnd=hwnd,
                username=self.config.roles[role].username,
            )
            for role, hwnd in self._bound.items()
        }
        return Session(
            windows,
            points=self.config.points,
            icon_lib=self.icon_lib,
            log=lambda msg: self.log_signal.emit(msg),
            stop=self._stop_event,
            regions=self.config.regions,
        )

    def _run_in_thread(self, target) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop_event.clear()
        self.dryrun_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        def runner() -> None:
            session = self._build_session()
            try:
                target(session)
            except Exception as exc:  # surface worker errors to the log
                self.log_signal.emit(f"✗ error: {exc!r}")
            finally:
                self.log_signal.emit("— worker finished —")
                self.worker_done.emit()  # re-enable controls on the UI thread

        self._worker = threading.Thread(target=runner, daemon=True)
        self._worker.start()

    def _on_worker_done(self) -> None:
        self.start_btn.setEnabled(True)
        self.dryrun_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def _on_dry_run(self) -> None:
        self._append_log("Running dry-run detection on all windows…")
        self._run_in_thread(lambda s: s.dry_run())

    def _on_start(self) -> None:
        self._append_log("Starting CA loop…")
        self._run_in_thread(lambda s: s.run_loop())

    def _on_stop(self) -> None:
        self._append_log("Stop requested…")
        self._stop_event.set()
