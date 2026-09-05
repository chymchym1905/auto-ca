"""Offline tests for the queue-refusal policy (steps 2 and 5).

Covers what the game tells us and what the automation does about it:
  * verdicts parsed from System-log lines (not ready / offline / timed block /
    event unavailable), including the online-vs-offline trap;
  * step 2 re-pressing **Go** in place, and step 5 re-pressing **Find matches**
    in place, instead of failing back to step 1;
  * the per-reason budgets, and the two cases that stop the loop.
"""
import sys

sys.path.insert(0, 'src')

from tof_automation import actions, detector
from tof_automation.automation import (
    GoUnavailable, MatchingCooldown, MembersBlocked, QueueCheck, RoleWindow,
    Session,
)

fails = 0
logs = []
win = {'mainhost': RoleWindow('mainhost', 0, 'swipedgy$'),
       'main': RoleWindow('main', 0, 'chymchym1905'),
       'althost': RoleWindow('althost', 0, 'Klee 1')}


def check(label, ok, detail=''):
    global fails
    fails += not ok
    print(f'  [{"PASS" if ok else "FAIL"}] {label}{"  " + detail if detail else ""}')


# ---------------------------------------------------------------- verdicts
print('=== verdicts from real log lines ===')
LIVE_NOT_READY = ['FRchymchym1905 left the team.', 'SRJinShynestragt is not ready.',
                  'SRRenazeL#a is not ready.', 'FRAurelAFI01 is not ready.']
LIVE_OFFLINE = ['SRJinShynestraR is offline.', 'SRJinShynestragt canceled matching.',
                '—rchymchym1905 left the team.', '—Rchymchyml 905 left the team.']
check('3x not ready', detector.classify_system_message(LIVE_NOT_READY) == ('member_blocked', 3),
      str(detector.classify_system_message(LIVE_NOT_READY)))
check('offline line', detector.classify_system_message(LIVE_OFFLINE) == ('member_blocked', 1),
      str(detector.classify_system_message(LIVE_OFFLINE)))
check("'is online' is NOT 'is offline'",
      detector.classify_system_message(['FRpmeimei is online.'] * 4) == ('unknown', None))
check('timed ban', detector.classify_system_message(['Matching banned for 5 minutes.']) == ('cooldown', 300))
check('cooldown', detector.classify_system_message(['Matching cooldown ends in 23s.']) == ('cooldown', 23))
check('event unavailable', detector.classify_system_message(['FREvent unavailable']) == ('unavailable', None))
check('a fresher reason wins over a blocked tail',
      detector.classify_system_message(LIVE_NOT_READY + ['FREvent unavailable']) == ('unavailable', None))


# ------------------------------------------------------------- step drivers
def make(refusals, **kw):
    """A Session whose queue press always fails until ``refusals`` is exhausted."""
    s = Session(win, log=logs.append, **kw)
    s.stop.wait = lambda t: False               # no real sleeping
    s.detect = lambda role: detector.Classification(state='team_lobby', confidence=1.0)
    s.wait_for = lambda role, target, timeout, interval=1.5: True
    s.presses = 0

    def press(hwnd, q, threshold=0.7, hold_alt=False):
        s.presses += 1
        return True

    actions.click_text = press
    s._go_button_stuck = lambda role, **kw2: QueueCheck(stuck=s.presses < refusals + 1)
    return s


print('\n=== step 2: Go is re-pressed in place ===')
# 1. blocked members, then they ready up
s = make(refusals=2, not_ready_delay=0)
s._diagnose_match_failure = lambda role, toast=None: (_ for _ in ()).throw(
    MembersBlocked(3, ['A (not ready)', 'B (offline)']))
logs.clear()
res = s.step2_mainhost_go()
check('recovers when members ready up', res is True and s.presses == 3,
      f'pressed Go {s.presses}x, returned {res}')

# 2. the full policy: 10 retries then stop the loop
s = make(refusals=999, not_ready_delay=0)
s._diagnose_match_failure = lambda role, toast=None: (_ for _ in ()).throw(
    MembersBlocked(3, ['A (offline)']))
logs.clear()
try:
    s.step2_mainhost_go()
    check('blocked budget stops the loop', False, 'no exception')
except MembersBlocked:
    check('blocked budget stops the loop', s.presses == 11,
          f'pressed Go {s.presses}x (1 + {s.not_ready_retries} retries)')

# 3. a timed block is waited out IN the step, not bounced to step 1
s = make(refusals=3, not_ready_delay=0)
s._diagnose_match_failure = lambda role, toast=None: (_ for _ in ()).throw(
    MatchingCooldown(300))
logs.clear()
res = s.step2_mainhost_go()
check('cooldown handled in place', res is True and s.presses == 4,
      f'pressed Go {s.presses}x, returned {res}')
check('  …and it never escaped to run_loop',
      not any('Stopping' in ln for ln in logs))

# 4. the reason changing mid-retry is picked up
seq = []
s = make(refusals=999, not_ready_delay=0)


def changing(role, toast=None):
    seq.append(s.presses)
    if s.presses <= 2:
        raise MembersBlocked(2, ['A (not ready)'])
    raise GoUnavailable('event closed')


s._diagnose_match_failure = changing
logs.clear()
try:
    s.step2_mainhost_go()
    check('a changed reason takes over', False, 'no exception')
except GoUnavailable:
    check('a changed reason takes over', s.presses == 3,
          f'2 blocked retries, then GoUnavailable at press {s.presses}')

# 5. an unexplained refusal gets a few quick retries, then gives up
s = make(refusals=999, not_ready_delay=0)
s._diagnose_match_failure = lambda role, toast=None: 'unknown'
logs.clear()
res = s.step2_mainhost_go()
check('unknown refusals are bounded', res is False and s.presses == 4,
      f'pressed Go {s.presses}x (1 + {s.unknown_retries} retries), returned {res}')

print('\n=== step 5: Find matches is re-pressed in place ===')
s = Session(win, log=logs.append, not_ready_delay=0)
s.stop.wait = lambda t: False
s.detect = lambda role: detector.Classification(state='arena_screen', confidence=1.0)
s._ensure_ca_selected = lambda hwnd, attempts=4: True
s.presses = 0


def press_region(hwnd, q, region, upscale=3, threshold=0.7, hold_alt=False):
    s.presses += 1
    return True


actions.click_text_in_region = press_region
s._find_match_stuck = lambda role, **kw: QueueCheck(stuck=s.presses < 3)
s._diagnose_match_failure = lambda role, toast=None: (_ for _ in ()).throw(
    MatchingCooldown(60))
logs.clear()
res = s.step5_althost_ca()
check('althost re-presses Find matches', res is True and s.presses == 3,
      f'pressed {s.presses}x, returned {res}')
for ln in logs[-4:]:
    print(f'          {ln.strip()}')

print(f'\n{"ALL PASS" if not fails else f"{fails} FAILURE(S)"}')
sys.exit(1 if fails else 0)
