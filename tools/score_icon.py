"""Score an icon template (and all its variants) against the live windows.

Use it to check whether a template actually matches the screen you're on — most
usefully right after forming a party, since that HUD state is what broke the
team_toggle match in a live run.

Usage:
  .venv\\Scripts\\python.exe tools\\score_icon.py [icon_name] [role ...]

  icon_name  base name, e.g. team_toggle (default), priority_entry, arena_tile
  role       mainhost / main / althost (default: all bound roles)

A score at or above the variant's threshold is marked '*'. Compare it with the
noise floor: a template that scores ~0.5 on screens where the icon is absent
gains nothing from a 0.5 threshold.
"""
import sys

sys.path.insert(0, 'src')

from tof_automation import actions, capturer, detector, icons as ic, ocr
from tof_automation.config import Config

name = sys.argv[1] if len(sys.argv) > 1 else 'team_toggle'
want = sys.argv[2:]

lib = ic.IconLibrary().load()
variants = lib.variants(ic.base_name(name))
if not variants:
    sys.exit(f'no template named {name!r} (have: {sorted(lib.templates)})')
print(f'{name}: {len(variants)} variant(s)')
for t in variants:
    print(f'  {t.name:16s} {t.gray.shape[1]}x{t.gray.shape[0]}px  src_h={t.src_h}  '
          f'threshold={t.threshold}  region={[round(x, 3) for x in (t.region or ())]}')

cfg = Config.load()
print()
for role, rc in cfg.roles.items():
    if want and role not in want:
        continue
    hwnd = capturer.find_window_by_pid(rc.pid, rc.window_title_hint) if rc.pid else None
    if not hwnd:
        print(f'{role:9s} no live window (pid={rc.pid})')
        continue
    actions.focus_window(hwnd)
    cap = capturer.capture_window(hwnd)
    bgr = ic.bgra_to_bgr(cap.image_bgra, cap.width, cap.height)
    state = detector.classify(ocr.recognize_bgra(cap.image_bgra, cap.width, cap.height)).state
    print(f'{role:9s} {cap.width}x{cap.height}  state={state}')
    passed = []
    for t in variants:
        m = ic.scan_best(bgr, t)
        if m is None:
            print(f'    {t.name:16s} no scale fit')
            continue
        ok = m.score >= t.threshold
        passed += [t.name] if ok else []
        print(f'    {t.name:16s} {m.score:.3f}{"*" if ok else " "} at ({m.cx},{m.cy}) '
              f'nx={m.cx / cap.width:.3f} ny={m.cy / cap.height:.3f} scale={m.scale:.2f}')
    print(f'    -> {"MATCH via " + ", ".join(passed) if passed else "NO MATCH"}\n')
