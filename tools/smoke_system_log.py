"""Smoke test for the System-log Go-failure check.

Runs the REAL pipeline (pixels -> WinRT OCR -> detector -> Session) with no game:
  A. renders each message we care about into a real chat screenshot,
  B. OCRs it and asks the detector for a verdict,
  C. drives Session._diagnose_match_failure with actions/detect faked, recording the
     exact keys/clicks it sends, to check the navigation order.
"""
import sys
from pathlib import Path

sys.path.insert(0, 'src')

from PIL import Image, ImageDraw, ImageFont

from tof_automation import actions, detector, ocr
from tof_automation.automation import (
    GoUnavailable, MatchingCooldown, RoleWindow, Session,
)

REF = Path('ref-images/find logs 3.png')
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else '.') / 'smoke'
OUT.mkdir(parents=True, exist_ok=True)

# The ref screenshot's last log line sits at y=497; rows are ~21px apart and the
# message column starts at x=114. Draw the new "newest" message just below it.
ROW_Y, ROW_X, ROW_H = 518, 114, 21
FONT = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 15)


def render(message: str, name: str) -> Path:
    """Append ``message`` to the ref chat log as the newest line."""
    img = Image.open(REF).convert('RGB')
    d = ImageDraw.Draw(img)
    d.rectangle([ROW_X - 10, ROW_Y - 3, ROW_X + 460, ROW_Y + ROW_H], fill=(18, 22, 30))
    d.text((ROW_X - 8, ROW_Y), '\u25a0', font=FONT, fill=(150, 150, 150))  # [system] badge
    d.text((ROW_X + 6, ROW_Y), message, font=FONT, fill=(235, 235, 235))
    path = OUT / f'{name}.png'
    img.save(path)
    return path


CASES = [
    ('cooldown', 'Matching cooldown ends in 2 min 25 sec.', ('cooldown', 145)),
    ('cooldown_secs', 'Matching cooldown ends in 45 sec.', ('cooldown', 45)),
    ('unavailable', 'Event unavailable', ('unavailable', None)),
    ('normal', 'chymchym1905 left the team.', ('unknown', None)),
]

print('=== A/B: rendered message -> OCR -> detector verdict ===')
rendered: dict[str, Path] = {}
fails = 0
for name, message, expect in CASES:
    path = render(message, name)
    rendered[name] = path
    result = ocr.recognize_file(str(path))
    state = detector.classify(result).state
    msgs = detector.read_system_messages(result)
    verdict = detector.classify_system_message(msgs)
    ok = state == 'chat_system' and verdict == expect
    fails += not ok
    print(f'  [{"PASS" if ok else "FAIL"}] {message!r}')
    print(f'         state={state}  newest OCR line={msgs[-1]!r}')
    print(f'         verdict={verdict}  expected={expect}')

# --- C: navigation state machine, with all real input faked ------------------
print('\n=== C: _diagnose_match_failure navigation (inputs recorded, none sent) ===')


def run_nav(states, log_image, label):
    """Drive the diagnosis over a scripted sequence of detected states."""
    sent: list[str] = []
    seq = list(states)

    def fake_detect(role):
        state = seq.pop(0) if seq else 'chat_system'
        sent.append(f'detect->{state}')
        return detector.Classification(state=state, confidence=1.0)

    def fake_press(hwnd, key, presses=1, settle=0.6):
        sent.append(f'key:{key}')

    def fake_click_text(hwnd, query, threshold=0.7, hold_alt=False):
        sent.append(f'click:{query}{"+alt" if hold_alt else ""}')
        return True

    def fake_read(hwnd):
        return ocr.recognize_file(str(log_image))

    actions.press_key, actions.click_text = fake_press, fake_click_text
    actions.read_window_focused = fake_read
    logs: list[str] = []
    s = Session({'mainhost': RoleWindow('mainhost', 0, 'swipedgy$')}, log=logs.append)
    s.detect = fake_detect
    outcome = None
    try:
        outcome = f'returned {s._diagnose_match_failure("mainhost")!r}'
    except MatchingCooldown as exc:
        outcome = f'MatchingCooldown({exc.seconds}s)'
    except GoUnavailable:
        outcome = 'GoUnavailable (loop stops)'
    print(f'  {label}: {outcome}')
    print(f'    inputs sent: {[x for x in sent if not x.startswith("detect")]}')
    print(f'    states seen: {[x[8:] for x in sent if x.startswith("detect")]}')
    for line in logs:
        print(f'      | {line}')
    return outcome


real_press, real_click, real_read = (
    actions.press_key, actions.click_text, actions.read_window_focused,
)
try:
    # From the stuck lobby: Esc out, Enter for chat, click System, read, Esc back.
    # esc out -> enter (chat opens on World) -> click System -> read -> esc back
    from_lobby = ['team_lobby', 'overworld',                 # _return_to_overworld
                  'overworld', 'overworld_chat',             # Enter, then the poll
                  'overworld_chat',                          # -> click System
                  'chat_system',                             # verified
                  'overworld']                               # _close_chat
    from_arena = ['arena_screen', 'priority_challenge', 'priority_menu', 'overworld',
                  'overworld', 'overworld_chat',
                  'overworld_chat', 'chat_system', 'overworld']
    r1 = run_nav(from_lobby, rendered['cooldown'], 'mainhost from team_lobby, cooldown')
    r2 = run_nav(from_arena, rendered['unavailable'], 'althost from arena, unavailable')
    r3 = run_nav(from_lobby, rendered['normal'], 'from overworld, nothing relevant')
finally:
    actions.press_key, actions.click_text, actions.read_window_focused = (
        real_press, real_click, real_read,
    )

fails += (not r1.startswith('MatchingCooldown(145')) + ('GoUnavailable' not in r2) \
    + ("returned 'unknown'" != r3)
print(f'\n{"ALL PASS" if not fails else str(fails) + " FAILURE(S)"}   images: {OUT}')
sys.exit(1 if fails else 0)
