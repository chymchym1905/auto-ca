"""Offline test of the burst-capture queue check, fed by real screenshots."""
import sys, time
sys.path.insert(0, 'src')

from PIL import Image

from tof_automation import actions, detector
from tof_automation.automation import RoleWindow, Session
from tof_automation.capturer import CaptureResult


def cap(path):
    im = Image.open(path).convert('RGBA')
    r, g, b, a = im.split()
    data = Image.merge('RGBA', (b, g, r, a)).tobytes()
    return CaptureResult(role='', hwnd=0, title='', rect=(0, 0, im.width, im.height),
                         image_bgra=data, width=im.width, height=im.height)


GO_FAIL = cap('ref-images/go fail.png')                  # Go still there + toast
LOBBY = cap('ref-images/teams screen.png')               # lobby, no Go pressed yet
ARENA = cap('ref-images/althost find match 3.png')       # Find matches present
QUEUED = cap('ref-images/find match succeed.png')        # button gone, queued

logs = []
win = {r: RoleWindow(role=r, hwnd=0, username=r) for r in ('mainhost', 'althost')}
session = Session(win, log=logs.append)

CASES = [
    ('mainhost Go refused (toast in frame 3)',
     'mainhost', '_go_button_stuck', [GO_FAIL] * 9, (True, ('cooldown', 23))),
    ('mainhost Go refused, toast already gone',
     'mainhost', '_go_button_stuck', [LOBBY] + [LOBBY] * 8, (True, ('unknown', None))),
    ('althost queued (button gone by frame 4)',
     'althost', '_find_match_stuck', [ARENA] * 3 + [QUEUED] * 6, (False, ('unknown', None))),
    ('althost refused (button never leaves)',
     'althost', '_find_match_stuck', [ARENA] * 9, (True, ('unknown', None))),
]

fails = 0
for label, role, method, frames, expect in CASES:
    actions.burst_capture = lambda hwnd, count, interval=0.45, _f=frames: _f[:count]
    t0 = time.time()
    res = getattr(session, method)(role)
    got = (res.stuck, res.toast)
    ok = got == expect
    fails += not ok
    print(f'  [{"PASS" if ok else "FAIL"}] {label}')
    print(f'          stuck={res.stuck} toast={res.toast}  expected={expect}  '
          f'({time.time() - t0:.1f}s of OCR)')

# teams screen must never be read as a cooldown (no false positives)
print(f'\n  lobby with no toast -> {detector.read_toast_verdict(__import__("tof_automation.ocr", fromlist=["x"]).recognize_bgra(LOBBY.image_bgra, LOBBY.width, LOBBY.height))}')
print(f'\n{"ALL PASS" if not fails else f"{fails} FAILURE(S)"}')
sys.exit(1 if fails else 0)
