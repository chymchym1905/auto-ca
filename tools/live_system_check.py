"""LIVE test: navigate each role to its real queue screen, then run ONLY the
System-log check. Never clicks Go or Find matches — nothing is queued.

  mainhost -> team lobby  (the screen it is on when Go fails)
  althost  -> Arena       (the screen it is on when Find matches fails)

For althost it also OCRs the new FIND_MATCH_REGION crop, which is what
_find_match_stuck() reads to decide whether the queue took.
"""
import sys
import time

sys.path.insert(0, 'src')

from tof_automation import actions, capturer, detector, textmatch
from tof_automation.automation import RoleWindow, Session
from tof_automation.config import Config
from tof_automation.icons import IconLibrary

cfg = Config.load()
windows = {}
for role, rc in cfg.roles.items():
    hwnd = capturer.find_window_by_pid(rc.pid, rc.window_title_hint) if rc.pid else None
    if hwnd:
        windows[role] = RoleWindow(role=role, hwnd=hwnd, username=rc.username)
    print(f'{role:9s} pid={rc.pid} hwnd={hwnd}')
for r in ('mainhost', 'althost'):
    if r not in windows:
        sys.exit(f'ABORT: {r} window not found')

session = Session(windows, points=cfg.points, icon_lib=IconLibrary().load(),
                  regions=cfg.regions, log=lambda m: print(f'    {m}', flush=True))


def probe(role: str) -> tuple:
    print(f'\n  >>> running the System-log check on {role}', flush=True)
    t0 = time.time()
    opened = session._open_system_chat(role)
    print(f'    opened={opened} state={session.detect(role).state}', flush=True)
    msgs = session._read_system_log(role)
    print(f'    {len(msgs)} log lines; newest:', flush=True)
    for m in msgs[-6:]:
        print(f'      | {m}', flush=True)
    verdict = detector.classify_system_message(msgs)
    print(f'    VERDICT: {verdict}   ({time.time() - t0:.1f}s)', flush=True)
    session._close_chat(role)
    return opened, verdict


# ---------------- mainhost: put it on the team lobby ----------------
print('\n=== mainhost: navigate to the team lobby ===', flush=True)
if session.detect('mainhost').state != 'team_lobby':
    session.open_team_lobby('mainhost')
print(f'  mainhost state: {session.detect("mainhost").state}', flush=True)
main_res = probe('mainhost')

# ---------------- althost: put it on the Arena screen ----------------
print('\n=== althost: navigate to the Arena screen (no Find matches click) ===', flush=True)
hwnd = windows['althost'].hwnd
state = session.detect('althost').state
for i in range(12):
    state = session.detect('althost').state
    print(f'  [{i}] althost: {state}', flush=True)
    if state == 'arena_screen':
        break
    if state in ('overworld_chat', 'chat_system'):
        actions.press_key(hwnd, 'esc')
    elif state == 'priority_challenge':
        m = actions.find_icon(hwnd, session.icons, 'arena_tile')
        if m:
            actions.click_client(hwnd, m.cx, m.cy)
    elif state == 'priority_menu':
        session._click_challenge_tab('althost')
    else:
        session._open_priority('althost')
    time.sleep(1.5)

if state == 'arena_screen':
    region = tuple(cfg.regions.get('find_match', detector.FIND_MATCH_REGION))
    _, crop = actions.read_window_and_region(hwnd, region, session.timer_upscale)
    text = ' | '.join(ln.text for ln in crop.lines)
    hit = textmatch.find_phrase(crop, 'Find matches', 0.7)
    print(f'  FIND_MATCH_REGION{region} OCR: {text!r}', flush=True)
    print(f'  -> "Find matches" found: {hit and round(hit.score, 2)}  '
          f'(_find_match_stuck would return {hit is not None})', flush=True)
else:
    print(f'  ! could not reach arena_screen (stuck at {state}) — probing anyway',
          flush=True)
alt_res = probe('althost')

print('\n=== summary ===')
for role, (opened, verdict) in (('mainhost', main_res), ('althost', alt_res)):
    print(f'  {role:9s} chat_opened={opened}  verdict={verdict}')
