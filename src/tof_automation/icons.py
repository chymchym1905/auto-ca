"""Scale-tolerant icon template matching for buttons OCR can't read.

Some flow elements are icons with no text (team toggle, Priority entry) or are
purely visual states (match-found / loading). Those are matched with OpenCV
template matching.

The three windows can run at different resolutions, so matching is **scale
tolerant**: each template records the height of the frame it was captured from
(``src_h``). At match time the base scale is ``frame_height / src_h`` (ToF scales
its UI with resolution), and a small multiplier range is searched around it. A
normalized search ``region`` restricts where a template can match, which keeps
the icon-dense HUD from producing false positives.

Templates live next to the executable in ``templates/`` with a ``manifest.json``
so they are hand-editable and survive PyInstaller bundling.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

TEMPLATES_DIRNAME = "templates"
MANIFEST_NAME = "manifest.json"

# Some icons have several visual variants of the *same* button (e.g. the Priority
# entry can show a red-dot badge, a "new" tag, or appear plain). Each variant is a
# separate template whose name is ``<base>#<n>``; lookups match against every
# variant of a base and take the best, so any one matching is enough.
VARIANT_SEP = "#"


def base_name(name: str) -> str:
    """Strip a ``#<n>`` variant suffix, e.g. ``priority_entry#2`` → ``priority_entry``."""
    return name.split(VARIANT_SEP, 1)[0]

# Canonical icon templates the flow looks up, with a hint for the capture dialog.
# These are the names :mod:`automation` references; the capture UI offers them as
# radio choices so names always match exactly (no typos).
KNOWN_ICONS: tuple[tuple[str, str], ...] = (
    ("team_toggle", "Overworld side-panel team/quest toggle"),
    ("priority_entry", "Overworld trigger that opens the Priority panel"),
    ("team_lobby_tab", "The 'Team Lobby' tab in the Lobby"),
    ("priority_challenge_tab", "The 'Challenge' tab icon in the Priority side nav"),
    ("arena_tile", "The Arena tile on Priority → Challenge"),
    ("arena_ca_selected", "The Arena 'The Critical Abyss' tab when SELECTED (highlighted)"),
    ("ca_leave", "The leave/exit button (top-left HUD) inside the CA instance"),
    ("overworld_marker", "Top-right HUD scanner/order icon — present in the overworld, absent in the CA instance"),
)


def templates_dir() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parents[2]
    return base / TEMPLATES_DIRNAME


@dataclass
class IconTemplate:
    name: str
    gray: np.ndarray                       # grayscale template image
    src_h: int                             # height of the frame it was cropped from
    region: tuple[float, float, float, float] | None  # normalized (x, y, w, h)
    threshold: float = 0.80


@dataclass(frozen=True)
class IconMatch:
    name: str
    score: float
    cx: int
    cy: int
    scale: float


def bgra_to_bgr(bgra: bytes, width: int, height: int) -> np.ndarray:
    """Convert captured BGRA bytes to an OpenCV BGR array."""
    arr = np.frombuffer(bgra, np.uint8).reshape(height, width, 4)
    return np.ascontiguousarray(arr[:, :, :3])


