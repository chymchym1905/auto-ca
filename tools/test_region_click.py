"""Verify click_text_in_region maps the crop match back to the right pixel,
and that read_window_robust rescues a near-empty OCR. Fed by real screenshots."""
import sys
sys.path.insert(0, 'src')

from PIL import Image

from tof_automation import actions, detector, ocr, textmatch
from tof_automation.capturer import CaptureResult


def cap_of(path):
    im = Image.open(path).convert('RGBA')
    r, g, b, a = im.split()
    return CaptureResult(role='', hwnd=0, title='', rect=(0, 0, im.width, im.height),
                         image_bgra=Image.merge('RGBA', (b, g, r, a)).tobytes(),
                         width=im.width, height=im.height)


clicks = []
actions.focus_window = lambda hwnd, **kw: True
actions.click_client = lambda hwnd, cx, cy, settle=0.2, hold_alt=False: clicks.append((cx, cy))

fails = 0

# --- 1. region click lands on the button ---------------------------------
for path, truth in [
    # (file, button bbox measured by red-pixel thresholding, not eyeballed)
    ('ref-images/althost find match 3.png', (846, 485, 994, 536)),
    ('ref-images/althost find match 4.png', (846, 485, 995, 563)),
]:
    c = cap_of(path)
    actions.capture_window = lambda hwnd, _c=c: _c
    clicks.clear()
    ok = actions.click_text_in_region(0, 'Find matches', detector.FIND_MATCH_REGION, 3, 0.7)
    x, y = clicks[-1] if clicks else (-1, -1)
    inside = truth[0] <= x <= truth[2] and truth[1] <= y <= truth[3]
    fails += not (ok and inside)
    print(f'  [{"PASS" if ok and inside else "FAIL"}] {path}')
    print(f'          found={ok} click=({x},{y})  button box={truth}')

# compare against what the old full-frame path would have clicked
c = cap_of('ref-images/althost find match 3.png')
full = ocr.recognize_bgra(c.image_bgra, c.width, c.height)
m = textmatch.find_phrase(full, 'Find matches', 0.7)
print(f'\n  full-frame match for reference: {m and (m.text, m.center, round(m.score, 2))}')

# --- 2. near-empty read is rescued ---------------------------------------
c = cap_of('ref-images/find match succeed.png')
actions.capture_window = lambda hwnd, _c=c: _c
res = actions.read_window_robust(0)
state = detector.classify(res).state
ok = state == 'arena_screen'
fails += not ok
print(f'\n  [{"PASS" if ok else "FAIL"}] read_window_robust on the 0-line frame '
      f'-> {len(res.lines)} lines, state={state}')

# a healthy frame must NOT pay for the retry
c = cap_of('ref-images/teams screen.png')
actions.capture_window = lambda hwnd, _c=c: _c
res = actions.read_window_robust(0)
print(f'  healthy frame -> {len(res.lines)} lines, state={detector.classify(res).state}')

print(f'\n{"ALL PASS" if not fails else f"{fails} FAILURE(S)"}')
sys.exit(1 if fails else 0)
