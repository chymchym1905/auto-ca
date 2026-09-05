"""LIVE test of althost's Find matches click (the step-5 Arena branch).

Runs exactly what step 5 runs — navigate to Arena if needed, confirm the Critical
Abyss tab, click Find matches via the region crop, then verify the queue started
with _find_match_stuck — and reports where the click landed plus what the old
full-frame path would have clicked instead.

NOTE: on success this really does queue althost for matchmaking. Cancel it in
game (the X on the Matching widget) if you don't want the run.

Usage:  .venv\\Scripts\\python.exe tools\\live_find_match_test.py [role]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, 'src')

import cv2

from tof_automation import actions, capturer, detector, ocr, textmatch
from tof_automation.automation import RoleWindow, Session
from tof_automation.config import Config
from tof_automation.icons import IconLibrary, bgra_to_bgr

role = sys.argv[1] if len(sys.argv) > 1 else 'althost'
OUT = Path('replays') / f'findmatch_test_{time.strftime("%Y%m%d_%H%M%S")}'
OUT.mkdir(parents=True, exist_ok=True)

cfg = Config.load()
rc = cfg.roles[role]
hwnd = capturer.find_window_by_pid(rc.pid, rc.window_title_hint) if rc.pid else None
if not hwnd:
    sys.exit(f'ABORT: no live window for {role} (pid={rc.pid})')
print(f'{role}: hwnd={hwnd} "{capturer.get_window_title(hwnd)}"\n', flush=True)

session = Session({role: RoleWindow(role=role, hwnd=hwnd, username=rc.username)},
                  points=cfg.points, icon_lib=IconLibrary().load(),
                  regions=cfg.regions, log=lambda m: print(f'    {m}', flush=True))

# record every click this test makes
clicks = []
_real_click = actions.click_client


def spy_click(h, cx, cy, settle=0.20, hold_alt=False):
    clicks.append((cx, cy))
    print(f'    >>> CLICK at client ({cx},{cy}){" +alt" if hold_alt else ""}', flush=True)
    return _real_click(h, cx, cy, settle=settle, hold_alt=hold_alt)


actions.click_client = spy_click


def snap(label):
    cap = capturer.capture_window(hwnd)
    cv2.imwrite(str(OUT / f'{label}.png'),
                bgra_to_bgr(cap.image_bgra, cap.width, cap.height))
    return cap


# ---------------- 1. get to the Arena screen ----------------
print('=== 1. navigate to the Arena screen ===', flush=True)
state = session.detect(role).state
print(f'  start state: {state}', flush=True)
for i in range(10):
    state = session.detect(role).state
    if state == 'arena_screen':
        break
    print(f'  [{i}] {state} -> navigating', flush=True)
    if state in ('overworld_chat', 'chat_system'):
        actions.press_key(hwnd, 'esc')
    elif state == 'priority_challenge':
        m = actions.find_icon(hwnd, session.icons, 'arena_tile')
        if m:
            actions.click_client(hwnd, m.cx, m.cy)
    elif state == 'priority_menu':
        session._click_challenge_tab(role)
    else:
        session._open_priority(role)
    time.sleep(1.5)
if state != 'arena_screen':
    snap('00_not_arena')
    sys.exit(f'ABORT: could not reach arena_screen (stuck at {state}); '
             f'screenshot in {OUT}')
print(f'  reached arena_screen\n', flush=True)

# ---------------- 2. compare click targets ----------------
print('=== 2. where each path would click ===', flush=True)
cap = snap('01_before')
region = tuple(cfg.regions.get('find_match', detector.FIND_MATCH_REGION))
full = ocr.recognize_bgra(cap.image_bgra, cap.width, cap.height)
crop = ocr.recognize_bgra_region(cap.image_bgra, cap.width, cap.height, region,
                                 session.timer_upscale)
fm_full = textmatch.find_phrase(full, 'Find matches', 0.7)
fm_crop = textmatch.find_phrase(crop, 'Find matches', 0.7)
print(f'  frame {cap.width}x{cap.height}, full-frame OCR: {len(full.lines)} lines', flush=True)
print(f'  OLD full-frame match: {fm_full and (fm_full.text, fm_full.center, round(fm_full.score,2))}', flush=True)
if fm_crop:
    px0, py0 = int(region[0]*cap.width), int(region[1]*cap.height)
    mx, my = fm_crop.center
    mapped = (px0 + mx//session.timer_upscale, py0 + my//session.timer_upscale)
    print(f'  NEW crop match: {fm_crop.text!r} score={fm_crop.score:.2f} -> client {mapped}', flush=True)
else:
    print('  NEW crop match: NONE', flush=True)

# ---------------- 3. the real step-5 actions ----------------
print('\n=== 3. running the step-5 Arena branch ===', flush=True)
if not session._ensure_ca_selected(hwnd):
    print('  ! CA tab not confirmed selected', flush=True)
else:
    print('  CA tab confirmed selected', flush=True)
clicked = actions.click_text_in_region(
    hwnd, 'Find matches', region, session.timer_upscale, 0.7
) or actions.click_text(hwnd, 'Find matches', threshold=0.7)
print(f'  clicked Find matches: {clicked}', flush=True)

check = session._find_match_stuck(role)
snap('02_after')
print(f'\n  RESULT: stuck={check.stuck}  toast={check.toast}', flush=True)
if not check.stuck:
    print('  ✓ QUEUED — the button left its region (cancel it in game if unwanted)', flush=True)
else:
    print('  ✗ NOT QUEUED — the button never changed', flush=True)
    print(f'  clicks this test made: {clicks}', flush=True)
print(f'\n  screenshots: {OUT}', flush=True)
