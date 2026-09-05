"""Persistent configuration for the ToF automation app.

Stores per-role usernames (and a window-title hint) in a hand-editable
``config.json`` placed next to the executable (when frozen by PyInstaller) or at
the project root during development. Window handles are intentionally *not*
persisted — they change every launch and are re-acquired during capture.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .capturer import ROLES

CONFIG_FILENAME = "config.json"
DEFAULT_TITLE_HINT = "Tower of Fantasy"


def config_path() -> Path:
    """Return the path to ``config.json`` (next to the exe, or project root)."""
    if getattr(sys, "frozen", False):  # running as a PyInstaller bundle
        base = Path(sys.executable).parent
    else:
        # src/tof_automation/config.py -> project root is two parents up.
        base = Path(__file__).resolve().parents[2]
    return base / CONFIG_FILENAME


@dataclass
class RoleConfig:
    username: str = ""
    window_title_hint: str = DEFAULT_TITLE_HINT
    # Last-captured process id for this role's window. HWNDs change every launch,
    # but the PID is stable while the client stays open, so it is used to re-bind
    # the window without recapturing. May be stale/None.
    pid: int | None = None


@dataclass
class Config:
    roles: dict[str, RoleConfig] = field(default_factory=dict)
    # Calibrated icon-button click points as normalized [x, y] fractions of the
    # client area (resolution-independent, identical across the three windows).
    # e.g. {"team_toggle": [0.95, 0.18]}. Icons have no text, so they cannot be
    # OCR-located and must be calibrated (or hand-edited) once.
    points: dict[str, list[float]] = field(default_factory=dict)
    # Normalized OCR crop regions as [x0, y0, x1, y1] client fractions (0..1),
    # resolution-independent. Used to OCR a tight, upscaled box instead of the
    # whole frame (far more accurate for small elements like the CA countdown).
    # e.g. {"ca_timer": [0.44, 0.0, 0.56, 0.07]}. Omit to use the built-in default.
    regions: dict[str, list[float]] = field(default_factory=dict)

    @classmethod
    def default(cls) -> "Config":
        return cls(roles={role: RoleConfig() for role in ROLES})

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """Load config from disk, falling back to defaults for missing/invalid data."""
        path = path or config_path()
        cfg = cls.default()
        if not path.exists():
            return cfg
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cfg
        roles = raw.get("roles", {})
        for role in ROLES:
            entry = roles.get(role, {})
            if isinstance(entry, dict):
                raw_pid = entry.get("pid")
                cfg.roles[role] = RoleConfig(
                    username=str(entry.get("username", "")),
                    window_title_hint=str(
                        entry.get("window_title_hint", DEFAULT_TITLE_HINT)
                    ),
                    pid=int(raw_pid) if isinstance(raw_pid, int) else None,
                )
        raw_points = raw.get("points", {})
        if isinstance(raw_points, dict):
            for name, xy in raw_points.items():
                if isinstance(xy, (list, tuple)) and len(xy) == 2:
                    cfg.points[str(name)] = [float(xy[0]), float(xy[1])]
        raw_regions = raw.get("regions", {})
        if isinstance(raw_regions, dict):
            for name, box in raw_regions.items():
                if isinstance(box, (list, tuple)) and len(box) == 4:
                    cfg.regions[str(name)] = [float(v) for v in box]
        return cfg

    def save(self, path: Path | None = None) -> Path:
        """Write the config to disk as pretty JSON and return the path."""
        path = path or config_path()
        data = {
            "roles": {role: asdict(rc) for role, rc in self.roles.items()},
            "points": self.points,
            "regions": self.regions,
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return path
