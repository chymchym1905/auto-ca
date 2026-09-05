"""Build the single-file Windows executable, then stage its runtime files.

    .venv\\Scripts\\python.exe build.py

Produces ``dist\\ToF-Auto-CA.exe`` plus, beside it, the files the app reads and
writes at runtime:

    dist\\templates\\      icon templates + manifest.json (hand-editable)
    dist\\config.json      usernames / calibrated points / regions

Those stay *outside* the exe on purpose. A one-file bundle unpacks to a fresh
temp directory on every launch, so anything written inside it is lost and any
hand edit is invisible; ``config.config_path``, ``icons.templates_dir`` and
``replay.replays_dir`` all resolve next to ``sys.executable`` when frozen, which
is what makes them persist. ``replays\\`` is created by the app on first dump.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / 'dist'
EXE = DIST / 'ToF-Auto-CA.exe'


def run_pyinstaller() -> None:
    cmd = [sys.executable, '-m', 'PyInstaller', 'tof-auto-ca.spec', '--noconfirm']
    print(f'$ {" ".join(cmd)}\n', flush=True)
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(f'PyInstaller failed ({result.returncode})')


def stage_runtime_files() -> None:
    """Copy templates/ and config.json next to the exe (never overwriting)."""
    templates_src, templates_dst = ROOT / 'templates', DIST / 'templates'
    if templates_src.is_dir():
        templates_dst.mkdir(parents=True, exist_ok=True)
        for src in templates_src.iterdir():
            if src.is_file():
                shutil.copy2(src, templates_dst / src.name)
        print(f'  templates -> {templates_dst} '
              f'({len(list(templates_dst.glob("*.png")))} icons)')
    else:
        print('  ! no templates/ directory — capture icons with F8 after launch')

    config_dst = DIST / 'config.json'
    if config_dst.exists():
        # Never clobber a config the packaged app has already been using.
        print(f'  config.json already present at {config_dst}; left alone')
    else:
        # Seed a *blank* config rather than copying the dev one: the exe is meant
        # to be shipped, and the dev config carries real in-game usernames and
        # stale PIDs. The app fills it in on the first capture.
        starter = {
            'roles': {r: {'username': '', 'window_title_hint': 'Tower of Fantasy',
                          'pid': None}
                      for r in ('mainhost', 'main', 'althost')},
            'points': {},
            'regions': {},
        }
        config_dst.write_text(json.dumps(starter, indent=2), encoding='utf-8')
        print(f'  config.json -> {config_dst} (blank starter)')


def main() -> int:
    run_pyinstaller()
    if not EXE.is_file():
        raise SystemExit(f'expected {EXE} but it was not produced')
    print('\nStaging runtime files next to the exe:')
    stage_runtime_files()
    size_mb = EXE.stat().st_size / (1024 * 1024)
    print(f'\nBuilt {EXE} ({size_mb:.1f} MB)')
    print('Ship the whole dist\\ folder: the exe reads templates\\ and config.json '
          'from beside itself.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