def scan_best(
    frame_bgr: np.ndarray,
    tmpl: IconTemplate,
    scale_margin: float = 0.18,
    steps: int = 9,
) -> IconMatch | None:
    """Best scale-tolerant match of ``tmpl`` in ``frame_bgr``, **ignoring** the
    threshold (for diagnostics), or ``None`` if no scale fits.

    Searches scales around ``frame_h / src_h`` within ``±scale_margin``,
    restricted to the template's normalized region.
    """
    fh, fw = frame_bgr.shape[:2]

    if tmpl.region:
        rx, ry, rw, rh = tmpl.region
        x0, y0 = max(0, int(rx * fw)), max(0, int(ry * fh))
        x1, y1 = min(fw, int((rx + rw) * fw)), min(fh, int((ry + rh) * fh))
        if x1 - x0 < 4 or y1 - y0 < 4:
            return None
        sub = frame_bgr[y0:y1, x0:x1]
        off_x, off_y = x0, y0
    else:
        sub = frame_bgr
        off_x, off_y = 0, 0

    gray = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
    th, tw = tmpl.gray.shape[:2]
    base = (fh / tmpl.src_h) if tmpl.src_h else 1.0

    best: tuple[float, tuple[int, int], int, int, float] | None = None
    for s in np.linspace(base * (1 - scale_margin), base * (1 + scale_margin), steps):
        nw, nh = int(round(tw * s)), int(round(th * s))
        if nw < 6 or nh < 6 or nh > gray.shape[0] or nw > gray.shape[1]:
            continue
        resized = cv2.resize(tmpl.gray, (nw, nh), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(gray, resized, cv2.TM_CCOEFF_NORMED)
        _, maxv, _, maxloc = cv2.minMaxLoc(res)
        if best is None or maxv > best[0]:
            best = (maxv, maxloc, nw, nh, float(s))

    if best is None:
        return None
    score, (mx, my), nw, nh, scale = best
    return IconMatch(tmpl.name, score, off_x + mx + nw // 2, off_y + my + nh // 2, scale)


def match_icon(
    frame_bgr: np.ndarray,
    tmpl: IconTemplate,
    scale_margin: float = 0.18,
    steps: int = 9,
) -> IconMatch | None:
    """Scale-tolerant match of ``tmpl`` in ``frame_bgr``, or ``None`` if the best
    match is below ``tmpl.threshold``."""
    best = scan_best(frame_bgr, tmpl, scale_margin, steps)
    if best is None or best.score < tmpl.threshold:
        return None
    return best


class IconLibrary:
    """Loads/saves icon templates and their manifest from ``templates/``."""

    def __init__(self, directory: Path | None = None) -> None:
        self.dir = directory or templates_dir()
        self.templates: dict[str, IconTemplate] = {}

    def load(self) -> "IconLibrary":
        manifest = self.dir / MANIFEST_NAME
        if not manifest.exists():
            return self
        try:
            entries = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return self
        for name, meta in entries.items():
            img_path = self.dir / meta["file"]
            img = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            region = meta.get("region")
            self.templates[name] = IconTemplate(
                name=name,
                gray=img,
                src_h=int(meta.get("src_h", img.shape[0])),
                region=tuple(region) if region else None,
                threshold=float(meta.get("threshold", 0.80)),
            )
        return self

    def has(self, base: str) -> bool:
        """True if any template (variant included) exists for base name ``base``."""
        return any(base_name(n) == base for n in self.templates)

    def variants(self, base: str) -> list[IconTemplate]:
        """All templates whose base name is ``base`` (the icon's variants)."""
        return [t for n, t in self.templates.items() if base_name(n) == base]

    def next_variant_name(self, base: str) -> str:
        """Next free ``<base>#<n>`` name, so a new capture doesn't overwrite."""
        if base not in self.templates:
            return base
        i = 2
        while f"{base}{VARIANT_SEP}{i}" in self.templates:
            i += 1
        return f"{base}{VARIANT_SEP}{i}"

    def add(
        self,
        name: str,
        gray: np.ndarray,
        src_h: int,
        region: tuple[float, float, float, float] | None,
        threshold: float = 0.80,
    ) -> None:
        """Add/replace a template and persist it (PNG + manifest entry).

        Pass a ``<base>#<n>`` name (see :meth:`next_variant_name`) to keep an
        existing capture as a separate variant instead of replacing it.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        filename = f"{name}.png"
        cv2.imwrite(str(self.dir / filename), gray)
        self.templates[name] = IconTemplate(name, gray, src_h, region, threshold)
        self._save_manifest(changed=name)

    def _save_manifest(self, changed: str | None = None) -> None:
        """Persist the manifest **without clobbering edits made since we loaded**.

        The manifest is hand-editable (thresholds and regions are tuned there),
        and the app is normally left running while it's tuned — so writing the
        whole in-memory copy would stamp the file with whatever was loaded at
        startup. That really happened: a capture reverted a threshold edit and
        resurrected a variant whose PNG had been deleted, which then logged
        'can't open/read file' on every load.

        So the on-disk file is re-read and used as the base; only ``changed`` (the
        template this call is saving) is written over it, and entries whose PNG no
        longer exists are dropped. With ``changed=None`` every in-memory template
        is written, which is only for callers rebuilding the file wholesale.
        """
        path = self.dir / MANIFEST_NAME
        entries: dict[str, dict] = {}
        if path.exists():
            try:
                entries = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                entries = {}
        # Forget variants whose image was deleted by hand.
        entries = {
            name: meta for name, meta in entries.items()
            if (self.dir / meta.get("file", f"{name}.png")).exists()
        }
        for name in ([changed] if changed else list(self.templates)):
            t = self.templates.get(name)
            if t is None:
                continue
            entries[name] = {
                "file": f"{name}.png",
                "src_h": t.src_h,
                "region": list(t.region) if t.region else None,
                "threshold": t.threshold,
            }
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    def find(self, name: str, frame_bgr: np.ndarray) -> IconMatch | None:
        return self.find_scored(name, frame_bgr)[0]

    def find_scored(
        self, name: str, frame_bgr: np.ndarray
    ) -> tuple[IconMatch | None, float | None]:
        """Best match across all variants of ``name``'s base, plus the best score.

        Each variant is scanned; if any passes its own threshold the highest-
        scoring passing one is returned. The best (thresholdless) score across
        variants is also returned for diagnostics — it lets callers log *how far
        under* a near miss is instead of guessing whether to recapture. The score
        is ``None`` only when no template/variant exists or no scale fit.
        """
        templates = self.variants(base_name(name))
        if not templates:
            return None, None
        best_score: float | None = None
        passing: list[IconMatch] = []
        for tmpl in templates:
            m = scan_best(frame_bgr, tmpl)
            if m is None:
                continue
            if best_score is None or m.score > best_score:
                best_score = m.score
            if m.score >= tmpl.threshold:
                passing.append(m)
        if passing:
            return max(passing, key=lambda m: m.score), best_score
        return None, best_score
