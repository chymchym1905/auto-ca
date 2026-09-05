"""Rolling instant-replay buffer for the CA loop.

While the loop runs, a background thread periodically grabs each role's window
(reusing :func:`capturer.capture_window` — no focus needed, since ``mss`` reads
whatever is currently on screen) and keeps only the last ``duration`` seconds,
downscaled and JPEG-compressed to bound memory. On a step failure or a caught
exception, :meth:`ReplayRecorder.dump` encodes each role's buffered frames into
an ``.mp4`` — an OBS-style instant replay of what was on screen leading up to
the failure, without having to have already been recording.

Capture cadence intentionally matches the automation's own poll cadence
(~1s) rather than real video framerates — the target is catching *state*
(a dialog that never appeared, a button that didn't register), not smooth
motion, and low cadence keeps CPU/memory overhead small during the loop.

Assumes the three role windows are tiled on screen (not overlapping) — a
window fully covered by another at capture time yields whatever is on top,
same limitation any screen-region recorder has.
"""

from __future__ import annotations

import collections
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import cv2
import numpy as np

from .capturer import CaptureError, capture_window
from .icons import bgra_to_bgr

if TYPE_CHECKING:
    from .automation import RoleWindow

REPLAYS_DIRNAME = "replays"

LogFn = Callable[[str], None]


def replays_dir() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parents[2]
    return base / REPLAYS_DIRNAME


@dataclass
class _Frame:
    ts: float
    jpg: bytes
    w: int
    h: int


class ReplayRecorder:
    """Per-role rolling frame buffers, captured on a background thread."""

    def __init__(
        self,
        windows: "dict[str, RoleWindow]",
        duration: float = 300.0,
        interval: float = 1.0,
        max_width: int = 960,
        jpeg_quality: int = 70,
        log: LogFn | None = None,
    ) -> None:
        self._windows = windows
        self._duration = duration
        self._interval = interval
        self._max_width = max_width
        self._jpeg_quality = jpeg_quality
        self._log = log or (lambda _msg: None)
        self._buffers: dict[str, collections.deque[_Frame]] = {
            role: collections.deque() for role in windows
        }
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background capture thread. No-op if already running."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background capture thread and wait for it to exit."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            start = time.monotonic()
            for role, rw in self._windows.items():
                self._capture_one(role, rw.hwnd)
            elapsed = time.monotonic() - start
            self._stop.wait(max(0.0, self._interval - elapsed))

    def _capture_one(self, role: str, hwnd: int) -> None:
        try:
            cap = capture_window(hwnd)
        except CaptureError:
            return  # window vanished/minimized this tick — skip, try again next
        arr = bgra_to_bgr(cap.image_bgra, cap.width, cap.height)
        if arr.shape[1] > self._max_width:
            scale = self._max_width / arr.shape[1]
            w = max(2, int(arr.shape[1] * scale)) & ~1  # even width (codec-friendly)
            h = max(2, int(arr.shape[0] * scale)) & ~1
            arr = cv2.resize(arr, (w, h), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality])
        if not ok:
            return
        frame = _Frame(ts=time.time(), jpg=buf.tobytes(), w=arr.shape[1], h=arr.shape[0])
        with self._lock:
            dq = self._buffers[role]
            dq.append(frame)
            cutoff = frame.ts - self._duration
            while dq and dq[0].ts < cutoff:
                dq.popleft()

    def dump(self, label: str) -> list[Path]:
        """Encode each role's currently-buffered frames to ``replays/<label>/<role>.mp4``.

        Returns the list of written video paths (roles with no buffered frames
        yet — e.g. a failure in the first second of the loop — are skipped).
        """
        with self._lock:
            snapshot = {role: list(dq) for role, dq in self._buffers.items()}

        out_dir = replays_dir() / label
        written: list[Path] = []
        for role, frames in snapshot.items():
            if not frames:
                continue
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{role}.mp4"
            span = frames[-1].ts - frames[0].ts
            fps = (len(frames) / span) if len(frames) > 1 and span > 0 else 1.0
            w, h = frames[0].w, frames[0].h
            # 'avc1' (H.264) via OpenCV's Windows Media Foundation fallback — the
            # bundled FFmpeg backend lacks an H.264 encoder (no libopenh264), but
            # MSMF picks it up transparently. No bitrate knob is exposed on this
            # build (VIDEOWRITER_PROP_QUALITY.set() is a no-op here), so encoding
            # uses the backend's own default rather than a specific target.
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"avc1"), fps, (w, h))
            try:
                for f in frames:
                    arr = cv2.imdecode(np.frombuffer(f.jpg, np.uint8), cv2.IMREAD_COLOR)
                    if arr is None:
                        continue
                    if (arr.shape[1], arr.shape[0]) != (w, h):
                        arr = cv2.resize(arr, (w, h))
                    writer.write(arr)
            finally:
                writer.release()
            written.append(path)
        return written
