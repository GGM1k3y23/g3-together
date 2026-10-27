"""Runner for our own mod (G3 Together) in the isolated test copies. Prints RESULT: PASS/FAIL.

    together.py --suite stage1     host copy only: bot-driven new game, mod reports the hero every second
    together.py --suite stage2     host + client: connected, puppets visible, puppet follows teleport steps and
                                   real walks with walk/run motions, no own AI, no respawn flapping, removed
                                   when the client leaves

Requires `profile.py together` in the workspace (see workspace.py). Every launched process is tracked and cleaned up; the owner's real
save folder is hashed before and after.
"""
import argparse
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import Bot, RUNS, emit, place_on_second_screen, post_key, snapshot  # noqa: E402
from workspace import WORKSPACE  # noqa: E402
from shot import screenshot  # noqa: E402
import hangstack  # noqa: E402

# Two pairs of test copies so that two agents can test in parallel (G3_PAIR=1 or 2; owner's approval 2026-10-18). Each pair
# has its own copies, port and QA lock; pair 2 is created by runtime/make_pair2.py and checked by isolation/verify_pair2.py.
PAIR = os.environ.get('G3_PAIR', '1')
assert PAIR.isdigit() and int(PAIR) >= 1, 'G3_PAIR must be 1, 2 or a further pair of runtime/make_pair.py (owner 24.10.)'
_SUFFIX = '' if PAIR == '1' else PAIR
COPIES = {'host': WORKSPACE/f'test-install{_SUFFIX}', 'client': WORKSPACE/f'test-client{_SUFFIX}'}
PORT = 34566 + int(PAIR)
# A network as over the internet (Hamachi, first human test 21.10.): G3_NETSIM="delay_ms,jitter_ms,loss_percent" puts a UDP
# relay of this runner between the client and the host; each direction is held back and thinned out, in order. Nothing is
# installed. The report gets the counts and the largest datagram (the mod caps ENet at 1200 bytes: hamachi-net-c.md).
NETSIM = os.environ.get('G3_NETSIM', '')
# Or the playbook's chaos box (coop-anything tools/chaos, read 21.10.: standard library only): G3_CHAOS=<profile> (lan,
# good_wifi, bad_wifi, mobile_tether, transatlantic, hell) and G3_CHAOS_DIR=<its folder>. Seeded bursts, reordering,
# duplicates, and outages a suite can switch on (stageNetOutage).
CHAOS_PROFILE = os.environ.get('G3_CHAOS', '')
CHAOS = None  # the running chaos.relay.Relay
NETLINK = None  # the running NetSim
RELAY_PORT = PORT + 10
RECEIPT = WORKSPACE/'runtime/myrtana-deployment.json'
OURS = 'scripts/Script_G3Together.dll'
PUPPET_TEMPLATE = 'PC_Hero'
WEAPON = 'It_1H_Sword_Rust_01'


class NetSim:
    def __init__(self, spec):
        import heapq
        import random
        import socket
        self.heapq, self.random = heapq, random
        self.delay, self.jitter, self.loss = (float(x) / d for x, d in zip(spec.split(','), (1000, 1000, 100)))
        self.from_client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.from_client.bind(('127.0.0.1', RELAY_PORT))
        self.to_host = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.to_host.bind(('127.0.0.1', 0))
        for s in (self.from_client, self.to_host):
            s.settimeout(0.5)
            try:  # Windows: an ICMP "port unreachable" (game not up yet) would fail the next recvfrom
                s.ioctl(socket.SIO_UDP_CONNRESET, False)
            except (AttributeError, OSError):
                pass
        self.client = None
        self.cut_until = {'up': 0.0, 'down': 0.0}  # outages (stageNetOutage): nothing through until then
        self.queue, self.seq, self.last_due = [], 0, {'up': 0.0, 'down': 0.0}
        self.cv = threading.Condition()
        self.stats = {d: {'packets': 0, 'dropped': 0, 'max_bytes': 0, 'over_1200': 0} for d in ('up', 'down')}
        self.running = True
        for target in (self._up, self._down, self._send):
            threading.Thread(target=target, daemon=True).start()

    def _hold(self, direction, data, sock, addr):
        stat = self.stats[direction]
        stat['packets'] += 1
        stat['max_bytes'] = max(stat['max_bytes'], len(data))
        stat['over_1200'] += len(data) > 1200
        if time.monotonic() < self.cut_until[direction] or self.random.random() < self.loss:
            stat['dropped'] += 1
            return
        with self.cv:
            due = max(time.monotonic() + self.delay + self.random.uniform(0, self.jitter), self.last_due[direction])
            self.last_due[direction] = due
            self.seq += 1
            self.heapq.heappush(self.queue, (due, self.seq, sock, data, addr))
            self.cv.notify()

    def _up(self):  # client -> host
        while self.running:
            try:
                data, self.client = self.from_client.recvfrom(65536)
            except OSError:
                continue
            self._hold('up', data, self.to_host, ('127.0.0.1', PORT))

    def _down(self):  # host -> client
        while self.running:
            try:
                data, _ = self.to_host.recvfrom(65536)
            except OSError:
                continue
            if self.client:
                self._hold('down', data, self.from_client, self.client)

    def _send(self):
        while self.running:
            with self.cv:
                while self.running and (not self.queue or self.queue[0][0] > time.monotonic()):
                    self.cv.wait(max(0.001, self.queue[0][0] - time.monotonic()) if self.queue else 0.5)
                if not self.running:
                    return
                _, _, sock, data, addr = self.heapq.heappop(self.queue)
            try:
                sock.sendto(data, addr)
            except OSError:
                pass

    def cut(self, direction, seconds):
        """Nothing through for `seconds` (inf: for good); direction up (client to host), down or both."""
        for d in (('up', 'down') if direction == 'both' else (direction,)):
            self.cut_until[d] = time.monotonic() + seconds

    def stop(self):
        self.running = False
        with self.cv:
            self.cv.notify_all()
        for s in (self.from_client, self.to_host):
            s.close()
        return self.stats


class LaunchLock:
    """One Gothic start at a time on this PC, across all QA runners (named mutex; Windows frees it if a runner dies)."""

    def __init__(self, seconds=600):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel = kernel
        self.handle = kernel.CreateMutexW(None, False, r'Local\CoopAnything_Gothic3QA_Launch')
        self.held = bool(self.handle) and kernel.WaitForSingleObject(self.handle, int(seconds * 1000)) in (0, 0x80)  # signaled or abandoned

    def release(self):
        if self.handle:
            if self.held:
                self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle)
            self.handle, self.held = None, False


class RaidSlot:
    """At most G3_RAID_SLOTS (default 2) pairs play the opening raid at once on this PC: six games in it at once (three pairs)
    once made the display driver reset (24.10. 18:16; not seen again, owner 25.10.). One named mutex per running raid; Windows
    frees it if its runner dies. Taken before a run that starts a new game, given back once the raid is cleared or the run ends."""

    def __init__(self):
        self.handles, self.held = [], None

    def take(self, report, seconds=3600):
        if self.held:
            return
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.WaitForMultipleObjects.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_bool, ctypes.c_uint32]
        kernel.WaitForMultipleObjects.restype = ctypes.c_uint32
        count = max(1, int(os.environ.get('G3_RAID_SLOTS', '2')))
        self.handles = [kernel.CreateMutexW(None, False, rf'Local\CoopAnything_Gothic3QA_Raid{i}') for i in range(count)]
        began = time.monotonic()
        result = kernel.WaitForMultipleObjects(count, (ctypes.c_void_p * count)(*self.handles), False, int(seconds * 1000))
        index = result if result < count else result - 0x80 if 0x80 <= result < 0x80 + count else None
        report['raid_slot'] = {'slot': index, 'waited_s': round(time.monotonic() - began)}
        if index is None:
            self.release()
            raise RuntimeError(f'No raid slot free after {seconds} s (WaitForMultipleObjects {result:#x})')
        self.held = self.handles[index]

    def release(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        if self.held:
            kernel.ReleaseMutex(self.held)
        for handle in self.handles:
            if handle:
                kernel.CloseHandle(handle)
        self.handles, self.held = [], None


RAID = RaidSlot()


def driver_resets(seconds):
    """Display driver resets (System event 4101) in the last `seconds`, as UTC times; None if the log cannot be read.
    25.10.: 17 resets since 21.10., none in 28.6 h with one pair running, 8 in 17.4 h with two, 2 in 1.7 h with three; the
    games then hang in d3d9 at their end and the run turns red without a fault of the mod."""
    query = f'*[System[(EventID=4101) and TimeCreated[timediff(@SystemTime) <= {int(seconds * 1000)}]]]'
    try:
        out = subprocess.run(['wevtutil', 'qe', 'System', '/q:' + query, '/f:xml', '/c:20'], capture_output=True, text=True,
                             errors='replace', timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return re.findall(r"SystemTime='([^']+)'", out)


class Copy:
    def __init__(self, tag, run, deadline, report):
        self.tag, self.root, self.run, self.deadline, self.report = tag, COPIES[tag], run, deadline, report
        self.process = None
        self.bot = None
        self.ini_extra = ''  # INI lines of this copy only (stageWear: the client's own look)

    def launch(self, role='host', name='Host'):
        (self.root/'g3together.ini').write_text(
            f'role = {role}\naddress = 127.0.0.1\nport = {RELAY_PORT if (NETSIM or CHAOS_PROFILE) and role == "client" else PORT}\nname = {name}\n'
            f'puppet_template = {PUPPET_TEMPLATE}\n'
            + ''.join(f'{line}\n' for line in os.environ.get('G3_INI_EXTRA', '').split(';') if line) + self.ini_extra)
        (self.root/'qa-enable.txt').write_text('Enabled only for this bounded QA run\n')
        # Gothic 3 allows one running game per PC (named mutex GenomeMutex); a game that finds it exits at once with
        # code 0. With two pairs of test copies (two agents) any game may be the one holding it, so every game
        # releases it as soon as its QA hook answers, and a start that exits early is retried after a random pause.
        # With three pairs side by side (24.10.) the starts kept colliding (a client: six starts, all at once gone): every
        # runner of this PC starts its games one at a time, under one launch lock held until the game let go of the mutex.
        import random
        launch_lock = LaunchLock()
        for attempt in range(6):
            for name in ('g3together.log', 'qa-engine.log', 'g3trace.log'):
                (self.root/name).unlink(missing_ok=True)
            (self.root/'qa-command.txt').write_text('1 state\n')
            self.process = subprocess.Popen([str(self.root/'Gothic3.exe')], cwd=self.root)
            until = time.monotonic() + 120
            ready = False
            while time.monotonic() < until and self.process.poll() is None:
                log = self.root/'qa-engine.log'
                if log.exists() and 'COMMAND sequence=1 ' in log.read_text(errors='replace'):
                    ready = True
                    break
                time.sleep(0.5)
            if ready:
                break
            if self.process.poll() is None:
                break  # still starting (slow PC): let the suite's own timeouts decide
            self.report.setdefault('launch_retries', {}).setdefault(self.tag, 0)
            self.report['launch_retries'][self.tag] += 1
            time.sleep(random.uniform(3, 12))
        place_on_second_screen(self.process.pid, 0 if self.tag == 'host' else 1)
        self.bot = Bot(self.process, self.run, self.deadline, root=self.root, name=self.tag)
        try:
            if ready:
                start = self.bot.send('releaselock')
                self.bot.line('LOCK scan complete', 10, 'lock scan', start)
        finally:
            launch_lock.release()
        self.report['processes'][self.tag+'_pid'] = self.process.pid
        emit({'event': self.tag+'-started', 'pid': self.process.pid})
        return self

    last_change = 0.0
    _last_size = -1

    def hero_reports(self):
        """Per-second mod reports with the hero in the campaign world; tracks when the log last grew."""
        text = self.mod_log()
        if len(text) != self._last_size:
            self._last_size, self.last_change = len(text), time.monotonic()
        return [l for l in text.splitlines() if 'world=G3_World_01 running=1 hero=PC_Hero pos=' in l]

    @staticmethod
    def fps(reports):
        frames = [int(re.search(r'frame=(\d+)', l).group(1)) for l in reports[-2:]]
        return frames[-1]-frames[0] if len(frames) == 2 else 0

    def mod_log(self):
        path = self.root/'g3together.log'
        return path.read_text(errors='replace') if path.exists() else ''

    def collect(self):
        out = self.run/self.tag
        out.mkdir(exist_ok=True)
        for name in ('g3together.log', 'qa-engine.log', 'qa-command.txt', 'g3trace.log'):
            if (self.root/name).is_file():
                shutil.copy2(self.root/name, out/name)
        for source in (self.root/'isolated-profile').rglob('*.log'):
            target = out/'profile-logs'/source.relative_to(self.root/'isolated-profile')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        (self.root/'qa-command.txt').write_text('1 state\n')
        (self.root/'qa-enable.txt').unlink(missing_ok=True)


def state(bot):
    start = bot.send('state', 20)
    return bot.line('STATE ', 10, 'state', start)


def enter_world(copy, startup=60):
    """Startup menu to the hero in G3_World_01, like a player: New Game, Yes, skip the intro."""
    bot = copy.bot
    folder = 'save_folder='+str(copy.root/'isolated-profile/Documents/gothic3')+'\\'
    menu = bot.line('STATE running=0 paused=1 save_allowed=0 world=G3_StartUp hero=none ', startup, 'startup menu', 0)
    assert folder in menu, 'Save folder is not the isolated profile: '+menu
    if copy.tag == 'client':  # before the world: its first look comes within a second of it
        address_default(copy)
    start = bot.send('focusnew')
    assert bot.line('MENU_FOCUS id=20571 ', 10, 'New Game focus', start).startswith('MENU_FOCUS id=20571 focused=1')
    start = bot.send('pressenter')
    assert 'queued=1' in bot.line('PRESSENTER ', 10, 'Enter on New Game', start)
    for attempt in range(10):
        time.sleep(1)
        start = bot.send('focusyes')
        if bot.wait(lambda t: 'MENU_FOCUS id=20711 focused=1' in t and 'ok' or 'REFUSED' in t and 'no', 15, 'Yes focus', start) == 'ok':
            break
    else:
        raise RuntimeError('Confirmation dialog never appeared')
    start = bot.send('pressenter')
    assert 'queued=1' in bot.line('PRESSENTER ', 10, 'Enter on Yes', start)
    # The intro video (214 s) starts only after the world is active and blocks the engine loop: the
    # mod's per-second report stops. Escape is posted to this window only once that stall is seen.
    copy.report[copy.tag+'_intro_escapes'] = 0
    began = time.monotonic()
    while time.monotonic()-began < 300:
        time.sleep(1)
        reports = copy.hero_reports()
        if len(reports) < 2:
            continue
        stalled = time.monotonic()-copy.last_change > 3
        if stalled and copy.report[copy.tag+'_intro_escapes'] < 3:
            copy.report[copy.tag+'_intro_escapes'] += post_key(copy.process.pid, 0x1B)
            copy.last_change = time.monotonic()
        if copy.fps(reports) >= 30 and len(reports) >= 5:
            break
    else:
        raise RuntimeError('Game never ran smoothly in G3_World_01')
    current = state(bot)
    assert current.startswith('STATE running=1 paused=0 ') and 'world=G3_World_01 hero=PC_Hero ' in current, current
    assert folder in current, 'Campaign save folder is not isolated: '+current
    return current


def stage1(copies, report, negative=False):
    host = copies['host'].launch()
    enter_world(host)
    time.sleep(5)
    lines = host.hero_reports()[-5:]
    frames = [int(re.search(r'frame=(\d+)', l).group(1)) for l in lines]
    rates = [b-a for a, b in zip(frames, frames[1:])]
    assert len(lines) == 5 and min(rates) >= 30, f'Game not running smoothly in the world: {rates} frames/s'
    report['hero_reports'] = len(host.hero_reports())
    report['last_report'] = lines[-1].split(' ', 1)[1]
    report['frames_per_second'] = rates
    expected = 'G3_ImpossibleNegativeControl' if negative else 'G3_World_01'
    assert f'world={expected} ' in lines[-1], 'Unexpected world in '+lines[-1]


def remotes(bot):
    """Parse the bot's REMOTE lines: {id: {'pos': (x, y, z), 'puppet': bool, 'puppet_pos': (x, y, z), ...}}."""
    start = bot.send('remotes', 20)
    bot.line('REMOTES end', 10, 'remotes', start)
    found = {}
    for line in bot.log()[start:].splitlines():
        m = re.match(r'REMOTE id=(\d+) name=(\S+) world=\w+ same_world=(\d) age_ms=(\d+) pos=([-\d.,]+) puppet=(\d) puppet_pos=([-\d.,]+)', line)
        if m:
            found[int(m[1])] = {'name': m[2], 'same_world': m[3] == '1', 'age_ms': int(m[4]),
                                'pos': tuple(map(float, m[5].split(','))), 'puppet': m[6] == '1',
                                'puppet_pos': tuple(map(float, m[7].split(',')))}
    return found


def distance(a, b):
    return sum((x-y)**2 for x, y in zip(a, b))**0.5


def wait_for(predicate, seconds, what):
    until = time.monotonic()+seconds
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(1)
    raise AssertionError('Timed out: '+what)


def stage2(copies, report, negative=False):
    host = copies['host'].launch('host', 'Host')
    enter_world(host)
    start = host.bot.send('releaselock')
    host.bot.line('LOCK scan complete', 10, 'lock scan', start)
    client = copies['client'].launch('client', 'Client')
    enter_world(client, startup=120)
    # 1. Connected, and each side shows the other as a puppet in the same world.
    wait_for(lambda: remotes(host.bot).get(2, {}).get('puppet'), 30, 'host shows a puppet for the client')
    wait_for(lambda: remotes(client.bot).get(1, {}).get('puppet'), 30, 'client shows a puppet for the host')
    report['puppets_both_sides'] = True
    # The game must still resolve the real player, also when puppets use the hero's template.
    for copy in (host, client):
        check = anim_lines(copy.bot, 'PLAYER_CHECK')
        assert check and check[-1] == 'PLAYER_CHECK getplayer_is_hero=1 by_name_is_hero=1', f'{copy.tag}: {check}'
    report['real_player_resolved'] = True
    # 2. The client walks a 3 m circle in eight steps; the host's puppet must follow each step.
    tolerance = 50.0  # world units (cm)
    errors = []
    for index in range(8):
        start = client.bot.send(f'step {index}')
        line = client.bot.line('STEP ', 10, 'step', start)
        actual = tuple(map(float, re.search(r' pos=([-\d.,]+)', line)[1].split(',')))
        # Compared with where the client's hero is now, not where the step put it: raiders' hits push it on (20.10.: the
        # puppet followed the hero exactly, 1.3 m from the step's position, and the test timed out).
        def follows():
            now = tuple(map(float, re.search(r' pos=([-\d.,]+)', report_line(client.bot, 'heropos', 'HEROPOS '))[1].split(',')))
            expected = tuple(a+(1000.0 if negative else 0.0) for a in now)
            seen = remotes(host.bot).get(2, {})
            return seen.get('puppet') and distance(seen['puppet_pos'], expected) <= tolerance and dict(seen, now=now)
        seen = wait_for(follows, 10, f'host puppet reaches step {index} within {tolerance} units')
        errors.append(round(distance(seen['puppet_pos'], seen['now']), 1))
    report['circle_errors'] = errors
    # 3. The client really walks (game AI goto, 15 m legs); the puppet must walk too, never run its own
    #    AI, and stand within 60 cm of the player once the player stops.
    mark = len(host.mod_log())
    walk_errors = []
    for index in range(4):
        start = client.bot.send(f'walkto {index}')
        assert 'started=1' in client.bot.line('WALKTO ', 10, 'walkto', start)
        time.sleep(6)
        def settled():
            seen = remotes(host.bot).get(2, {})
            return seen.get('puppet') and distance(seen['puppet_pos'], seen['pos']) <= (0.0 if negative else 60.0) and seen
        seen = wait_for(settled, 8, f'puppet stands within 60 cm of the player after walk {index}')
        walk_errors.append(round(distance(seen['puppet_pos'], seen['pos']), 1))
    report['walk_final_errors'] = walk_errors
    trace = [l for l in host.mod_log()[mark:].splitlines() if 'TRACE puppet2 ' in l]
    motions = [re.search(r' motion=(\S+)', l)[1] for l in trace]
    walking = [m for m in motions if re.search(r'_Move_(Walk|Run|Sprint)_', m)]
    tasks = {re.search(r' task=(\S*)', l)[1] for l in trace}
    report['puppet_walk_samples'] = len(walking)
    report['puppet_tasks'] = sorted(tasks)
    report['continue_routine'] = report_line(host.bot, 'continueroutine', 'CONTINUEROUTINE ')  # the fix at work (20.10.)
    assert len(walking) >= 10, f'Puppet showed a walk/run motion in only {len(walking)} of {len(trace)} samples'
    assert tasks <= {'ZS_HangAround_DoNothing', ''}, f'Puppet ran its own AI: {sorted(tasks)}'
    spawns = host.mod_log().count('remote: puppet 2 spawned')
    report['puppet_spawns'] = spawns
    assert spawns == 1, f'Puppet was spawned {spawns} times (flapping)'
    # 4. The client leaves; its puppet disappears on the host.
    report['client_closed_normally'] = close(client)
    wait_for(lambda: not remotes(host.bot).get(2, {}).get('puppet', False), 15, 'host removes the puppet of a client that left')
    report['puppet_removed_on_leave'] = True


# Owner 22.10.: tests faster, quality the same. Stages that do not need the opening raid start from a save made right after
# it (fixture `postraidsave`): the host loads it from the menu, the client joins and loads the host's copy the way a player
# does (autoload). Stages that look at the raid itself (its orcs, kills, corpses or plunder) start a new game as before.
POSTRAID_SLOT = 'G3T_PostRaid'
POSTRAID_FIXTURE = WORKSPACE/f'runtime/fixtures/postraid-p{PAIR}'
LIVE_RAID = {'stageLiberate', 'animalscandiag', 'stageRaid', 'stageControl', 'stageNpcSpell', 'stagePoison', 'syncdiag', 'raiddiag', 'aggrodiag', 'soak',
             'stage3c', 'stage5b', 'chestdiag', 'stageChest', 'quicklootdiag', 'stageQuickLoot', 'stageU1', 'stageReloadLoot', 'postraidsave'}
# Stages that need the host in a new game nobody saved (stageS3: from the post-raid save the client's "own" save was made in
# the host's campaign, so the host rightly let it in; regression 24.10.).
NEW_GAME = {'stageS3'}
POSTRAID = False      # this run starts from the post-raid save (main, session.py)
RAID_FROM_SAVE = False  # set by join_both when it did


def postraid_ready():
    return (POSTRAID_FIXTURE/'meta.json').is_file() and os.environ.get('G3_POSTRAID', '1') != '0'


def postraid_for(name):
    """May this stage start from the post-raid save? Only one that clears the raid itself (so it wants the world after it)
    and does not look at the raid (LIVE_RAID): a stage without clear_raid uses the raid's orcs (stageA2 needs a living
    one to knock the hero out; it failed from the save, 24.10.)."""
    import inspect
    return name not in LIVE_RAID and name not in NEW_GAME and 'clear_raid(' in inspect.getsource(SUITES[name]) and postraid_ready()


def join_from_save(copies, host_before=None):
    """join_both from the post-raid save: fixture files into both isolated profiles, the host loads the save from its menu,
    the client joins from its menu and loads the host's copy; if it does not within 60 s, it starts a new game and is
    loaded from there (as stageS2 shows it works)."""
    global RAID_FROM_SAVE
    clear_slot(copies, POSTRAID_SLOT)
    for tag in ('host', 'client'):
        folder = copies[tag].root/'isolated-profile/Documents/gothic3'
        for item in (POSTRAID_FIXTURE/tag).iterdir():
            shutil.copy2(item, folder/item.name)
    host = copies['host'].launch('host', 'Host')
    host.bot.line('STATE running=0 paused=1 save_allowed=0 world=G3_StartUp hero=none ', 60, 'host in the startup menu', 0)
    start = host.bot.send('releaselock')
    host.bot.line('LOCK scan complete', 10, 'lock scan', start)
    start = host.bot.send(f'nativeload {POSTRAID_SLOT}', 60)
    load = host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''), 120, 'nativeload', start)
    assert 'ok=1' in load, load
    wait_running(host, 180, 'host world running after loading the post-raid save')
    current = state(host.bot)
    assert 'world=G3_World_01 hero=PC_Hero ' in current, current
    if host_before:
        report_line(host.bot, *host_before)
    client = copies['client'].launch('client', 'Client')
    client.bot.line('STATE running=0 paused=1 save_allowed=0 world=G3_StartUp hero=none ', 120, 'client in the startup menu', 0)
    address_default(client)
    copies['client'].report['client_postraid_from_menu'] = True
    try:
        wait_mod_log(client, r'persist: loading G3T_host_'+POSTRAID_SLOT+r'\b', 60, "the client loads the host's save from its menu")
    except AssertionError:
        copies['client'].report['client_postraid_from_menu'] = False
        since = len(client.mod_log())
        enter_world(client, startup=5)
        wait_mod_log(client, r'persist: loading G3T_host_'+POSTRAID_SLOT+r'\b', 120, "the client loads the host's save", since)
    wait_running(client, 180, "client world running in the host's save")
    current = state(client.bot)
    folder = 'save_folder='+str(client.root/'isolated-profile/Documents/gothic3')+'\\'
    assert folder in current and 'world=G3_World_01 hero=PC_Hero ' in current, current
    wait_for(lambda: remotes(host.bot).get(2, {}).get('puppet'), 60, 'host shows a puppet for the client')
    RAID_FROM_SAVE = True
    copies['host'].report['postraid'] = True
    return host, client


def address_default(client):
    """NPCs with an important info walk up to the client and it talks to them (address.cpp, 25.10.), as the game does for
    its own player. After the raid Hamlar has one for each hero and drew the client's bot into a dialog in the middle of
    other stages (regression 25.10.: QuickLoot, V4, P2hit and the quest stages red). Off unless G3_ADDRESS=1; stageAddressed
    turns it on itself."""
    if os.environ.get('G3_ADDRESS') != '1':
        report_line(client.bot, 'addressoff', 'ADDRESSOFF ')


NATIVE_WATCHED = []  # G3_NATIVEWATCH: both machines' bots, counted at the end of the run (report['native'])


def join_both(copies, host_before=None):
    """host_before: (command, reply prefix) the host runs before the client joins (a negative control that must be in
    place before the client is first close to anything). G3_NATIVEWATCH=A,B: every call of those scripts at their code
    is counted on both machines from here on (QA nativewatch), the counts in report['native'] at the end; G3_AICOUNT=prefix:
    the AI states and functions with that prefix (QA aicount), in report['aicount']."""
    host, client = join_both_plain(copies, host_before)
    names = [n for n in os.environ.get('G3_NATIVEWATCH', '').split(',') if n]
    prefix = os.environ.get('G3_AICOUNT', '')
    for c in (host, client):
        for n in names:
            report_line(c.bot, 'nativewatch ' + n, 'NATIVEWATCH ')
        if prefix:
            report_line(c.bot, 'aicount ' + prefix, ('AICOUNT end',))
        if names or prefix:
            NATIVE_WATCHED.append(c)
    return host, client


def join_both_plain(copies, host_before=None):
    global RAID_FROM_SAVE
    RAID_FROM_SAVE = False
    if POSTRAID:
        return join_from_save(copies, host_before)
    host = copies['host'].launch('host', 'Host')
    enter_world(host)
    start = host.bot.send('releaselock')
    host.bot.line('LOCK scan complete', 10, 'lock scan', start)
    if host_before:
        report_line(host.bot, *host_before)
    client = copies['client'].launch('client', 'Client')
    enter_world(client, startup=120)
    wait_for(lambda: remotes(host.bot).get(2, {}).get('puppet'), 30, 'host shows a puppet for the client')
    return host, client


def anim_lines(bot, prefix):
    start = bot.send('remotes', 20)
    bot.line('REMOTES end', 10, 'remotes', start)
    return [l for l in bot.log()[start:].splitlines() if l.startswith(prefix)]


def animdiag(copies, report, negative=False):
    """Diagnostic: the client runs (game AI goto) to four points; record animation names on both sides."""
    host, client = join_both(copies)
    samples = []
    for index in range(4):
        start = client.bot.send(f'walkto {index}')
        samples.append(client.bot.line('WALKTO ', 10, 'walkto', start))
        for _ in range(4):
            time.sleep(1)
            samples += anim_lines(client.bot, 'LOCAL_ANIM') + anim_lines(client.bot, 'MOTIONS who=hero')
            samples += anim_lines(host.bot, 'ANIM id=2') + anim_lines(host.bot, 'MOTIONS who=puppet2')
    report['samples'] = samples
    report['status_note'] = 'diagnostic only'


def npcs(bot):
    """Parse the bot's NPC lines: {guid: {'name', 'pos', 'hp', 'dead', 'mirrored', 'task'}}."""
    start = bot.send('npcs', 20)
    bot.line('NPCS end', 10, 'npcs', start)
    found = {}
    for line in bot.log()[start:].splitlines():
        m = re.match(r'NPC guid=(\S+) name=(\S+) pos=([-\d.,]+) hp=(-?\d+) dead=(\d) mirrored=(\d) task=(\S*) repacified=(\d+) last_foreign=(\S+)(?: owner=(\d+))?', line)
        if m:
            found[m[1]] = {'name': m[2], 'pos': tuple(map(float, m[3].split(','))), 'hp': int(m[4]),
                           'dead': m[5] == '1', 'mirrored': m[6] == '1', 'task': m[7], 'repacified': int(m[8]), 'last_foreign': m[9],
                           'owner': int(m[10]) if m[10] else 0}
    return found


def stage3a(copies, report, negative=False):
    """NPCs with host authority: the client shows the host's NPCs at the host's positions, dead when dead."""
    host, client = join_both(copies)
    time.sleep(10)
    samples = []
    for _ in range(10):
        samples.append((npcs(host.bot), npcs(client.bot)))
        time.sleep(2)
    host_npcs, client_npcs = samples[-1]
    common = sorted(set(host_npcs) & set(client_npcs))
    report['npcs_host'] = len(host_npcs)
    report['npcs_client'] = len(client_npcs)
    report['npcs_common'] = len(common)
    assert len(common) >= 5, f'Only {len(common)} NPCs seen on both sides'
    alive = [g for g in common if not host_npcs[g]['dead']]
    # Since stage E1 an NPC the client started simulating first (beyond the host's 40 m) stays the client's and the
    # host mirrors it; simulated on both machines (mirrored on neither) is the error.
    client_owned = [g for g in alive if host_npcs[g]['mirrored'] and host_npcs[g]['owner'] == 2]
    report['client_owned'] = sorted(host_npcs[g]['name'] for g in client_owned)
    unmirrored = [client_npcs[g]['name'] for g in alive if not client_npcs[g]['mirrored'] and g not in client_owned]
    assert not unmirrored, f'Client NPCs not mirrored: {unmirrored}'
    # The game's AI may pick a task between our per-frame correction and the bot's query; that single frame
    # is undone at once. A foreign task seen in two consecutive samples (2 s apart) means the AI kept control.
    def foreign(sample):
        return {g for g, n in sample[1].items() if n['mirrored'] and not n['dead'] and n['task'] != 'ZS_HangAround_DoNothing'}
    kept = set()
    for a, b in zip(samples, samples[1:]):
        kept |= foreign(a) & foreign(b)
    assert not kept, f'Mirrored NPCs kept their own AI on the client: {sorted(client_npcs.get(g, {}).get("name", g) for g in kept)}'
    report['repacified_max'] = max((client_npcs[g]['repacified'] for g in alive), default=0)
    errors = sorted(distance(host_npcs[g]['pos'], client_npcs[g]['pos']) for g in alive)
    report['position_errors_cm'] = [round(e) for e in errors]
    median = errors[len(errors)//2] if errors else 0
    limit = 0.001 if negative else 150.0
    assert median <= limit, f'Median NPC position error {median:.0f} cm'
    assert sum(e <= 300 for e in errors) >= 0.8*len(errors), 'Fewer than 80% of NPCs within 3 m'
    # Every NPC dead on the host in the second-to-last sample is dead on the client in the last one.
    dead_before = {g for g, n in samples[-2][0].items() if n['dead']}
    missing = [client_npcs[g]['name'] for g in dead_before if g in client_npcs and not client_npcs[g]['dead']]
    report['host_dead'] = sorted(host_npcs[g]['name'] for g in host_npcs if host_npcs[g]['dead'])
    assert not missing, f'Dead on host but alive on client: {missing}'
    # The AI-state wrapper runs for every NPC on both sides: it must always find the state being run
    # (unknown == 0), and on the client it must actually keep mirrored NPCs out of the game's AI.
    for copy in (host, client):
        net_line = anim_lines(copy.bot, 'NET ')[-1]
        counts = {k: int(v) for k, v in re.findall(r'(wrapped|skipped|unknown|blocked)=(\d+)', net_line)}
        report[copy.tag+'_ai_counts'] = counts
        assert counts.get('wrapped', 0) > 0, f'{copy.tag}: AI states not wrapped'
        assert counts.get('unknown', 1) == 0, f'{copy.tag}: AI wrapper met unknown states: {net_line}'
    assert report['client_ai_counts'].get('skipped', 0) > 0, 'Client never kept a mirrored NPC out of the game AI'


def scriptdiag(copies, report, negative=False):
    """Diagnostic, host only: where the game's damage-related script functions are registered."""
    host = copies['host'].launch()
    enter_world(host)
    lines = []
    names = [n for n in os.environ.get('G3_NAMES', '').split(',') if n]
    for name in names or ('AssessTalk', 'PlayerOpenDialog', 'StartSayRefuseTalk', 'OnDamage', 'AddHitPoints', 'DoLogicalDamage',
                 'OnItemReceived', 'OnExitProcessingRange', 'CanReceivePerceptionFrom', 'AssessFightSound', 'IsDeadlyDamage'):
        start = host.bot.send(f'scriptinfo {name}')
        lines.append(host.bot.line('SCRIPTINFO ', 10, 'scriptinfo', start))
    report['scriptinfo'] = lines


def scriptdumpdiag(copies, report, negative=False):
    """Inventory (owner, 20.10.), host only: every script, AI callback, state, function and routine the game registered,
    with module, RVA and source file, copied into the run folder as scripts.tsv."""
    host = copies['host'].launch()
    enter_world(host)
    start = host.bot.send('scriptdump', 30)
    report['scriptdump'] = host.bot.line('SCRIPTDUMP ', 30, 'scriptdump', start)
    shutil.move(host.root/'scripts.tsv', host.run/'scripts.tsv')  # the install must hold only its own files (isolation check)


def murder_counts(bot, town):
    counts = enclave_counts(bot, town)
    return counts[4] if counts else None


def use_field(line, key):
    m = re.search(rf' {key}=(\S+)', line)
    return m[1] if m else None


def quest_status(bot, quest):
    line = report_line(bot, 'quest ' + quest, 'QUEST ')
    m = re.search(r' status=(-?\d+)', line)
    return int(m[1]) if m else None


def stageEnterPlace(copies, report, negative=False):
    """Quest type 8, enter a place (8 quests, Kapitel-Checkliste): one player reaching the destination fulfils the quest for
    both. From the post-raid save the main quest Xardas_FindXardas ("Find Xardas!", destination Xardas_Tower) is running;
    the client alone goes to Xardas and walks about there (the area's trigger fires on movement, a teleport alone did not
    on 24.10.): on both machines the quest is a success, and both earn its experience (owner 21.10.: quest rewards for
    everyone). G3_WHO=host: the host goes (positive control of the game's own trigger). G3_QUEST / G3_PLACE_AT pick another
    quest and an NPC at its destination. Negative control: storyoff on both (the other one never hears of it)."""
    quest = os.environ.get('G3_QUEST', 'Xardas_FindXardas')
    spot = os.environ.get('G3_PLACE_AT', 'Xardas')
    host, client = join_both(copies)
    clear_raid(host, report)
    goer = host if os.environ.get('G3_WHO') == 'host' else client
    other = client if goer is host else host
    report['goer'] = goer.tag
    if negative:
        report['negative'] = [report_line(c.bot, 'storyoff', 'STORY') for c in (host, client)]
    before = report['before'] = {c.tag: quest_status(c.bot, quest) for c in (host, client)}
    assert before == {'host': 1, 'client': 1}, f'{quest} is not running on both: {before}'
    xp = {c.tag: xp_of(c.bot) for c in (host, client)}
    report['teleport'] = teleport_to(goer.bot, spot)
    time.sleep(3)
    report['walks'] = []
    done = False
    for index in (0, 2, 4, 6, 1, 5):
        report['walks'].append(report_line(goer.bot, f'walkto {index}', ('WALKTO ', 'REFUSED walkto')))
        until = time.monotonic() + 7
        while time.monotonic() < until and not done:
            done = quest_status(goer.bot, quest) == 2
            time.sleep(1)
        if done:
            break
    try:
        assert done, f'{goer.tag}: {quest} not fulfilled at {spot}'
        wait_for(lambda: quest_status(other.bot, quest) == 2, 20, f'the {other.tag} has {quest} fulfilled too')
    finally:
        report['after'] = {c.tag: quest_status(c.bot, quest) for c in (host, client)}
        report['xp'] = {c.tag: (xp[c.tag], xp_of(c.bot)) for c in (host, client)}
    assert all(a < b for a, b in report['xp'].values()), f"not both earned the quest's experience: {report['xp']}"


def stageArtifactFound(copies, report, negative=False):
    """Quest type 0 with the hero as destination (the main story's artefacts, Kapitel-Checkliste): the quest is fulfilled
    when the hero holds the item. The host starts PC_Hero_FindAmulet ("The divine artefact of Trelis"), it runs on both;
    the client alone comes by It_Artefact_Amulet: the quest is a success on both machines, and both earn its experience.
    G3_QUEST / G3_ITEM pick another. Negative control: storyoff on both."""
    quest = os.environ.get('G3_QUEST', 'PC_Hero_FindAmulet')
    item = os.environ.get('G3_ITEM', 'It_Artefact_Amulet')
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report['negative'] = [report_line(c.bot, 'storyoff', 'STORY') for c in (host, client)]
    report['run'] = report_line(host.bot, 'runquest ' + quest, 'QUEST ')
    if not negative:
        wait_for(lambda: quest_status(client.bot, quest) == 1, 20, f'the client has {quest} running too')
    else:
        report_line(client.bot, 'runquest ' + quest, 'QUEST ')
    report['before'] = {c.tag: quest_status(c.bot, quest) for c in (host, client)}
    xp = {c.tag: xp_of(c.bot) for c in (host, client)}
    report['found'] = report_line(client.bot, 'receiveitem ' + item, ('RECEIVEITEM ', 'REFUSED receiveitem'))  # as a pickup: OnReceiveItem
    try:
        wait_for(lambda: quest_status(client.bot, quest) == 2, 30, f'the client fulfils {quest}')
        wait_for(lambda: quest_status(host.bot, quest) == 2, 20, f'the host has {quest} fulfilled too')
    finally:
        report['after'] = {c.tag: quest_status(c.bot, quest) for c in (host, client)}
        report['xp'] = {c.tag: (xp[c.tag], xp_of(c.bot)) for c in (host, client)}
    assert all(a < b for a, b in report['xp'].values()), f"not both earned the quest's experience: {report['xp']}"


def story_view(c, quests, events, sectors):
    """One machine's main-story state: quest statuses, known events, sectors, the hero's chapter and experience."""
    view = {'quests': {q: quest_status(c.bot, q) for q in quests}}
    view['events'] = {e: report_line(c.bot, 'event ' + e, 'EVENT ').split('known=')[-1][:1] for e in events}
    view['sectors'] = {s: report_line(c.bot, 'sector ' + s, 'SECTOR ').split('on=')[-1][:1] for s in sectors}
    view['chapter'] = report_line(c.bot, 'chapter', 'CHAPTER ').split('value=')[-1]
    view['xp'] = xp_of(c.bot)
    return view


def talk_choose(copy, owner, infos, report, tag):
    """One talk with `owner` (an NPC or a dialog object such as Innos_Tester): the infos picked in that order where offered."""
    teleport_to(copy.bot, owner)
    time.sleep(3)
    line, offered = talk_infos(copy, owner)
    report[tag] = {'talk': line[:300], 'chosen': []}
    for info in infos:
        if info not in offered:  # a child offered only after its parent: look again
            now = report_line(copy.bot, 'talkstate', 'TALKSTATE')
            report[tag]['chosen'].append(f'{info} not offered at the start; {now[:100]}')
            if 'running=1' not in now:
                continue
        report[tag]['chosen'].append(report_line(copy.bot, 'talkinfo ' + info, ('TALKINFO', 'REFUSED talkinfo'))[:80])
        for _ in range(25):  # its lines play out
            time.sleep(2)
            if f'current={info} ' not in report_line(copy.bot, 'talkstate', 'TALKSTATE'):
                break
    report[tag]['end'] = report_line(copy.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))


def stageStoryInnos(copies, report, negative=False):
    """P4 main story (Kapitel-Checkliste: Hauptstory), the client alone on Innos' way: the host runs the artefact quests, the
    client finds the five artefacts and the scepter, chooses Innos at Innos_Tester (Ready info FILLER34: RunQuest Innos_Contact,
    Innos_chosen, a sector; its child, the Delivery FILLER36: four quests closed, the artefacts given, SetChapter 2); then the
    two Innos quests succeed (QA) and the client takes the rewards and the epilogue's start (SKALVERAM3188, 3196, 3210). After
    each step the story is the same on both machines: quests, events, the sector and the heroes' chapter (before, the host's
    hero stayed in chapter 1, run 221246). G3_WHO=host: the host walks the way (single-player control). The epilogue itself
    (the hero's own infos, EndGame) is only looked at: the infos each hero is offered. Negative control: chaptersyncoff on
    both."""
    # FILLER34 wants the scepter of Varant as well (CondItems, Infos.pak; run 215102 offered only FILLER38 without it).
    artefacts = ['It_Artefact_Amulet', 'It_Artefact_Crown', 'It_Artefact_Ring_01', 'It_Artefact_Ring_02', 'It_Artefact_Robe', 'It_Scepter']
    finds = ['PC_Hero_FindAmulet', 'PC_Hero_FindCrown', 'PC_Hero_FindRing_01', 'PC_Hero_FindRing_02', 'PC_Hero_FindRobe', 'PC_Hero_FindArtefacts',
             'PC_Hero_FindScepter']
    quests = finds + ['Innos_Contact', 'Beliar_Contact', 'Adanos_DestroyArtefacts', 'Zuben_XardasPlan', 'Rhobar_XardasPlan',
                      'Innos_KillZuben', 'Innos_FreeMyrtana']
    events = ['Innos_chosen', 'Innos_Zuben_OK', 'Innos_Myrtana_OK', 'Epilog_Innos', 'Epilog_FirstInfo']
    sectors = ['G3_Nordmar_01_Story_Innos_01']
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report['negative'] = [report_line(c.bot, 'chaptersyncoff', 'CHAPTERSYNCOFF ') for c in (host, client)]
    actor = host if os.environ.get('G3_WHO') == 'host' else client  # host: the single-player control
    report['actor'] = actor.tag
    views = report['views'] = {}
    look = lambda tag: views.__setitem__(tag, {c.tag: story_view(c, quests, events, sectors) for c in (host, client)})
    look('start')
    report['runs'] = [report_line(host.bot, 'runquest ' + q, 'QUEST ') for q in finds + ['Rhobar_XardasPlan', 'Zuben_XardasPlan']]
    time.sleep(5)
    look('quests_run')
    for item in artefacts:
        report_line(actor.bot, 'receiveitem ' + item, ('RECEIVEITEM ', 'REFUSED receiveitem'))
        time.sleep(2)
    time.sleep(10)
    look('artefacts_found')
    report_line(actor.bot, 'unarm', 'UNARM')
    talk_choose(actor, 'Innos_Tester', ['FILLER34', 'FILLER36'], report, 'choose')  # the Delivery is the Ready info's child
    time.sleep(8)
    look('handed_over')
    report['artefacts_left'] = {i: carried(actor.bot, i) for i in artefacts}
    for q in ('Innos_KillZuben', 'Innos_FreeMyrtana'):  # the long way (Zuben, seven towns) skipped: the quests succeed
        report.setdefault('ending_quests', []).append(report_line(host.bot, 'runquest ' + q, 'QUEST '))
        time.sleep(1)
        report['ending_quests'].append(report_line(host.bot, 'succeedquest ' + q, 'QUEST '))
    time.sleep(6)
    look('ending_quests_done')
    talk_choose(actor, 'Innos_Tester', ['SKALVERAM3188', 'SKALVERAM3196'], report, 'rewards')
    # After one reward the other was gone from the offer (host alone, run 220505): its event set by QA, so the epilogue can start.
    report['myrtana_ok'] = report_line(actor.bot, 'setevent Innos_Myrtana_OK', 'EVENT ')
    report['zuben_ok'] = report_line(actor.bot, 'setevent Innos_Zuben_OK', 'EVENT ')
    time.sleep(3)
    talk_choose(actor, 'Innos_Tester', ['SKALVERAM3210'], report, 'epilog_start')
    time.sleep(6)
    look('epilog_started')
    # The epilogue: infos of the hero himself (owner PC_Hero, BPANKRATZ31831 ...; the last ends with EndGame). Only what each
    # hero is offered; nothing chosen, the game would end.
    report['epilog_offer'] = {}
    for c in (host, client):
        line, offered = talk_infos(c, 'PC_Hero')
        report['epilog_offer'][c.tag] = line[:300]
        report_line(c.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))
    for tag, view in views.items():  # the story is the same on both machines after every step
        mine, theirs = view['host'], view['client']
        for part in ('quests', 'events', 'sectors', 'chapter'):
            assert mine[part] == theirs[part], f'{tag}: {part} differ: host {mine[part]}, client {theirs[part]}'
    assert views['handed_over']['host']['chapter'] == '2', f"no chapter 2 after the hand-over: {views['handed_over']}"
    gained = {t: views['epilog_started'][t]['xp'] - views['start'][t]['xp'] for t in ('host', 'client')}
    report['xp_gained'] = gained
    assert gained['host'] == gained['client'], f'experience differs: {gained}'


def stageStoryNordmar(copies, report, negative=False):
    """P4 main story (Kapitel-Checkliste: Hauptstory), the client alone on the other story steps: the host runs the quests of
    the fire chalices and the teleport stone to the king; the client finds all chalices (PC_Hero_FindAllFiremageCups, the
    amount from the quest's counter: 12), throws them into the holy fire at HolyFire_Tester (Activator SKALVERAM3658, Delivery
    SKALVERAM3657: a sector) and finds It_Teleport_VangardTempel. After each step the story is the same
    on both machines (quests, events, sectors, chapter) and both earn the same experience. G3_WHO=host: the host does it
    (single-player control). Negative control: storyoff on the client."""
    quests = ['PC_Hero_FindAllFiremageCups', 'PC_Hero_Bless_FiremageCups', 'PC_Hero_FIndTeleportToKing']
    events = []
    sectors = ['G3_Nordmar_01_Monastery_FiremageCups_Blessed']
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        story_off(client)
    actor = host if os.environ.get('G3_WHO') == 'host' else client
    report['actor'] = actor.tag
    views = report['views'] = {}
    look = lambda tag: views.__setitem__(tag, {c.tag: story_view(c, quests, events, sectors) for c in (host, client)})
    look('start')
    report['runs'] = [report_line(host.bot, 'runquest ' + q, 'QUEST ') for q in ('PC_Hero_FindAllFiremageCups', 'PC_Hero_FIndTeleportToKing')]
    time.sleep(5)
    if not negative:
        wait_for(lambda: quest_status(client.bot, 'PC_Hero_FindAllFiremageCups') == RUNNING and ['yes'], 20, 'the quests run on the client too')
    start = host.bot.send('questcounters PC_Hero_FindAllFiremageCups', 20)
    report['cups_counter'] = host.bot.line('QUESTCOUNTER name=', 15, 'questcounters', start)
    m = re.search(r'It_FiremageCup:-?\d+/(\d+)', report['cups_counter'])
    cups = int(m[1]) if m and int(m[1]) > 0 else int(os.environ.get('G3_CUPS', '5'))
    report['cups'] = cups
    look('quests_run')
    for _ in range(cups):
        report_line(actor.bot, 'receiveitem It_FiremageCup', ('RECEIVEITEM ', 'REFUSED receiveitem'))
        time.sleep(1)
    time.sleep(8)
    look('cups_found')
    report_line(actor.bot, 'unarm', 'UNARM')
    talk_choose(actor, 'HolyFire_Tester', ['SKALVERAM3658', 'SKALVERAM3657'], report, 'holy_fire')
    time.sleep(8)
    look('cups_blessed')
    report['cups_left'] = carried(actor.bot, 'It_FiremageCup')
    # The fifth temple is left out: its book has no entity of either name in the world (the info's owner is
    # MonasteryBooik_Tester, the quest's delivery MonasteryBook_Tester; findall found neither, run 224630).
    report['stone'] = report_line(actor.bot, 'receiveitem It_Teleport_VangardTempel', ('RECEIVEITEM ', 'REFUSED receiveitem'))
    time.sleep(10)
    look('stone_found')
    for tag, view in views.items():
        mine, theirs = view['host'], view['client']
        for part in ('quests', 'events', 'sectors', 'chapter'):
            assert mine[part] == theirs[part], f'{tag}: {part} differ: host {mine[part]}, client {theirs[part]}'
    final = views['stone_found']['host']['quests']
    report['final'] = final
    assert all(final[q] == SUCCESS for q in quests), \
        f'not every step succeeded: {final}'
    gained = {t: views['stone_found'][t]['xp'] - views['start'][t]['xp'] for t in ('host', 'client')}
    report['xp_gained'] = gained
    assert gained['host'] == gained['client'] and gained['host'] > 0, f'experience differs: {gained}'


def swimdiag(copies, report, negative=False):
    """P2 swimming (PS_Swim, OnMovementModeChanged): where is water? The client goes to a spot by water (G3_WATER_AT, an NPC;
    default QP_Fisher, the fisherman) and steps out in 16 directions, 4 to 24 m, until its movement mode is swim, dive or
    wade (QA movemode: 7, 8, 10). There it stays and moves a little; the host's puppet of it over 20 s: movement mode and
    motion (TRACE). Diagnostic."""
    host, client = join_both(copies)
    clear_raid(host, report)
    at = os.environ.get('G3_WATER_AT', 'QP_Fisher')
    report['at'] = teleport_to(client.bot, at) if at != 'start' else 'start'  # start: where the client stands after the join
    time.sleep(4)
    base = hero_pos(client.bot)
    mode = lambda c: dict(re.findall(r'(\w+)=(-?\d+)', report_line(c.bot, 'movemode', 'MOVEMODE ')))
    found = None
    report['probe'] = []
    # G3_WATER_SPOT x_y_z (found 26.10.: 102767_-608_-4075, sea 160 m from the start) and G3_WATER_SHORE x_y_z for the host:
    # no probing.
    xyz = lambda v: tuple(float(c) for c in v.split('_'))
    if os.environ.get('G3_WATER_SPOT'):
        move_hero(client.bot, xyz(os.environ['G3_WATER_SPOT']))
        time.sleep(4)
        report['probe'].append(('spot', 0, mode(client).get('hero'), round(hero_pos(client.bot)[1])))
        found = hero_pos(client.bot) if report['probe'][-1][2] in ('7', '8', '10') else None
    for dist in [] if os.environ.get('G3_WATER_SPOT') else [int(v) for v in os.environ.get('G3_WATER_STEPS', '600,1200,2000,3000,4500,6000').split(',')]:
        for k in [int(v) for v in os.environ.get('G3_WATER_DIRS', ','.join(map(str, range(16)))).split(',')]:
            a = 2 * math.pi * k / 16
            p = (base[0] + dist * math.cos(a), base[1] + int(os.environ.get('G3_WATER_DROP', '0')), base[2] + dist * math.sin(a))  # at the spot's height: over water it falls in
            move_hero(client.bot, p)
            time.sleep(float(os.environ.get('G3_WATER_WAIT', '3')))  # a long fall needs longer
            m = mode(client).get('hero')
            report['probe'].append((dist, k, m, round(hero_pos(client.bot)[1])))
            if m in ('7', '8', '10'):
                found = hero_pos(client.bot)
                break
        if found:
            break
    report['water'] = tuple(round(v) for v in found) if found else None
    if not found:
        report['status_note'] = 'diagnostic only: no water found'
        return
    # The host near, so it shows the puppet: by the NPC, or (start) on the last dry probe before the water.
    if os.environ.get('G3_WATER_SHORE'):
        move_hero(host.bot, xyz(os.environ['G3_WATER_SHORE']))
    elif at != 'start':
        teleport_to(host.bot, at)
    else:
        dry = [q for q in report['probe'] if q[2] == '1']
        d, k = (dry[-1][0], dry[-1][1]) if dry else (0, 0)
        a = 2 * math.pi * k / 16
        move_hero(host.bot, (base[0] + d * math.cos(a), dry[-1][3] + 100 if dry else base[1], base[2] + d * math.sin(a)))
    time.sleep(4)
    samples = []
    for i in range(10):
        move_hero(client.bot, (found[0] + 150 * (i % 2), found[1], found[2]))
        time.sleep(2)
        lines = [l for l in host.mod_log().splitlines()[-300:] if 'TRACE puppet2 ' in l]
        motion = re.search(r' motion=(\S+)', lines[-1])[1] if lines else '-'
        samples.append({'client': mode(client).get('hero'), 'host_puppet': mode(host).get('puppet2'), 'host_motion': motion[:70]})
    report['samples'] = samples
    # G3_WATER_SWIM=1: the client swims forward by the game's AI goto (QA runto), 8 m along the shore and back; both sides'
    # motions every 0.5 s (does the puppet play the swim strokes or glide?).
    if os.environ.get('G3_WATER_SWIM'):
        strokes = []
        for k, dx in enumerate((800, -800, 800)):
            here = hero_pos(client.bot)
            strokes.append(report_line(client.bot, f'runto {here[0]:.0f}_{here[1]:.0f}_{here[2] + dx:.0f}', ('RUNTO ', 'REFUSED runto')))
            for _ in range(8):
                time.sleep(0.5)
                own = [l for l in client.mod_log().splitlines()[-40:] if 'TRACE hero ' in l]
                seen = [l for l in host.mod_log().splitlines()[-300:] if 'TRACE puppet2 ' in l]
                strokes.append({'client': (re.search(r' motion=(\S+)', own[-1])[1][:60] if own else '-'), 'mode': mode(client).get('hero'),
                                'pos': tuple(round(v) for v in hero_pos(client.bot)),
                                'host': (re.search(r' motion=(\S+)', seen[-1])[1][:60] if seen else '-')})
        report['strokes'] = strokes
    report['status_note'] = 'diagnostic only'


def equipped_stacks(bot, guid):
    """An NPC's equipped stacks (slot not 0) as name@slot (QA stacks)."""
    start = bot.send('stacks ' + guid, 20)
    bot.line('STACKS end', 10, 'stacks', start)
    return sorted(f'{m[1]}@{m[2]}' for m in re.finditer(r'^STACK \d+ name=(\S+) amount=\d+ slot=(\d+)', bot.log()[start:], re.M) if m[2] != '0')


def stageEnterRange(copies, report, negative=False):
    """P3 refilling per machine (OnEnterProcessingRange, RefreshHitPoints, RefreshWeaponry): an NPC that comes into a machine's
    processing range is refilled there and its routine reset, as in the single-player game when the player comes back. Here
    the host stays by a wounded NPC (it simulates it, the client mirrors it); the client goes 400 m away and comes back, so the
    NPC enters the client's range again: afterwards it has the host's hit points on both machines, not refilled on the
    client, and it wears the same weapons (QA stacks). Negative control: npcrecvoff on the client before it comes back."""
    host, client = join_both(copies)
    clear_raid(host, report)
    rows = npcs(host.bot)
    me = hero_pos(host.bot)
    living = [(distance(v['pos'], me), g, v) for g, v in rows.items() if not v['dead'] and not v['mirrored'] and v['hp'] > 60
              and v['name'] not in ('Gorn', 'Diego', 'Milten', 'Lester', 'PC_Hero')]
    assert living, 'no NPC the host simulates near it'
    _, guid, npc = min(living)
    report['npc'] = {'name': npc['name'], 'hp': npc['hp']}
    wounded = max(10, npc['hp'] // 3)
    report['wound'] = report_line(host.bot, f'npchp {guid}_{wounded}', ('NPCHP ', 'STATUSOF ', 'REFUSED'))
    time.sleep(4)
    report['client_before'] = npcs(client.bot).get(guid, {})
    report['equipped_before'] = {c.tag: equipped_stacks(c.bot, guid) for c in (host, client)}
    x, y, z = hero_pos(client.bot)
    report['away'] = move_hero(client.bot, (x + 40000, y + 200, z))
    time.sleep(8)
    if negative:
        report['negative'] = report_line(client.bot, 'npcrecvoff', 'NPCRECVOFF')
    move_hero(client.bot, (x, y, z))
    time.sleep(8)
    host_view, client_view = npcs(host.bot).get(guid, {}), npcs(client.bot).get(guid, {})
    report['after'] = {'host': {k: host_view.get(k) for k in ('hp', 'mirrored', 'task')}, 'client': {k: client_view.get(k) for k in ('hp', 'mirrored', 'task')}}
    report['find'] = {c.tag: re.sub(r'FIND name=\S+ found=1 ', '', find_npc(c.bot, npc['name']))[:180] for c in (host, client)}
    report['equipped_after'] = {c.tag: equipped_stacks(c.bot, guid) for c in (host, client)}
    assert client_view, f"{npc['name']} is gone on the client"
    assert host_view.get('hp') == wounded, f"the host's {npc['name']} changed: {host_view}"
    assert client_view.get('hp') == host_view.get('hp'), f"the client refilled {npc['name']}: client {client_view.get('hp')}, host {host_view.get('hp')}"
    # RefreshWeaponry ran on the client's copy as it came back: it wears what the host's wears (26.10.: Hamlar It_1H_Sword_01 slot 6).
    assert report['equipped_after']['client'] == report['equipped_after']['host'], f"the weapons differ after the return: {report['equipped_after']}"


def stageQuickUse(copies, report, negative=False):
    """P2 quick slots (PS_QuickUse, the Community Patch's OnPlayerUseQuickSlot and OnPlayerAssignStackKey): the client puts a
    healing potion on quick slot 1 and uses it as its key does (QA quickuse): one potion fewer and the hero healed on the
    client, nothing changes for the host's hero, and the host still shows the client (its own hero's matter, as alone)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    potion = os.environ.get('G3_POTION', 'It_Potion_Health')
    report['give'] = report_line(client.bot, f'givepotion {potion}-2', ('GIVEPOTION ', 'REFUSED givepotion'))  # as stage A2 does
    report_line(client.bot, 'sethp 20', 'SETHP ')
    hp = lambda c: int(re.search(r' hero=(-?\d+)', report_line(c.bot, 'hp', 'HP '))[1])
    before = report['before'] = {'client_potions': carried(client.bot, potion), 'client_hp': hp(client), 'host_potions': carried(host.bot, potion),
                                 'host_hp': hp(host)}
    report['use'] = report_line(client.bot, 'quickuse ' + potion, ('QUICKUSE ', 'REFUSED quickuse'))
    time.sleep(6)
    after = report['after'] = {'client_potions': carried(client.bot, potion), 'client_hp': hp(client), 'host_potions': carried(host.bot, potion),
                               'host_hp': hp(host)}
    report['host_sees_client'] = remotes(host.bot).get(2, {}).get('puppet')
    assert after['client_potions'] == before['client_potions'] - 1, f'the potion was not used: {before} -> {after}'
    assert after['client_hp'] > before['client_hp'], f'the client was not healed: {before} -> {after}'
    assert (after['host_potions'], after['host_hp']) == (before['host_potions'], before['host_hp']), f'the host changed: {before} -> {after}'
    assert report['host_sees_client'], 'the host lost the client'


def stageSleepBed(copies, report, negative=False):
    """P2 sleeping in a bed (PS_Interact_SleepBed, PlayerSleep): the client goes to the nearest bed (G3_Object_Interact_Bed_01 to
    _06; each owns four Sleep infos, 0, 8, 12 and 20 o'clock), uses it the game's way (QA use: PS_Interact_SleepBed, which talks
    to the bed), chooses 'until 8' (FILLERX293 + 4 per bed number) and the talk ends: the client's hero is refilled
    (PlayerSleep), and the clock moves on for both (clock.cpp asks the host). G3_BED=Sleep_Ground: a sleeping place on the ground
    (use type 75, PS_Interact_SleepGround; 57 found by objectscan): the state sleeps until 8 at once where no Sleep info
    asks, but these are the NPCs' free points (Nav_Freepoints_Sleep_Ground): the hero does not lie down there (26.10.).
    Negative control: clockoff on the client."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report_line(client.bot, 'clockoff', 'CLOCKOFF ')
    me = hero_pos(client.bot)
    names = [os.environ['G3_BED']] if os.environ.get('G3_BED') else [f'G3_Object_Interact_Bed_{n:02d}' for n in range(1, 7)]
    beds = [(distance(p, me), name, p) for name in names for p, dead in find_all(client.bot, name)]
    assert beds, f'no {names} in the world'
    report['tried'] = []
    report['talk'] = None
    for _, bed, spot in sorted(beds)[:8]:  # the nearest that lets the hero lie down and talk (Bed_01 at 86282,743,31395 never did)
        move_hero(client.bot, (spot[0] + 100, spot[1] + 50, spot[2]))
        time.sleep(4)
        if not report['tried']:
            report['hp_set'] = report_line(client.bot, 'sethp 20', 'SETHP ')
            report['clock_before'] = {c.tag: clock_of(c.bot)[1] for c in (host, client)}
        use = report_line(client.bot, 'use ' + bed, ('USE ', 'REFUSED use'))
        # A place without a Sleep info (PS_Interact_SleepGround, re/out/p2-rest-20261026-b.c: FUN_10030d50 finds no info) sleeps
        # until 8 at once (FUN_10076440 with 8), without asking.
        before = report['clock_before']['client']['timestamp']
        def asked_or_slept():
            st = report_line(client.bot, 'talkstate', 'TALKSTATE')
            if 'running=1' in st:
                return [st]
            now = clock_of(client.bot)[1]  # slept: 8 o'clock and more than an hour on (a try takes half an hour of game time)
            return ['slept'] if now['hour'] == 8 and now['timestamp'] - before >= 3600 else None
        try:
            report['talk'] = wait_for(asked_or_slept, 25, 'the bed asks how long to sleep')[0]
        except AssertionError:
            report['tried'].append({'bed': bed, 'pos': tuple(round(v) for v in spot), 'use': use[:100]})
            continue
        report['bed'] = {'name': bed, 'pos': tuple(round(v) for v in spot)}
        break
    assert report['talk'], f"no bed asked how long to sleep: {report['tried']}"
    if report['talk'] == 'slept':
        report['sleep'] = 'direct ok=1'
        time.sleep(10)
    # The 8 o'clock info of the bed the talk is with (FILLERX293 + 4 per bed number; Sleep_Ground: whichever runs).
    partner = re.search(r'partner=(\S+)', report['talk'])[1] if report['talk'] != 'slept' else ''
    number = int(partner[-2:]) if partner.startswith('G3_Object_Interact_Bed_') else 0
    candidates = [f'FILLERX{293 + 4 * (n - 1)}' if n < 3 else f'FILLERX{2101 + 4 * (n - 3)}' for n in ([number] if number else range(1, 7))]
    report['partner'] = partner
    for info in (candidates if partner else []):
        report['info'] = info
        report['sleep'] = report_line(client.bot, 'talkinfo ' + info, ('TALKINFO ', 'REFUSED talkinfo'))
        if ' ok=1' in report['sleep']:
            break
    if partner:
        time.sleep(12)
        report_line(client.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))
        time.sleep(8)
    report['clock_after'] = {c.tag: clock_of(c.bot)[1] for c in (host, client)}
    report['hp_after'] = re.search(r' hero=(\d+) max=(\d+)', report_line(client.bot, 'hp', 'HP ')).groups()
    jump = {t: report['clock_after'][t]['timestamp'] - report['clock_before'][t]['timestamp'] for t in ('host', 'client')}
    report['jump_s'] = jump
    gap = abs(report['clock_after']['host']['timestamp'] - report['clock_after']['client']['timestamp'])
    report['gap_s'] = gap
    assert ' ok=1' in report['sleep'], f'the bed would not let the client sleep: {report["sleep"]}'
    assert jump['client'] >= 1800, f"the client did not sleep: {jump}"
    assert int(report['hp_after'][0]) > 20, f'the sleep did not refill the hero: {report["hp_after"]}'
    assert jump['host'] >= 1800 and gap <= 180, f'the host clock did not follow: jump {jump}, gap {gap} s'


def stageEnding(copies, report, negative=False):
    """P4 main story's end (owner 25.10.: both players see the end, not only the one who triggers it). The epilogue is a chain
    of the hero's own infos (BPANKRATZ31831 for Innos; the last ends with EndGame) and played only where it started
    (storyepilogdiag 25.10.: the host's info manager stayed idle while the client's played 15 pictures). The client's
    epilogue starts (its events by QA, then BPANKRATZ31831 in a talk with the hero himself): the host starts the same info
    for its own hero, and the chain runs on both. G3_WHO=host: the host starts it. G3_TO_END=1: wait for the end as well.
    Negative control: endingsyncoff on the other machine."""
    host, client = join_both(copies)
    clear_raid(host, report)
    actor = host if os.environ.get('G3_WHO') == 'host' else client
    other = client if actor is host else host
    report['actor'] = actor.tag
    if negative:
        report['negative'] = report_line(other.bot, 'endingsyncoff', 'ENDINGSYNCOFF ')
    report['events'] = [report_line(actor.bot, 'setevent ' + e, 'EVENT ') for e in ('Innos_chosen', 'Epilog_Innos', 'Epilog_FirstInfo')]
    time.sleep(4)
    report['talk'], offered = talk_infos(actor, 'PC_Hero')
    first = next((i for i in offered if i != 'BPANKRATZ30'), None)
    assert first, f'no epilogue offered: {report["talk"]}'
    report['start'] = report_line(actor.bot, 'talkinfo ' + first, ('TALKINFO', 'REFUSED talkinfo'))
    state = lambda c: dict(re.findall(r'(\w+)=(\S+)', report_line(c.bot, 'talkstate', 'TALKSTATE')))
    report['other_started'] = wait_for(lambda: (lambda st: st.get('running') == '1' and st.get('partner') == 'PC_Hero' and [st])(state(other)), 30,
                                       f'the epilogue plays on the {other.tag} too')[0]
    seen = {c.tag: set() for c in (host, client)}
    samples = []
    for _ in range(36 if os.environ.get('G3_TO_END') == '1' else 8):
        time.sleep(5)
        sample = {}
        for c in (host, client):
            try:
                st = state(c)
                seen[c.tag].add(st.get('current', '-'))
                sample[c.tag] = f"{st.get('running')}:{st.get('current')}"
            except Exception as error:
                sample[c.tag] = f'no answer: {str(error)[:60]}'
        samples.append(sample)
    report['samples'] = samples
    report['infos_seen'] = {t: sorted(v - {'-'}) for t, v in seen.items()}
    report['ending'] = {c.tag: report_line(c.bot, 'ending', 'ENDING ') for c in (host, client)}
    assert len(report['infos_seen'][other.tag]) >= 3, f"the epilogue did not run on the {other.tag}: {report['infos_seen']}"


def stageEndingWait(copies, report, negative=False):
    """P4 main story's end, review 26.10. (point 5): another player's epilogue waited at most 60 s for this hero to be free
    (out of a dialog, menu or fight), then it was dropped without a word. Now it waits as long as it takes, and the player
    is told why. The client starts its epilogue (as stageEnding) while the host counts as busy for 75 s (QA endinghold):
    the host is told (HUD, ENDING told >= 1), nothing plays there during the hold, and after it the epilogue plays on the host
    too. Negative control: endinglimit on the host (the 60 s limit as before): it is dropped (given_up=1) and never plays."""
    host, client = join_both(copies)
    clear_raid(host, report)
    hold = int(os.environ.get('G3_HOLD', '75'))
    if negative:
        report['negative'] = report_line(host.bot, 'endinglimit', 'ENDINGLIMIT ')
    report['hold'] = report_line(host.bot, f'endinghold {hold}', 'ENDINGHOLD ')
    held_at = time.time()
    report['events'] = [report_line(client.bot, 'setevent ' + e, 'EVENT ') for e in ('Innos_chosen', 'Epilog_Innos', 'Epilog_FirstInfo')]
    time.sleep(4)
    report['talk'], offered = talk_infos(client, 'PC_Hero')
    first = next((i for i in offered if i != 'BPANKRATZ30'), None)
    assert first, f'no epilogue offered: {report["talk"]}'
    began = time.time()
    report['start'] = report_line(client.bot, 'talkinfo ' + first, ('TALKINFO', 'REFUSED talkinfo'))
    state = lambda c: dict(re.findall(r'(\w+)=(\S+)', report_line(c.bot, 'talkstate', 'TALKSTATE')))
    early = []
    while time.time() - held_at < hold - 12:  # the hold runs from endinghold, before the talk (first run: played at 74 s of 75)
        time.sleep(10)
        st = state(host)
        early.append(f"{round(time.time() - began)}s {st.get('running')}:{st.get('partner')}:{st.get('current')}")
    report['host_during_hold'] = early
    report['host_told'] = report_line(host.bot, 'hud', 'HUD ')
    report['ending_during_hold'] = report_line(host.bot, 'ending', 'ENDING ')
    assert not any(e.split()[1].startswith('1:PC_Hero') for e in early), f'the epilogue played on the host while it was busy: {early}'
    try:
        report['host_started'] = wait_for(lambda: (lambda st: st.get('running') == '1' and st.get('partner') == 'PC_Hero' and [st])(state(host)),
                                          40, 'the epilogue plays on the host after the hold')[0]
    finally:
        report['ending'] = {c.tag: report_line(c.bot, 'ending', 'ENDING ') for c in (host, client)}
        report['waited_s'] = round(time.time() - began)
    told = int((re.search(r' told=(\d+)', report['ending_during_hold']) or [0, '0'])[1])
    assert told >= 1, f"the host was not told that the epilogue waits: {report['ending_during_hold']}"


def storyepilogdiag(copies, report, negative=False):
    """P4 main story's end (Kapitel-Checkliste: ein Ende): the epilogue is a chain of infos of the hero himself (owner PC_Hero,
    BPANKRATZ31831 ... for Innos; the last one ends with EndGame). Its events (Epilog_Innos, Epilog_FirstInfo) are shared, so
    both heroes are offered it (stageStoryInnos). Here the client plays it through: the events set by QA, then every epilogue
    info the hero is offered, until EndGame. Watched on both machines: the talk, whether the client is still in the world and
    on the host, and the mods' logs. G3_WHO=host: the host plays it. Diagnostic."""
    host, client = join_both(copies)
    clear_raid(host, report)
    actor = host if os.environ.get('G3_WHO') == 'host' else client
    other = client if actor is host else host
    report['actor'] = actor.tag
    report['events'] = [report_line(actor.bot, 'setevent ' + e, 'EVENT ') for e in ('Innos_chosen', 'Epilog_Innos', 'Epilog_FirstInfo')]
    time.sleep(4)
    report['other_events'] = [report_line(other.bot, 'event ' + e, 'EVENT ') for e in ('Epilog_Innos', 'Epilog_FirstInfo')]
    report['chain'] = []
    for _ in range(8):
        try:
            line, offered = talk_infos(actor, 'PC_Hero')
        except Exception as error:
            report['chain'].append(f'no answer: {str(error)[:100]}')
            break
        todo = [i for i in offered if i != 'BPANKRATZ30']
        report['chain'].append(line[:200])
        if not todo:
            report_line(actor.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))
            break
        report['chain'].append(report_line(actor.bot, 'talkinfo ' + todo[0], ('TALKINFO', 'REFUSED talkinfo'))[:80])
        for _ in range(40):  # pictures, subtitles and waits play out
            time.sleep(2)
            try:
                state = report_line(actor.bot, 'talkstate', 'TALKSTATE')
            except Exception as error:
                state = f'no answer: {str(error)[:80]}'
                break
            if f'current={todo[0]} ' not in state:
                break
        report['chain'].append(state[:120])
        if state.startswith('no answer'):
            break
    samples = []
    for _ in range(12):
        time.sleep(5)
        sample = {}
        for c in (host, client):
            try:
                sample[c.tag] = report_line(c.bot, 'talkstate', 'TALKSTATE')[:80]
            except Exception as error:
                sample[c.tag] = f'no answer: {str(error)[:60]}'
        try:
            r = remotes(host.bot).get(2, {})
            sample['host_sees_client'] = {k: r.get(k) for k in ('same_world', 'age_ms', 'puppet')}
        except Exception as error:
            sample['host_sees_client'] = f'no answer: {str(error)[:60]}'
        samples.append(sample)
    report['after'] = samples
    for c in (host, client):
        report[c.tag + '_log'] = [l[-150:] for l in c.mod_log().splitlines()
                                  if 'EndGame' in l or 'world deactivated' in l or 'left' in l.lower() or 'menu' in l.lower()][-10:]
    report['status_note'] = 'diagnostic only'


def town_status(bot, town):
    start = bot.send('enclaves', 20)
    bot.line('ENCLAVES end', 15, 'enclaves', start)
    m = re.search(r'ENCLAVE members=\d+ first=\S+ status=(\d+) enclave=' + re.escape(town) + ' ', bot.log()[start:])
    return int(m[1]) if m else None


def town_members(bot, town):
    """QA murdercand: the town's members, [{'member', 'alive', 'counts', 'guid', ...}]."""
    start = bot.send('murdercand ' + town, 20)
    bot.line('MURDERCAND end', 15, 'murdercand', start)
    return [dict(re.findall(r'(\w+)=(\S+)', l)) for l in bot.log()[start:].splitlines() if l.startswith('MURDERCAND enclave=')]


def client_frees_town(host, client, town, report, fits=lambda name: name.startswith('Orc_'), each_round=None):
    """The client kills the town's defenders whose names `fits`: the host's near the host (lethal claims), its own near the
    client (it kills them itself, the game's way), and the town's far from both (40 m lists; one walked off in a netsim run:
    claims by GUID), until three rounds in a row find none."""
    report_line(client.bot, 'arm ' + WEAPON, 'ARM ')
    kills, quiet, claimed = [], 0, {}
    for _ in range(30):
        if each_round:
            each_round()
        near = {g: v for g, v in npcs(host.bot).items() if fits(v['name']) and not v['dead'] and v['hp'] > 0}
        raiders = [g for g, v in near.items() if not v['mirrored']]
        own = {g: v['name'] for g, v in npcs(client.bot).items() if fits(v['name']) and not v['dead'] and v['hp'] > 0
               and not v['mirrored'] and g not in raiders}
        far = [m['guid'] for m in town_members(host.bot, town) if fits(m['member']) and m['alive'] == '1'
               and m['guid'] not in near and m['guid'] not in own]
        if far:
            report.setdefault('far', []).append(len(far))
        for guid in raiders + far:
            claimed[guid] = claimed.get(guid, 0) + 1
            report_line(client.bot, f'hitnpcburst {guid}_1', ('HITNPCBURST ', 'REFUSED hitnpcburst'))
            time.sleep(0.3)
        for guid in own:  # by GUID: a town's defenders share names (Montera)
            report.setdefault('client_own', []).append(report_line(client.bot, 'farkill ' + guid, ('FARKILL ', 'REFUSED farkill')))
        raiders += far + list(own)
        kills.append(len(raiders))
        quiet = quiet + 1 if not raiders else 0
        if quiet >= 3:
            break
        time.sleep(5)
    report['client_kills'] = kills
    report['host_killonce'] = report_line(host.bot, 'killreport', 'KILLONCE ')  # undone=: claim kills that did not take
    assert quiet >= 3, f'the defenders did not all fall: {kills}'
    stubborn = {g: n for g, n in claimed.items() if n > 5}  # before 24.10.: three raiders took claims for 14 rounds
    assert not stubborn, f'defenders the client claimed in more than five rounds without dying: {stubborn}'


def stageLiberate(copies, report, negative=False):
    """Quest type 10, free a town (31 quests, Kapitel-Checkliste), as the game's opening does it: Ardea occupied by the raid's
    orcs, the quest Ardea_Revolution ("Free Ardea from the orcs!"). From a new game, the joining player kills the raiders:
    the host's with lethal claims (NotifyEnclave runs on the host for each death), the ones it simulates itself on its own
    PC (NotifyEnclave there); when the raid is over, Ardea's status and the quest are the same on both PCs (the post-raid
    save has Ardea at 2, Flee: freed). G3_WHO=host: the host ends the raid itself (QA killraiders, positive control).
    G3_TOWN, G3_QUEST: another town. Negative control: townstatusoff on both, and the host ends the raid (the town turns
    there only; before 24.10. its status never travelled). Not with the client's kills: when the last raider dies on the
    client (one it simulates), both PCs turn the town themselves (negative control green 24.10. 19:35)."""
    town = os.environ.get('G3_TOWN', 'Ardea')
    quest = os.environ.get('G3_QUEST', 'Ardea_Revolution')
    host, client = join_both(copies)
    if negative:
        report['negative'] = [report_line(c.bot, 'townstatusoff', 'TOWNSTATUSOFF ') for c in (host, client)]
    if os.environ.get('G3_KILLCONFIRM') == 'off':  # the kill check's negative control (claims on raiders out of range)
        report['killconfirm'] = report_line(host.bot, 'killconfirmoff', 'KILLCONFIRMOFF ')
    if os.environ.get('G3_LIBERATE_AT'):  # another town: both go there first (P4: a town other than Ardea)
        clear_raid(host, report)
        report['at'] = {c.tag: teleport_to(c.bot, os.environ['G3_LIBERATE_AT'])[:80] for c in (host, client)}
        time.sleep(10)
        report['run'] = report_line(host.bot, 'runquest ' + quest, 'QUEST ')
    report['before'] = {c.tag: {'status': town_status(c.bot, town), 'quest': quest_status(c.bot, quest)} for c in (host, client)}
    if (os.environ.get('G3_WHO') == 'host' or negative) and not os.environ.get('G3_LIBERATE_AT'):
        clear_raid(host, report)
    else:
        client_frees_town(host, client, town, report, lambda name: 'Orc' in name)  # any town's orcs (G3_LIBERATE_AT)
    time.sleep(8)
    try:
        wait_for(lambda: town_status(host.bot, town) == 2, 30, f'{town} is freed (Flee) on the host')
        wait_for(lambda: quest_status(host.bot, quest) == 2, 20, f'{quest} is a success on the host')
        wait_for(lambda: quest_status(client.bot, quest) == 2, 20, f'{quest} is a success on the client')
        wait_for(lambda: town_status(client.bot, town) == 2, 20, f'{town} is freed on the client too')
    finally:
        report['after'] = {c.tag: {'status': town_status(c.bot, town), 'quest': quest_status(c.bot, quest)} for c in (host, client)}


def stageTownFree(copies, report, negative=False):
    """Quest type 10 in the middle of the game (Kapitel-Checkliste: Stadt befreien, other towns than Ardea's raid): the host
    takes the town's quest, the client alone kills the occupiers: the town is freed (Flee) and the quest a success on both
    PCs. Default Montera_Revolution (Montera's orcs), from the post-raid save; the host stays at Ardea, so the client
    simulates the town itself (G3_HOST_NEAR=1: the host stands at the town too, the orcs are its NPCs and the client's kills
    are claims). G3_TOWN, G3_QUEST, G3_GOTO (an NPC in the town), G3_MEMBERS (part of the occupiers' names, default Orc).
    Negative control: townstatusoff on both."""
    town = os.environ.get('G3_TOWN', 'Montera')
    quest_id = os.environ.get('G3_QUEST', 'Montera_Revolution')
    goto = os.environ.get('G3_GOTO', 'Varek')
    part = os.environ.get('G3_MEMBERS', 'Orc').lower()
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report['negative'] = [report_line(c.bot, 'townstatusoff', 'TOWNSTATUSOFF ') for c in (host, client)]
    report['members'] = [(m['member'], m['alive'], m['counts']) for m in town_members(host.bot, town)]
    report['run'] = quest(host.bot, 'runquest', quest_id)
    assert report['run'] == RUNNING, f'the host could not start {quest_id}'
    wait_for(lambda: quest(client.bot, 'quest', quest_id) == RUNNING and ['yes'], 20, f'{quest_id} runs on the client too')
    report['before'] = {c.tag: {'status': town_status(c.bot, town), 'quest': quest_status(c.bot, quest_id)} for c in (host, client)}
    if os.environ.get('G3_HOST_NEAR') == '1':
        report['host_at'] = teleport_to(host.bot, goto)
        time.sleep(4)
    report['client_at'] = teleport_to(client.bot, goto)
    time.sleep(8)
    client_frees_town(host, client, town, report, lambda name: part in name.lower(),
                      lambda: report_line(client.bot, 'sethp 1000', 'SETHP '))
    time.sleep(8)
    try:
        wait_for(lambda: town_status(host.bot, town) == 2, 40, f'{town} is freed (Flee) on the host')
        wait_for(lambda: quest_status(host.bot, quest_id) == 2, 20, f'{quest_id} is a success on the host')
        wait_for(lambda: quest_status(client.bot, quest_id) == 2, 20, f'{quest_id} is a success on the client')
        wait_for(lambda: town_status(client.bot, town) == 2, 20, f'{town} is freed on the client too')
    finally:
        report['after'] = {c.tag: {'status': town_status(c.bot, town), 'quest': quest_status(c.bot, quest_id)} for c in (host, client)}
        report['left'] = [(m['member'], m['alive']) for m in town_members(host.bot, town) if m['alive'] == '1']


def stageDefeatQuest(copies, report, negative=False):
    """Quest types 3 and 4, defeat or drive away (13 quests, Kapitel-Checkliste): a knock-out of the quest's NPC fulfils the
    quest, also when the joining player beats it (the host knocks it out for that player's claim, with the puppet as the
    attacker, stageMurder). Default Otis_DriveAwayMerc01 ("Drive the tired mercenary away", Trano in Montera). G3_WHO=host:
    the host beats Trano itself (positive control). G3_QUEST, G3_TARGET: another quest; G3_DUEL=1: a duel (duelhero), as the
    duel quests (Folleck_DefeatHim, Sivert_Defeat ...) start it. Negative control: storyoff on both."""
    quest = os.environ.get('G3_QUEST', 'Otis_DriveAwayMerc01')
    name = os.environ.get('G3_TARGET', 'Trano')
    host, client = join_both(copies)
    clear_raid(host, report)
    striker = host if os.environ.get('G3_WHO') == 'host' else client
    other = client if striker is host else host
    report['striker'] = striker.tag
    if negative:
        report['negative'] = [report_line(c.bot, 'storyoff', 'STORY') for c in (host, client)]
    report['teleport'] = [teleport_to(c.bot, name) for c in (host, client)]
    if striker is client:  # the host stays near (it simulates Trano), out of the fight
        x, y, z = hero_pos(host.bot)
        move_hero(host.bot, (x + 700, y, z))
    time.sleep(6)
    report['run'] = report_line(host.bot, 'runquest ' + quest, 'QUEST ')
    if not negative:
        wait_for(lambda: quest_status(client.bot, quest) == 1, 20, f'the client has {quest} running too')
    report['before'] = {c.tag: quest_status(c.bot, quest) for c in (host, client)}
    guid = next((g for g, v in npcs(host.bot).items() if v['name'] == name and not v['dead']), None)
    assert guid, f'{name} is not among the host NPCs'
    report_line(striker.bot, 'arm ' + WEAPON, 'ARM ')
    if os.environ.get('G3_DUEL') == '1':  # a duel quest: the fight starts as the quest's dialog starts it (stageA4)
        report['duel'] = report_line(striker.bot, 'duelhero ' + name, 'DUELHERO ')
        time.sleep(3)
    report_line(host.bot, f'npchp {name}_30', 'NPCHP ')
    report['hits'] = []
    state = {}
    for _ in range(25):
        hit = hit_npc(striker.bot, guid)
        report['hits'].append(hit['line'][-90:])
        time.sleep(0.8)
        state = npcs(host.bot).get(guid, {})
        if state.get('dead') or state.get('task') in KO_TASKS:
            break
        time.sleep(0.6)
    report['state'] = state
    assert state.get('task') in KO_TASKS and not state.get('dead'), f'{name} was not knocked out: {state}'
    try:
        wait_for(lambda: quest_status(striker.bot, quest) == 2, 30, f'{quest} is a success for the {striker.tag}')
        wait_for(lambda: quest_status(other.bot, quest) == 2, 20, f'{quest} is a success for the {other.tag} too')
    finally:
        report['after'] = {c.tag: quest_status(c.bot, quest) for c in (host, client)}


def find_ents(bot, prefix):
    start = bot.send('findents ' + prefix, 20)
    bot.line('FINDENTS end', 10, 'findents', start)
    return [tuple(float(v) for v in m[1].split('_')) for m in re.finditer(r'FINDENT name=\S+ pos=(\S+)', bot.log()[start:])]


def stageHailCopy(copies, report, negative=False):
    """P6, a partner's hailstorm falls where the partner is: the copy of a summon (MagicSummon replayed by the caster's puppet)
    spawns its weather Wth_Hailstorm at Entity::GetPlayer and pulls it there; summon.cpp puts it back on the puppet every frame.
    The client, 30 m from the host, casts a hailstorm; on the host the hail stands at the client's puppet, not at the host's
    hero. Negative control: weatherfollowoff on the host."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report['negative'] = report_line(host.bot, 'weatherfollowoff', 'WEATHERFOLLOWOFF ')
    x, y, z = hero_pos(host.bot)
    move_hero(client.bot, (x + 3000, y, z))
    time.sleep(4)
    near = min(((g, v) for g, v in npcs(client.bot).items() if not v['dead']), key=lambda gv: distance(gv[1]['pos'], hero_pos(client.bot)))
    report['ready'] = report_line(client.bot, 'ready It_Spell_Hailstorm', 'READY ')[6:200]
    report['cast'] = report_line(client.bot, 'castat ' + near[0], 'CASTAT ')[7:300]
    time.sleep(6)
    snap(host, 'hail', report)  # for a look at the picture: where the host sees the client's hail
    snap(client, 'hail', report)
    host_hero, client_hero = hero_pos(host.bot), hero_pos(client.bot)
    hail = report['host_hail'] = find_ents(host.bot, 'Wth_Hail')
    report['client_hail'] = find_ents(client.bot, 'Wth_Hail')
    report['weather'] = report_line(host.bot, 'weather', 'WEATHER ')
    assert hail, 'no hailstorm weather on the host'
    report['distances'] = [(round(distance(h, client_hero)), round(distance(h, host_hero))) for h in hail]
    assert all(dc < dh for dc, dh in report['distances']), f"on the host the hail is at the host's hero, not at the caster: {report['distances']}"


def followdiag(copies, report, negative=False):
    """P9 measurement (Kapitel-Checkliste: Folgen, 57 quests): with a follow quest running, the guide NPC on the host picks its
    guiding by the running quest (SelectRoutineTask) and guides Entity::GetPlayer, the host's hero. Here the client stands at
    the guide, the host 30 m away; the guide's task and position on the host over 40 s, and the quest on both. G3_QUEST,
    G3_GUIDE: another follow quest (default Gorn_ShowReddock). G3_WHO=host: the host stands at the guide instead. Diagnostic."""
    quest = os.environ.get('G3_QUEST', 'Mitch_Trolls' if os.environ.get('G3_VIA') == 'dialog' else 'Gorn_ShowReddock')
    guide = os.environ.get('G3_GUIDE', 'Mitch' if os.environ.get('G3_VIA') == 'dialog' else 'Gorn')
    host, client = join_both(copies)
    clear_raid(host, report)
    near, far = (host, client) if os.environ.get('G3_WHO') == 'host' else (client, host)
    report['near'] = near.tag
    teleport_to(near.bot, guide)
    teleport_to(far.bot, guide)
    x, y, z = hero_pos(far.bot)
    move_hero(far.bot, (x + 3000, y, z))
    time.sleep(4)
    if os.environ.get('G3_VIA') == 'dialog':
        # The quest the way a player takes it (a5881ef: runquest alone does not start the guiding): the near player talks to
        # the guide and picks the quest's Activator info (condition type 6) in one dialog, its parent first. Data from
        # Infos.pak: Mitch_Trolls in Faring, SKALVERAM3409 (needs Zakosh_KillTrolls running and the event Mitch_WhereTrolls)
        # opens the Activator SKALVERAM3395. G3_RUNQUESTS, G3_EVENTS: set first on the near player; G3_INFO: infos in order.
        for name in filter(None, os.environ.get('G3_RUNQUESTS', 'Zakosh_KillTrolls').split(',')):
            report.setdefault('preruns', []).append(report_line(near.bot, 'runquest ' + name, 'QUEST '))
        for name in filter(None, os.environ.get('G3_EVENTS', 'Mitch_WhereTrolls').split(',')):
            report.setdefault('events', []).append(report_line(near.bot, 'setevent ' + name, 'EVENT '))
        report_line(near.bot, 'unarm', 'UNARM')
        teleport_to(near.bot, guide)
        time.sleep(3)
        report['talks'] = [report_line(near.bot, 'talk ' + guide, 'TALK ')]
        for info in filter(None, os.environ.get('G3_INFO', 'SKALVERAM3409,SKALVERAM3395').split(',')):
            start = near.bot.send('infodesc ' + info, 20)
            near.bot.line('INFODESC end', 10, 'infodesc', start)
            report['talks'].append([l for l in near.bot.log()[start:].splitlines() if l.startswith('INFODESC ')])
            report['talks'].append(report_line(near.bot, 'talkinfo ' + info, ('TALKINFO', 'REFUSED talkinfo')))
            for _ in range(20):  # the spoken lines run before the info ends
                time.sleep(2)
                state = report_line(near.bot, 'talkstate', 'TALKSTATE')
                if f'current={info} ' not in state:
                    break
            report['talks'].append(state)
        report['talks'].append(report_line(near.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk')))
        time.sleep(2)
        report['run'] = {c.tag: quest_status(c.bot, quest) for c in (host, client)}
    else:
        report['run'] = report_line(host.bot, 'runquest ' + quest, 'QUEST ')
        report['routine'] = report_line(host.bot, 'npcroutine ' + guide, ('NPCROUTINE ', 'REFUSED npcroutine'))  # as at the dialog's end
    report['samples'] = []
    for _ in range(int(os.environ.get('G3_SAMPLES', '40'))):
        time.sleep(2)
        state = next((v for v in npcs(host.bot).values() if v['name'] == guide), {})
        there = next((v for v in npcs(client.bot).values() if v['name'] == guide), {})
        report['samples'].append((state.get('task'), [round(c) for c in state.get('pos', ())], there.get('task'), there.get('mirrored'), state.get('owner'),
                                  [round(c) for c in hero_pos(near.bot)], [round(c) for c in hero_pos(far.bot)]))
    report['quest'] = {c.tag: quest_status(c.bot, quest) for c in (host, client)}
    report['status_note'] = 'diagnostic only'


def take_quest(copy, npc, infos, report, runquests=(), events=()):
    """The player takes a quest as in the game: its preconditions set (quests run, events known), then the infos picked in
    one dialog with npc (a parent opens its children). Data from Infos.pak."""
    for name in runquests:
        report.setdefault('preruns', []).append(report_line(copy.bot, 'runquest ' + name, 'QUEST '))
    for name in events:
        report.setdefault('events', []).append(report_line(copy.bot, 'setevent ' + name, 'EVENT '))
    report_line(copy.bot, 'unarm', 'UNARM')
    teleport_to(copy.bot, npc)
    time.sleep(3)
    talks = report.setdefault('talks', [])
    talks.append(report_line(copy.bot, 'talk ' + npc, 'TALK '))
    for info in infos:
        talks.append(report_line(copy.bot, 'talkinfo ' + info, ('TALKINFO', 'REFUSED talkinfo')))
        for _ in range(20):  # the spoken lines run before the info ends
            time.sleep(2)
            state = report_line(copy.bot, 'talkstate', 'TALKSTATE')
            if f'current={info} ' not in state:
                break
        talks.append(state)
    talks.append(report_line(copy.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk')))


def guide_state(bot, npc):
    return dict(re.findall(r'(\w+)=(\S+)', report_line(bot, 'guide ' + npc, 'GUIDE ')))


def npc_pos(bot, name):
    """Where this machine has the NPC (findnpc: any distance), or None."""
    line = report_line(bot, 'findnpc ' + name, 'FIND ')
    return tuple(float(v) for v in line.split('pos=')[1].split()[0].split('_')) if 'found=1' in line else None


def guide_host_case(host, client, quest, npc, report):
    """stageGuide G3_WHO=host: the host takes the quest, as before P9: Mitch guides the host (the game's own way; on
    25.10. it started ~50 s after the dialog), the client only mirrors him and takes nothing over."""
    teleport_to(client.bot, npc)
    x, y, z = hero_pos(client.bot)
    move_hero(client.bot, (x + 1500, y, z))
    take_quest(host, npc, ['SKALVERAM3409', 'SKALVERAM3395'], report, runquests=['Zakosh_KillTrolls'], events=['Mitch_WhereTrolls'])
    report['guiding'] = wait_for(lambda: (lambda g: g.get('hero_leader') == npc and g.get('task', '').startswith('ZS_GuidePlayer') and [g])(
        guide_state(host.bot, npc)), 90, f'{npc} guides the host')[0]
    start = npc_pos(host.bot, npc)
    for _ in range(20):
        at = npc_pos(host.bot, npc)
        if at:
            move_hero(host.bot, (at[0] - 150, at[1] + 50, at[2]))
        time.sleep(2)
        here = npc_pos(host.bot, npc)
        if here and distance(here, start) > 2000:
            break
    report['led_m'] = round(distance(npc_pos(host.bot, npc), start) / 100)
    assert report['led_m'] >= 15, f'{npc} led the host only {report["led_m"]} m'
    report['client_view'] = guide_state(client.bot, npc)
    assert report['client_view'].get('mirrored') == '1' and report['client_view'].get('claimed') == '0', f'the client took {npc}: {report["client_view"]}'
    report['quest'] = {c.tag: quest_status(c.bot, quest) for c in (host, client)}


def stageGuide(copies, report, negative=False):
    """P9, a follow quest the joining player takes (Mitch_Trolls in Faring: Mitch guides to the trolls). The game guides
    only the local hero (StartGuidePlayer, SelectRoutineTask), and the host simulated Mitch: he never guided the client
    (followdiag G3_VIA=dialog, 25.10.). Now the client takes Mitch over while he guides it: he guides the client there, the
    host shows him where the client's game has him, and the quest runs on both. Negative control: guidesyncoff."""
    quest, npc = 'Mitch_Trolls', 'Mitch'
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report_line(client.bot, 'guidesyncoff', 'GUIDESYNCOFF')
    watching = [n for n in os.environ.get('G3_SCRIPTWATCH', '').split(',') if n]  # scripts counted on both machines (P9)
    for c in (host, client):
        for n in watching:
            report_line(c.bot, 'scriptwatch ' + n, 'SCRIPTWATCH ')

    def watched():
        for c in (host, client):
            start = c.bot.send('scriptwatch', 20)
            c.bot.line('SCRIPTWATCH end', 10, 'scriptwatch', start)
            report[c.tag + '_scripts'] = [l[12:] for l in c.bot.log()[start:].splitlines() if l.startswith('SCRIPTWATCH name=')]
    if os.environ.get('G3_WHO') == 'host':
        guide_host_case(host, client, quest, npc, report)
        if watching:
            watched()
        return
    teleport_to(host.bot, npc)
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 3000, y, z))
    take_quest(client, npc, ['SKALVERAM3409', 'SKALVERAM3395'], report, runquests=['Zakosh_KillTrolls'], events=['Mitch_WhereTrolls'])
    report['quest_run'] = {c.tag: quest_status(c.bot, quest) for c in (host, client)}
    assert report['quest_run'] == {'host': 1, 'client': 1}, f'the quest does not run on both: {report["quest_run"]}'
    report['guiding'] = wait_for(lambda: (lambda g: g.get('claimed') == '1' and g.get('task', '').startswith('ZS_GuidePlayer') and [g])(
        guide_state(client.bot, npc)), 40, f'{npc} guides the client on the client')[0]
    # The client walks behind him; he leads on (ZS_GuidePlayer_Wait while the client lags).
    start = npc_pos(client.bot, npc)
    report['walk'] = []
    for _ in range(30):
        at = npc_pos(client.bot, npc)
        if at:
            move_hero(client.bot, (at[0] - 150, at[1] + 50, at[2]))
        time.sleep(2)
        here, there = npc_pos(client.bot, npc), npc_pos(host.bot, npc)
        report['walk'].append([[round(c) for c in here] if here else None, [round(c) for c in there] if there else None,
                               guide_state(client.bot, npc).get('task')])
        if here and start and distance(here, start) > 3000:
            break
    here = npc_pos(client.bot, npc)
    report['led_m'] = round(distance(here, start) / 100) if here and start else 0
    assert report['led_m'] >= 20, f'{npc} led the client only {report["led_m"]} m'
    time.sleep(2)
    here, there = npc_pos(client.bot, npc), npc_pos(host.bot, npc)
    report['host_off_m'] = round(distance(here, there) / 100, 1) if here and there else None
    assert report['host_off_m'] is not None and report['host_off_m'] < 4, f'the host shows {npc} elsewhere: {report["host_off_m"]} m'
    report['host_view'] = next((v for v in npcs(host.bot).values() if v['name'] == npc), {})
    assert report['host_view'].get('mirrored') and report['host_view'].get('owner') == 2, f'the host does not mirror the client\'s {npc}'
    report['quest'] = {c.tag: quest_status(c.bot, quest) for c in (host, client)}
    if watching:
        watched()
    assert report['quest'] == {'host': 1, 'client': 1}, f'the quest no longer runs on both: {report["quest"]}'


def escort_host_case(host, client, quest, npc, report):
    """stageEscort G3_WHO=host: the host takes the slave along; he follows the host at once now (before P9 ~50 s after the
    join, 25.10.), and the client mirrors him."""
    teleport_to(client.bot, npc)
    x, y, z = hero_pos(client.bot)
    move_hero(client.bot, (x + 1500, y, z))
    take_quest(host, npc, ['BPANKRATZ346', 'KALVERAMX233'], report)
    report['following'] = wait_for(lambda: (lambda g: g.get('leader') == 'PC_Hero' and g.get('task', '').startswith('ZS_FollowPlayer') and [g])(
        guide_state(host.bot, npc)), 20, f'{npc} follows the host')[0]
    start = npc_pos(host.bot, npc)
    goal = npc_pos(host.bot, 'Freman')
    for _ in range(40):
        me, here = hero_pos(host.bot), npc_pos(host.bot, npc)
        if here and distance(here, start) > 2000 and distance(here, me) < 700:
            break
        if here and distance(here, me) < 700:
            n = distance(me, goal)
            move_hero(host.bot, tuple(me[i] + (goal[i] - me[i]) * 300 / n for i in range(3)))
        time.sleep(1.5)
    report['followed_m'] = round(distance(npc_pos(host.bot, npc), start) / 100)
    assert report['followed_m'] >= 15, f'{npc} followed the host only {report["followed_m"]} m'
    here, there = npc_pos(host.bot, npc), npc_pos(client.bot, npc)
    report['client_off_m'] = round(distance(here, there) / 100, 1)
    report['client_view'] = guide_state(client.bot, npc)
    assert report['client_off_m'] < 4 and report['client_view'].get('claimed') == '0', f'client: {report["client_off_m"]} m, {report["client_view"]}'


def stageEscort(copies, report, negative=False):
    """P9, an escort quest the joining player takes (Freman_BringSlave_01 in Silden: the escaped slave joins, follows and
    is handed over to Freman). Before, he followed only a host that took him (followdiag G3_VIA=dialog, 25.10.). Now the
    client takes him over while he follows it; at the hand-over he goes back to the host, and the quest succeeds on both.
    Negative control: guidesyncoff."""
    quest, npc, trader = 'Freman_BringSlave_01', 'Silden_EscapedSlave_01', 'Freman'
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report_line(client.bot, 'guidesyncoff', 'GUIDESYNCOFF')
    if os.environ.get('G3_WHO') == 'host':
        return escort_host_case(host, client, quest, npc, report)
    # The host stands 15 m off: it simulates the slave, the client sees a mirror (alone, the client simulated him itself,
    # and the host's copy 300 m away was never driven, 25.10.).
    teleport_to(host.bot, npc)
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 1500, y, z))
    goal = npc_pos(host.bot, trader)
    take_quest(client, npc, ['BPANKRATZ346', 'KALVERAMX233'], report)
    report['quest_run'] = {c.tag: quest_status(c.bot, quest) for c in (host, client)}
    report['following'] = wait_for(lambda: (lambda g: g.get('leader') == 'PC_Hero' and g.get('task', '').startswith('ZS_FollowPlayer')
                                            and g.get('claimed') == '1' and [g])(guide_state(client.bot, npc)), 40, f'{npc} follows the client on the client')[0]
    # 1. The client walks 20 m, a step only once the slave has caught up (a follower left far behind stands still).
    start = npc_pos(client.bot, npc)
    report['walk'] = []
    for _ in range(40):
        me = hero_pos(client.bot)
        here = npc_pos(client.bot, npc)
        gap = distance(here, me) if here else 1e9
        if here and distance(here, start) > 2000 and gap < 700:
            break
        if gap < 700:
            d = [goal[i] - me[i] for i in range(3)]
            n = distance(me, goal)
            move_hero(client.bot, tuple(me[i] + d[i] * 300 / n for i in range(3)))
        time.sleep(1.5)
        report['walk'].append([round(gap / 100), guide_state(client.bot, npc).get('task')])
    here = npc_pos(client.bot, npc)
    report['followed_m'] = round(distance(here, start) / 100) if here and start else 0
    assert report['followed_m'] >= 15, f'{npc} followed the client only {report["followed_m"]} m'
    here, there = npc_pos(client.bot, npc), npc_pos(host.bot, npc)
    report['host_off_m'] = round(distance(here, there) / 100, 1) if here and there else None
    report['host_view'] = next((v for v in npcs(host.bot).values() if v['name'] == npc), {})
    assert report['host_off_m'] is not None and report['host_off_m'] < 4, f'the host shows {npc} elsewhere: {report["host_off_m"]} m'
    assert report['host_view'].get('mirrored') and report['host_view'].get('owner') == 2, f'the host does not mirror {npc} as the client one'
    # 2. The rest of the way (~280 m past wolves that killed the weak slave, 25.10.) is skipped: the client and the slave
    # are put next to Freman; he stays the client's follower.
    move_hero(client.bot, (goal[0] + 600, goal[1] + 50, goal[2]))
    time.sleep(2)
    me = hero_pos(client.bot)
    report['farmove'] = report_line(client.bot, f'farmove {round(me[0]) + 150}_{round(me[1])}_{round(me[2])}_{npc}', ('FARMOVE', 'REFUSED farmove'))
    time.sleep(8)
    for _ in range(3):  # a few steps around: he keeps following
        me = hero_pos(client.bot)
        move_hero(client.bot, (me[0], me[1], me[2] + 250))
        time.sleep(3)
    time.sleep(4)
    report['at_freman'] = guide_state(client.bot, npc)
    here, there = npc_pos(client.bot, npc), npc_pos(host.bot, npc)
    report['slave_to_client_m'] = round(distance(here, hero_pos(client.bot)) / 100, 1) if here else None
    assert report['at_freman'].get('leader') == 'PC_Hero' and report['at_freman'].get('mirrored') == '0', f'{npc} is no longer the client follower: {report["at_freman"]}'
    assert report['slave_to_client_m'] is not None and report['slave_to_client_m'] < 8, f'{npc} did not keep up: {report["slave_to_client_m"]} m'
    # The hand-over (Freman's Delivery info: gold, SetSlaveryEnabled false, the slave's new routine).
    take_quest(client, trader, ['BPANKRATZ3112'], report)
    for copy in (host, client):
        wait_for(lambda: quest_status(copy.bot, quest) == 2 and ['yes'], 30, f'the quest succeeds on the {copy.tag}')
    report['after'] = wait_for(lambda: (lambda g: g.get('claimed') == '0' and [g])(guide_state(client.bot, npc)), 30,
                               f'{npc} goes back to the host after the hand-over')[0]
    report['client_guide'] = report_line(client.bot, 'guide', 'GUIDE ')
    report['walk_owner'] = report['following'].get('claimed')


def stageEscortFar(copies, report, negative=False):
    """P9, an escort over a long way with the joining player alone (Kapitel-Checkliste: Hinbringen): as stageEscort the
    client takes Freman_BringSlave_01 next to the host and the slave follows it; then the host goes far away (G3_HOME_PLACE,
    default Diego), the client brings the slave to Freman and hands him over: the quest succeeds on both. The host comes to
    Freman afterwards and finds the slave there, as the client left him; one machine simulates him, the other mirrors him
    (the client, there first, keeps him: run 181240). Before, the host's copy stood where the host had last seen it, 290 m
    off, and both machines simulated their own copy (run 180656). Negative control: givebackposoff on the client."""
    quest, npc, trader = 'Freman_BringSlave_01', 'Silden_EscapedSlave_01', 'Freman'
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report['negative'] = report_line(client.bot, 'givebackposoff', 'GIVEBACKPOSOFF ')
    teleport_to(host.bot, npc)
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 1500, y, z))
    goal = npc_pos(host.bot, trader)
    take_quest(client, npc, ['BPANKRATZ346', 'KALVERAMX233'], report)
    report['following'] = wait_for(lambda: (lambda g: g.get('leader') == 'PC_Hero' and g.get('claimed') == '1' and [g])(guide_state(client.bot, npc)),
                                   40, f'{npc} follows the client')[0]
    report['host_leaves'] = teleport_to(host.bot, os.environ.get('G3_HOME_PLACE', 'Diego'))
    time.sleep(4)
    me = hero_pos(client.bot)
    report['host_far_m'] = round(distance(hero_pos(host.bot), me) / 100)
    for _ in range(8):  # a few steps towards Freman, the slave behind
        me = hero_pos(client.bot)
        d = [goal[i] - me[i] for i in range(3)]
        n = distance(me, goal)
        move_hero(client.bot, tuple(me[i] + d[i] * 300 / n for i in range(3)))
        time.sleep(2)
    report['slave_walked'] = {'client': npc_pos(client.bot, npc), 'host': npc_pos(host.bot, npc)}
    move_hero(client.bot, (goal[0] + 600, goal[1] + 50, goal[2]))
    time.sleep(2)
    me = hero_pos(client.bot)
    report['farmove'] = report_line(client.bot, f'farmove {round(me[0]) + 150}_{round(me[1])}_{round(me[2])}_{npc}', ('FARMOVE', 'REFUSED farmove'))
    time.sleep(10)
    take_quest(client, trader, ['BPANKRATZ3112'], report)
    try:
        for copy in (host, client):
            wait_for(lambda: quest_status(copy.bot, quest) == 2 and ['yes'], 30, f'the quest succeeds on the {copy.tag}')
    finally:
        report['quest'] = {c.tag: quest_status(c.bot, quest) for c in (host, client)}
    report['after_handover'] = {'client': guide_state(client.bot, npc), 'host_pos': npc_pos(host.bot, npc), 'client_pos': npc_pos(client.bot, npc)}
    time.sleep(10)
    report['host_comes'] = teleport_to(host.bot, trader)
    samples = []
    for _ in range(6):
        time.sleep(5)
        h, c = npc_pos(host.bot, npc), npc_pos(client.bot, npc)
        samples.append({'host_to_freman_m': round(distance(h, goal) / 100, 1) if h else None,
                        'client_to_freman_m': round(distance(c, goal) / 100, 1) if c else None,
                        'host_client_m': round(distance(h, c) / 100, 1) if h and c else None,
                        'host': re.sub(r'FIND name=\S+ found=1 ', '', find_npc(host.bot, npc))[:160],
                        'client': re.sub(r'FIND name=\S+ found=1 ', '', find_npc(client.bot, npc))[:160]})
    report['samples'] = samples
    last = samples[-1]
    assert last['host_client_m'] is not None and last['host_client_m'] < 15, f'the slave stands {last["host_client_m"]} m apart on the two machines'
    mirrored = ['mirrored=1' in last[t] for t in ('host', 'client')]
    assert mirrored.count(True) == 1, f'not exactly one machine simulates the slave: {last}'


def stageGuideSave(copies, report, negative=False):
    """P9 across the host's save: the client takes Mitch_Trolls and Mitch guides it (as in stageGuide); the host saves,
    the client leaves, the host loads that save and the client joins again. The host's sidecar names Mitch as the
    client's (a taken-over NPC counts as a companion there); handed back, he guides the client again (StartGuidePlayer
    for a running follow quest) instead of following it. Negative control: companionsoff on the host."""
    quest, npc = 'Mitch_Trolls', 'Mitch'
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report_line(host.bot, 'companionsoff', 'COMPANIONSOFF ')
    teleport_to(host.bot, npc)
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 1500, y, z))
    take_quest(client, npc, ['SKALVERAM3409', 'SKALVERAM3395'], report, runquests=['Zakosh_KillTrolls'], events=['Mitch_WhereTrolls'])
    report['guiding'] = wait_for(lambda: (lambda g: g.get('claimed') == '1' and g.get('task', '').startswith('ZS_GuidePlayer') and [g])(
        guide_state(client.bot, npc)), 40, f'{npc} guides the client on the client')[0]
    folder = host.root/'isolated-profile/Documents/gothic3'
    for old in list(folder.glob('G3T_P9.*')) + list((client.root/'isolated-profile/Documents/gothic3').glob('*G3T_P9*')):
        old.unlink(missing_ok=True)
    start = host.bot.send('nativesave G3T_P9', 60)
    report['save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
    assert 'ok=1' in report['save'], report['save']
    sidecar = (folder/'G3T_P9.g3t').read_text(errors='replace')
    report['sidecar_companions'] = [l for l in sidecar.splitlines() if l.startswith('companion ')]
    assert any(l.endswith(' Client') for l in report['sidecar_companions']), sidecar
    assert close(client), 'the client did not close'
    client.collect()
    (client.run/'client').rename(client.run/'client-first')
    wait_for(lambda: 2 not in remotes(host.bot), 30, 'host drops the client')
    start = host.bot.send('nativeload G3T_P9', 60)
    report['host_load'] = host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''), 120,
                                        'host nativeload', start)
    wait_running(host, 180, 'host world running after load')
    time.sleep(5)
    start = host.bot.send('releaselock')
    host.bot.line('LOCK scan complete', 10, 'lock scan', start)
    client.launch('client', 'Client')
    wait_running(client, 300, "client world running from the host's campaign")
    time.sleep(5)
    report['quest_after_load'] = {c.tag: quest_status(c.bot, quest) for c in (host, client)}
    report['guiding_again'] = wait_for(lambda: (lambda g: g.get('claimed') == '1' and g.get('hero_leader') == npc
                                                and g.get('task', '').startswith('ZS_GuidePlayer') and [g])(guide_state(client.bot, npc)),
                                       60, f'{npc} guides the client again after the load')[0]
    # He leads on.
    start_at = npc_pos(client.bot, npc)
    for _ in range(15):
        at = npc_pos(client.bot, npc)
        if at:
            move_hero(client.bot, (at[0] - 150, at[1] + 50, at[2]))
        time.sleep(2)
        here = npc_pos(client.bot, npc)
        if here and distance(here, start_at) > 1500:
            break
    report['led_after_load_m'] = round(distance(npc_pos(client.bot, npc), start_at) / 100)
    assert report['led_after_load_m'] >= 10, f'{npc} led the client only {report["led_after_load_m"]} m after the load'


def soultraveldiag(copies, report, negative=False):
    """P6 measurement (soul travel, MagicSoulTravel: a ghost PC_Hero_Ghost through StartTransform, the body stays where it
    was cast): the client casts it 10 m from the host and walks the ghost away; per sample the client's body (findnpc PC_Hero),
    its controlled body (HEROPOS: the ghost), and on the host the client's puppet (position, shape). Diagnostic."""
    host, client = join_both(copies)
    clear_raid(host, report)
    x, y, z = hero_pos(host.bot)
    move_hero(client.bot, (x + 1000, y, z))
    time.sleep(3)
    find_pos = lambda bot, name: (lambda l: tuple(round(float(v)) for v in l.split('pos=')[1].split()[0].split('_')) if 'found=1' in l else None)(
        report_line(bot, 'findnpc ' + name, 'FIND '))

    def sample(tag):
        r = remotes(host.bot).get(2, {})
        pose = puppet_pose(host.bot, 2) or {}
        report['samples'].append({'at': tag, 'client_body': find_pos(client.bot, 'PC_Hero'),
                                  'client_controlled': [round(c) for c in hero_pos(client.bot)],
                                  'host_puppet': [round(c) for c in r.get('puppet_pos', ())], 'host_shape': pose.get('shape'),
                                  'host_body': pose.get('body')})

    report['samples'] = []
    sample('before')
    report_line(client.bot, 'mana', 'MANA ')
    report['ready'] = report_line(client.bot, 'ready It_Spell_SoulTravel', 'READY ')[6:200]
    near = min(((g, v) for g, v in npcs(client.bot).items() if not v['dead']), key=lambda gv: distance(gv[1]['pos'], hero_pos(client.bot)))
    report['cast'] = report_line(client.bot, 'castat ' + near[0], 'CASTAT ')[7:300]
    time.sleep(2)
    report['state'] = report_line(client.bot, 'transformstate', 'TRANSFORMSTATE ')
    sample('cast')
    for i in range(4):
        report_line(client.bot, 'walkby 0', ('WALKBY ',))
        time.sleep(3)
        sample(f'walk{i}')
    for i in range(4):
        time.sleep(5)
        sample(f'later{i}')
    report['state_after'] = report_line(client.bot, 'transformstate', 'TRANSFORMSTATE ')
    report['status_note'] = 'diagnostic only'


def inventory_of(bot):
    start = bot.send('inventory', 20)
    bot.line('INV end', 10, 'inventory', start)
    found = {}
    for m in re.finditer(r'INV name=(\S+) amount=(-?\d+)', bot.log()[start:]):
        found[m[1]] = found.get(m[1], 0) + int(m[2])
    return found


def treasurediag(copies, report, negative=False, prepare=None):
    """P1 measurement, a pirate treasure (PS_Interact_PirateTreasure: a shovel in hand, the treasure's loot from the game's
    treasure sets, a game event): the one in Faring (objectscan 24.10.: 46 m from Tarok). Both stand at it; the host digs,
    then the client; per player what it gained and the treasure as both machines see it. Can both dig up the same
    treasure? G3_TREASURE, G3_TREASURE_AT ("x_y_z"), G3_NEAR (an NPC to teleport to first), G3_TREASURE_ITEM (given to the
    digger first; default It_Shovel, - for none), G3_FIRST=client: the client digs first. Diagnostic."""
    name = os.environ.get('G3_TREASURE', 'G3_Object_Interact_PirateTreasure_01')
    at = tuple(float(v) for v in os.environ.get('G3_TREASURE_AT', '59531_4645_57760').split('_'))
    host, client = join_both(copies)
    clear_raid(host, report)
    if prepare:
        prepare(host, client)
    for c, dz in ((host, 80), (client, -80)):  # both west of it: the host east of it (+150) did not start digging (20:16)
        teleport_to(c.bot, os.environ.get('G3_NEAR', 'Tarok'))
        time.sleep(3)
        move_hero(c.bot, (at[0] - 150, at[1] + 50, at[2] + dz))
    time.sleep(6)
    report['before'] = {c.tag: use_info(c.bot, name) for c in (host, client)}
    order = ((client, host), (host, client)) if os.environ.get('G3_FIRST') == 'client' else ((host, client), (client, host))
    for digger, other in order:
        tool = os.environ.get('G3_TREASURE_ITEM', 'It_Shovel')  # the first run without one: PS_Interact, then back at once
        if tool != '-':
            report[digger.tag + '_tool'] = report_line(digger.bot, 'receiveitem ' + tool, ('RECEIVEITEM ', 'REFUSED receiveitem'))
        before = inventory_of(digger.bot)
        report[digger.tag + '_use'] = report_line(digger.bot, 'use ' + name, ('USE ', 'REFUSED use'))
        states = []
        for _ in range(15):
            time.sleep(1)
            states.append(use_field(use_info(digger.bot, name), 'hero_state'))
        report[digger.tag + '_states'] = sorted(set(s for s in states if s))
        # The loot lies in the treasure (GeneratePlunderInvFromTreasureSets): what each machine sees in it, then the digger
        # takes it all (QA loottake, as from the loot window).
        guid = use_field(use_info(digger.bot, name), 'guid')
        report[digger.tag + '_loot'] = {c.tag: report_line(c.bot, 'contents ' + guid, ('CONTENTS ', 'REFUSED contents')) for c in (host, client)}
        taken = report[digger.tag + '_taken'] = []
        for _ in range(12):
            line = report_line(digger.bot, 'loottake ' + guid, ('LOOTTAKE ', 'REFUSED loottake'))
            if 'REFUSED' in line:
                break
            taken.append(line[9:])
        time.sleep(2)
        report[digger.tag + '_loot_after'] = {c.tag: report_line(c.bot, 'contents ' + guid, ('CONTENTS ', 'REFUSED contents')) for c in (host, client)}
        after = inventory_of(digger.bot)
        report[digger.tag + '_gained'] = {k: after.get(k, 0) - before.get(k, 0) for k in set(after) | set(before) if after.get(k, 0) != before.get(k, 0)}
        report[digger.tag + '_dug_sees'] = {c.tag: use_info(c.bot, name) for c in (host, client)}
        # The loot window (PS_HUD_Loop) keeps the digger the treasure's user, so the other is refused ("... is using this.",
        # 20:20): close it as the Escape key does, then the other tries the same treasure.
        post_key(digger.process.pid, 0x1B)
        time.sleep(3)
        report[digger.tag + '_closed'] = {c.tag: use_info(c.bot, name) for c in (host, client)}
        # Single-player check: may the digger dig it again?
        report[digger.tag + '_again'] = report_line(digger.bot, 'use ' + name, ('USE ', 'REFUSED use'))
        again = []
        for _ in range(8):
            time.sleep(1)
            again.append(use_field(use_info(digger.bot, name), 'hero_state'))
        report[digger.tag + '_again_states'] = sorted(set(a for a in again if a))
        post_key(digger.process.pid, 0x1B)
        time.sleep(3)
    report['status_note'] = 'diagnostic only'


def stageTreasure(copies, report, negative=False):
    """P1, a pirate treasure is looted once: the joining player digs up the one in Faring (treasurediag) and takes all
    its loot; it gains each item once, the treasure is empty on both PCs, and the host, digging after it, finds nothing.
    Before 24.10. the client's loot window of a treasure was not its open container: its takes were never sent, the
    host's contents came back, and it took them again (ore 10 of 5; the host then took all 5 as well). Negative control:
    containersyncoff interact on the client."""
    os.environ.setdefault('G3_FIRST', 'client')
    prepare = (lambda host, client: report.__setitem__('negative', report_line(client.bot, 'containersyncoff interact', 'CONTAINERSYNCOFF '))) if negative else None
    treasurediag(copies, report, prepare=prepare)
    report.pop('status_note', None)
    items = lambda line: {} if line.endswith('items=-') else {k: int(v) for k, v in (x.split(':') for x in line.split(' items=')[1].split(','))}
    loot = items(report['client_loot']['client'])
    assert loot, f"the client's dig found no loot: {report['client_loot']}"
    assert report['client_gained'] == loot, f"the client gained {report['client_gained']} from loot {loot}"
    after = {tag: items(line) for tag, line in report['client_loot_after'].items()}
    assert after == {'host': {}, 'client': {}}, f'the treasure is not empty after the take: {after}'
    assert not report['host_gained'], f"the host took the treasure's loot again: {report['host_gained']}"


def hp_counters(bot):
    line = report_line(bot, 'hp', 'HP ')
    return {k: int(v) for k, v in re.findall(r' (claims_sent|npc_hits_sent|xp_awarded|xp)=(-?\d+)', line)}


def stageShapeFight(copies, report, negative=False):
    """P6, a player in an animal form fights as the player (the game counts the creature it controls as Entity::GetPlayer;
    GetHero stays the hidden human body): the client turns into a wolf and strikes a villager the host simulates; its hits
    go to the host as the player's claims (before: as hits of one of the client's NPCs, no experience, no knock-out
    judgement, no murder), and the client earns the experience of the knock-out or kill. G3_SHAPE_ITEM=It_Potion_Wolf: the
    client turns by drinking the game's transformation potion (MagicTransform) instead. Negative control: shapehitsoff on
    the client."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report['negative'] = report_line(client.bot, 'shapehitsoff', 'SHAPEHITSOFF ')
    keep = ('Diego', 'Gorn', 'Milten', 'Lester')
    calm = [g for g, v in npcs(host.bot).items() if not v['dead'] and not v['mirrored'] and 'Orc' not in v['name']
            and v['name'] not in keep and v['name'] != 'PC_Hero' and v['hp'] > 0]
    assert calm, 'no villager near the host'
    target = next((g for g in calm if npcs(host.bot)[g]['name'].startswith('Slave')), calm[0])
    report['target'] = npcs(host.bot)[target]['name']
    goal = npcs(host.bot)[target]['pos']
    move_hero(client.bot, (goal[0] + 150, goal[1], goal[2]))
    time.sleep(2)
    # G3_SHAPE_ITEM (a transformation potion, It_Potion_Wolf): the client drinks it from a quick slot, so the game's own
    # MagicTransform turns it (SendPerceptionTransform, the potion's spawn), instead of the QA transform.
    item = os.environ.get('G3_SHAPE_ITEM')
    if item:
        report['give'] = report_line(client.bot, f'givepotion {item}-2', ('GIVEPOTION ', 'REFUSED givepotion'))
    shape = lambda: (report_line(client.bot, 'quickuse ' + item, ('QUICKUSE ', 'REFUSED quickuse')) if item
                     else report_line(client.bot, 'transform Wolf', ('TRANSFORM ',)))
    report['transform'] = shape()
    time.sleep(4)
    report['shape'] = report_line(client.bot, 'transformstate', ('TRANSFORMSTATE ',))
    assert ' same=0 ' in report['shape'], f"the client did not change its shape: {report['shape']}"
    before = report['before'] = hp_counters(client.bot)
    report_line(host.bot, f"npchp {report['target']}_30", 'NPCHP ')
    report['hits'] = []
    for _ in range(25):
        hit = hit_npc(client.bot, target)
        report['hits'].append(hit['line'][-100:])
        time.sleep(0.8)
        state = npcs(host.bot).get(target, {})
        if state.get('dead') or state.get('task') in KO_TASKS:
            break
    time.sleep(3)
    after = report['after'] = hp_counters(client.bot)
    report['host_state'] = npcs(host.bot).get(target, {})
    report['back'] = report_line(client.bot, 'transformback', ('TRANSFORM',))
    assert after['claims_sent'] > before['claims_sent'], f'the wolf\'s hits did not go out as the player\'s: {before} -> {after}'
    # Rewarded the player's way (GiveXP for this hero); a slave is worth no experience (24.10.: xp 750 -> 750).
    assert after['xp_awarded'] > before['xp_awarded'], f"the wolf's kill was not rewarded to the player: {before} -> {after}"
    # The other way round: an NPC of the host strikes the wolf; the creature takes it, not the hidden human body.
    report['retransform'] = shape()
    time.sleep(3)
    shape_before = report['shape_before'] = report_line(client.bot, 'transformstate', ('TRANSFORMSTATE ',))
    striker = next((g for g, v in npcs(host.bot).items() if not v['dead'] and not v['mirrored'] and g != target and 'Orc' not in v['name']
                    and v['name'] not in keep and v['name'] != 'PC_Hero'), None)
    assert striker, 'no NPC of the host to strike the wolf'
    report['struck'] = [report_line(host.bot, 'hitpuppetby ' + striker, ('HITPUPPETBY ', 'REFUSED hitpuppetby')) for _ in range(3)]
    time.sleep(3)
    shape_after = report['shape_after'] = report_line(client.bot, 'transformstate', ('TRANSFORMSTATE ',))
    report_line(client.bot, 'transformback', ('TRANSFORM',))
    hp = lambda l, k: int(re.search(rf' {k}=(-?\d+)', l)[1])
    assert hp(shape_after, 'player_hp') < hp(shape_before, 'player_hp'), f'the wolf took nothing: {shape_before} -> {shape_after}'
    assert hp(shape_after, 'hero_hp') == hp(shape_before, 'hero_hp'), f'the hidden human body took it: {shape_before} -> {shape_after}'


def seated(line):
    """The hero uses the object (USEINFO): its user is this machine's hero, in one of the game's interaction states (the
    seated ones: PS_Interact_SitStool, then PS_Interact_WaitForExit)."""
    return use_field(line, 'user') == 'PC_Hero' and (use_field(line, 'hero_state') or '').startswith('PS_Interact_')


def stageSharedUse(copies, report, negative=False):
    """P1 (plan to 90 %), one user per object as in single-player: the game sets an object's user (Interaction.User) on the
    machine where its user acts, so two heroes sat on one stool and at a campfire the second stayed in PS_Interact (A,
    usediag 20.10.). The host sits on a free stool near the start; the client sees the host's puppet as its user; the
    client sets out to use the same stool (QA use: PS_Interact, as the use key): it is stopped before it sits ("<Name> is
    using this."); the host stands up: the client sees the stool free, uses it and sits. Negative control: usesyncoff on
    both (the client sits on the host's stool)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report['negative'] = [report_line(c.bot, 'usesyncoff', 'USESYNCOFF ') for c in (host, client)]
    # G3_USE_AT: an NPC both go to first; G3_USETYPE: another kind of object (57: anvil, the Community Patch's
    # PS_Interact_Anvil; Tarok in Faring has one).
    if os.environ.get('G3_USE_AT'):
        report['teleport'] = {c.tag: teleport_to(c.bot, os.environ['G3_USE_AT'])[:80] for c in (host, client)}
        time.sleep(8)
    kind = os.environ.get('G3_USETYPE', '83')
    around = usables(host.bot, os.environ.get('G3_USE_RADIUS', 'all4000'))
    if kind != '83':
        report['candidates'] = [(u['name'], u['user'], u['distance']) for u in around if u['usetype'] == kind]
    stools = sorted((u for u in around if u['usetype'] == kind and u['user'] == '-'), key=lambda u: float(u['distance']))
    assert stools, f'no free object of use type {kind} within 40 m of the host'
    stool = report['stool'] = stools[0]['name']
    report['host_use'] = report_line(host.bot, 'use ' + stool, ('USE ', 'REFUSED use'))
    report['host_sits'] = wait_for(lambda: (lambda l: seated(l) and l)(use_info(host.bot, stool)), 15,
                                   'the host sits down')
    move_hero(client.bot, hero_pos(host.bot))
    time.sleep(3)
    report['client_sees'] = use_info(client.bot, stool)
    report['client_use'] = report_line(client.bot, 'use ' + stool, ('USE ', 'REFUSED use'))
    samples = []
    for _ in range(6):
        time.sleep(1)
        samples.append(use_info(client.bot, stool))
    states = report['client_states'] = [use_field(l, 'hero_state') for l in samples]
    report['occupancy'] = {c.tag: report_line(c.bot, 'occupancy', 'OCCUPANCY ') for c in (host, client)}
    assert not any(seated(l) for l in samples), f"the client sat on the host's stool: {states}"
    assert not (states[-1] or '').startswith('PS_Interact'), f'the client hangs at the stool: {states}'
    report['unuse'] = report_line(host.bot, 'unuse', ('UNUSE ', 'REFUSED unuse'))
    report['client_freed'] = wait_for(lambda: (lambda l: use_field(l, 'user') == '-' and l)(use_info(client.bot, stool)), 10,
                                      'the client sees the stool free')
    report['client_use_free'] = report_line(client.bot, 'use ' + stool, ('USE ', 'REFUSED use'))
    report['client_sits'] = wait_for(lambda: (lambda l: seated(l) and l)(use_info(client.bot, stool)), 15,
                                     'the client sits on the free stool')


def pick_cands(bot):
    start = bot.send('pickcands', 20)
    bot.line('PICKCANDS end', 10, 'pickcands', start)
    return [dict(re.findall(r'(\w+)=(\S+)', l)) for l in bot.log()[start:].splitlines() if l.startswith('PICKCAND guid=')]


def stagePickPocket(copies, report, negative=False):
    """P1 (plan to 90 %), a picked pocket is picked for everyone: the game decides it on the thief's machine (PickPocket:
    loot from the NPC's treasure sets for the thief, a theft for its town, the mark Dialog.PickedPocket so that nobody picks
    it again), and the mark stayed on the thief's copy. The client picks the pocket of an NPC the host simulates (the
    client's hero has learned it, thiefprep: Perk_PickPocket_3, theft 150): the host's NPC is marked too.
    Negative control: pickpocketoff on the client (the host's NPC stays unmarked)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report['negative'] = report_line(client.bot, 'pickpocketoff', 'PICKPOCKETOFF ')
    report['thief'] = report_line(client.bot, 'thiefprep', ('THIEFPREP ', 'REFUSED thiefprep'))  # as a hero who learned it
    time.sleep(3)
    cands = [c for c in pick_cands(client.bot) if c['mirrored'] == '1' and c['picked'] == '0' and c['down'] == '0' and not c['name'].startswith('G3T_')]
    cands.sort(key=lambda c: (int(c['level']), float(c['dist'])))
    report['candidates'] = [(c['name'], c['level'], c['dist']) for c in cands[:8]]
    assert cands, 'no NPC of the host within 30 m whose pocket the client could pick'
    report['tries'] = []
    chosen = None
    for c in cands[:4]:
        line = report_line(client.bot, 'pickpocket ' + c['guid'], ('PICKPOCKET ', 'REFUSED pickpocket'))
        report['tries'].append(line)
        if ' picked_after=1' in line:
            chosen = c
            break
        time.sleep(3)
    assert chosen, f'the client picked no pocket: {report["tries"]}'
    # The loot comes from the NPC's treasure sets; some have none (Diego, 24.10.).
    report['loot'] = {k: int(v) for k, v in re.findall(r'(items_before|items_after)=(-?\d+)', report['tries'][-1])}
    report['victim'] = chosen['name']
    marked = wait_for(lambda: (lambda l: ' picked=1' in l and l)(report_line(host.bot, 'pickinfo ' + chosen['guid'], ('PICKINFO ', 'REFUSED pickinfo'))),
                      10, 'the host marks the NPC as picked') if not negative else None
    if negative:
        time.sleep(5)  # as long as the sync would take, and more
    report['host_info'] = marked or report_line(host.bot, 'pickinfo ' + chosen['guid'], ('PICKINFO ', 'REFUSED pickinfo'))
    report['host_sync'] = report_line(host.bot, 'picksync', 'PICKSYNC ')
    report['client_sync'] = report_line(client.bot, 'picksync', 'PICKSYNC ')
    assert ' picked=1' in report['host_info'], f'the host\'s {chosen["name"]} is still unpicked: {report["host_info"]}'


DEATH_TASKS = ('ZS_RagDollDead', 'ZS_PiercedDead', 'ZS_Dead')
KO_TASKS = ('ZS_Unconscious', 'ZS_LieKnockDown', 'ZS_PiercedKO')


def murder_town(host, report, default=None):
    """A town with routine status (G3_TOWN, else `default`, else the first with members) and two members whose murder the
    game counts there (QA murdercand: counts=1), unique names, not Zuben (a special case in NotifyEnclave, C 12:28)."""
    start = host.bot.send('enclaves', 20)
    host.bot.line('ENCLAVES end', 15, 'enclaves', start)
    towns = [m.groupdict() for m in re.finditer(r'ENCLAVE members=(?P<members>\d+) first=\S+ status=(?P<status>\d+) enclave=(?P<name>\S+)', host.bot.log()[start:])]
    wanted = os.environ.get('G3_TOWN', default)
    town = next((t['name'] for t in towns if (t['name'] == wanted if wanted else t['status'] == '0' and int(t['members']) >= 4)), None)
    assert town, f'no town with routine status and members: {towns}'
    report['town'] = town
    start = host.bot.send('murdercand ' + town, 20)
    host.bot.line('MURDERCAND end', 15, 'murdercand', start)
    cands = [dict(re.findall(r'(\w+)=(\S+)', l)) for l in host.bot.log()[start:].splitlines() if l.startswith('MURDERCAND enclave=')]
    from collections import Counter
    names = Counter(c['member'] for c in cands)
    good = [c['member'] for c in cands if c['alive'] == '1' and c['counts'] == '1' and names[c['member']] == 1 and c['member'] != 'Zuben']
    assert len(good) >= 2, f'fewer than two countable members in {town}: {cands[:10]}'
    return town, good


def murder_counts(bot, town):
    counts = enclave_counts(bot, town)
    return counts[4] if counts else None


def stageMurder(copies, report, negative=False):
    """P7 (plan to 90 %), murders by the other machine's player count for the town as in single-player (C
    re/findings/notify-enclave-murder-followup-20261024-c.md). In a town with routine status: (1) the host's hero beats a
    member down and finishes it with its own hits: the game counts one murder (positive control); (2) the client's hits on
    another member (applied on the host): the lethal one knocks it out as the game decided on the client (24.10. it killed
    at once), no murder; (3) the client finishes it lying there: one murder, on both machines. Negative control (host):
    remotemurdersoff (no murder in 3); with G3_MURDER_OFF=ko: remotekooff (2 kills). G3_REPUTATION=personal
    or together: that mode in the host's INI; personal: each murder counts only on its killer's machine (EnclaveCredit).
    """
    reputation = os.environ.get('G3_REPUTATION', 'shared')
    report['reputation'] = reputation
    if reputation != 'shared':
        copies['host'].ini_extra = f'reputation = {reputation}\n'
    personal = reputation == 'personal'
    host, client = join_both(copies)
    clear_raid(host, report)
    town, good = murder_town(host, report)
    local_victim, remote_victim = good[0], good[1]
    if negative:
        off = {'ko': 'remotekooff', 'queue': 'creditqueueoff'}.get(os.environ.get('G3_MURDER_OFF', ''), 'remotemurdersoff')
        report['negative'] = report_line(host.bot, off, off.upper() + ' ')
    # (1) Positive control: the host's hero, with its own hits, until the game sets a death task.
    teleport_to(host.bot, local_victim)
    report_line(host.bot, 'arm ' + WEAPON, 'ARM ')
    time.sleep(4)
    guid = next((g for g, v in npcs(host.bot).items() if v['name'] == local_victim), None)
    assert guid, f'{local_victim} is not among the host NPCs'
    before = {c.tag: murder_counts(c.bot, town) for c in (host, client)}
    report_line(host.bot, f'npchp {local_victim}_30', 'NPCHP ')
    report['local_hits'] = []
    for _ in range(40):
        hit = hit_npc(host.bot, guid)
        report['local_hits'].append(hit['line'][-100:])
        if any(f'task={t} ' in hit['line'] for t in DEATH_TASKS):
            break
        time.sleep(1.5 if 'Stumble' in hit['line'] else 0.4)
    time.sleep(8)
    after = {c.tag: murder_counts(c.bot, town) for c in (host, client)}
    report['local'] = {'victim': local_victim, 'before': before, 'after': after}
    assert after['host'] == before['host'] + 1, f'positive control: the host\'s own murder of {local_victim} counted {before} -> {after}'
    if personal:
        assert after['client'] == before['client'], f'personal: the host\'s murder changed the client\'s record {before} -> {after}'
    # (2) The client knocks another member out.
    teleport_to(host.bot, remote_victim)
    teleport_to(client.bot, remote_victim)
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 600, y, z))
    report_line(client.bot, 'arm ' + WEAPON, 'ARM ')
    time.sleep(4)
    guid = next((g for g, v in npcs(host.bot).items() if v['name'] == remote_victim), None)
    assert guid, f'{remote_victim} is not among the host NPCs'
    before = {c.tag: murder_counts(c.bot, town) for c in (host, client)}
    xp_before = re.search(r' xp=(-?\d+)', report_line(client.bot, 'hp', 'HP '))
    report_line(host.bot, f'npchp {remote_victim}_30', 'NPCHP ')
    time.sleep(2)
    report['ko_hits'] = []
    state = {}
    host_ko = os.environ.get('G3_MURDER_HOSTKO') == '1'  # the host knocks it out with its own hits (C review 2)
    if host_ko:
        report_line(host.bot, 'arm ' + WEAPON, 'ARM ')
    for _ in range(30):
        report_line(client.bot, 'arm ' + WEAPON, 'ARM ')
        hit = hit_npc(host.bot if host_ko else client.bot, guid)
        report['ko_hits'].append(hit['line'][-110:])
        time.sleep(0.6)
        state = npcs(host.bot).get(guid, {})
        if state.get('dead') or state.get('task') in KO_TASKS or state.get('hp', 1) <= 0:
            break
        time.sleep(1.5 if 'Stumble' in hit['line'] else 0.4)
    time.sleep(3)
    state = npcs(host.bot).get(guid, {})
    ko = {c.tag: murder_counts(c.bot, town) for c in (host, client)}
    xp_ko = re.search(r' xp=(-?\d+)', report_line(client.bot, 'hp', 'HP '))
    report['knockout'] = {'victim': remote_victim, 'state': state, 'before': before, 'after': ko,
                          'xp': [int(m[1]) if m else None for m in (xp_before, xp_ko)]}
    report['host_hp'] = report_line(host.bot, 'hp', 'HP ')
    assert not state.get('dead'), f'the client\'s lethal hit killed {remote_victim} (the game knocks out): {state}'
    assert state.get('task') in KO_TASKS, f'{remote_victim} not knocked out: {state}'
    assert ko == before, f'a knock-out counted as murder: {before} -> {ko}'
    report['ko_by'] = 'host' if host_ko else 'client'
    # (3) The client finishes it.
    time.sleep(4)
    hold = os.environ.get('G3_CREDIT_HOLD')  # personal: the killer counts as out of the host's world that many ms
    if hold:
        report['credit_hold'] = report_line(host.bot, 'credithold ' + hold, 'CREDITHOLD ')
    report['finish_hits'] = []
    for _ in range(30):
        hit = hit_npc(client.bot, guid)
        report['finish_hits'].append(hit['line'][-110:])
        time.sleep(0.6)
        if npcs(host.bot).get(guid, {}).get('dead'):
            break
        time.sleep(0.6)
    time.sleep(8)
    after = {c.tag: murder_counts(c.bot, town) for c in (host, client)}
    report['murder'] = {'dead': npcs(host.bot).get(guid, {}).get('dead'), 'before': ko, 'after': after}
    report['host_sync'] = report_line(host.bot, 'enclavesync', 'ENCLAVESYNC ')
    report['host_log'] = [l for l in host.mod_log().splitlines() if 'knocked' in l or 'killed' in l or 'murder' in l or 'NotifyEnclave' in l][-8:]
    assert report['murder']['dead'], f'the client could not finish {remote_victim}'
    expected = {'host': ko['host'], 'client': ko['client'] + 1} if personal else {'host': ko['host'] + 1, 'client': ko['client'] + 1}
    assert after == expected, f'the client\'s murder of {remote_victim} ({reputation}): {ko} -> {after}, expected {expected}'


def murderwitnessdiag(copies, report, negative=False):
    """P7 measurement, witnesses of a murder: what the game's AssessMurder does on both machines when (1) the host's hero
    kills a town member (single-player behaviour) and (2) the client's hero kills another one (the host's hero 600 away).
    Records the mod's murderwitness lines, the WITNESS counters and the tasks of NPCs near each victim; asserts nothing."""
    host, client = join_both(copies)
    clear_raid(host, report)
    town, good = murder_town(host, report)

    def around(bot, pos):
        return {v['name']: v['task'] for v in npcs(bot).values()
                if not v['dead'] and sum((a - b) ** 2 for a, b in zip(v['pos'], pos)) < 2500 ** 2}

    def watch(label, killer, victim, finish, blow=False):
        marks = {c.tag: len(c.mod_log()) for c in (host, client)}
        guid = next((g for g, v in npcs(host.bot).items() if v['name'] == victim), None)
        assert guid, f'{victim} is not among the host NPCs'
        pos = npcs(host.bot)[guid]['pos']
        tasks_before = around(host.bot, pos)
        report_line(host.bot, f'npchp {victim}_30', 'NPCHP ')
        report_line(killer.bot, 'sethp 1000', 'SETHP ')
        hits = []
        for _ in range(60):
            report_line(killer.bot, 'arm ' + WEAPON, 'ARM ')
            hits.append(hit_npc(killer.bot, guid)['line'][-100:])
            time.sleep(0.6)
            state = npcs(host.bot).get(guid, {})
            if state.get('dead') or (not finish and state.get('task') in KO_TASKS):
                break
            time.sleep(0.6)
        if blow:  # the finishing blow as a player strikes one lying there (PS_Melee_FinishingAttack)
            time.sleep(3)
            for _ in range(5):
                report_line(killer.bot, 'sethp 1000', 'SETHP ')  # the town's guards may have struck the killer down
                hits.append(report_line(killer.bot, 'finishat ' + guid, 'FINISHAT ')[-120:])
                time.sleep(4)
                if npcs(host.bot).get(guid, {}).get('dead'):
                    break
        time.sleep(6)
        report[label] = {
            'victim': victim, 'dead': npcs(host.bot).get(guid, {}).get('dead'), 'hits': hits[-4:],
            'tasks_before': tasks_before, 'tasks_after': around(host.bot, pos), 'client_tasks_after': around(client.bot, pos),
            'host_hero': report_line(host.bot, 'hp', 'HP '), 'client_hero': report_line(client.bot, 'hp', 'HP '),
            'witness': {c.tag: report_line(c.bot, 'witnesses', 'WITNESS ') for c in (host, client)},
            'log': {c.tag: [l for l in c.mod_log()[marks[c.tag]:].splitlines() if 'murderwitness' in l or 'witness:' in l][-30:]
                    for c in (host, client)}}

    teleport_to(host.bot, good[0])
    report_line(host.bot, 'arm ' + WEAPON, 'ARM ')
    time.sleep(4)
    watch('host_kill', host, good[0], False, blow=True)
    teleport_to(host.bot, good[1])
    teleport_to(client.bot, good[1])
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 600, y, z))
    time.sleep(4)
    watch('client_ko', client, good[1], False)
    time.sleep(4)
    watch('client_kill', client, good[1], True)


def defeatwitnessdiag(copies, report, negative=False):
    """P7 measurement, onlookers of a knock-out outside a bout (AssessDefeat, Script_Game 0x29fb0: help the one knocked
    out, attack the other or flee; a duel returns at once): (1) the host's hero knocks a town member out (single-player
    behaviour), (2) the client's hero knocks another out (the host's hero 600 away; the host tells those around with
    SendPerceptionDefeat, witness.cpp KnockOutSeen). Records the FIGHTMEM counters (defeats_seen, defeats_yes), the
    fightmem 'saw ... knocked out' lines and the tasks near each victim; asserts nothing. Negative (host): murderseenoff."""
    host, client = join_both(copies)
    clear_raid(host, report)
    town, good = murder_town(host, report)
    if negative:
        report['negative'] = report_line(host.bot, 'murderseenoff', 'MURDERSEENOFF ')

    def around(bot, pos):
        return {v['name']: v['task'] for v in npcs(bot).values()
                if not v['dead'] and sum((a - b) ** 2 for a, b in zip(v['pos'], pos)) < 2500 ** 2}

    def knock(label, attacker, victim):
        marks = {c.tag: len(c.mod_log()) for c in (host, client)}
        counts = {c.tag: report_line(c.bot, 'fightmem', 'FIGHTMEM ') for c in (host, client)}
        guid = next((g for g, v in npcs(host.bot).items() if v['name'] == victim), None)
        assert guid, f'{victim} is not among the host NPCs'
        pos = npcs(host.bot)[guid]['pos']
        tasks_before = around(host.bot, pos)
        report_line(host.bot, f'npchp {victim}_30', 'NPCHP ')
        report_line(attacker.bot, 'sethp 1000', 'SETHP ')
        hits = []
        for _ in range(60):
            report_line(attacker.bot, 'arm ' + WEAPON, 'ARM ')
            hits.append(hit_npc(attacker.bot, guid)['line'][-100:])
            time.sleep(0.6)
            state = npcs(host.bot).get(guid, {})
            if state.get('dead') or state.get('task') in KO_TASKS:
                break
            time.sleep(0.6)
        time.sleep(6)
        report[label] = {
            'victim': victim, 'state': npcs(host.bot).get(guid, {}).get('task'), 'hits': hits[-3:],
            'tasks_before': tasks_before, 'tasks_after': around(host.bot, pos),
            'fightmem_before': counts, 'fightmem': {c.tag: report_line(c.bot, 'fightmem', 'FIGHTMEM ') for c in (host, client)},
            'log': {c.tag: [l[13:] for l in c.mod_log()[marks[c.tag]:].splitlines() if 'knocked out by' in l or 'knocked' in l][-30:]
                    for c in (host, client)}}

    teleport_to(host.bot, good[0])
    report_line(host.bot, 'arm ' + WEAPON, 'ARM ')
    time.sleep(4)
    knock('host_ko', host, good[0])
    teleport_to(host.bot, good[1])
    teleport_to(client.bot, good[1])
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 600, y, z))
    time.sleep(4)
    knock('client_ko', client, good[1])


def stageDefeatSeen(copies, report, negative=False):
    """P7, onlookers of the other player's knock-out (AssessDefeat, Script_Game 0x29fb0). The game tells those around of
    its own player's knock-outs from its hit path (SendPerceptionDefeat after a hit judged as a knock-out); the client's
    knock-outs are applied on the host (combat.cpp), which tells those around (witness.cpp KnockOutSeen) with the puppet as
    the player (fightmem.cpp). defeatwitnessdiag 26.10. (Ishtar): onlookers of the host's hero (knocked out by a guard)
    took up the fight against it; onlookers of the client's knock-out of Surus attacked the puppet. In the first town with
    routine status (G3_TOWN), the client knocks a member out, the host's hero 6 m off: at least one NPC on the host judges
    the knock-out with the puppet (FIGHTMEM defeats). Whether one reacts depends on the scene as in single-player (Ishtar
    26.10.: once two attacked the puppet, once all were fighting it already and none did); recorded, not asserted.
    Negative control (host): murderseenoff (nobody is told)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    town, good = murder_town(host, report)
    if negative:
        report['negative'] = report_line(host.bot, 'murderseenoff', 'MURDERSEENOFF ')
    victim = good[1]
    teleport_to(host.bot, victim)
    teleport_to(client.bot, victim)
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 600, y, z))
    time.sleep(4)
    mark = len(host.mod_log())
    before = report_line(host.bot, 'fightmem', 'FIGHTMEM ')
    guid = next((g for g, v in npcs(host.bot).items() if v['name'] == victim), None)
    assert guid, f'{victim} is not among the host NPCs'
    report_line(host.bot, f'npchp {victim}_30', 'NPCHP ')
    report_line(client.bot, 'sethp 1000', 'SETHP ')
    hits = []
    for _ in range(60):
        report_line(client.bot, 'arm ' + WEAPON, 'ARM ')
        hits.append(hit_npc(client.bot, guid)['line'][-100:])
        time.sleep(0.6)
        state = npcs(host.bot).get(guid, {})
        if state.get('dead') or state.get('task') in KO_TASKS:
            break
        time.sleep(0.6)
    time.sleep(6)
    after = report_line(host.bot, 'fightmem', 'FIGHTMEM ')
    seen = [l[13:] for l in host.mod_log()[mark:].splitlines() if f'saw {victim} knocked out by' in l]
    report.update({'victim': victim, 'state': state.get('task'), 'hits': hits[-3:], 'fightmem': [before, after], 'seen': seen[:12]})
    count = lambda line, key: int(re.search(rf' {key}=(\d+)', line)[1])
    report['puppet_defeats'] = count(after, 'defeats') - count(before, 'defeats')
    assert state.get('task') in KO_TASKS, f'the client did not knock {victim} out: {state}'
    puppet_seen = [l for l in seen if '(a puppet)' in l]
    if negative:
        assert report['puppet_defeats'] == 0 and not puppet_seen, f'murderseenoff: still judged {report["puppet_defeats"]}, {puppet_seen}'
        return
    assert report['puppet_defeats'] >= 1, f'no NPC on the host judged the knock-out of {victim} by the puppet: {before} -> {after}'


def run_up(copy, host, npc):
    """The player at `copy` comes back to `npc`: 8 m off, then runs up to it (the game's perception, Script_Game
    0x8bd0, takes the player in view with a free line of sight, or within 10 m when not standing)."""
    teleport_to(copy.bot, npc)
    x, y, z = hero_pos(copy.bot)
    move_hero(copy.bot, (x + 800, y + 30, z))
    time.sleep(1)
    pos = next((v['pos'] for bot in (copy.bot, host.bot) for v in npcs(bot).values() if v['name'] == npc), None)
    if pos:  # its own list first: the host may be too far off to list the NPC (stageUpset 26.10.: 'no position')
        return report_line(copy.bot, f'runto {pos[0]:.0f}_{pos[1]:.0f}_{pos[2]:.0f}', ('RUNTO ', 'REFUSED runto'))
    return 'no position'


def upsetdiag(copies, report, negative=False):
    """P7 measurement, a fight the NPC gave up (CancelAttack 0x1a4d0: LastFightAgainstPlayer Cancel and
    SetLastFightTimestamp 0x113d0, both only for GetPlayer) and the grudge on sight (IsUpset 0x115d0, AssessActivePerception
    slot 0, the local player only: Cancel, a timestamp, LastPlayerAR within its time window, then AssessTarget again). An
    NPC (G3_UPSET_NPC) attacks the actor (G3_WHO=host: the host's hero, QA npcfight, the single-player reference; else the
    client's puppet, QA aggro); the actor jumps 60 m away and the NPC gives up (QA cancelattack, the game's script; the
    chase after the puppet did not end in 40 s, G3_UPSET_CHASE=1 waits for it); then the other player walks up to it,
    then the actor comes back. Records the NPC's memory (fightmemory, crime: reason, last_player_ar, target) on both machines
    after each step; asserts nothing."""
    npc = os.environ.get('G3_UPSET_NPC', 'Javier')
    who = os.environ.get('G3_WHO', 'host')
    host, client = join_both(copies)
    clear_raid(host, report)
    actor, other = (host, client) if who == 'host' else (client, host)
    for c in (host, client):  # the host first: it simulates the town
        teleport_to(c.bot, npc)
        time.sleep(2)

    def mem(tag):
        report.setdefault('mem', {})[tag] = {c.tag: {
            'fm': re.sub(r'^name=\S+ ', '', report_line(c.bot, f'fightmemory {npc}', ('FIGHTMEMORY ', 'REFUSED fightmemory'))[12:]),
            'crime': {k: crime_of(c.bot, npc).get(k) for k in ('reason', 'last_player_ar', 'target', 'task')}} for c in (host, client)}

    def away(c, dx):
        x, y, z = hero_pos(c.bot)
        move_hero(c.bot, (x + dx, y + 30, z))

    away(other, 4000)
    report_line(actor.bot, 'sethp 1000', 'SETHP ')
    mem('start')
    if actor is host:
        report['attack'] = report_line(host.bot, f'npcfight {npc}_hero', ('NPCFIGHT ', 'REFUSED npcfight'))
    else:
        report['attack'] = report_line(host.bot, f'aggro {npc}_12', ('AGGRO ', 'REFUSED aggro'))
    time.sleep(float(os.environ.get('G3_UPSET_FIGHT', '0.3')))  # soon: in Reddock the town joined in and knocked the hero out
    away(actor, 6000)
    report['fighting'] = report_line(host.bot, f'fightmemory {npc}', ('FIGHTMEMORY ', 'REFUSED fightmemory'))[12:]
    if os.environ.get('G3_UPSET_CHASE') != '1':  # the chase did not end for the puppet (26.10.): the game's give-up script
        report['cancel'] = report_line(host.bot, f'cancelattack {npc}', ('CANCELATTACK ', 'REFUSED cancelattack'))
    timeline = []
    for _ in range(40):
        time.sleep(1)
        task = crime_of(host.bot, npc).get('task', '-')
        timeline.append(task)
        if len(timeline) > 3 and not task.startswith('ZS_Attack') and not timeline[-2].startswith('ZS_Attack'):
            break
    report['timeline'] = timeline[::3]
    time.sleep(3)
    mem('given_up')
    run_up(other, host, npc)
    time.sleep(10)
    mem('other_near')
    away(other, 4000)
    time.sleep(3)
    report['run_up'] = run_up(actor, host, npc)
    time.sleep(10)
    mem('actor_back')
    report['actor_hero'] = report_line(actor.bot, 'hp', 'HP ')
    report['log'] = {c.tag: [l[13:] for l in c.mod_log().splitlines() if 'fightmem:' in l][-12:] for c in (host, client)}


def stageUpset(copies, report, negative=False):
    """P7, the grudge on sight for the other player (IsUpset 0x115d0, asked by AssessActivePerception for the game's own
    player only: a fight the NPC gave up, CancelAttack, within a while by the reason it fought for, then AssessTarget again).
    The host keeps the client's fights apart (fightmem.cpp; before, CancelAttack with the puppet as the player left the
    cancelled fight in the host's NPC, held against the host's hero) and asks IsUpset for the puppet (guards.cpp). An NPC
    (G3_UPSET_NPC, Javier in Reddock) attacks the client's puppet (QA aggro, reason Enemy), the client jumps 60 m off and
    the NPC gives up (QA cancelattack, the game's script): the host keeps Cancel for player 2 and its NPC the host hero's
    memory; the host's hero walks up: the NPC leaves it alone; the client comes back: within 15 s the NPC attacks the
    puppet again. Negative control: grudgeoff on the host."""
    npc = os.environ.get('G3_UPSET_NPC', 'Javier')
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'grudgeoff', 'GRUDGEOFF ')
    for c in (host, client):  # the host first: it simulates the town
        teleport_to(c.bot, npc)
        time.sleep(2)

    def memory():
        return report_line(host.bot, f'fightmemory {npc}', ('FIGHTMEMORY ', 'REFUSED fightmemory'))[12:]

    def away(c, dx):
        x, y, z = hero_pos(c.bot)
        move_hero(c.bot, (x + dx, y + 30, z))

    away(host, 4000)
    before = re.search(r' last=(\d+)', memory())[1]
    report_line(client.bot, 'sethp 1000', 'SETHP ')
    report['attack'] = report_line(host.bot, f'aggro {npc}_12', ('AGGRO ', 'REFUSED aggro'))
    time.sleep(1)
    report['fighting'] = memory()
    away(client, 6000)
    report['cancel'] = report_line(host.bot, f'cancelattack {npc}', ('CANCELATTACK ', 'REFUSED cancelattack'))
    time.sleep(3)
    report['given_up'] = memory()
    run_up(host, host, npc)
    time.sleep(8)
    report['host_near'] = memory()
    away(host, 4000)
    time.sleep(2)
    report['run_up'] = run_up(client, host, npc)
    timeline = []
    for _ in range(15):
        time.sleep(1)
        line = memory()
        timeline.append(re.sub(r'^name=\S+ ', '', line)[:120])
        if 'task=ZS_Attack' in line and 'target=G3T_Player2' in line:
            break
    report['client_back'] = timeline[-4:]
    report['fightmem'] = report_line(host.bot, 'fightmem', 'FIGHTMEM ')
    report['log'] = [l[13:] for l in host.mod_log().splitlines() if 'still holds' in l or 'called off' in l][-6:]
    assert 'target=G3T_Player2' in report['fighting'], f'{npc} did not attack the puppet: {report["fighting"]}'
    if not negative:
        assert 'kept=2:3:' in report['given_up'], f'the host does not keep the fight {npc} gave up against player 2: {report["given_up"]}'
        assert f' last={before} ' in report['given_up'], f"the client's fight went into {npc}'s memory of the host's hero: {before} -> {report['given_up']}"
    assert 'target=PC_Hero' not in report['host_near'], f"{npc} took the client's fight out on the host's hero: {report['host_near']}"
    assert 'target=G3T_Player2' in timeline[-1] and 'task=ZS_Attack' in timeline[-1], f'{npc} did not take up the fight against the returning client: {timeline[-3:]}'


def stageGrudgeLoad(copies, report, negative=False):
    """P7, a grudge against the other player does not outlive a load (review 26.10.): the host kept the fights its NPCs
    gave up against the client (fightmem.cpp) through every load, so after loading a save from before such a fight the
    NPC still held it against the client on sight; in single-player only what the save holds counts. The host saves near
    an NPC (G3_UPSET_NPC, Javier in Reddock); the NPC attacks the client's puppet (QA aggro), the client jumps 60 m off and
    the NPC gives up (QA cancelattack): the host keeps Cancel for player 2. The host loads the save, the client is loaded
    into it: the host keeps no grudge, and the client walks up to the NPC and is left alone for 15 s. Negative control:
    grudgeloadoff on the host."""
    npc = os.environ.get('G3_UPSET_NPC', 'Javier')
    host, client = join_both(copies)
    clear_raid(host, report)
    slot = 'G3T_GRUDGE'
    clear_slot(copies, slot)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'grudgeloadoff', 'GRUDGELOADOFF ')
    for c in (host, client):  # the host first: it simulates the town
        teleport_to(c.bot, npc)
        time.sleep(2)

    def memory():
        return report_line(host.bot, f'fightmemory {npc}', ('FIGHTMEMORY ', 'REFUSED fightmemory'))[12:]

    def away(c, dx):
        x, y, z = hero_pos(c.bot)
        move_hero(c.bot, (x + dx, y + 30, z))

    away(host, 4000)
    time.sleep(2)
    start = host.bot.send(f'nativesave {slot}', 60)
    report['save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
    assert 'ok=1' in report['save'], report['save']
    time.sleep(3)
    report_line(client.bot, 'sethp 1000', 'SETHP ')
    report['attack'] = report_line(host.bot, f'aggro {npc}_12', ('AGGRO ', 'REFUSED aggro'))
    time.sleep(1)
    report['fighting'] = memory()
    away(client, 6000)
    report['cancel'] = report_line(host.bot, f'cancelattack {npc}', ('CANCELATTACK ', 'REFUSED cancelattack'))
    time.sleep(3)
    report['given_up'] = memory()
    assert 'target=G3T_Player2' in report['fighting'], f'{npc} did not attack the puppet: {report["fighting"]}'
    assert 'kept=2:3:' in report['given_up'], f'the host does not keep the fight {npc} gave up against player 2: {report["given_up"]}'
    since = len(client.mod_log())
    start = host.bot.send(f'nativeload {slot}', 60)
    report['load'] = host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''), 120,
                                   'nativeload', start)
    assert 'ok=1' in report['load'], report['load']
    wait_running(host, 180, 'host world running after the load')
    wait_mod_log(client, r'persist: loading G3T_host_'+slot+r'\b', 120, "the client is loaded into the host's save", since)
    wait_running(client, 180, 'client world running after the load')
    report['in_world'] = wait_mod_log(client, r"persist: in the host's world", 60, "the client is in the host's world", since)
    wait_for(lambda: remotes(host.bot).get(2, {}).get('puppet'), 30, "the client's puppet on the host")
    time.sleep(3)
    report['fightmem'] = report_line(host.bot, 'fightmem', 'FIGHTMEM ')
    report['after_load'] = memory()
    report['run_up'] = run_up(client, host, npc)
    timeline = []
    for _ in range(15):
        time.sleep(1)
        line = memory()
        timeline.append(re.sub(r'^name=\S+ ', '', line)[:120])
        if 'task=ZS_Attack' in line and 'target=G3T_Player2' in line:
            break
    report['client_back'] = timeline[-4:]
    report['log'] = [l[13:] for l in host.mod_log().splitlines() if 'still holds' in l or 'called off' in l or 'forgotten' in l][-6:]
    assert ' kept=0 ' in report['fightmem'], f"the host kept a grudge against the client through the load: {report['fightmem']}"
    assert 'kept=-' in report['after_load'] or 'kept=' not in report['after_load'], f'{npc} still has a grudge after the load: {report["after_load"]}'
    assert not any('task=ZS_Attack' in t and 'target=G3T_Player2' in t for t in timeline), \
        f'{npc} took up a fight against the client that the loaded save never had: {timeline[-3:]}'


def aggressordiag(copies, report, negative=False):
    """P7 measurement, one blow on a friendly NPC (AssessAgressor 0x24490, re/out/aggressor-b.c: in its routine an NPC
    friendly to the attacker takes it up with ZS_ReactToDamage, any other attacks, AssessTarget reason 9; the client's
    blows on the host make the NPC judge the puppet with AssessTarget reason 9 directly, combat.cpp). In Reddock the host's
    hero strikes one rebel once (single-player reference), then the client strikes another once (the host 7 m off).
    Records p7probe lines (AssessAgressor), the struck NPC's task and target for 8 s, and its attitude; asserts nothing."""
    names = os.environ.get('G3_AGG_NPCS', 'Norris,Copper').split(',')
    host, client = join_both(copies)
    clear_raid(host, report)
    for c in (host, client):
        report_line(c.bot, 'p7probeon', 'P7PROBEON ')
    for c in (host, client):  # the host first: it simulates the town
        teleport_to(c.bot, names[0])
        time.sleep(2)

    def strike(striker, name, tag):
        teleport_to(striker.bot, name)
        time.sleep(2)
        marks = {c.tag: len(c.mod_log()) for c in (host, client)}
        guid = next((g for g, v in npcs(host.bot).items() if v['name'] == name), None)
        assert guid, f'{name} is not among the host NPCs'
        attitude = report_line(host.bot, 'attitude ' + name, ('ATTITUDE ', 'REFUSED attitude'))
        report_line(striker.bot, 'arm ' + WEAPON, 'ARM ')
        time.sleep(1)
        hit = hit_npc(striker.bot, guid)['line'][-100:]
        samples = []
        for _ in range(8):
            time.sleep(1)
            cr = crime_of(host.bot, name)
            samples.append(f"{cr.get('task')}:{cr.get('target')}")
        report[tag] = {'npc': name, 'attitude': attitude, 'hit': hit, 'samples': samples,
                       'probe': {c.tag: [l[13:] for l in c.mod_log()[marks[c.tag]:].splitlines() if 'AssessAgressor' in l][-8:]
                                 for c in (host, client)}}

    strike(host, names[0], 'host_blow')
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 3000, y + 30, z))
    time.sleep(3)
    teleport_to(host.bot, names[1])
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 700, y + 30, z))
    strike(client, names[1], 'client_blow')


def stageMurderWitness(copies, report, negative=False):
    """P7, witnesses of the other player's fight and murder react as in single-player (owner 25.10.: only the murderer is
    pursued; onlookers and guards come in already during the beating). The host applies the client's hits on its NPCs with
    AddHitPoints and OnCheatKill, which tell nobody around (murderwitnessdiag 25.10.: the host's own beating in Ishtar drew
    onlookers and guards, the client's nobody). Now the host sends the game's perceptions with the client's puppet as the
    attacker. In a town with routine status the client beats a member down and finishes it, the host's hero 6 m away:
    (1) during the beating NPCs around may turn to the fight (recorded only: Reddock's rebels step in on their own, through
    their AI's look at fights, also with the negative control; 1 of 4 runs none did); (2) at the murder witnesses react
    (AssessMurder), and those that judged the puppet a murderer judge the client's hero on the client too, so its game
    remembers the murder there (a town member's crime goes to its town, KnownPlayerCrime 4, else to the NPC's
    LastPlayerCrime; MemorizePlayerCrime). Negative control: murderseenoff on the host."""
    host, client = join_both(copies)
    clear_raid(host, report)
    # Not Ishtar (the first town): the host's hero 6 m away stands in a guarded zone there, and its guards go after it.
    town, good = murder_town(host, report, 'Reddock')
    victim = good[1]
    if negative:
        assert 'ok=1' in report_line(host.bot, 'murderseenoff', 'MURDERSEENOFF ')
    teleport_to(host.bot, victim)
    teleport_to(client.bot, victim)
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 600, y, z))
    report_line(client.bot, 'arm ' + WEAPON, 'ARM ')
    time.sleep(4)
    guid = next((g for g, v in npcs(host.bot).items() if v['name'] == victim), None)
    assert guid, f'{victim} is not among the host NPCs'
    pos = npcs(host.bot)[guid]['pos']
    near = sorted({v['name'] for g, v in npcs(host.bot).items() if g != guid and not v['dead'] and not v['name'].startswith('G3T_')
                   and sum((a - b) ** 2 for a, b in zip(v['pos'], pos)) < 1500 ** 2})
    report['victim'], report['near'] = victim, {n: crime_of(host.bot, n).get('task') for n in near}
    report['known_before'] = {c.tag: crime_of(c.bot, victim).get('known') for c in (host, client)}  # the town's known crime
    report_line(host.bot, f'npchp {victim}_120', 'NPCHP ')  # a few hits before it falls: the fight is heard
    report_line(client.bot, 'sethp 1000', 'SETHP ')

    def strike(until):
        hits = []
        for _ in range(40):
            report_line(client.bot, 'arm ' + WEAPON, 'ARM ')
            hits.append(hit_npc(client.bot, guid)['line'][-90:])
            time.sleep(0.6)
            state = npcs(host.bot).get(guid, {})
            if until(state):
                break
            time.sleep(0.6)
        return hits[-3:]

    report['ko_hits'] = strike(lambda s: s.get('dead') or s.get('task') in KO_TASKS)
    time.sleep(3)
    fight = {n: crime_of(host.bot, n) for n in near}
    report['fight'] = {n: f"{c.get('task')}>{c.get('target')}:{c.get('reason')}" for n, c in fight.items()}
    report['fight_turned'] = [n for n, c in fight.items() if c.get('target') == 'puppet2' or c.get('task') in ('ZS_WatchFight', 'ZS_Flee')]
    report['fight_witness'] = witnesses_of(host.bot)[0]
    time.sleep(4)
    report['finish_hits'] = strike(lambda s: s.get('dead'))
    time.sleep(6)
    assert npcs(host.bot).get(guid, {}).get('dead'), f'the client could not finish {victim}'
    report['murder'] = {n: f"{c.get('task')}>{c.get('target')}:{c.get('reason')}" for n, c in ((n, crime_of(host.bot, n)) for n in near)}
    line, host_w = witnesses_of(host.bot)
    report['host_witness'] = line
    report['client_witness'], client_w = witnesses_of(client.bot)
    report['host_log'] = [l for l in host.mod_log().splitlines() if 'witness:' in l][-12:]
    report['client_log'] = [l for l in client.mod_log().splitlines() if 'witness:' in l][-8:]
    judged = sorted({m[1] for l in report['host_log'] for m in [re.search(r'witness: (\S+) judged player 2', l)] if m})
    report['client_crimes'] = {n: (lambda c: f"{c.get('last_crime')}/{c.get('enclave')}:{c.get('known')}")(crime_of(client.bot, n)) for n in judged}
    report['host_crimes'] = {n: (lambda c: f"{c.get('last_crime')}/{c.get('enclave')}:{c.get('known')}")(crime_of(host.bot, n)) for n in judged}
    assert host_w.get('murders_seen', 0) >= 1 and host_w.get('murder_reacted', 0) >= 1, f'no witness of the murder reacted: {line}'
    assert judged and client_w.get('judges_applied', 0) >= 1, f"no witness judged the client's puppet a murderer: {report['murder']}"
    remembered = [n for n, c in report['client_crimes'].items() if c.startswith('4/') or c.endswith(':4')]
    assert remembered, f"the client's game does not remember the murder: {report['client_crimes']}"


def trespassdiag(copies, report, negative=False):
    """P7 measurement, guards and the other player's drawn weapon and trespassing: the game checks both only against its
    local player (AssessPlayerWeapon, AssessPlayerZone ask GetPlayer/IsPlayer). (1) Reddock: the client draws its
    weapon next to Brenton, the host's hero 15 m off; then the host does the same as single-player reference. (2) Ishtar:
    the client walks into the assassins' guarded place at Muntasir, the host's hero 30 m off; then the host. Records the
    tasks and targets of the NPCs around on the host after 10 s; asserts nothing."""
    host, client = join_both(copies)
    clear_raid(host, report)

    def around(name, radius=2000):
        rows = npcs(host.bot)
        pos = next(v['pos'] for v in rows.values() if v['name'] == name)
        names = sorted({v['name'] for v in rows.values() if not v['dead'] and not v['name'].startswith('G3T_')
                        and distance(v['pos'], pos) < radius})
        return {n: (lambda c: f"{c.get('task')}>{c.get('target')}:{c.get('reason')}")(crime_of(host.bot, n)) for n in names}

    def aside(bot, name, offset):
        teleport_to(bot, name)
        x, y, z = hero_pos(bot)
        move_hero(bot, (x + offset, y, z))

    def melee(bot, on):
        if on:
            report_line(bot, 'arm ' + WEAPON, 'ARM ')
        return report_line(bot, 'act ' + ('PS_Melee-PS_Melee_Loop' if on else 'PS_Normal-PS_Normal_Loop'), ('ACT ', 'REFUSED act'))

    for place, offset, armed in (('Brenton', 1500, True), ('Muntasir', 3000, False)):
        aside(host.bot, place, offset)
        teleport_to(client.bot, place)
        time.sleep(3)
        report[f'{place}_before'] = around(place)
        if armed:
            report[f'{place}_client_act'] = melee(client.bot, True)
        time.sleep(10 if armed else 20)  # an intruder is warned before the guard attacks
        report[f'{place}_client'] = around(place)
        report[f'{place}_guards'] = report_line(host.bot, 'guards', 'GUARDS ')
        report[f'{place}_log'] = [l for l in host.mod_log().splitlines() if 'guards:' in l][-6:]
        if armed:
            melee(client.bot, False)
        aside(client.bot, place, -offset)
        time.sleep(15)
        teleport_to(host.bot, place)
        if armed:
            report[f'{place}_host_act'] = melee(host.bot, True)
        time.sleep(10)
        report[f'{place}_host'] = around(place)
        if armed:
            melee(host.bot, False)


def stageTrespass(copies, report, negative=False):
    """P7, guards react to the other player's drawn weapon and trespassing as in single-player (owner 25.10.). The game
    checks both only against its own player (trespassdiag 25.10.: the host's hero was attacked for each, the client's
    never). (1) Reddock: the client draws its weapon next to Brenton, the host's hero 15 m off: within 20 s an NPC there
    attacks puppet2 for the weapon (attack reason 6, ReactToWeapon). (2) Ishtar: the client walks into Muntasir's guarded
    place, the host's hero 30 m off: within 30 s it is warned and attacked as an intruder (reason 11). Nobody turns on the
    host's hero meanwhile. Negative control: trespassoff on the host."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'trespassoff', 'TRESPASSOFF ')

    def around(name, radius=2000):
        rows = npcs(host.bot)
        pos = next(v['pos'] for v in rows.values() if v['name'] == name)
        names = sorted({v['name'] for v in rows.values() if not v['dead'] and not v['name'].startswith('G3T_')
                        and distance(v['pos'], pos) < radius})
        return {n: crime_of(host.bot, n) for n in names}

    def attacked(name, reason):
        state = around(name)
        report['last'] = {n: f"{c.get('task')}>{c.get('target')}:{c.get('reason')}" for n, c in state.items() if c}
        on_host = [n for n, c in state.items() if c.get('target') == 'hero' and c.get('task') == 'ZS_Attack']
        assert not on_host, f'{on_host} attack the host for what the client did: {report["last"]}'
        return [n for n, c in state.items() if c.get('task') == 'ZS_Attack' and c.get('target') == 'puppet2' and c.get('reason') == reason]

    for place, offset, reason, seconds in (('Brenton', 1500, '6', 20), ('Muntasir', 3000, '11', 30)):
        teleport_to(host.bot, place)
        x, y, z = hero_pos(host.bot)
        move_hero(host.bot, (x + offset, y, z))
        teleport_to(client.bot, place)
        if reason == '6':
            report_line(client.bot, 'arm ' + WEAPON, 'ARM ')
            report['act'] = report_line(client.bot, 'act PS_Melee-PS_Melee_Loop', ('ACT ', 'REFUSED act'))
        try:
            report[place] = wait_for(lambda: attacked(place, reason), seconds, f'a guard at {place} attacks the client (reason {reason})')
        finally:
            report[f'{place}_state'] = report.pop('last', None)
            report[f'{place}_guards'] = report_line(host.bot, 'guards', 'GUARDS ')
        if reason == '6':
            report_line(client.bot, 'act PS_Normal-PS_Normal_Loop', ('ACT ', 'REFUSED act'))
        x, y, z = hero_pos(client.bot)
        move_hero(client.bot, (x - 4000, y, z))
        report_line(client.bot, 'sethp 1000', 'SETHP ')


def stageCompanionMurder(copies, report, negative=False):
    """P7, a murder by a player's companion is perceived as in single-player. G3_WHO=client (default): the client recruits
    Lester (as stageP3a), Lester kills the host NPC nearest to him; the host applies the kill (OnNpcHit with OnCheatKill),
    and sends the game's murder perception with Lester as the killer, as the game does where the kill is decided
    (G3_WHO=host, single-player reference 25.10.: the native kill sends it once; asserts nothing then). Records the tasks
    and targets of the NPCs around the victim. G3_TOWN: in that town with witnesses instead (Lester moved next to a member
    whose murder counts there). Negative control: murderseenoff on the host (no perception)."""
    who = os.environ.get('G3_WHO', 'client')
    host, client = join_both(copies)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'murderseenoff', 'MURDERSEENOFF ')
    leader = host if who == 'host' else client
    clear_raid(host, report)
    teleport_to(host.bot, 'Lester')
    time.sleep(6)
    teleport_to(client.bot, 'Lester')
    time.sleep(8)
    report_line(leader.bot, 'setevent GetLester', 'EVENT ')
    talk_to(leader, 'Lester', report, 'partners')
    report_line(leader.bot, 'talkinfo NEWM1055', ('TALKINFO ', 'REFUSED talkinfo'))
    wait_for(lambda: talkstate(leader.bot).get('running') == '0', 90, 'the recruiting dialog ends')
    if who == 'client':
        wait_for(lambda: 'owner=2' in find_npc(host.bot, 'Lester').split() and ['yes'], 15, 'the client has Lester')
    if os.environ.get('G3_TOWN'):
        # With witnesses (P7 rest 25.10.): the leader takes Lester into a town (G3_TOWN, e.g. Reddock) next to a member whose
        # murder the game counts there, the other hero 15 m off.
        town, good = murder_town(host, report)
        name = good[1]
        other = client if leader is host else host
        teleport_to(leader.bot, name)
        teleport_to(other.bot, name)
        x, y, z = hero_pos(other.bot)
        move_hero(other.bot, (x + 1500, y, z))
        x, y, z = hero_pos(leader.bot)
        report['lester_moved'] = report_line(leader.bot, f'farmove {x + 150:.0f}_{y + 50:.0f}_{z:.0f}_Lester', ('FARMOVE ', 'REFUSED farmove'))
        time.sleep(4)
        victim = next(g for g, v in npcs(host.bot).items() if v['name'] == name)
        report_line(host.bot, f'npchp {name}_60', 'NPCHP ')
        report['known_before'] = {c.tag: crime_of(c.bot, name).get('known') for c in (host, client)}
    else:
        rows = npcs(leader.bot)
        lester = next(v['pos'] for v in rows.values() if v['name'] == 'Lester')
        names = [v['name'] for v in rows.values()]
        candidates = sorted((distance(v['pos'], lester), g, v['name']) for g, v in rows.items()
                            if (v['mirrored'] or who == 'host') and not v['dead'] and v['name'] != 'Lester' and names.count(v['name']) == 1
                            and not v['name'].startswith('G3T_'))
        assert candidates, 'no host NPC with a unique name near Lester'
        _, victim, name = candidates[0]
    pos = npcs(host.bot)[victim]['pos']
    near = sorted({v['name'] for g, v in npcs(host.bot).items() if g != victim and not v['dead'] and not v['name'].startswith('G3T_')
                   and distance(v['pos'], pos) < 1500})
    report['victim'], report['near'] = name, near
    marks = len(host.mod_log())
    report['fight'] = report_line(leader.bot, f'npcfight Lester_{victim}', ('NPCFIGHT ', 'REFUSED npcfight'))
    wait_for(lambda: npcs(host.bot).get(victim, {}).get('dead') and ['dead'], 120, 'Lester kills the victim')
    time.sleep(6)
    report['after'] = {n: (lambda c: f"{c.get('task')}>{c.get('target')}:{c.get('reason')}")(crime_of(host.bot, n)) for n in near}
    report['witness'] = {c.tag: witnesses_of(c.bot)[0] for c in (host, client)}
    report['host_log'] = [l for l in host.mod_log()[marks:].splitlines() if 'witness' in l or 'killed' in l][-20:]
    if os.environ.get('G3_TOWN'):
        time.sleep(6)
        report['later'] = {n: (lambda c: f"{c.get('task')}>{c.get('target')}:{c.get('reason')}")(crime_of(host.bot, n)) for n in near}
        report['known_after'] = {c.tag: crime_of(c.bot, name).get('known') for c in (host, client)}
        report['client_log'] = [l for l in client.mod_log().splitlines() if 'witness' in l][-10:]
        report['lester'] = crime_of(host.bot, 'Lester')
    if who == 'client':
        seen = witnesses_of(host.bot)[1]
        assert seen.get('murders_seen', 0) >= 1 and seen.get('murder_sent', 0) >= 1, f"no murder perception for Lester's kill: {report['witness']['host']}"


def sneakhomediag(copies, report, negative=False):
    """P7 measurement, sneaking and walking into someone's home (Reddock): (1) the client sneaks next to Brenton for 12 s,
    the host's hero 15 m off; then the host does the same as single-player reference. (2) Homes (QA homes): the client
    walks onto the bed of an NPC away from home; then the host onto another one's. Records the tasks and targets of the
    NPCs around on the host and the GUARDS line; asserts nothing."""
    host, client = join_both(copies)
    clear_raid(host, report)

    def around(pos, radius=2500):
        rows = npcs(host.bot)
        names = sorted({v['name'] for v in rows.values() if not v['dead'] and not v['name'].startswith('G3T_') and distance(v['pos'], pos) < radius})
        return {n: (lambda c: f"{c.get('task')}>{c.get('target')}:{c.get('reason')}")(crime_of(host.bot, n)) for n in names}

    teleport_to(host.bot, 'Brenton')
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 1500, y, z))
    teleport_to(client.bot, 'Brenton')
    spot = hero_pos(client.bot)
    report['sneak_client_on'] = report_line(client.bot, 'sneak 1', ('SNEAK ', 'REFUSED sneak'))
    time.sleep(12)
    report['sneak_client'] = around(spot)
    report['sneak_client_guards'] = report_line(host.bot, 'guards', 'GUARDS ')
    report_line(client.bot, 'sneak 0', ('SNEAK ', 'REFUSED sneak'))
    move_hero(client.bot, (spot[0] - 4000, spot[1], spot[2]))
    time.sleep(15)
    move_hero(host.bot, spot)
    report['sneak_host_on'] = report_line(host.bot, 'sneak 1', ('SNEAK ', 'REFUSED sneak'))
    time.sleep(12)
    report['sneak_host'] = around(spot)
    report_line(host.bot, 'sneak 0', ('SNEAK ', 'REFUSED sneak'))

    start = host.bot.send('homes 6000', 20)
    host.bot.line('HOMES end', 15, 'homes', start)
    homes = [dict(re.findall(r'(\w+)=(\S+)', l)) for l in host.bot.log()[start:].splitlines() if l.startswith('HOME npc=')]
    report['homes'] = homes[:12]
    homes = [h for h in homes if h.get('mirrored') == '0']
    assert len(homes) >= 2, f'fewer than two NPCs away from home near Brenton: {report["homes"]}'
    for who, home in (('client', homes[0]), ('host', homes[1])):
        bot = client.bot if who == 'client' else host.bot
        other = host.bot if who == 'client' else client.bot
        x, y, z = hero_pos(other)
        move_hero(other, (x + 3000, y, z))
        bed = tuple(map(float, home['bed'].split(',')))
        move_hero(bot, (bed[0], bed[1] + 50, bed[2]))
        time.sleep(15)
        report[f'home_{who}'] = {'owner': home['npc'], 'zone': home['home'], 'owner_now': crime_of(host.bot, home['npc']).get('task'),
                                 'target': crime_of(host.bot, home['npc']).get('target'), 'around': around(bed, 1500)}
        report[f'home_{who}_guards'] = report_line(host.bot, 'guards', 'GUARDS ')
    report['log'] = [l for l in host.mod_log().splitlines() if 'guards:' in l][-15:]


def talk_through(copy, npc, seconds=40, choices=20):
    """Wait for `npc` to start a dialog with this player's hero (it walks up with an important info), then pick the first
    option until the dialog ends. Returns what was picked."""
    picked = []
    try:
        wait_for(lambda: (lambda t: t.get('running') == '1' and t.get('partner') == npc and ['yes'])(talkstate(copy.bot)), seconds,
                 f'{npc} talks to {copy.tag}')
    except AssertionError:
        return ['no dialog']
    for _ in range(choices):
        time.sleep(2)
        state = talkstate(copy.bot)
        if state.get('running') != '1':
            break
        if state.get('waiting') == '1' and int(state.get('available', '0')) > 0:
            last = int(state['available']) - 1  # the last option is mostly the end of the talk
            picked.append(report_line(copy.bot, f'talkchoose {last}', ('TALKCHOOSE ', 'REFUSED talkchoose'))[:60])
    if talkstate(copy.bot).get('running') == '1':
        report_line(copy.bot, 'endtalk', ('TALKSTATE ', 'REFUSED endtalk'))
        picked.append('endtalk')
    return picked


def p7opendiag(copies, report, negative=False):
    """P7 measurement (owner 25.10. evening: the open P7 points, measure first): with QA p7probeon on both machines, the
    actor (the client; G3_WHO=host: the host, the single-player reference) in Reddock (A) draws its weapon next to Brenton,
    (B) beats a town member down while others watch, (C) uses objects there; then (D) walks into Muntasir's guarded place
    in Ishtar with 60 hit points and stays. Records per scene and machine how often OnWatchFight, SendPerceptionWarn,
    AssessWarn, HasAttackReasonToKill, SendPerceptionUseInteractObject and AssessUseInteractObject ran (with a puppet,
    positive), the onlookers' fight memory (LastFightAgainstPlayer, 5 = Friendly) and what the NPCs around do; asserts
    nothing."""
    who = os.environ.get('G3_WHO', 'client')
    host, client = join_both(copies)
    clear_raid(host, report)
    actor, other = (host, client) if who == 'host' else (client, host)
    for c in (host, client):
        report[f'{c.tag}_probe_on'] = report_line(c.bot, 'p7probeon', 'P7PROBEON ')

    def probes():
        out = {}
        for c in (host, client):
            start = c.bot.send('p7probe', 20)
            c.bot.line('P7PROBE end', 15, 'p7probe', start)
            out[c.tag] = {m[1]: (int(m[2]), int(m[3]), int(m[4])) for m in re.finditer(r'P7PROBE name=(\S+) calls=(\d+) puppet=(\d+) positive=(\d+)', c.bot.log()[start:])}
        return out

    def delta(before, after):
        return {tag: {n: tuple(a - b for a, b in zip(after[tag][n], before[tag].get(n, (0, 0, 0)))) for n in after[tag]
                      if after[tag][n] != before[tag].get(n, (0, 0, 0))} for tag in after}

    def around(pos, radius=1500):
        rows = npcs(host.bot)
        names = sorted({v['name'] for v in rows.values() if not v['dead'] and not v['name'].startswith('G3T_') and distance(v['pos'], pos) < radius})
        return {n: (lambda c: f"{c.get('task')}>{c.get('target')}:{c.get('reason')}")(crime_of(host.bot, n)) for n in names}

    def logs(mark):
        return {c.tag: [l[13:] for l in c.mod_log()[mark[c.tag]:].splitlines() if 'p7probe:' in l][-14:] for c in (host, client)}

    def arrive(anchor, aside):
        # The host first, so that it simulates the town (a client arriving alone takes the region, stage E).
        for c in (host, client):
            teleport_to(c.bot, anchor)
            if c is other:
                x, y, z = hero_pos(c.bot)
                move_hero(c.bot, (x + aside, y + 30, z))
            time.sleep(2)

    # (A) weapon next to Brenton
    town, good = murder_town(host, report, 'Reddock')
    arrive('Brenton', 1500)
    spot = hero_pos(actor.bot)
    time.sleep(3)
    before, mark = probes(), {c.tag: len(c.mod_log()) for c in (host, client)}
    report_line(actor.bot, 'arm ' + WEAPON, 'ARM ')
    report['A_act'] = report_line(actor.bot, 'act PS_Melee-PS_Melee_Loop', ('ACT ', 'REFUSED act'))
    time.sleep(12)
    report['A_around'] = around(spot)
    report['A_probe'] = delta(before, probes())
    report['A_log'] = logs(mark)
    report_line(actor.bot, 'act PS_Normal-PS_Normal_Loop', ('ACT ', 'REFUSED act'))
    move_hero(actor.bot, (spot[0] - 4000, spot[1] + 30, spot[2]))
    time.sleep(15)

    # (B) a fight others watch: the actor beats a town member down
    victim = good[0]
    arrive(victim, 1500)
    guid = next(g for g, v in npcs(host.bot).items() if v['name'] == victim)
    pos = npcs(host.bot)[guid]['pos']
    onlookers = sorted({v['name'] for g, v in npcs(host.bot).items() if g != guid and not v['dead'] and not v['name'].startswith('G3T_')
                        and distance(v['pos'], pos) < 1500})[:8]
    report['B_victim'], report['B_onlookers'] = victim, onlookers
    report['B_memory_before'] = {n: report_line(host.bot, f'fightmemory {n}', ('FIGHTMEMORY ', 'REFUSED fightmemory'))[12:] for n in onlookers}
    report_line(host.bot, f'npchp {victim}_120', 'NPCHP ')
    report_line(actor.bot, 'sethp 1000', 'SETHP ')
    before, mark = probes(), {c.tag: len(c.mod_log()) for c in (host, client)}
    for _ in range(40):
        report_line(actor.bot, 'arm ' + WEAPON, 'ARM ')
        hit_npc(actor.bot, guid)
        time.sleep(1.2)
        state = npcs(host.bot).get(guid, {})
        if state.get('dead') or state.get('task') in KO_TASKS:
            break
    time.sleep(10)
    report['B_victim_state'] = npcs(host.bot).get(guid, {}).get('task')
    report['B_memory_after'] = {n: report_line(host.bot, f'fightmemory {n}', ('FIGHTMEMORY ', 'REFUSED fightmemory'))[12:] for n in onlookers}
    report['B_around'] = around(pos)
    report['B_probe'] = delta(before, probes())
    report['B_log'] = logs(mark)

    # (C) using objects in the town
    start = actor.bot.send('usables all2500', 20)
    actor.bot.line('USABLE end', 15, 'usables', start)
    objects = [dict(re.findall(r'(\w+)=(\S+)', l)) for l in actor.bot.log()[start:].splitlines() if l.startswith('USABLE name=')]
    report['C_objects'] = [f"{o.get('name')}:{o.get('usefunc')}:{o.get('usetype')}:{o.get('user')}" for o in objects[:12]]
    before, mark = probes(), {c.tag: len(c.mod_log()) for c in (host, client)}
    used = []
    for o in [o for o in objects if o.get('user') == '-'][:3]:
        used.append(report_line(actor.bot, f"use {o['name']}", ('USE ', 'REFUSED use'))[:120])
        time.sleep(6)
        report_line(actor.bot, 'unuse', ('UNUSE ', 'REFUSED unuse'))
        time.sleep(2)
    report['C_used'] = used
    report['C_around'] = around(hero_pos(actor.bot))
    report['C_probe'] = delta(before, probes())
    report['C_log'] = logs(mark)

    # (D) intruder at Muntasir with few hit points: knocked down or killed?
    arrive('Muntasir', 3000)
    report_line(actor.bot, 'sethp 60', 'SETHP ')
    before, mark = probes(), {c.tag: len(c.mod_log()) for c in (host, client)}
    states = []
    for _ in range(12):
        time.sleep(4)
        states.append(report_line(actor.bot, 'herostate', 'HERO ')[:90])
    report['D_hero'] = states
    report['D_around'] = around(npcs(host.bot)[next(g for g, v in npcs(host.bot).items() if v['name'] == 'Muntasir')]['pos'], 2500)
    report['D_probe'] = delta(before, probes())
    report['D_log'] = logs(mark)


def fightmemorydiag(copies, report, negative=False):
    """P7 measurement, the NPCs' memory of a fight with the player (LastFightAgainstPlayer: 1 Lost, 2 Won, 3 Cancel, 4 Running,
    5 Friendly; read by dialogs, IsUpset, IsTemporaryFriendlyToPlayer, GetAttitudeToPlayer). The striker (the client;
    G3_WHO=host: the host, the single-player reference) knocks Folleck out in a duel (duelhero, as Folleck_DefeatHim starts
    it), the other hero 7 m off; with QA p7probeon on both machines. Records Folleck's and the onlookers' memory on both
    machines before, during and after, and the probe counts (OnWatchFight); asserts nothing."""
    name = os.environ.get('G3_TARGET', 'Folleck')
    host, client = join_both(copies)
    clear_raid(host, report)
    striker = host if os.environ.get('G3_WHO') == 'host' else client
    other = client if striker is host else host
    for c in (host, client):
        report_line(c.bot, 'p7probeon', 'P7PROBEON ')
    for c in (host, client):  # the host first: it simulates them
        teleport_to(c.bot, name)
        time.sleep(2)
    x, y, z = hero_pos(other.bot)
    move_hero(other.bot, (x + 700, y + 30, z))
    time.sleep(5)
    guid = next((g for g, v in npcs(host.bot).items() if v['name'] == name and not v['dead']), None)
    assert guid, f'{name} is not among the host NPCs'
    pos = npcs(host.bot)[guid]['pos']
    near = sorted({v['name'] for g, v in npcs(host.bot).items() if g != guid and not v['dead'] and not v['name'].startswith('G3T_')
                   and distance(v['pos'], pos) < 1500})[:8]
    report['near'] = near

    def memory(tag):
        return {c.tag: {n: re.sub(r'^name=\S+ ', '', report_line(c.bot, f'fightmemory {n}', ('FIGHTMEMORY ', 'REFUSED fightmemory'))[12:])
                        for n in [name] + near} for c in (host, client)}
    report['memory_before'] = memory('before')
    report_line(striker.bot, 'arm ' + WEAPON, 'ARM ')
    report['duel'] = report_line(striker.bot, 'duelhero ' + name, 'DUELHERO ')
    time.sleep(3)
    report['memory_running'] = memory('running')
    report_line(host.bot, f'npchp {name}_30', 'NPCHP ')
    state = {}
    for _ in range(25):
        hit_npc(striker.bot, guid)
        time.sleep(0.8)
        state = npcs(host.bot).get(guid, {})
        if state.get('dead') or state.get('task') in KO_TASKS:
            break
        time.sleep(0.6)
    report['state'] = state
    time.sleep(8)
    report['memory_after'] = memory('after')
    report['around'] = {n: (lambda c: f"{c.get('task')}>{c.get('target')}:{c.get('reason')}")(crime_of(host.bot, n)) for n in near}
    out = {}
    for c in (host, client):
        start = c.bot.send('p7probe', 20)
        c.bot.line('P7PROBE end', 15, 'p7probe', start)
        out[c.tag] = re.findall(r'P7PROBE name=(\S+) calls=(\d+) puppet=(\d+) positive=(\d+)', c.bot.log()[start:])
    report['probe'] = out
    report['log'] = {c.tag: [l[13:] for l in c.mod_log().splitlines() if 'p7probe: OnWatchFight' in l][-8:] for c in (host, client)}


def useownediag(copies, report, negative=False):
    """P7 measurement, using other people's things (AssessUseInteractObject: an NPC that sees the player use an object in an
    indoor owner zone treats it like theft, attack reason 5). After the raid in Ardea the host lists the interact objects in
    owner zones within 50 m (QA ownedobj); the actor (the client; G3_WHO=host: the host, the single-player reference) uses up
    to three free ones in turn, the other hero 15 m off; with QA p7probeon on both machines. Records the probe counts, the
    log lines and who around targets the actor (task, reason); asserts nothing."""
    who = os.environ.get('G3_WHO', 'client')
    host, client = join_both(copies)
    clear_raid(host, report)
    actor, other = (host, client) if who == 'host' else (client, host)
    expected = 'hero' if who == 'host' else 'puppet2'
    for c in (host, client):
        report_line(c.bot, 'p7probeon', 'P7PROBEON ')
    start = host.bot.send('ownedobj 5000', 20)
    host.bot.line('OWNEDOBJ end', 15, 'ownedobj', start)
    objects = [dict(re.findall(r'(\w+)=(\S+)', l)) for l in host.bot.log()[start:].splitlines() if l.startswith('OWNEDOBJ guid=')]
    report['objects'] = [f"{o['name']}:{o['usetype']}:{o['usefunc']}:{o['user']}:{o['zone']}:{o['zone_owner']}:{o['security']}:{o['distance_cm']}" for o in objects][:16]
    free = [o for o in objects if o.get('user') == '-'][:3]
    assert free, 'no free object in an owner zone near the start'
    for k, o in enumerate(free):
        at = tuple(float(v) for v in o['pos'].split('_'))
        move_hero(host.bot, (at[0] + (0 if who == 'host' else 1500), at[1] + 60, at[2]))
        time.sleep(2)
        move_hero(client.bot, (at[0] + (1500 if who == 'host' else 0), at[1] + 60, at[2]))
        time.sleep(3)
        marks = {c.tag: len(c.mod_log()) for c in (host, client)}
        report[f'use{k}'] = report_line(actor.bot, f"use {o['name']}", ('USE ', 'REFUSED use'))[:140]
        time.sleep(8)
        rows = npcs(host.bot)
        near = sorted({v['name'] for v in rows.values() if not v['dead'] and not v['name'].startswith('G3T_') and distance(v['pos'], at) < 1500})
        report[f'use{k}_around'] = {n: (lambda c: f"{c.get('task')}>{c.get('target')}:{c.get('reason')}")(crime_of(host.bot, n)) for n in near}
        report[f'use{k}_on_actor'] = [n for n, t in report[f'use{k}_around'].items() if f'>{expected}:' in t]
        report[f'use{k}_log'] = {c.tag: [l[13:] for l in c.mod_log()[marks[c.tag]:].splitlines() if 'p7probe:' in l][-10:] for c in (host, client)}
        report_line(actor.bot, 'unuse', ('UNUSE ', 'REFUSED unuse'))
        time.sleep(3)
    out = {}
    for c in (host, client):
        start = c.bot.send('p7probe', 20)
        c.bot.line('P7PROBE end', 15, 'p7probe', start)
        out[c.tag] = re.findall(r'P7PROBE name=(\S+) calls=(\d+) puppet=(\d+) positive=(\d+)', c.bot.log()[start:])
    report['probe'] = out


def intruderkodiag(copies, report, negative=False):
    """P7 measurement, the intent to kill an intruder (HasAttackReasonToKill: for the attack reasons GateGuard 10 and Intruder
    11 only when the target is Entity::GetPlayer and stands in the guarded zone). Ishtar: the actor (the client; G3_WHO=host:
    the host, the single-player reference) walks into Muntasir's guarded place with 60 hit points and stays, the other hero
    30 m off; with QA p7probeon on both machines. Records the actor's state every 4 s for 60 s (down, dead, hit points), who
    attacks it and why, and the probe log of HasAttackReasonToKill; asserts nothing."""
    who = os.environ.get('G3_WHO', 'client')
    host, client = join_both(copies)
    clear_raid(host, report)
    actor, other = (host, client) if who == 'host' else (client, host)
    for c in (host, client):
        report_line(c.bot, 'p7probeon', 'P7PROBEON ')
    for c in (host, client):
        teleport_to(c.bot, 'Muntasir')
        if c is other:
            x, y, z = hero_pos(c.bot)
            move_hero(c.bot, (x + 3000, y + 30, z))
        time.sleep(2)
    report_line(actor.bot, 'sethp 60', 'SETHP ')
    marks = {c.tag: len(c.mod_log()) for c in (host, client)}
    states = []
    for _ in range(15):
        time.sleep(4)
        states.append(re.sub(r' state=\S+| ani_state=\S+| ai_mode=\S+| paused=\S+| running=\S+| page=\S+', '', report_line(actor.bot, 'herostate', 'HERO ')))
    report['hero'] = states
    at = next(v['pos'] for v in npcs(host.bot).values() if v['name'] == 'Muntasir')
    rows = npcs(host.bot)
    near = sorted({v['name'] for v in rows.values() if not v['dead'] and not v['name'].startswith('G3T_') and distance(v['pos'], at) < 2500})
    report['around'] = {n: (lambda c: f"{c.get('task')}>{c.get('target')}:{c.get('reason')}")(crime_of(host.bot, n)) for n in near}
    report['log'] = {c.tag: [l[13:] for l in c.mod_log()[marks[c.tag]:].splitlines() if 'HasAttackReasonToKill' in l or 'guards:' in l or 'downed' in l][-14:]
                     for c in (host, client)}
    if who == 'client':
        report['down_log'] = [l[13:] for l in client.mod_log()[marks['client']:].splitlines() if 'down' in l.lower() or 'killed' in l][-8:]


def stageIntruderFight(copies, report, negative=False):
    """P7, the guards' intent to kill an intruder as in single-player for the other player too. HasAttackReasonToKill holds
    for the attack reasons GateGuard and Intruder only against Entity::GetPlayer in the guarded zone (re/out/p7open-b.c):
    for the client's puppet it said 0, for the host's hero 1 (intruderkodiag 25.10.; with it the guards fought the host on
    after a knock-out, the client they let be). Now guards.cpp asks with the puppet as the game's player. Ishtar: the client
    (G3_WHO=host: the host, the single-player reference) walks into Muntasir's guarded place with 60 hit points and stays,
    the other hero 30 m off. Within 60 s at least one intent to kill was asked about the puppet and held (GUARDS kill_yes),
    and the intruder is not dead. Negative control: fightsoff on the host."""
    who = os.environ.get('G3_WHO', 'client')
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'fightsoff', 'FIGHTSOFF ')
    if os.environ.get('G3_FIGHTMEM') == '0':  # comparison: the fight memory's puppet-as-player in the attack states off
        report['fightmem'] = report_line(host.bot, 'fightmemoff', 'FIGHTMEMOFF ')
    actor, other = (host, client) if who == 'host' else (client, host)
    for c in (host, client):  # the host first: it simulates Ishtar
        teleport_to(c.bot, 'Muntasir')
        if c is other:
            x, y, z = hero_pos(c.bot)
            move_hero(c.bot, (x + 3000, y + 30, z))
        time.sleep(2)
    report_line(actor.bot, 'sethp 60', 'SETHP ')
    states, yes = [], 0
    for _ in range(15):
        time.sleep(4)
        states.append(re.sub(r' state=\S+| ani_state=\S+| ai_mode=\S+| paused=\S+| running=\S+| page=\S+', '', report_line(actor.bot, 'herostate', 'HERO ')))
        g = dict(re.findall(r'(\w+)=(\d+)', report_line(host.bot, 'guards', 'GUARDS ')))
        yes = int(g.get('kill_yes', 0))
        if who == 'client' and yes >= 1 and len(states) >= 6:
            break
    report['hero'] = states
    report['knocked_out'] = sum(1 for a, b in zip(states, states[1:]) if ' down=0' in a and ' down=1' in b) + (' down=1' in states[0])
    report['guards'] = report_line(host.bot, 'guards', 'GUARDS ')
    report['log'] = [l[13:] for l in host.mod_log().splitlines() if 'guards:' in l][-6:]
    assert ' dead=1' not in ''.join(states), f'the intruder died: {states[-3:]}'
    if who == 'client':
        assert yes >= 1, f'no intent to kill the intruding puppet: {report["guards"]}'


def chestwitnessdiag(copies, report, negative=False):
    """P7 measurement, opening a chest others can see (re/out/p7open-use-b.c: PS_Interact_Chest before unlocking and
    PS_Normal_Loop before rolling a chest's loot call SendPerceptionUseInteractObject; an NPC that sees it judges it with
    AssessUseInteractObject, attack reason 5, and the opening stops). After the raid in Ardea the actor (the client;
    G3_WHO=host: the host, the single-player reference) opens up to three unrolled chests near the start, nearest first, the
    other hero 15 m off; with QA p7probeon on both machines. Records the probe counts and log, what the actor does after
    and who around targets it (task, reason); asserts nothing."""
    who = os.environ.get('G3_WHO', 'client')
    host, client = join_both(copies)
    clear_raid(host, report)
    actor, other = (host, client) if who == 'host' else (client, host)
    expected = 'hero' if who == 'host' else 'puppet2'
    for c in (host, client):
        report_line(c.bot, 'p7probeon', 'P7PROBEON ')
    if os.environ.get('G3_CHEST_AT'):  # another town (chestscandiag 25.10.: Faring's chests stand in security level 3)
        for c in (host, client):
            teleport_to(c.bot, os.environ['G3_CHEST_AT'])
            time.sleep(2)
        time.sleep(4)
    found = [c for c in chests(host.bot) if c.get('gen_plunder') == '0' and c.get('sets') != '-'][:3]
    report['chests'] = [f"{c['guid'][:8]} {c['distance_cm']}cm locked={c.get('locked')}" for c in found]
    assert found, 'no unrolled chest near the start'
    for k, c in enumerate(found):
        info = report_line(host.bot, 'useinfo ' + c['guid'], ('USEINFO ', 'REFUSED useinfo'))
        m = re.search(r' pos=([-\d]+)_([-\d]+)_([-\d]+)', info)
        if not m:
            report[f'chest{k}'] = info[:160]
            continue
        at = tuple(float(v) for v in m.groups())
        move_hero(other.bot, (at[0] + 1500, at[1] + 60, at[2]))
        move_hero(actor.bot, (at[0] + 120, at[1] + 60, at[2]))
        time.sleep(3)
        rows = npcs(host.bot)
        near = sorted({v['name'] for v in rows.values() if not v['dead'] and not v['name'].startswith('G3T_') and distance(v['pos'], at) < 1200})
        marks = {x.tag: len(x.mod_log()) for x in (host, client)}
        report[f'chest{k}'] = {'name': re.search(r'name=(\S+)', info)[1], 'near': near,
                               'use': report_line(actor.bot, 'use ' + c['guid'], ('USE ', 'REFUSED use'))[:140]}
        time.sleep(8)
        report[f'chest{k}']['actor'] = report_line(actor.bot, 'herostate', 'HERO ')[:110]
        report[f'chest{k}']['around'] = {n: (lambda x: f"{x.get('task')}>{x.get('target')}:{x.get('reason')}")(crime_of(host.bot, n)) for n in near}
        report[f'chest{k}']['on_actor'] = [n for n, t in report[f'chest{k}']['around'].items() if f'>{expected}:' in t]
        report[f'chest{k}']['log'] = {x.tag: [l[13:] for l in x.mod_log()[marks[x.tag]:].splitlines() if 'p7probe:' in l or 'witness' in l][-10:]
                                      for x in (host, client)}
        report_line(actor.bot, 'unuse', ('UNUSE ', 'REFUSED unuse'))
        time.sleep(3)
    out = {}
    for c in (host, client):
        start = c.bot.send('p7probe', 20)
        c.bot.line('P7PROBE end', 15, 'p7probe', start)
        out[c.tag] = re.findall(r'P7PROBE name=(\S+) calls=(\d+) puppet=(\d+) positive=(\d+)', c.bot.log()[start:])
    report['probe'] = out


def chestscandiag(copies, report, negative=False):
    """P7 measurement, chests that stand in an indoor owner zone (where AssessUseInteractObject's helper sees an owner): the
    host's hero visits a member of each town (G3_HOMESCAN: name list) and lists the interact objects named *Chest* within
    80 m that QA ownedobj reports, with zone, owner and security level; asserts nothing."""
    host, client = join_both(copies)
    clear_raid(host, report)
    for anchor in os.environ.get('G3_HOMESCAN', 'Brenton,Lares,Lars,Konrad,Wenzel,Rhobar,Bernado,Orknarok,Zuben,Tjalf,Hamlar,Tarok,Javier').split(','):
        try:
            teleport_to(host.bot, anchor)
        except AssertionError as error:
            report[anchor] = str(error)[:100]
            continue
        time.sleep(5)
        start = host.bot.send('ownedobj 8000', 20)
        host.bot.line('OWNEDOBJ end', 15, 'ownedobj', start)
        rows = [dict(re.findall(r'(\w+)=(\S+)', l)) for l in host.bot.log()[start:].splitlines() if l.startswith('OWNEDOBJ guid=')]
        report[anchor] = [f"{r['name']}:{r['guid'][:8]}:{r['zone']}:{r['zone_owner']}:{r['security']}:{r['user']}:{r['pos']}" for r in rows if 'Chest' in r.get('name', '')][:10]


def stageWarn(copies, report, negative=False):
    """P7, a guard's warning about the other player reaches the NPCs around as in single-player. A guard that reacts to the
    player sends SendPerceptionWarn; the NPCs around judge it with AssessWarn against Entity::GetPlayer (re/out/p7open-b.c),
    which guards.cpp names as the puppet while the guard's reaction runs (p7opendiag 25.10.: host 2 warnings, 4 positive
    judgements; client 3 warnings, 4 positive). Reddock: the client draws its weapon next to Brenton, the host's hero 15 m
    off, with QA p7probeon on the host. Within 20 s at least one warning about the puppet went out and at least one NPC
    judged it for the puppet, and NPCs there turn to the puppet. Negative control: trespassoff on the host."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'trespassoff', 'TRESPASSOFF ')
    report['probe_on'] = report_line(host.bot, 'p7probeon', 'P7PROBEON ')
    for c in (host, client):  # the host first: it simulates Reddock
        teleport_to(c.bot, 'Brenton')
        if c is host:
            x, y, z = hero_pos(c.bot)
            move_hero(c.bot, (x + 1500, y + 30, z))
        time.sleep(2)
    report_line(client.bot, 'arm ' + WEAPON, 'ARM ')
    report['act'] = report_line(client.bot, 'act PS_Melee-PS_Melee_Loop', ('ACT ', 'REFUSED act'))

    def counts():
        start = host.bot.send('p7probe', 20)
        host.bot.line('P7PROBE end', 15, 'p7probe', start)
        return {m[1]: (int(m[2]), int(m[3]), int(m[4])) for m in re.finditer(r'P7PROBE name=(\S+) calls=(\d+) puppet=(\d+) positive=(\d+)', host.bot.log()[start:])}
    log = []

    def warned():
        c = counts()
        report['probe'] = c
        sent = [l for l in host.mod_log().splitlines() if 'p7probe: SendPerceptionWarn' in l and 'target=puppet' in l and 'result=1' in l]
        judged = [l for l in host.mod_log().splitlines() if 'p7probe: AssessWarn' in l and 'player=puppet' in l and 'result=1' in l]
        log[:] = (sent + judged)[-6:]
        return sent and judged and ['yes']
    try:
        wait_for(warned, 20, 'a warning about the puppet is sent and judged')
    finally:
        report['log'] = [l[13:] for l in log]
        rows = npcs(host.bot)
        at = next(v['pos'] for v in rows.values() if v['name'] == 'Brenton')
        near = sorted({v['name'] for v in rows.values() if not v['dead'] and not v['name'].startswith('G3T_') and distance(v['pos'], at) < 2000})
        report['around'] = {n: (lambda c: f"{c.get('task')}>{c.get('target')}:{c.get('reason')}")(crime_of(host.bot, n)) for n in near}
    report_line(client.bot, 'act PS_Normal-PS_Normal_Loop', ('ACT ', 'REFUSED act'))
    assert any('>puppet2:' in t for t in report['around'].values()), f'nobody turns to the puppet: {report["around"]}'


def stageObserveSuspect(copies, report, negative=False):
    """P7, NPCs watch the other player as a suspect where it does not belong, as in single-player. AssessPlayerZone puts
    NPCs that see a stranger in their guarded place into ZS_ObserveSuspect (the host's hero in Hammerclan: Tjalf, 25.10.);
    guards.cpp checks the puppets too and runs the state's loop with the puppet as the game's player. Ishtar: the client
    walks into Muntasir's place, the host's hero 30 m off. Within 25 s an NPC there observes the puppet (ZS_ObserveSuspect,
    target puppet2) and nobody observes the host's hero. Negative control: trespassoff on the host."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'trespassoff', 'TRESPASSOFF ')
    for c in (host, client):
        teleport_to(c.bot, 'Muntasir')
        if c is host:
            x, y, z = hero_pos(c.bot)
            move_hero(c.bot, (x + 3000, y + 30, z))
        time.sleep(2)
    rows = npcs(host.bot)
    at = next(v['pos'] for v in rows.values() if v['name'] == 'Muntasir')
    near = sorted({v['name'] for v in rows.values() if not v['dead'] and not v['name'].startswith('G3T_') and distance(v['pos'], at) < 2500})

    def observed():
        state = {n: crime_of(host.bot, n) for n in near}
        report['around'] = {n: f"{c.get('task')}>{c.get('target')}" for n, c in state.items()}
        return [n for n, c in state.items() if str(c.get('task', '')).startswith('ZS_ObserveSuspect') and c.get('target') == 'puppet2']
    try:
        report['observers'] = wait_for(observed, 25, 'an NPC observes the puppet as a suspect')
    finally:
        report['guards'] = report_line(host.bot, 'guards', 'GUARDS ')
    on_host = [n for n, t in report['around'].items() if t.startswith('ZS_ObserveSuspect') and t.endswith('>hero')]
    assert not on_host, f'{on_host} observe the host for what the client did'


def observesuspectdiag(copies, report, negative=False):
    """P7 measurement (regression for 0.5.1, 26.10.): what Muntasir and the NPCs around his place in Ishtar do when a
    stranger walks in, for the client's puppet (G3_WHO=client, as stageObserveSuspect) or for the host's own hero
    (G3_WHO=host: the game's own player, the single-player reference; the client waits 30 m off). Every half second for
    30 s each NPC's task and target on the host (QA crime); the first ZS_ObserveSuspect, ZS_ReactToZone and ZS_Attack per
    NPC with their second. Needs only crime, npcs and teleport, so it runs on older DLLs too (0.5.0: 287f549c)."""
    who = os.environ.get('G3_WHO', 'client')
    host, client = join_both(copies)
    clear_raid(host, report)
    teleport_to(host.bot, 'Muntasir')
    x, y, z = hero_pos(host.bot)
    outside = (x + 3000, y + 30, z)
    move_hero(host.bot, outside)
    time.sleep(2)
    if who == 'host':
        move_hero(client.bot, outside)
        time.sleep(4)
        teleport_to(host.bot, 'Muntasir')
    else:
        teleport_to(client.bot, 'Muntasir')
    t0 = time.time()
    rows = npcs(host.bot)
    at = next(v['pos'] for v in rows.values() if v['name'] == 'Muntasir')
    near = sorted({v['name'] for v in rows.values() if not v['dead'] and not v['name'].startswith('G3T_') and distance(v['pos'], at) < 2500})
    first, timeline, last = {}, [], None
    while time.time() - t0 < 30:
        state = {n: crime_of(host.bot, n) for n in near}
        second = round(time.time() - t0, 1)
        row = {n: f"{c.get('task')}>{c.get('target')}" for n, c in state.items()}
        if row != last:
            timeline.append([second, row])
            last = row
        for n, c in state.items():
            for kind in ('ZS_ObserveSuspect', 'ZS_ReactToZone', 'ZS_Attack'):
                if str(c.get('task', '')).startswith(kind):
                    first.setdefault(f'{kind}:{n}>{c.get("target")}', second)
        time.sleep(0.5)
    report['who'] = who
    report['first'] = first
    report['timeline'] = timeline
    report['guards'] = report_line(host.bot, 'guards', 'GUARDS ')


def stageFightMemory(copies, report, negative=False):
    """P7, the fight memory: onlookers watch the other player's duel and the opponent remembers it, as in single-player
    (fightmem.cpp). Reddock arena: the striker (the client; G3_WHO=host: the host, the single-player reference) duels Joey
    (duelhero, as the arena quest starts it) and knocks him out, the other hero 7 m off. Single-player (fightmemorydiag
    25.10.): Copper came to watch (ZS_WatchFight) and remembered the fight (LastFightAgainstPlayer 5, Friendly), Joey
    remembered losing (1); with the client nobody came and nobody remembered. Now: during the fight an onlooker watches it,
    afterwards Joey remembers losing and an onlooker remembers the fight on the host, and for the client its game's copies
    remember the same (an onlooker's memory 5 is what watching leaves; the task is seldom caught between hits). Since the
    grudge (26.10.): the host keeps the client's fight apart (FIGHTMEMORY kept=2:...), its NPCs keep what they remember of
    the host's hero. Negative control: fightmemoff on the host."""
    name = os.environ.get('G3_TARGET', 'Joey')
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'fightmemoff', 'FIGHTMEMOFF ')
    striker = host if os.environ.get('G3_WHO') == 'host' else client
    other = client if striker is host else host
    for c in (host, client):  # the host first: it simulates them
        teleport_to(c.bot, name)
        time.sleep(2)
    x, y, z = hero_pos(other.bot)
    move_hero(other.bot, (x + 700, y + 30, z))
    time.sleep(5)
    guid = next((g for g, v in npcs(host.bot).items() if v['name'] == name and not v['dead']), None)
    assert guid, f'{name} is not among the host NPCs'
    pos = npcs(host.bot)[guid]['pos']
    near = sorted({v['name'] for g, v in npcs(host.bot).items() if g != guid and not v['dead'] and not v['name'].startswith('G3T_')
                   and distance(v['pos'], pos) < 1500})[:8]
    report['near'] = near

    def memory(bot, n, player=None):
        line = report_line(bot, f'fightmemory {n}', ('FIGHTMEMORY ', 'REFUSED fightmemory'))
        m = re.search(rf' kept=(?:\S*,)?{player}:(\d+):', line) if player else re.search(r' last=(\d+)', line)
        return int(m[1]) if m else -1
    before = {n: memory(host.bot, n) for n in [name] + near}
    report_line(striker.bot, 'arm ' + WEAPON, 'ARM ')
    report['duel'] = report_line(striker.bot, 'duelhero ' + name, 'DUELHERO ')
    time.sleep(2)
    report_line(host.bot, f'npchp {name}_30', 'NPCHP ')
    watchers, state = set(), {}
    for _ in range(25):
        hit_npc(striker.bot, guid)
        time.sleep(0.8)
        for n in near:
            if str(crime_of(host.bot, n).get('task', '')).startswith('ZS_WatchFight'):
                watchers.add(n)
        state = npcs(host.bot).get(guid, {})
        if state.get('dead') or state.get('task') in KO_TASKS:
            break
    report['state'] = state
    report['watchers'] = sorted(watchers)
    time.sleep(8)
    after = {c.tag: {n: memory(c.bot, n) for n in [name] + near} for c in (host, client)}
    if striker is client:
        after['kept'] = {n: memory(host.bot, n, 2) for n in [name] + near}
    report['memory_before'] = before
    report['memory_after'] = after
    report['fightmem'] = {c.tag: report_line(c.bot, 'fightmem', 'FIGHTMEM ') for c in (host, client)}
    report['log'] = {c.tag: [l[13:] for l in c.mod_log().splitlines() if 'fightmem:' in l][-8:] for c in (host, client)}
    assert state.get('task') in KO_TASKS and not state.get('dead'), f'{name} was not knocked out: {state}'
    held = after['kept'] if striker is client else after['host']
    assert held[name] == 1, f'{name} does not remember losing on the host: {held}'
    assert any(held[n] == 5 for n in near), f'no onlooker remembers the fight on the host: {held}'
    if striker is client:
        assert after['host'] == before, f"the host's NPCs took the client's fight for the host's: {before} -> {after['host']}"
        assert after['client'][name] == 1, f"{name}'s copy does not remember losing on the client: {after['client']}"
        assert any(after['client'][n] == 5 for n in near), f"no onlooker's copy remembers the fight on the client: {after['client']}"


def newsscandiag(copies, report, negative=False):
    """P7 measurement, news of guards and bosses: which dialog infos say them (info script commands SayGuardNews,
    SayBossNews; _AI_SayGuardNews says Halt, then KnownMurder or KnownTheft by what the speaker's town knows of the player),
    who owns them and what they need (condition type, PlayerKnows). Host only; asserts nothing."""
    host, client = join_both(copies)
    clear_raid(host, report)
    for command in ('SayGuardNews', 'SayBossNews'):
        start = host.bot.send('infoscan ' + command, 30)
        host.bot.line('INFOSCAN end', 30, 'infoscan', start)
        rows = [l for l in host.bot.log()[start:].splitlines() if l.startswith('INFOSCAN ')]
        report[command] = rows[:40]
        infos = sorted({m[1] for l in rows for m in [re.search(r'info=(\S+)', l)] if m})[:6]
        for info in infos:
            start = host.bot.send('infoscan info_' + info, 30)
            host.bot.line('INFOSCAN end', 30, 'infoscan', start)
            report[f'{command}_{info}'] = [l[9:180] for l in host.bot.log()[start:].splitlines() if l.startswith('INFOSCAN info=')][:12]


def guardnewsdiag(copies, report, negative=False):
    """P7 measurement, news of guards (info FILLERX2140, condition type 31, script SayGuardNews: _AI_SayGuardNews says Halt,
    then KnownMurder or KnownTheft by what the speaker's town knows of the player). Reddock knows a theft of the player (QA
    crimeknown Reddock-3, shared by the town counters); the actor (the client; G3_WHO=host: the host, the single-player
    reference) stands 3 m from a guard (G3_GUARD, default RebWarrior_01), the other hero 20 m off. Records for 25 s whether
    a guard walks up and talks (dialog running on the actor, partner), then which infos the guard offers when the actor
    talks to it and what the news info does; asserts nothing. G3_ADDRESS=1 should be set for the client."""
    who = os.environ.get('G3_WHO', 'client')
    guard = os.environ.get('G3_GUARD', 'RebWarrior_01')
    host, client = join_both(copies)
    clear_raid(host, report)
    actor, other = (host, client) if who == 'host' else (client, host)
    for c in (host, client):  # the host first: it simulates Reddock
        teleport_to(c.bot, guard)
        if c is other:
            x, y, z = hero_pos(c.bot)
            move_hero(c.bot, (x + 2000, y + 30, z + 600))
        time.sleep(2)
    x, y, z = hero_pos(actor.bot)
    move_hero(actor.bot, (x + 300, y + 30, z))
    town = os.environ.get('G3_TOWN', 'Reddock')
    report['known'] = report_line(host.bot, f'crimeknown {town}-3', ('CRIMEKNOWN ', 'REFUSED crimeknown'))
    time.sleep(3)
    report['known_both'] = {c.tag: crime_of(c.bot, guard).get('known') for c in (host, client)}
    timeline = []
    for _ in range(25):
        time.sleep(1)
        t = talkstate(actor.bot)
        timeline.append(f"{t.get('running')}:{t.get('partner')}:{t.get('current')}")
        if t.get('running') == '1':
            break
    report['timeline'] = timeline[-8:]
    report['addressed'] = talkstate(actor.bot)
    if report['addressed'].get('running') == '1':
        time.sleep(6)
        report['addressed_after'] = talkstate(actor.bot)
        report_line(actor.bot, 'endtalk', ('TALKSTATE ', 'REFUSED endtalk'))
        time.sleep(2)
    line, infos = talk_infos(actor, guard)
    report['talk'] = line[:300]
    report['infos'] = infos
    news = next((i for i in ('FILLERX2140', 'FILLERX2144') if i in infos), None)  # guard news, boss news
    if news:
        report['news'] = report_line(actor.bot, 'talkinfo ' + news, ('TALKINFO ', 'REFUSED talkinfo'))
        time.sleep(6)
        report['news_state'] = talkstate(actor.bot)
    report['log'] = {c.tag: [l[13:] for l in c.mod_log().splitlines() if 'address:' in l or 'dialog' in l.lower()][-8:] for c in (host, client)}
    report_line(actor.bot, 'endtalk', ('TALKSTATE ', 'REFUSED endtalk'))


def crimenewsdiag(copies, report, negative=False):
    """P7 measurement, guard news after a counted crime. HasGuardNews (Script_Game 0xbcf00): a guard of the town (NPC type
    1, SVM block GuardAndBoss) has news while the town's count of a crime exceeds its SuspectComment and KnownPlayerCrime
    is below that crime (0x1000b8b0, re/out/news2-b.c); guardnewsdiag set KnownPlayerCrime to theft, which rules theft news
    out. The host adds one crime to G3_TOWN (QA crimeadd <town>-<G3_NEWS_CRIME, 3 theft>-1); the first player (G3_WHO,
    default host: the single-player reference) talks to guards near G3_GUARD (task with Guard, or G3_GUARDS) until one
    offers FILLERX2140 (guard news) or FILLERX2144 (boss news) and chooses it; then the other player talks to that guard.
    Records the offers and the town's counters (crimes, suspect, known) on both machines after each step; asserts nothing."""
    who = os.environ.get('G3_WHO', 'host')
    town = os.environ.get('G3_TOWN', 'Reddock')
    crime = os.environ.get('G3_NEWS_CRIME', '3')
    anchor = os.environ.get('G3_GUARD', 'Brenton')
    host, client = join_both(copies)
    clear_raid(host, report)
    first, second = (host, client) if who == 'host' else (client, host)
    for c in (host, client):  # the host first: it simulates the town
        teleport_to(c.bot, anchor)
        time.sleep(2)

    def counters(tag):
        report.setdefault('counters', {})[tag] = {
            c.tag: {k: crime_of(c.bot, anchor).get(k) for k in ('crimes', 'suspect', 'known')} for c in (host, client)}

    counters('start')
    report['add'] = report_line(host.bot, f'crimeadd {town}-{crime}-1', ('CRIMEADD ', 'REFUSED crimeadd'))
    time.sleep(2)
    report['suspect'] = report_line(host.bot, f'crimesuspect {town}-{crime}-0', ('CRIMESUSPECT ', 'REFUSED crimesuspect'))
    time.sleep(4)
    counters('added')
    pos = hero_pos(host.bot)
    listed = [g for g in os.environ.get('G3_GUARDS', '').split(',') if g]
    near = [v['name'] for v in npcs(host.bot).values() if not v['dead'] and 'Guard' in v['task']
            and sum((a - b) ** 2 for a, b in zip(v['pos'], pos)) < 4000 ** 2]
    candidates = list(dict.fromkeys(listed + near + [anchor]))[:8]
    report['candidates'] = candidates
    report['offers'] = {}
    guard = None
    for name in candidates:
        teleport_to(first.bot, name)
        time.sleep(2)
        _, infos = talk_infos(first, name)
        report['offers'][name] = infos
        news = next((i for i in ('FILLERX2140', 'FILLERX2144') if i in infos), None)
        if news:
            guard = name
            report['news'] = report_line(first.bot, 'talkinfo ' + news, ('TALKINFO ', 'REFUSED talkinfo'))
            time.sleep(8)
            report['news_state'] = talkstate(first.bot)
        report_line(first.bot, 'endtalk', ('TALKSTATE ', 'REFUSED endtalk'))
        time.sleep(1)
        if guard:
            break
    report['guard'] = guard
    time.sleep(4)
    counters('after_first')
    if guard:
        teleport_to(second.bot, guard)
        time.sleep(2)
        _, report['second_offers'] = talk_infos(second, guard)
        report_line(second.bot, 'endtalk', ('TALKSTATE ', 'REFUSED endtalk'))
        time.sleep(3)
        counters('after_second')


def guardcommentdiag(copies, report, negative=False):
    """P7 measurement, comments on sight (AssessActivePerception: the player within 7 m, not HasCrimeRefuse, HasCommentInfo
    -> StartComment; the guards' news FILLERX2140 is such a comment: HasGuardNews 0xbcf00 while the town's murder count is
    above SuspectComment, and saying it sets SuspectComment to the count). The town (G3_TOWN, Reddock) gets a murder (QA
    crimeadd, crimesuspect 0). (1) The client walks up to the town's people (G3_GUARDS, else those near G3_GUARD) while
    the host's hero is 25 m off: does anyone comment (SuspectComment, p7probe HasGuardNews)? (2) The host's hero walks up
    the same way (single-player reference). Records counters, probe lines and talk states; asserts nothing."""
    town = os.environ.get('G3_TOWN', 'Reddock')
    anchor = os.environ.get('G3_GUARD', 'Brenton')
    host, client = join_both(copies)
    clear_raid(host, report)
    for c in (host, client):
        report_line(c.bot, 'p7probeon', 'P7PROBEON ')
    for c in (host, client):  # the host first: it simulates the town
        teleport_to(c.bot, anchor)
        time.sleep(2)
    pos = hero_pos(host.bot)
    listed = [g for g in os.environ.get('G3_GUARDS', '').split(',') if g]
    near = [v['name'] for v in npcs(host.bot).values() if not v['dead'] and not v['name'].startswith('G3T_')
            and sum((a - b) ** 2 for a, b in zip(v['pos'], pos)) < 3000 ** 2]
    people = list(dict.fromkeys(listed + near))[:8]
    report['people'] = people

    def counters():
        return {c.tag: crime_of(c.bot, anchor).get('suspect') for c in (host, client)}

    def walk(walker, tag):
        marks = {c.tag: len(c.mod_log()) for c in (host, client)}
        report_line(host.bot, f'crimeadd {town}-4-1', ('CRIMEADD ', 'REFUSED crimeadd'))
        time.sleep(1)
        report_line(host.bot, f'crimesuspect {town}-4-0', ('CRIMESUSPECT ', 'REFUSED crimesuspect'))
        time.sleep(2)
        steps = [counters()]
        talks = []
        for name in people:
            report_line(walker.bot, 'endtalk', ('TALKSTATE ', 'REFUSED endtalk'))  # an addressing NPC held the walker
            teleport_to(walker.bot, name)
            time.sleep(4)
            steps.append(counters())
            t = talkstate(walker.bot)
            talks.append(f"{name}:{t.get('running')}:{t.get('partner')}:{t.get('current')}")
        report[tag] = {'suspect': steps, 'talks': talks,
                       'probe': {c.tag: [l[13:] for l in c.mod_log()[marks[c.tag]:].splitlines()
                                         if 'HasGuardNews' in l and ('result=1' in l or 'puppet' in l)][:12]
                                 for c in (host, client)},
                       'asked': {c.tag: sum(1 for l in c.mod_log()[marks[c.tag]:].splitlines() if 'HasGuardNews' in l) for c in (host, client)}}

    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 2500, y + 30, z))  # 80 m off, the town went over to the client (26.10.)
    time.sleep(3)
    walk(client, 'client_walk')
    x, y, z = hero_pos(client.bot)
    move_hero(client.bot, (x + 2500, y + 30, z))
    time.sleep(3)
    walk(host, 'host_walk')


def commentdiag(copies, report, negative=False):
    """P7 measurement, a guard's comment on sight (AssessActivePerception for the game's player within 7 m: HasCommentInfo,
    StartComment; the guards' murder news FILLERX2140 is one, and saying it sets the town's SuspectComment to its murder
    count). The town (G3_TOWN, Reddock) gets a murder (QA crimeadd, crimesuspect 0) before each run-up to the guard
    (G3_COMMENT_NPC, Reddock_Rebel_01, which had news for the host in guardcommentdiag): (1) the host's hero runs up
    (single-player reference); (2) the host stands 12 m off (it keeps simulating the guard) and the client runs up.
    Records SuspectComment, the walker's talk state and p7probe HasGuardNews lines for the guard; asserts nothing."""
    town = os.environ.get('G3_TOWN', 'Reddock')
    guard = os.environ.get('G3_COMMENT_NPC', 'Reddock_Rebel_01')
    host, client = join_both(copies)
    clear_raid(host, report)
    for c in (host, client):
        report_line(c.bot, 'p7probeon', 'P7PROBEON ')
    for c in (host, client):  # the host first: it simulates the town
        teleport_to(c.bot, guard)
        time.sleep(2)
    for c in (host, client):
        x, y, z = hero_pos(c.bot)
        move_hero(c.bot, (x + 2500 + 500 * (c is client), y + 30, z))
    time.sleep(3)

    def run(walker, tag):
        report_line(host.bot, f'crimeadd {town}-4-1', ('CRIMEADD ', 'REFUSED crimeadd'))
        time.sleep(1)
        report[tag + '_suspect0'] = report_line(host.bot, f'crimesuspect {town}-4-0', ('CRIMESUSPECT ', 'REFUSED crimesuspect'))
        time.sleep(2)
        marks = {c.tag: len(c.mod_log()) for c in (host, client)}
        report[tag + '_mirrored'] = {c.tag: next((v['mirrored'] for v in npcs(c.bot).values() if v['name'] == guard), None)
                                     for c in (host, client)}
        run_up(walker, host, guard)
        samples = []
        for _ in range(10):
            time.sleep(1)
            t = talkstate(walker.bot)
            samples.append(f"{crime_of(host.bot, guard).get('suspect')}|{crime_of(client.bot, guard).get('suspect')}|"
                           f"{t.get('running')}:{t.get('partner')}:{t.get('current')}")
        report[tag] = samples
        report[tag + '_probe'] = {c.tag: [l[13:] for l in c.mod_log()[marks[c.tag]:].splitlines()
                                          if 'HasGuardNews' in l and f'self={guard} ' in l][:6] for c in (host, client)}
        report_line(walker.bot, 'endtalk', ('TALKSTATE ', 'REFUSED endtalk'))
        x, y, z = hero_pos(walker.bot)
        move_hero(walker.bot, (x + 2500, y + 30, z))
        time.sleep(3)

    run(host, 'host_run')
    teleport_to(host.bot, guard)
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 1200, y + 30, z))
    time.sleep(3)
    run(client, 'client_run')


def stageGuardNews(copies, report, negative=False):
    """P7, a guard's news for the client (_AI_SayGuardNews, info FILLERX2140: HasGuardNews 0xbcf00 while the town's murder
    count is above its SuspectComment; saying it sets SuspectComment to the count). It is an important info: the game's
    look around makes the guard address its player (AssessActivePerception, AssessTalk; commentdiag 26.10.: RebNovice_04
    told the host's hero at once as it ran up), and for the client a guard walks up the same way (address.cpp; the
    harness turns that off unless a stage asks, which is why commentdiag heard nothing for the client). Reddock gets a
    murder (QA crimeadd, crimesuspect 0); the host stands 12 m off the guard (G3_COMMENT_NPC, Reddock_Rebel_01), the client
    runs up: within 12 s the news runs in a dialog on the client and SuspectComment reaches the count on both machines.
    Negative control: addressoff on the client."""
    town = os.environ.get('G3_TOWN', 'Reddock')
    guard = os.environ.get('G3_COMMENT_NPC', 'Reddock_Rebel_01')
    host, client = join_both(copies)
    clear_raid(host, report)
    # The harness turns addressing off in the menu; here it is what the stage checks (negative: stays off).
    report['address'] = report_line(client.bot, 'addressoff' if negative else 'addresson', ('ADDRESSOFF ', 'ADDRESSON '))
    for c in (host, client):  # the host first: it simulates the town
        teleport_to(c.bot, guard)
        time.sleep(2)
    x, y, z = hero_pos(client.bot)
    move_hero(client.bot, (x + 3000, y + 30, z))
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 1200, y + 30, z))
    time.sleep(3)
    report['add'] = report_line(host.bot, f'crimeadd {town}-4-1', ('CRIMEADD ', 'REFUSED crimeadd'))
    time.sleep(1)
    report['suspect0'] = report_line(host.bot, f'crimesuspect {town}-4-0', ('CRIMESUSPECT ', 'REFUSED crimesuspect'))
    time.sleep(2)
    count = int(re.search(r' count=(\d+)', report['suspect0'])[1])
    report['run_up'] = run_up(client, host, guard)
    samples, news = [], False
    for _ in range(12):
        time.sleep(1)
        t = talkstate(client.bot)
        samples.append(f"{crime_of(host.bot, guard).get('suspect')}|{crime_of(client.bot, guard).get('suspect')}|"
                       f"{t.get('running')}:{t.get('partner')}:{t.get('current')}")
        news = news or t.get('current') == 'FILLERX2140'
    time.sleep(3)
    report['samples'] = samples
    report['address_after'] = report_line(client.bot, 'address', 'ADDRESS ')
    suspect = {c.tag: int(str(crime_of(c.bot, guard).get('suspect', '0')).split(',')[4]) for c in (host, client)}
    report['suspect'] = suspect
    report_line(client.bot, 'endtalk', ('TALKSTATE ', 'REFUSED endtalk'))
    assert news, f'no guard news for the client: {samples[-4:]}'
    assert suspect == {'host': count, 'client': count}, f'the town did not note the news ({count} murders): {suspect}'


def attitudeleakdiag(copies, report, negative=False):
    """P7 measurement, an NPC's own memory of the host's hero and the client (GetAttitudeToPlayer: the NPC's crime memory of
    the player, LastPlayerCrime, and the town's; ai.cpp answers GetAttitude about a puppet as about the host's hero). The
    host's hero commits a theft before NPC G3_LEAK_NPC (default Hamlar; QA witness <npc>_4: AssessTarget with reason
    Theft, the last step of AssessTakeItem), jumps 60 m off and the NPC gives up (QA cancelattack). Then the client runs up
    to the NPC alone. Records the NPC's attitude (hero, puppet, to_player, focus) and crime memory on both machines, and
    for 12 s whom it targets; asserts nothing."""
    npc = os.environ.get('G3_LEAK_NPC', 'Hamlar')
    host, client = join_both(copies)
    clear_raid(host, report)
    for c in (host, client):
        teleport_to(c.bot, npc)
        time.sleep(2)

    def state(tag):
        report.setdefault('state', {})[tag] = {c.tag: {
            'attitude': report_line(c.bot, 'attitude ' + npc, ('ATTITUDE ', 'REFUSED attitude'))[9:],
            'crime': {k: crime_of(c.bot, npc).get(k) for k in ('last_crime', 'reason', 'target', 'task', 'crimes', 'known')}}
            for c in (host, client)}

    x, y, z = hero_pos(client.bot)
    move_hero(client.bot, (x + 4000, y + 30, z))
    time.sleep(2)
    state('start')
    report['theft'] = report_line(host.bot, f'witness {npc}_4', ('WITNESSSAW ', 'REFUSED witness'))
    time.sleep(1)
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 6000, y + 30, z))
    report['cancel'] = report_line(host.bot, f'cancelattack {npc}', ('CANCELATTACK ', 'REFUSED cancelattack'))
    time.sleep(3)
    state('after_theft')
    run_up(client, host, npc)
    samples = []
    for _ in range(12):
        time.sleep(1)
        cr = crime_of(host.bot, npc)
        samples.append(f"{cr.get('task')}:{cr.get('target')}:{cr.get('reason')}")
    report['client_near'] = samples
    state('client_near')


def stageAttitudeShared(copies, report, negative=False):
    """P7, a town's anger at a theft as each player's game judges its own hero (GetAttitudeToPlayer, GetFocusNameAttitude:
    the focus name's colour). With reputation shared, what the town knows (KnownPlayerCrime) is the same on both machines
    (enclaves.cpp), so a town member's attitude to the client's hero in the client's own game follows the host's theft as
    the host's does (attitudeleakdiag 26.10.: Norris in Reddock 2 -> 3 on both, to_player and focus alike; Hamlar, whose
    memory of the theft is his own and not the town's, stayed friendly to both). The host's hero steals before G3_LEAK_NPC
    (Norris; QA witness <npc>_4, the last step of AssessTakeItem): on the client, to_player and focus of that NPC for the
    client's hero equal the host's for its hero and are above where they started. Negative control: enclavesyncoff on both."""
    npc = os.environ.get('G3_LEAK_NPC', 'Norris')
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        for c in (host, client):
            assert 'ok=1' in report_line(c.bot, 'enclavesyncoff', 'ENCLAVESYNCOFF ')
    for c in (host, client):  # the host first: it simulates the town
        teleport_to(c.bot, npc)
        time.sleep(2)
    x, y, z = hero_pos(client.bot)
    move_hero(client.bot, (x + 1500, y + 30, z))
    time.sleep(2)

    def view():
        out = {}
        for c in (host, client):
            line = report_line(c.bot, 'attitude ' + npc, ('ATTITUDE ', 'REFUSED attitude'))
            out[c.tag] = {k: int(v) for k, v in re.findall(r' (to_player|focus)=(-?\d+)', line)}
        return out

    before = view()
    report['before'] = before
    report['theft'] = report_line(host.bot, f'witness {npc}_4', ('WITNESSSAW ', 'REFUSED witness'))
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 6000, y + 30, z))
    report['cancel'] = report_line(host.bot, f'cancelattack {npc}', ('CANCELATTACK ', 'REFUSED cancelattack'))
    time.sleep(5)
    after = view()
    report['after'] = after
    report['known'] = {c.tag: crime_of(c.bot, npc).get('known') for c in (host, client)}
    assert after['host']['to_player'] > before['host']['to_player'], f'the theft did not anger {npc} at the host: {before} -> {after}'
    assert after['client'] == after['host'], f"{npc} sees the client's hero otherwise than the host's: {after}"


def stageFriendBlow(copies, report, negative=False):
    """P7, a blow on a friend (AssessAgressor 0x24490: in its routine an NPC friendly to the attacker complains first,
    ZS_ReactToDamage; aggressordiag 26.10.: Hamlar, struck once by the host's hero, complained 6 s and went back to his
    routine). The client's blows on the host's NPCs went to AssessTarget directly, so a friend fought back at once; now
    combat.cpp asks AssessAgressor with the puppet as the player. The client (G3_WHO=host: the host, the single-player
    reference) strikes G3_FRIEND (Hamlar, friendly to the heroes) once, the other hero 25 m off: within 6 s the friend
    complains (ZS_ReactToDamage) and does not attack; then a second blow, recorded. Negative control: agressoroff."""
    name = os.environ.get('G3_FRIEND', 'Hamlar')
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'agressoroff', 'AGRESSOROFF ')
    striker = host if os.environ.get('G3_WHO') == 'host' else client
    other = client if striker is host else host
    for c in (host, client):  # the host first: it simulates the NPC
        teleport_to(c.bot, name)
        time.sleep(2)
    # 25 m: Hamlar has an important info for the host's hero and walked up to talk within 20 m; struck in a dialog an NPC
    # fights back at once (AIMode Talk; the first single-player run).
    x, y, z = hero_pos(other.bot)
    move_hero(other.bot, (x + 2500, y + 30, z))
    # The striker off the NPC's destination: an NPC waiting there for a player (ZS_WaitForFreeDestination, for puppets
    # too since 26.10.) took the first blow as a fight at once, the host's hero standing there as well (26.10. 07:00).
    x, y, z = hero_pos(striker.bot)
    move_hero(striker.bot, (x + 400, y + 30, z))
    time.sleep(2)
    try:
        wait_for(lambda: 'task=ZS_WaitForFreeDestination' not in report_line(host.bot, 'findnpc ' + name, ('FIND ', 'REFUSED find'))
                 and ['routine'], 15, name + ' back in its routine')
    except AssertionError:
        report['still_waiting'] = True
    guid = next((g for g, v in npcs(host.bot).items() if v['name'] == name), None)
    assert guid, f'{name} is not among the host NPCs'
    report['attitude'] = report_line(host.bot, 'attitude ' + name, ('ATTITUDE ', 'REFUSED attitude'))
    report_line(host.bot, 'p7probeon', 'P7PROBEON ')  # counts AssessAgressor
    mark = len(host.mod_log())
    report_line(striker.bot, 'arm ' + WEAPON, 'ARM ')
    time.sleep(1)

    def sample(n):
        out = []
        for _ in range(n):
            time.sleep(1)
            cr = crime_of(host.bot, name)
            out.append(f"{cr.get('task')}:{cr.get('target')}")
        return out

    report['before_hit'] = re.sub(r'^FIND ', '', report_line(host.bot, 'findnpc ' + name, ('FIND ', 'REFUSED find')))[:220]
    report['first_hit'] = hit_npc(striker.bot, guid)['line'][-100:]
    first = sample(6)
    report['first'] = first
    report['second_hit'] = hit_npc(striker.bot, guid)['line'][-100:]
    report['second'] = sample(4)
    report['combat'] = combat_state(host.bot).get('reactions')
    report['probe'] = [l[13:] for l in host.mod_log()[mark:].splitlines() if 'AssessAgressor' in l or 'fight sound' in l][:8]
    target = 'hero' if striker is host else 'puppet2'
    assert any(s == f'ZS_ReactToDamage:{target}' for s in first), f'{name} did not complain about the first blow: {first}'
    assert not any(s.startswith('ZS_Attack') for s in first[:3]), f'{name} fought back at the first blow: {first}'


def stageForeignHome(copies, report, negative=False):
    """P7, walking into someone's home while its owner is out: the owner comes after the intruder as in single-player
    (AssessPlayerEnterZone sends the NPC whose sleeping point lies in the entered zone to the player: ZS_GotoPlayerZone).
    The game tells its NPCs only of its own player's zone changes; the host watches the puppets' zones (guards.cpp
    WatchZones). Hammerclan (homescandiag 25.10.): Tjalf sleeps in Tjalf_Room and is out by day; G3_HOME_OWNER: another
    owner. The owner must notice the intruder (re/out/social-guards-c.c 0x8bd0: in view, or within 10 m and not standing
    still), so the intruder runs in from behind the owner (AI goto). The client is the intruder; G3_WHO=host: the host is and the client stays away (single-player
    reference). Within 25 s the owner targets the intruder; nobody attacks the other hero. Negative control: trespassoff
    on the host."""
    who = os.environ.get('G3_WHO', 'client')
    owner = os.environ.get('G3_HOME_OWNER', 'Tjalf')
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'trespassoff', 'TRESPASSOFF ')
    intruder, other = (host, client) if who == 'host' else (client, host)
    teleport_to(host.bot, owner)
    teleport_to(client.bot, owner)
    time.sleep(5)

    def home_row():
        start = host.bot.send('homes 3000_all', 20)
        host.bot.line('HOMES end', 15, 'homes', start)
        rows = [dict(re.findall(r'(\w+)=(\S+)', l)) for l in host.bot.log()[start:].splitlines() if l.startswith(f'HOME npc={owner} ')]
        return rows[0] if rows else {}
    home = home_row()
    report['home'] = home
    assert home and home.get('home') != 'Nav_Zone', f'{owner} has no home of its own here: {home}'
    assert home.get('at_home') == '0', f'{owner} is at home: {home}'
    bed = tuple(map(float, home['bed'].split(',')))
    # Tjalf has an important info for each hero and walks up to talk first (stageForeignHome 25.10.): that talk first, its
    # options chosen through to the end, so that he stands in his routine again when the intruder comes.
    start = intruder.bot.send('important 2500', 20)
    intruder.bot.line('IMPORTANT end', 15, 'important', start)
    if any(l.startswith(f'IMPORTANT npc={owner} ') for l in intruder.bot.log()[start:].splitlines()):
        report['talk_first'] = talk_through(intruder, owner)
        time.sleep(5)
    start_at = next(v['pos'] for v in npcs(host.bot).values() if v['name'] == owner)
    # The intruder starts 3 m behind the owner (away from the bed) and runs past it into the room: moving within 10 m it is
    # heard (Tjalf_Room reaches to ~6 m from where Tjalf stands, stageForeignHome 25.10.). The other hero 15 m aside (40 m
    # for the reference: away from all of it).
    away = [a - b for a, b in zip(start_at, bed)]
    length = math.hypot(away[0], away[2]) or 1.0
    back = (start_at[0] + 300 * away[0] / length, start_at[1] + 30, start_at[2] + 300 * away[2] / length)
    move_hero(intruder.bot, back)
    x, y, z = start_at
    move_hero(other.bot, (x - 600 * away[2] / length * (6 if who == 'host' else 2.5), y + 30, z + 600 * away[0] / length * (6 if who == 'host' else 2.5)))
    time.sleep(4)
    report['intruder_zone_before'] = hero_zone(intruder.bot)
    expected = 'hero' if who == 'host' else 'puppet2'
    report['owner_before'] = crime_of(host.bot, owner)
    report['run'] = report_line(intruder.bot, f'runto {bed[0]:.0f}_{bed[1]:.0f}_{bed[2]:.0f}', ('RUNTO ', 'REFUSED runto'))
    timeline = []

    def came():
        state = crime_of(host.bot, owner)
        timeline.append(f"{state.get('task')}>{state.get('target')}:{state.get('reason')}|{hero_zone(intruder.bot)}")
        return state.get('target') == expected and state.get('task', '').startswith(('ZS_GotoPlayerZone', 'ZS_ReactToZone', 'ZS_ObserveZonePlayer')) and ['yes']
    try:
        wait_for(came, 25, f'{owner} goes after the intruder ({expected}) into its home')
    finally:
        report['owner_timeline'] = timeline[-14:]
        report['guards'] = report_line(host.bot, 'guards', 'GUARDS ')
        report['log'] = [l for l in host.mod_log().splitlines() if 'guards:' in l][-8:]
    time.sleep(8)
    report['owner_after'] = crime_of(host.bot, owner)
    near = {v['name'] for v in npcs(host.bot).values() if not v['dead'] and not v['name'].startswith('G3T_') and distance(v['pos'], bed) < 2500}
    on_other = [n for n in near if (lambda c: c.get('task') == 'ZS_Attack' and c.get('target') == ('puppet2' if who == 'host' else 'hero'))(crime_of(host.bot, n))]
    assert not on_other, f'{on_other} attack the other player for what the intruder did'


def stageSneak(copies, report, negative=False):
    """P7, NPCs judge the other player sneaking about as in single-player. AssessPlayerSneaking (re/out/rest-sneak-b.c)
    reacts to the game's own player: AniState Sneak, the NPC not fighting and no friend, within 7 m, in its field of view
    with a free line of sight: ZS_ReactToSneak. The sneaker (the client; G3_WHO=host: the host, the single-player
    reference) holds the sneak state (QA sneak: the sneak key's flag) 3 m from each watcher of G3_WATCHERS (default
    Gelford, Reddock) on each of four sides for 6 s, the other hero 20 m off; recorded: every NPC within 12 m whose target
    is the sneaker. Reference 25.10.: nobody noticed the host; the puppet check (eb4ed16) had Gelford, Reddock_Rebel_01 and
    four rebels notice the client, so sneaking is no longer checked against puppets (guards.cpp). G3_EXPECT_NOTICE=0
    (default): nobody notices; 1: someone does. Negative control: sneakcheckon on the host (the puppet check again)."""
    who = os.environ.get('G3_WHO', 'client')
    watchers = os.environ.get('G3_WATCHERS', 'Gelford').split(',')
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'sneakcheckon', 'SNEAKCHECKON ')
    sneaker, other = (host, client) if who == 'host' else (client, host)
    expected = 'hero' if who == 'host' else 'puppet2'
    noticed = []
    for watcher in watchers:
        teleport_to(other.bot, watcher)
        x, y, z = hero_pos(other.bot)
        move_hero(other.bot, (x - 2000, y + 30, z - 600))
        teleport_to(sneaker.bot, watcher)
        x, y, z = hero_pos(sneaker.bot)
        move_hero(sneaker.bot, (x + 1500, y + 30, z))  # out of reach while the stance changes
        time.sleep(2)
        timeline = []
        for side in range(4):
            at = next(v['pos'] for v in npcs(host.bot).values() if v['name'] == watcher)
            angle = side * math.pi / 2
            move_hero(sneaker.bot, (at[0] + 300 * math.cos(angle), at[1] + 30, at[2] + 300 * math.sin(angle)))
            stance = report_line(sneaker.bot, 'sneak 1', ('SNEAK ', 'REFUSED sneak'))
            for _ in range(6):
                time.sleep(1)
                ani = re.search(r'ani_state=(\d+)', report_line(sneaker.bot, 'heropos', 'HEROPOS '))[1]
                near = {v['name'] for v in npcs(host.bot).values() if not v['dead'] and not v['name'].startswith('G3T_') and distance(v['pos'], at) < 1200}
                on_me = sorted(n for n in near if crime_of(host.bot, n).get('target') == expected)
                timeline.append(f"{side}:ani {ani}:{','.join(on_me) or '-'}")
                noticed += [n for n in on_me if n not in noticed]
        report[f'{watcher}_timeline'] = timeline[-12:]
        report[f'{watcher}_stance'] = stance
        report_line(sneaker.bot, 'sneak 0', ('SNEAK ', 'REFUSED sneak'))
    report['guards'] = report_line(host.bot, 'guards', 'GUARDS ')
    report['log'] = [l for l in host.mod_log().splitlines() if 'guards:' in l][-6:]
    report['noticed'] = noticed
    assert all(t.split(':')[1] == 'ani 3' for t in timeline), f'the hero did not stay sneaking: {timeline[-4:]}'
    if os.environ.get('G3_EXPECT_NOTICE', '0') == '1':
        assert noticed, f'nobody noticed the sneaking {who}'
    else:
        assert not noticed, f'{noticed} noticed the sneaking {who}'


def stageAddressed(copies, report, negative=False):
    """P7 rest / dialogs, an NPC with an important info walks up to the client and talks to it, as the game's NPCs do for
    their player (address.cpp; importanttalkdiag 25.10.: Hamlar addressed only the host). After the raid the first
    village's Hamlar (G3_CALLER) has an important info for each hero. The host's hero stands 30 m from him (beyond the
    game's 20 m, within the host's 40 m: the host simulates him and he leaves the host alone), the client 8 m. Within 40 s
    the client's dialog with him runs, he walked up to the puppet on the host, and the host has no dialog. Negative control:
    addressoff on the client (no dialog)."""
    caller = os.environ.get('G3_CALLER', 'Hamlar')
    host, client = join_both(copies)
    clear_raid(host, report)
    assert 'ok=1' in report_line(client.bot, 'addressoff' if negative else 'addresson', ('ADDRESSOFF ', 'ADDRESSON '))
    at = next(v['pos'] for v in npcs(host.bot).values() if v['name'] == caller)
    move_hero(host.bot, (at[0] + 3000, at[1] + 50, at[2]))
    time.sleep(2)
    report['host_talk_before'] = talkstate(host.bot)
    assert report['host_talk_before'].get('running') == '0', f'{caller} addressed the host first: {report["host_talk_before"]}'
    move_hero(client.bot, (at[0], at[1] + 50, at[2] + 800))
    time.sleep(1)
    start = client.bot.send('important 2500', 20)
    client.bot.line('IMPORTANT end', 15, 'important', start)
    report['client_important'] = [l for l in client.bot.log()[start:].splitlines() if l.startswith('IMPORTANT npc=')]
    assert any(f'npc={caller} ' in l for l in report['client_important']), f'{caller} has no important info for the client'
    try:
        wait_for(lambda: (lambda t: t.get('running') == '1' and t.get('partner') == caller and ['yes'])(talkstate(client.bot)), 40,
                 f'{caller} talks to the client')
    finally:
        report['client_talk'] = talkstate(client.bot)
        report['host_talk'] = talkstate(host.bot)
        report['address'] = {c.tag: report_line(c.bot, 'address', 'ADDRESS ') for c in (host, client)}
        report['log'] = {c.tag: [l for l in c.mod_log().splitlines() if 'address:' in l][-6:] for c in (host, client)}
        puppet = remotes(host.bot).get(2, {}).get('puppet_pos')
        here = next((v['pos'] for v in npcs(host.bot).values() if v['name'] == caller), None)
        report['caller_to_puppet_m'] = round(distance(here, puppet) / 100, 1) if puppet and here else None
    assert report['host_talk'].get('running') == '0', f'the host is in a dialog too: {report["host_talk"]}'
    report_line(client.bot, 'endtalk', ('TALKSTATE ', 'REFUSED endtalk'))


def importanttalkdiag(copies, report, negative=False):
    """P7/dialog measurement, NPCs that walk up to the player to talk: AssessActivePerception lets an NPC within 20 m with an
    important info for Entity::GetPlayer start AssessTalk (re/out/p7-activeperc-b.c; guards with news, quest givers). On the
    client the host's NPCs are mirrors whose perception does not run. At each anchor (G3_IMPORTANT_AT, default the first
    village and Reddock) both heroes stand 6 m apart near it; lists on each machine the NPCs with an important info for its
    own hero (QA important), then for 30 s whom those NPCs target and what they do on the host. Asserts nothing."""
    host, client = join_both(copies)
    clear_raid(host, report)
    for anchor in os.environ.get('G3_IMPORTANT_AT', 'Hamlar,Javier,Gorn').split(','):
        teleport_to(host.bot, anchor)
        teleport_to(client.bot, anchor)
        x, y, z = hero_pos(client.bot)
        move_hero(client.bot, (x + 600, y, z))
        time.sleep(4)
        lists = {}
        for c in (host, client):
            start = c.bot.send('important 2500', 20)
            c.bot.line('IMPORTANT end', 15, 'important', start)
            lists[c.tag] = [dict(re.findall(r'(\w+)=(\S+)', l)) for l in c.bot.log()[start:].splitlines() if l.startswith('IMPORTANT npc=')]
        report[f'{anchor}_important'] = {tag: [f"{r['npc']}:{r['distance']}:m{r['mirrored']}:{r['task']}" for r in rows] for tag, rows in lists.items()}
        names = sorted({r['npc'] for rows in lists.values() for r in rows})
        timeline = []
        for _ in range(10):
            time.sleep(3)
            timeline.append({n: (lambda c: f"{c.get('task')}>{c.get('target')}")(crime_of(host.bot, n)) for n in names})
        report[f'{anchor}_timeline'] = timeline
        report[f'{anchor}_talk'] = {c.tag: talkstate(c.bot) for c in (host, client)}
        for c in (host, client):
            if talkstate(c.bot).get('running') == '1':
                report_line(c.bot, 'endtalk', ('TALKSTATE ', 'REFUSED endtalk'))


def stageBoulder(copies, report, negative=False):
    """P1, the orc boulder of Faring (boulderdiag 25.10.: G3_Object_Interact_Animated_OrcBoulder_Faring, usetype 102,
    UseFunc OB_Faring_Boulder): a strength test (re/out/rest-boulder-b.c: 200 to start, 250 to lift, else TooHeavy); a lift
    sets the game event Faring_Boulder_Success for the lifter's game, the boulder stays where it is. The client's hero, with
    strength 300, lifts it (the orc Tukash who uses it by default sent 25 m away on the host); within 30 s the event is set
    on both machines (story.cpp shares game events). Negative control: storyoff on the client (the host never learns it)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(client.bot, 'storyoff', 'STORYOFF ')
    boulder = 'G3_Object_Interact_Animated_OrcBoulder_Faring'
    event = 'Faring_Boulder_Success'
    report['before'] = {c.tag: report_line(c.bot, f'event {event}', ('EVENT ', 'REFUSED event')) for c in (host, client)}
    teleport_to(host.bot, boulder)
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 1000, y + 30, z))
    teleport_to(client.bot, boulder)
    time.sleep(4)
    for c in (host, client):
        start = c.bot.send('usables all1500', 20)
        c.bot.line('USABLE end', 15, 'usables', start)
        report[f'{c.tag}_boulder'] = [l for l in c.bot.log()[start:].splitlines() if 'OrcBoulder' in l][:2]
    user = re.search(r' user=(\S+)', ''.join(report['host_boulder']))
    if user and user[1] not in ('-', 'G3T_Player2'):
        report['user_moved'] = report_line(host.bot, f'farmove {x + 2500:.0f}_{y + 50:.0f}_{z:.0f}_{user[1]}', ('FARMOVE ', 'REFUSED farmove'))
        time.sleep(3)
    report['strength'] = report_line(client.bot, 'setattr STR_300', ('SETATTR ', 'REFUSED setattr'))
    report['use'] = report_line(client.bot, f'use {boulder}', ('USE ', 'REFUSED use'))
    try:
        wait_for(lambda: all('known=1' in report_line(c.bot, f'event {event}', ('EVENT ', 'REFUSED event')) for c in (host, client)) and ['yes'],
                 30, f'{event} on both machines')
    finally:
        report['after'] = {c.tag: report_line(c.bot, f'event {event}', ('EVENT ', 'REFUSED event')) for c in (host, client)}
        report['client_task'] = report_line(client.bot, 'herostate', 'HERO ')[:200]


def stageBoulderHold(copies, report, negative=False):
    """P1, the orc boulder's stances (boulderseqdiag 26.10.: the host alone and the client go the same way, PS_Interact ->
    TiltOrcBoulder (AniState 12) -> HoldOrcBoulder -> LiftOrcBoulder (13) -> LiftOrcBoulder_Loop (14) -> PS_Interact_End;
    the states run on the user's own machine). The client's hero with strength 300 uses the Faring boulder; within 25 s it
    must hold (12) and lift (14) it, and its puppet on the host must show the same stances (AniState 12 and 14, motions
    TiltOrcBoulder, HoldOrcBoulder, LiftOrcBoulder). Negative control: stanceoff on the host (the puppet keeps its own
    AniState)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'stanceoff', 'STANCEOFF ')
    boulder = 'G3_Object_Interact_Animated_OrcBoulder_Faring'
    teleport_to(host.bot, boulder)
    x, y, z = hero_pos(host.bot)
    start = host.bot.send('usables all1500', 20)
    host.bot.line('USABLE end', 15, 'usables', start)
    holder = re.search(r' user=(\S+)', ''.join(l for l in host.bot.log()[start:].splitlines() if 'OrcBoulder' in l))
    if holder and holder[1] not in ('-', 'G3T_Player2', 'PC_Hero'):
        report['user_moved'] = report_line(host.bot, f'farmove {x + 2500:.0f}_{y + 50:.0f}_{z:.0f}_{holder[1]}', ('FARMOVE ', 'REFUSED farmove'))
    move_hero(host.bot, (x + 800, y + 30, z))
    teleport_to(client.bot, boulder)
    time.sleep(4)
    report['strength'] = report_line(client.bot, 'setattr STR_300', ('SETATTR ', 'REFUSED setattr'))
    report['use'] = report_line(client.bot, f'use {boulder}', ('USE ', 'REFUSED use'))
    client_states, puppet_states, motions = set(), set(), set()
    for _ in range(50):
        hero = herostate(client.bot)
        client_states.add((re.search(r' state=(\S+)', hero)[1], int(re.search(r' ani_state=(\d+)', hero)[1])))
        puppet = report_line(host.bot, 'puppetstate', ('PUPPETSTATE ', 'REFUSED puppetstate'))
        puppet_states.add(int(re.search(r'ani_state=(\d+)', puppet)[1]))
        motion = re.search(r'motion=Hero_(\w+?)_None_None_P0_(\w+?)_', puppet)
        if motion:
            motions.update(m for m in motion.groups() if 'OrcBoulder' in m)
        if (('PS_Interact_End', 2) in client_states or ('PS_Normal_Loop', 2) in client_states) and any(a == 14 for _, a in client_states):
            break
        time.sleep(0.5)
    report['client_states'] = sorted(client_states)
    report['puppet_ani_states'] = sorted(puppet_states)
    report['puppet_motions'] = sorted(motions)
    client_ani = {a for _, a in client_states}
    assert {12, 14} <= client_ani, f"the client did not hold and lift the boulder: {report['client_states']} (test setup)"
    assert {12, 14} <= puppet_states, f"the host's puppet did not show holding and lifting: {report['puppet_ani_states']}"
    assert {'TiltOrcBoulder', 'HoldOrcBoulder', 'LiftOrcBoulder'} <= motions, f"motions on the host: {report['puppet_motions']}"


def boulderseqdiag(copies, report, negative=False):
    """P1 measurement, the orc boulder's states (Tilt, Hold, Lift: AniState 12, 13, 14; PS_Interact_TiltOrcBoulder ->
    HoldOrcBoulder -> LiftOrcBoulder): G3_WHO (default client; host = single-player control) uses the Faring boulder with
    strength G3_STR (default 300). For 25 s every 0.5 s the user's HERO line and, for the client, its puppet on the host
    (PUPPETSTATE). Asserts nothing."""
    host, client = join_both(copies)
    clear_raid(host, report)
    user = host if os.environ.get('G3_WHO') == 'host' else client
    other = client if user is host else host
    boulder = 'G3_Object_Interact_Animated_OrcBoulder_Faring'
    teleport_to(host.bot, boulder)
    x, y, z = hero_pos(host.bot)
    start = host.bot.send('usables all1500', 20)
    host.bot.line('USABLE end', 15, 'usables', start)
    holder = re.search(r' user=(\S+)', ''.join(l for l in host.bot.log()[start:].splitlines() if 'OrcBoulder' in l))
    if holder and holder[1] not in ('-', 'G3T_Player2', 'PC_Hero'):
        report['user_moved'] = report_line(host.bot, f'farmove {x + 2500:.0f}_{y + 50:.0f}_{z:.0f}_{holder[1]}', ('FARMOVE ', 'REFUSED farmove'))
    if user is client:
        move_hero(host.bot, (x + 800, y + 30, z))
        teleport_to(client.bot, boulder)
    time.sleep(4)
    report['strength'] = report_line(user.bot, 'setattr STR_' + os.environ.get('G3_STR', '300'), ('SETATTR ', 'REFUSED setattr'))
    report['use'] = report_line(user.bot, f'use {boulder}', ('USE ', 'REFUSED use'))
    samples = []
    for _ in range(50):
        row = {'user': re.sub(r'hp=\S+ max=\S+ dead=\S+ down=\S+ ', '', herostate(user.bot))[5:120]}
        if user is client:
            row['puppet'] = report_line(host.bot, 'puppetstate', ('PUPPETSTATE ', 'REFUSED puppetstate'))[12:140]
        samples.append(row)
        time.sleep(0.5)
    report['samples'] = samples
    report['status_note'] = 'diagnostic'


def boulderdiag(copies, report, negative=False):
    """P1 measurement, orc boulders (Templates.pak: G3_Object_Interact_Animated_OrcBoulder_Faring, _Light, _Medium, _Heavy,
    _01; the world scans near Faring found none): the host's hero teleports to each by name (Entity::GetEntity finds it
    anywhere when its name is the template's) and lists the boulders and usable objects around it. Asserts nothing."""
    host, client = join_both(copies)
    clear_raid(host, report)
    for kind in os.environ.get('G3_BOULDERS', 'Faring,Light,Medium,Heavy,01').split(','):
        name = f'G3_Object_Interact_Animated_OrcBoulder_{kind}'
        try:
            report[f'{kind}_tp'] = teleport_to(host.bot, name)
        except AssertionError as error:
            report[f'{kind}_tp'] = str(error)[:120]
            continue
        if 'ok=1' not in report[f'{kind}_tp']:
            continue
        time.sleep(4)
        start = host.bot.send('findents G3_Object_Interact_Animated_OrcBoulder', 20)
        host.bot.line('FINDENTS end', 15, 'findents', start)
        report[f'{kind}_found'] = [l for l in host.bot.log()[start:].splitlines() if l.startswith('FINDENT ')][:10]
        start = host.bot.send('usables', 20)
        host.bot.line('USABLE end', 15, 'usables', start)
        report[f'{kind}_usables'] = [l for l in host.bot.log()[start:].splitlines() if l.startswith('USABLE ') and 'Boulder' in l][:10]


def homescandiag(copies, report, negative=False):
    """P7 measurement, where is someone's home a place of its own (a house) rather than the open Nav_Zone, as Reddock's
    beds all are (sneakhomediag 25.10.)? The host's hero visits a member of each town (G3_HOMESCAN: name list) and lists
    the NPCs within 80 m whose sleeping point lies in another zone (QA homes 8000_all). Records per town the home zones
    with owners, whether each owner is at home now, and the zone position; asserts nothing."""
    host, client = join_both(copies)
    clear_raid(host, report)
    anchors = os.environ.get('G3_HOMESCAN', 'Mason,Lares,Lars,Konrad,Wenzel,Rhobar,Sinikar,Bernado,Orknarok,Rakus,Treslott,Onatas,Lukar,Tarok,Zuben,Tjalf,Alina').split(',')
    for anchor in anchors:
        try:
            report[f'{anchor}_tp'] = teleport_to(host.bot, anchor)
        except AssertionError as error:
            report[f'{anchor}_tp'] = str(error)[:120]
            continue
        time.sleep(6)
        start = host.bot.send('homes 8000_all', 20)
        host.bot.line('HOMES end', 15, 'homes', start)
        rows = [dict(re.findall(r'(\w+)=(\S+)', l)) for l in host.bot.log()[start:].splitlines() if l.startswith('HOME npc=')]
        zones = {}
        for row in rows:
            if row.get('home') == 'Nav_Zone':
                continue
            zone = zones.setdefault(row['home'], {'at': row.get('zone_at'), 'owners': []})
            zone['owners'].append(f"{row['npc']}{'@home' if row.get('at_home') == '1' else '>' + row.get('now', '-')}")
        report[anchor] = {'npcs': len(rows), 'nav_zone': sum(1 for r in rows if r.get('home') == 'Nav_Zone'), 'homes': zones}


def probe_rows(bot):
    """QA p7probe as {name: {calls, as_puppet, puppet_results, hero_results, ...}} (counts since the last p7probeon)."""
    start = bot.send('p7probe', 20)
    bot.line('P7PROBE end', 15, 'p7probe', start)
    rows = {}
    for l in bot.log()[start:].splitlines():
        if l.startswith('P7PROBE name='):
            r = dict(re.findall(r'(\w+)=(\S+)', l))
            rows[r['name']] = r
    return rows


def probe_results(row, whose):
    """The results a probe saw for a puppet ('puppet') or the local hero ('hero') as a set of ints."""
    text = (row or {}).get(whose + '_results', '-')
    return {int(v) for v in text.split(',') if v.lstrip('-').isdigit()}


def probe_log(copy, name, mark=0):
    return [l[l.index('p7probe:') + 9:] for l in copy.mod_log()[mark:].splitlines() if f'p7probe: {name} ' in l]


def stageZoneAttitude(copies, report, negative=False):
    """P7, a guarded place's attitude to the other player as in single-player (GetZoneAttitudeToPlayer). The game asks it
    with the ZONE as self (AssessPlayerZone 0x2fca0, AssessPlayerEnterZone 0x30000; re/out/p7-half-20261026-b.c): the
    zone's AI owner and security level and the fame of Entity::GetPlayer, 3 (a threat) in a place of security 3 for a player
    of fame below 75; only then does a guard there react (ZS_ReactToZone, attack reason 11). For another player the host's
    guards ask it with that player's puppet as the game's player (guards.cpp Acting). (An earlier QA zoneatt asked it with
    an NPC as self and got 1 everywhere, zonescandiag 26.10.) The client (G3_WHO=host: the host, single-player reference,
    the client stays at the start) walks into Muntasir's place in Ishtar (G3_PLACE), the host 30 m off. Asserts: the
    intruder's own game answers 3 for its hero there and the host's game 3 for the puppet (QA zoneatt -: zone_zatt,
    puppet_zone_zatt); the host's game itself asked it for the intruder and got 3 (QA p7probe: GetZoneAttitudeToPlayer for
    the puppet as the game's player; reference: for the hero); within 30 s a guard reacts to the intruder (ZS_ReactToZone or
    attack reason 11). Negative control: trespassoff on the host."""
    who = os.environ.get('G3_WHO', 'client')
    place = os.environ.get('G3_PLACE', 'Muntasir')
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'trespassoff', 'TRESPASSOFF ')
    intruder, expected = (host, 'hero') if who == 'host' else (client, 'puppet2')
    teleport_to(host.bot, place)
    x, y, z = hero_pos(host.bot)
    outside = (x + 3000, y + 30, z)
    move_hero(host.bot, outside)
    time.sleep(4)
    if who == 'host':
        teleport_to(host.bot, place)
    else:
        teleport_to(client.bot, place)
    time.sleep(2)
    # The host's puppet arrives a moment after the client's jump (stageZoneAttitude 26.10.: 2 s after it still stood at the
    # start): ask until the host sees it in a zone of the place's security, at most 10 s.
    for _ in range(10):
        report['zone'] = {c.tag: report_line(c.bot, 'zoneatt -', ('ZONEATT ', 'REFUSED zoneatt'))[13:] for c in (host, client)}
        mine = re.search(r' hero_zone=\S+/(-?\d+)/', ' ' + report['zone'][intruder.tag])
        theirs = re.search(r' puppet_zone=\S+/(-?\d+)/', ' ' + report['zone']['host'])
        if who == 'host' or (mine and theirs and mine[1] == theirs[1]):
            break
        time.sleep(1)
    # The probe counts from here: the QA question above asked the zone attitude too.
    report['probe_on'] = report_line(host.bot, 'p7probeon', 'P7PROBEON ')
    mark = len(host.mod_log())
    own = re.search(r' zone_zatt=(-?\d+)', ' ' + report['zone'][intruder.tag])
    seen = re.search(r' puppet_zone_zatt=(-?\d+)', ' ' + report['zone']['host'])

    def reacted():
        rows = npcs(host.bot)
        at = hero_pos(intruder.bot)
        names = sorted({v['name'] for v in rows.values() if not v['dead'] and not v['name'].startswith('G3T_') and distance(v['pos'], at) < 2500})
        state = {n: crime_of(host.bot, n) for n in names}
        report['last'] = {n: f"{c.get('task')}>{c.get('target')}:{c.get('reason')}" for n, c in state.items() if c}
        return [n for n, c in state.items() if c.get('target') == expected and
                (c.get('task', '').startswith('ZS_ReactToZone') or (c.get('task') == 'ZS_Attack' and c.get('reason') == '11'))]
    try:
        report['reacted'] = wait_for(reacted, 30, f'a guard at {place} reacts to the intruder ({expected})')
    finally:
        report['state'] = report.pop('last', None)
        report['guards'] = report_line(host.bot, 'guards', 'GUARDS ')
        rows = probe_rows(host.bot)
        report['probe'] = {k: v for k, v in rows.items() if k in ('GetZoneAttitudeToPlayer', 'AssessPlayerZone', 'GetPoliticalAttitude')}
        report['probe_log'] = probe_log(host, 'GetZoneAttitudeToPlayer', mark)[-6:]
    row = rows.get('GetZoneAttitudeToPlayer', {})
    if who == 'host':
        assert 3 in probe_results(row, 'hero'), f"the host's game never judged its hero's place a threat: {row}"
        assert own and own[1] == '3', f"zone attitude for the host's hero in the place: {report['zone']}"
    else:
        assert int(row.get('as_puppet', 0)) > 0 and 3 in probe_results(row, 'puppet'), f"the host's game never judged the client's place a threat: {row}"
        assert own and own[1] == '3', f"the client's own game does not take its place for a threat: {report['zone']}"
        assert seen and seen[1] == '3', f"the host's game does not take the puppet's place for a threat: {report['zone']}"


def stageTheftSeen(copies, report, negative=False):
    """P7, an onlooker minds the other player taking its things as in single-player (AssessTakeItem ->
    IsBotheredByPlayerTheftOf -> AssessTarget, attack reason Theft; re/out/social-crime-main-c.c, p7-half-20261026-b.c).
    The game sends the take perception (SendPerceptionTakeItem) on the thief's machine to the NPCs there; on the client those
    are mirrors, its own game judges its hero, and the witness bridge (witness.cpp) lets the host's NPC react to the puppet.
    At the first place of G3_THEFT_AT (default Treslott,Orknarok,Lukar,Lars,Konrad: Nemora, Mora Sul, Ben Erai, Silden,
    Trelis) where an NPC minds an item within 8 m of it that lies ahead of it (QA bothered, p7attscandiag 26.10.), the thief
    (the client; G3_WHO=host: the host, single-player reference, the client stays at the start) stands next to the item on
    the onlooker's side, unarmed, the host 20 m off, and takes it the game's way (itempick, PS_Normal_Take).
    With QA p7probeon on both machines. Asserts: on the thief's machine AssessTakeItem judged the take and
    IsBotheredByPlayerTheftOf answered 1 for its hero; within 15 s the onlooker on the host targets the thief (attack
    reason 4) or reacts to it. On the client the NPCs are mirrors, pacified (AIMode Abandoned), which the game's perception
    passes by (0x31c80): witness.cpp lets the mirrors near the local hero perceive its take and chest use (26.10.; before,
    the client's take went unseen while the host's was punished). Negative control: mirrorperceiveoff on both."""
    who = os.environ.get('G3_WHO', 'client')
    # G3_THEFT_KIND=object: a chest the onlooker minds is opened instead (QA use: PS_Interact_Chest sends
    # SendPerceptionUseInteractObject, AssessUseInteractObject asks IsBotheredByPlayerTheftOf, attack reason 5).
    kind = os.environ.get('G3_THEFT_KIND', 'item')
    assess, reason = ('AssessTakeItem', '4') if kind == 'item' else ('AssessUseInteractObject', '5')
    anchors = os.environ.get('G3_THEFT_AT', 'Treslott,Orknarok,Lukar,Lars,Konrad,Brenton,Hamlar,Bernado,Alina').split(',')
    reach = float(os.environ.get('G3_THEFT_REACH', '450'))  # Sancho 35 deg off at 6.8 m did not look (26.10.)
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        for c in (host, client):
            assert 'ok=1' in report_line(c.bot, 'mirrorperceiveoff', 'MIRRORPERCEIVEOFF ')
    thief, expected = (host, 'hero') if who == 'host' else (client, 'puppet2')
    # The onlooker must have the thief in its field of view (AssessTakeItem, 0x8f70: IsInFOV, then the line of sight): the
    # pair of an item and an NPC that minds it, within 8 m, with the item most nearly ahead of the NPC (NEARX yaw: the
    # compass angle of its forward vector). Treslott studying 4 m off never looked (stageTheftSeen 26.10., reference), and
    # once nobody there minded anything: the next place of G3_THEFT_AT then.
    pairs = []
    for anchor in anchors:
        teleport_to(host.bot, anchor)
        time.sleep(5)
        rows = []
        for _ in range(2):
            start = host.bot.send('bothered 3000', 20)
            host.bot.line('BOTHERED end', 30, 'bothered', start)
            rows = [dict(re.findall(r'(\w+)=(\S+)', l)) for l in host.bot.log()[start:].splitlines() if l.startswith('BOTHERED guid=')]
            rows = [r for r in rows if r.get('kind', 'item') == kind and (kind == 'item' or 'Chest' in r['name'])]
            if rows:
                break
            time.sleep(3)
        start = host.bot.send('nearnpcs', 20)
        host.bot.line('NEARX end', 10, 'nearnpcs', start)
        yaw = {m[1]: int(m[2]) for m in re.finditer(r'NEARX guid=\S+ name=(\S+) .* yaw=(-?\d+)', host.bot.log()[start:])}
        places = {v['name']: v['pos'] for v in npcs(host.bot).values() if not v['dead']}
        pairs = []
        for r in rows:
            spot = tuple(float(v) for v in r['pos'].split('_'))
            for n in r['npcs'].split(','):
                if n not in places or n not in yaw or distance(places[n], spot) > reach:
                    continue
                ahead = math.degrees(math.atan2(spot[0] - places[n][0], spot[2] - places[n][2]))
                off = abs((ahead - yaw[n] + 180) % 360 - 180)
                pairs.append((off, distance(places[n], spot), n, r))
        # Named NPCs first: QA crime finds an NPC by name, and numbered names (AssWarrior_10) repeat across the world.
        pairs.sort(key=lambda t: (t[0] > 40, bool(re.search(r'_\d+$', t[2])), t[1] if t[0] <= 40 else t[0]))
        report[f'candidates_{anchor}'] = [f"{n}->{r['name']}: {off:.0f} deg {d:.0f} cm" for off, d, n, r in pairs[:6]] or len(rows)
        if pairs and (pairs[0][0] <= 40 or (os.environ.get('G3_THEFT_TALK', '1') == '1' and not re.search(r'_\d+$', pairs[0][2]))):
            break
    report['anchor'] = anchor
    assert pairs, f'nobody minds an item within 8 m at {anchors}'
    _, _, at, item = pairs[0]
    report['onlooker'] = at
    me = {'pos': places[at]}
    report['item'] = item
    spot = tuple(float(v) for v in item['pos'].split('_'))
    if who == 'client':
        x, y, z = hero_pos(host.bot)
        move_hero(host.bot, (x + 2000, y + 30, z))
        teleport_to(client.bot, anchor)
        time.sleep(3)
    # The thief 70 cm from the item on the side of the onlooker, unarmed.
    d = [a - b for a, b in zip(me['pos'], spot)]
    length = math.hypot(d[0], d[2]) or 1.0
    report['thief_moved'] = move_hero(thief.bot, (spot[0] + 70 * d[0] / length, spot[1] + 40, spot[2] + 70 * d[2] / length))
    report_line(thief.bot, 'unarm', ('UNARM', 'REFUSED unarm'))
    time.sleep(3)
    if os.environ.get('G3_THEFT_TALK', '1') == '1':
        # The onlooker must look at the thief (none did in three references 26.10., 35 to 177 deg off): a word with it first
        # turns it to the thief; the take follows as soon as the dialog has ended.
        try:
            talk_to(thief, at, report, 'talk')
        except AssertionError as error:
            report['talk_error'] = str(error)[:160]
        report['endtalk'] = report_line(thief.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))
        try:
            wait_for(lambda: talkstate(thief.bot).get('running') == '0' and ['ok'], 15, 'the dialog ends')
        except AssertionError:
            pass
        report['thief_back'] = move_hero(thief.bot, (spot[0] + 70 * d[0] / length, spot[1] + 40, spot[2] + 70 * d[2] / length))
    for c in (host, client):
        report[c.tag + '_probe_on'] = report_line(c.bot, 'p7probeon', 'P7PROBEON ')
    marks = {c.tag: len(c.mod_log()) for c in (host, client)}
    report['before'] = crime_of(host.bot, at)
    if kind == 'item':
        report['pick'] = report_line(thief.bot, 'itempick ' + item['guid'], ('ITEMPICK ', 'REFUSED itempick'))
    else:
        report['pick'] = report_line(thief.bot, 'use ' + item['guid'], ('USE ', 'REFUSED use'))
    timeline = []

    def reacted():
        state = crime_of(host.bot, at)
        timeline.append(f"{state.get('task')}>{state.get('target')}:{state.get('reason')}:{state.get('last_crime')}")
        return state.get('target') == expected and (state.get('reason') == reason or state.get('task', '').startswith(('ZS_Attack', 'ZS_Threaten', 'ZS_ReactTo'))) and ['yes']
    try:
        report['reacted'] = wait_for(reacted, 15, f'{at} reacts to the thief ({expected})')
    finally:
        report['timeline'] = timeline[-10:]
        report['probe'] = {}
        for c in (host, client):
            rows = probe_rows(c.bot)
            report['probe'][c.tag] = {k: f"calls={v.get('calls')} as_puppet={v.get('as_puppet')} puppet_results={v.get('puppet_results')} hero_results={v.get('hero_results')}"
                                      for k, v in rows.items() if k in (assess, 'IsBotheredByPlayerTheftOf', 'MemorizeEnclaveCrime', 'SendPerceptionUseInteractObject')}
            report[c.tag + '_probe_log'] = [l for name in (assess, 'IsBotheredByPlayerTheftOf', 'MemorizeEnclaveCrime')
                                            for l in probe_log(c, name, marks[c.tag])][-8:]
            report[c.tag + '_witness'] = report_line(c.bot, 'witnesses', 'WITNESS ')
            if c is thief:
                thief_rows = rows
        report['after'] = {c.tag: crime_of(c.bot, at) for c in (host, client)}
        report['took'] = report_line(thief.bot, 'itemtook', 'ITEMTOOK ')
        # What the town knows (MemorizeEnclaveCrime on the thief's machine; shared, enclaves.cpp holds a theft back 3 s).
        known = {}
        for _ in range(10):
            known = {c.tag: crime_of(c.bot, at).get('known') for c in (host, client)}
            if known['host'] == known['client'] and known['host'] not in (None, '0'):
                break
            time.sleep(1)
        report['known'] = known
        if kind != 'item':
            report_line(thief.bot, 'unuse', ('UNUSE ', 'REFUSED unuse'))
    bothered = thief_rows.get('IsBotheredByPlayerTheftOf', {})
    taken = thief_rows.get(assess, {})
    assert int(taken.get('calls', 0)) > 0 and 1 in probe_results(taken, 'hero'), f"the thief's game did not judge the take: {taken}"
    assert 1 in probe_results(bothered, 'hero'), f"nobody minded the take in the thief's game: {bothered}"
    assert known['host'] == known['client'] and known['host'] not in (None, '0'), f'the town does not know the theft on both machines: {known}'


def stageEnterHome(copies, report, negative=False):
    """P7, walking into a home whose owner is out: the owner does not come after the intruder, as in single-player
    (AssessPlayerEnterZone 0x30000 -> ZS_GotoPlayerZone; re/out/p7-enterzone-b.c). SendPerceptionEnterZone has no caller in
    the game (the single-player reference, G3_WHO=host, 26.10.: 0 calls). Until 26.10. the host sent it for the puppets
    itself (guards.cpp WatchZones), and the owner went after the client only; owner 26.10.: taken out. The owner
    goes only where the home zone's attitude to the player is not friendly (GetZoneAttitudeToPlayer with the zone as self:
    security 1 gives 2; Tjalf_Room has security 0, hence no reaction in stageForeignHome 25.10.) and only if it notices the
    intruder (0x8bd0). G3_HOME_OWNER (default Brenton, Reddock: his bed 2 m from him in a zone of security 1, p7attscandiag
    26.10.). The intruder (the client; G3_WHO=host: the host, single-player reference, the client stays at the start) starts
    3 m from the owner away from the bed and runs to the bed (QA runto), the host 20 m off. With QA p7probeon on both.
    Asserts: the home zone's attitude is not 1 and the owner is out; for 25 s the owner never runs ZS_GotoPlayerZone after
    the intruder, and the host's game never asked AssessPlayerEnterZone (p7probe). Negative control: enterzoneon on the host
    (sent for puppets as before)."""
    who = os.environ.get('G3_WHO', 'client')
    owner = os.environ.get('G3_HOME_OWNER', 'Brenton')
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'enterzoneon', 'ENTERZONEON ')
    intruder, expected = (host, 'hero') if who == 'host' else (client, 'puppet2')
    teleport_to(host.bot, owner)
    time.sleep(4)
    start = host.bot.send('homes 3000_all', 20)
    host.bot.line('HOMES end', 15, 'homes', start)
    rows = [dict(re.findall(r'(\w+)=(\S+)', l)) for l in host.bot.log()[start:].splitlines() if l.startswith(f'HOME npc={owner} ')]
    home = rows[0] if rows else {}
    report['home'] = home
    assert home and home.get('zatt') not in (None, '1'), f'{owner} has no home the game guards: {home}'
    assert home.get('at_home') == '0', f'{owner} is at home: {home}'
    bed = tuple(map(float, home['bed'].split(',')))
    if who == 'client':
        x, y, z = hero_pos(host.bot)
        move_hero(host.bot, (x + 2000, y + 30, z))
        teleport_to(client.bot, owner)
        time.sleep(4)
    start_at = next(v['pos'] for v in npcs(host.bot).values() if v['name'] == owner)
    away = [a - b for a, b in zip(start_at, bed)]
    length = math.hypot(away[0], away[2]) or 1.0
    report['intruder_moved'] = move_hero(intruder.bot, (start_at[0] + 300 * away[0] / length, start_at[1] + 30, start_at[2] + 300 * away[2] / length))
    time.sleep(4)
    for c in (host, client):
        report[c.tag + '_probe_on'] = report_line(c.bot, 'p7probeon', 'P7PROBEON ')
    marks = {c.tag: len(c.mod_log()) for c in (host, client)}
    report['aicount'] = report_line(host.bot, 'aicount ZS_GotoPlayerZone', 'AICOUNT end')
    report['zone_before'] = hero_zone(intruder.bot)
    report['run'] = report_line(intruder.bot, f'runto {bed[0]:.0f}_{bed[1]:.0f}_{bed[2]:.0f}', ('RUNTO ', 'REFUSED runto'))
    timeline = []

    def came():
        state = crime_of(host.bot, owner)
        timeline.append(f"{state.get('task')}>{state.get('target')}|{hero_zone(intruder.bot)}")
        return state.get('target') == expected and state.get('task', '').startswith('ZS_GotoPlayerZone') and ['yes']
    try:
        report['came'] = None
        for _ in range(25):
            time.sleep(1)
            if came():
                report['came'] = timeline[-1]
                break
    finally:
        report['timeline'] = timeline[-14:]
        report['guards'] = report_line(host.bot, 'guards', 'GUARDS ')
        start = host.bot.send('aicount', 20)
        host.bot.line('AICOUNT end', 15, 'aicount', start)
        report['aicount_after'] = [l for l in host.bot.log()[start:].splitlines() if l.startswith('AICOUNT name=')]
        rows = probe_rows(host.bot)
        report['probe'] = {k: v for k, v in rows.items() if k in ('AssessPlayerEnterZone', 'GetZoneAttitudeToPlayer')}
        report['probe_log'] = probe_log(host, 'AssessPlayerEnterZone', marks['host'])[-6:]
        report['log'] = [l for l in host.mod_log()[marks['host']:].splitlines() if 'guards:' in l][-6:]
    entered = rows.get('AssessPlayerEnterZone', {})
    assert not report['came'], f"{owner} went after the intruder, which the game never does for its own player: {report['came']}"
    assert not probe_results(entered, 'hero' if who == 'host' else 'puppet'), f"the host's game was asked AssessPlayerEnterZone: {entered}"


def stageCrimeMemory(copies, report, negative=False):
    """P7, a town remembers the other player's theft as in single-player (MemorizeEnclaveCrime 0x60670: called from
    MemorizePlayerCrime when a witness judges Entity::GetPlayer; a guard or boss makes its town know a theft, KnownPlayerCrime,
    and the town's members turn angry: GetAttitudeToPlayer 3). For the client the witness's judgement runs on the client's
    own machine against its hero (the mirror there, QA witness <npc>_4: the last step of AssessTakeItem), so the town learns
    it there; reputation is shared (enclaves.cpp), so the host's town must know it too. The client (G3_WHO=host: the host,
    single-player reference) steals before G3_LEAK_NPC (Norris, Reddock), the other hero 15 m off. With QA p7probeon on both.
    Asserts: on the thief's machine MemorizeEnclaveCrime ran for its hero and answered 1; afterwards the town knows the
    theft on both machines (QA crime: known) and the NPC's attitude to the player (to_player) is the same on both and above
    where it started. Negative control: enclavesyncoff on both."""
    who = os.environ.get('G3_WHO', 'client')
    npc = os.environ.get('G3_LEAK_NPC', 'Norris')
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        for c in (host, client):
            assert 'ok=1' in report_line(c.bot, 'enclavesyncoff', 'ENCLAVESYNCOFF ')
    thief, other = (host, client) if who == 'host' else (client, host)
    for c in (host, client):  # the host first: it simulates the town
        teleport_to(c.bot, npc)
        time.sleep(2)
    x, y, z = hero_pos(other.bot)
    move_hero(other.bot, (x + 1500, y + 30, z))
    time.sleep(2)

    def view():
        out = {}
        for c in (host, client):
            line = report_line(c.bot, 'attitude ' + npc, ('ATTITUDE ', 'REFUSED attitude'))
            out[c.tag] = {k: int(v) for k, v in re.findall(r' (to_player|focus)=(-?\d+)', line)}
            out[c.tag]['known'] = int(crime_of(c.bot, npc).get('known', -1))
        return out

    before = view()
    report['before'] = before
    for c in (host, client):
        report[c.tag + '_probe_on'] = report_line(c.bot, 'p7probeon', 'P7PROBEON ')
    mark = len(thief.mod_log())
    report['theft'] = report_line(thief.bot, f'witness {npc}_4', ('WITNESSSAW ', 'REFUSED witness'))
    x, y, z = hero_pos(thief.bot)
    move_hero(thief.bot, (x + 6000, y + 30, z))
    report['cancel'] = report_line(host.bot, f'cancelattack {npc}', ('CANCELATTACK ', 'REFUSED cancelattack'))
    time.sleep(6)
    after = view()
    report['after'] = after
    rows = probe_rows(thief.bot)
    memo = rows.get('MemorizeEnclaveCrime', {})
    report['probe'] = memo
    report['probe_log'] = probe_log(thief, 'MemorizeEnclaveCrime', mark)[-4:]
    report['enclavesync'] = {c.tag: report_line(c.bot, 'enclavesync', 'ENCLAVESYNC ')[:160] for c in (host, client)}
    assert 1 in probe_results(memo, 'hero'), f"the thief's game did not make the town remember the theft: {memo}"
    assert after[thief.tag]['known'] > before[thief.tag]['known'], f"the thief's town does not know the theft: {before} -> {after}"
    assert after['host']['known'] == after['client']['known'], f'the town knows the theft on one machine only: {after}'
    assert after['host']['to_player'] == after['client']['to_player'] > before[thief.tag]['to_player'], f'{npc} judges the thief otherwise: {before} -> {after}'


def stagePoliticalChapter(copies, report, negative=False):
    """P7, the political attitude in the rules the host runs for another player as in single-player (GetPoliticalAttitude
    0xf620). The hero has no alignment (attchain 26.10.: hero_align 0, GetPoliticalAttitude to a hero 0 everywhere), so what
    decides is an NPC's attitude to a town or a zone's owner, and the only part of the player in it is the story chapter of
    Entity::GetPlayer: in chapter 2 the Hashishin (5) and the orcs (1) are enemies. The host asks the zone and weapon rules
    for another player with that player's puppet as the game's player (guards.cpp Acting), and a puppet had chapter 0
    (the host's hero 1). Now it carries the shared chapter (guards.cpp KeepChapter). Chapter 2 is set (QA setchapter, shared
    by story.cpp); in Trelis (an orc town with Hashishin, G3_POLITICAL_AT Konrad) the client stands 10 m from the host.
    QA attchain on the host for each NPC within 30 m: its political attitude to the owner of its zone and to its town, with the
    hero and with the puppet as the game's player; then the client walks 30 m through the town (the host's guards judge it,
    p7probe GetPoliticalAttitude with the puppet as the game's player). Asserts: some NPC there is hostile (4) to its zone's
    owner or its town in chapter 2 as the host's game judges for its hero, and with the puppet as the player every answer
    is the same. Negative control: chaptercopyoff on the host."""
    at = os.environ.get('G3_POLITICAL_AT', 'Konrad')
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'chaptercopyoff', 'CHAPTERCOPYOFF ')
    for c in (host, client):
        report[c.tag + '_chapter'] = report_line(c.bot, 'setchapter 2', ('SETCHAPTER ', 'REFUSED setchapter'))
    teleport_to(host.bot, at)
    time.sleep(3)
    teleport_to(client.bot, at)
    x, y, z = hero_pos(client.bot)
    move_hero(client.bot, (x + 1000, y + 30, z))
    time.sleep(4)
    report['chapter'] = {c.tag: report_line(c.bot, 'chapter', 'CHAPTER ') for c in (host, client)}
    for c in (host, client):
        report[c.tag + '_probe_on'] = report_line(c.bot, 'p7probeon', 'P7PROBEON ')
    mark = len(host.mod_log())
    here = hero_pos(host.bot)
    names = sorted({v['name'] for v in npcs(host.bot).values() if not v['dead'] and not v['name'].startswith('G3T_') and distance(v['pos'], here) < 3000})
    rows = {}
    for n in names[:24]:
        start = host.bot.send('attchain ' + n, 20)
        host.bot.line('ATTCHAIN2 ', 10, 'attchain', start)
        line = next((l for l in host.bot.log()[start:].splitlines() if l.startswith('ATTCHAIN2 ')), '')
        rows[n] = dict(re.findall(r'(\w+)=(\S+)', line))
    report['political'] = {n: f"{r.get('zone_owner')}:{r.get('political_owner')}/{r.get('political_owner_acting')} "
                              f"{r.get('town')}:{r.get('political_town')}/{r.get('political_town_acting')}" for n, r in rows.items()}
    # The client walks through the town: the host's guards judge it in their own rules.
    x, y, z = hero_pos(client.bot)
    report['walk'] = report_line(client.bot, f'runto {x + 3000:.0f}_{y:.0f}_{z:.0f}', ('RUNTO ', 'REFUSED runto'))
    time.sleep(12)
    report['probe'] = {k: v for k, v in probe_rows(host.bot).items() if k in ('GetPoliticalAttitude', 'AssessPlayerZone')}
    report['probe_log'] = [l for l in probe_log(host, 'GetPoliticalAttitude', mark) if 'player=puppet' in l][-10:]
    report['guards'] = report_line(host.bot, 'guards', 'GUARDS ')
    hostile = [n for n, r in rows.items() if '4' in (r.get('political_owner'), r.get('political_town'))]
    differ = [n for n, r in rows.items() if (r.get('political_owner'), r.get('political_town')) != (r.get('political_owner_acting'), r.get('political_town_acting'))]
    report['hostile'], report['differ'] = hostile, differ
    assert hostile, f'nobody near {at} is hostile to its zone owner or town in chapter 2: {report["political"]}'
    assert not differ, f'with the puppet as the player, the political attitude differs for {differ}'


def stageSpeciesAttitude(copies, report, negative=False):
    """P7, an animal judges the other player by its kind as in single-player (GetSpeciesAttitude 0xfbc0: for an animal
    the species of both, e.g. a ripper is angry (3) at humans; it is the last part of GetAttitude, after the player's own
    part, which an animal leaves at 0). The host's NPCs ask GetAttitude about a puppet as about its hero (ai.cpp). At Kent
    (G3_SPOT; rippers and deer the host simulates) both heroes, the client 8 m off; QA attchain on the host for every animal
    within 40 m: species attitude to the hero, to the puppet, and GetAttitude to the puppet with and without the redirect;
    on the client the same for its hero. The client then stands 2 m from the nearest animal that is hostile to the hero
    (3 to 5) and must be its target within 20 s (NEARX target_puppet). With QA p7probeon on the host (GetSpeciesAttitude
    for the puppet). Asserts: the animal's species attitude is the same for the puppet as for the host's hero and in the
    client's own game for its hero; the animal targets the puppet. Negative control: attitudeoff on the host."""
    spot = os.environ.get('G3_SPOT', 'Kent')
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report['attitudeoff'] = report_line(host.bot, 'attitudeoff', 'ATTITUDEOFF')
    teleport_to(host.bot, spot)
    time.sleep(3)
    teleport_to(client.bot, spot)
    x, y, z = hero_pos(client.bot)
    move_hero(client.bot, (x + 800, y + 30, z))
    time.sleep(4)
    report['probe_on'] = report_line(host.bot, 'p7probeon', 'P7PROBEON ')
    here = hero_pos(host.bot)
    rows = {}
    for g, v in npcs(host.bot).items():
        if v['dead'] or v['name'].startswith('G3T_') or distance(v['pos'], here) > 4000 or v['name'] in rows:
            continue
        line = report_line(host.bot, 'attchain ' + v['name'], ('ATTCHAIN ', 'REFUSED attchain'))
        r = dict(re.findall(r'(\w+)=(\S+)', line))
        if r.get('npc_species', '0') != '0':
            r['pos'] = v['pos']
            r['client'] = dict(re.findall(r'(\w+)=(\S+)', report_line(client.bot, 'attchain ' + v['name'], ('ATTCHAIN ', 'REFUSED attchain'))))
            rows[v['name']] = r
    report['animals'] = {n: f"sp={r.get('npc_species')} species_hero={r.get('species_hero')} species_puppet={r.get('species_puppet')} "
                            f"att_hero={r.get('attitude_hero')} att_puppet={r.get('attitude_puppet')} plain={r.get('attitude_puppet_plain')} "
                            f"client_species_hero={r['client'].get('species_hero')}" for n, r in rows.items()}
    hostile = [n for n, r in rows.items() if r.get('species_hero') in ('3', '4', '5')]
    assert hostile, f'no animal near {spot} is hostile to the hero: {report["animals"]}'
    name = min(hostile, key=lambda n: distance(rows[n]['pos'], hero_pos(client.bot)))
    report['animal'] = name
    p = rows[name]['pos']
    report['client_moved'] = move_hero(client.bot, (p[0] + 200, p[1] + 50, p[2]))
    seen = []

    def targeted():
        near = near_rows(host.bot)
        hit = [r for r in near.values() if r['name'] == name and r['target_puppet'] and not r['dead']]
        seen.append(sorted({f"{r['task']}>{r['target']}" for r in near.values() if r['name'] == name}))
        return hit and ['yes']
    try:
        report['targeted'] = wait_for(targeted, 20, f'{name} targets the client')
    finally:
        report['timeline'] = seen[-6:]
        report['probe'] = {k: v for k, v in probe_rows(host.bot).items() if k in ('GetSpeciesAttitude',)}
    differ = [n for n, r in rows.items() if not (r.get('species_hero') == r.get('species_puppet') == r['client'].get('species_hero'))]
    assert not differ, f'the species attitude differs between the players for {differ}: {report["animals"]}'


def p7attscandiag(copies, report, negative=False):
    """P7 attitudes (26.10., pair 1), where do the game's attitude rules show? At each NPC of G3_SPOTS both heroes, the
    client 20 m off: the homes around with their zone's AI owner, security and attitude to the player (QA homes: the game
    sends an owner after an intruder only where that is not 1, AssessPlayerEnterZone), the items someone minds being taken
    (QA bothered: IsBotheredByPlayerTheftOf), the zone attitude of where each hero stands (QA zoneatt -, the zone as self),
    and the parts of GetAttitude for the spot NPC and its two nearest (QA attchain). With QA p7probeon on both machines.
    Asserts nothing."""
    host, client = join_both(copies)
    clear_raid(host, report)
    for c in (host, client):
        report[c.tag + '_probe'] = report_line(c.bot, 'p7probeon', 'P7PROBEON ')
    spots = os.environ.get('G3_SPOTS', 'Muntasir,Brenton,Hamlar,Tjalf,Mason,Lares,Lars,Konrad,Wenzel,Rhobar,Sinikar,Bernado,'
                           'Orknarok,Rakus,Treslott,Onatas,Lukar,Tarok,Zuben,Alina').split(',')
    for spot in spots:
        try:
            for c in (host, client):
                teleport_to(c.bot, spot)
        except AssertionError as error:
            report[spot] = str(error)[:120]
            continue
        x, y, z = hero_pos(client.bot)
        move_hero(client.bot, (x + 2000, y + 30, z))
        time.sleep(5)
        out = {}
        start = host.bot.send('homes 8000_all', 20)
        host.bot.line('HOMES end', 15, 'homes', start)
        homes = {}
        for l in host.bot.log()[start:].splitlines():
            if not l.startswith('HOME npc='):
                continue
            r = dict(re.findall(r'(\w+)=(\S+)', l))
            if r.get('home') == 'Nav_Zone':
                continue
            h = homes.setdefault(r['home'], {'security': r.get('security'), 'owner': r.get('zone_owner'), 'zatt': r.get('zatt'), 'at': r.get('zone_at'), 'npcs': []})
            h['npcs'].append(r['npc'] + ('@home' if r.get('at_home') == '1' else ''))
        out['homes'] = homes
        start = host.bot.send('bothered 4000', 20)
        host.bot.line('BOTHERED end', 30, 'bothered', start)
        out['bothered'] = [l[9:220] for l in host.bot.log()[start:].splitlines() if l.startswith('BOTHERED guid=')][:12]
        out['zoneatt'] = {c.tag: report_line(c.bot, 'zoneatt -', ('ZONEATT ', 'REFUSED zoneatt'))[13:] for c in (host, client)}
        at = hero_pos(host.bot)
        near = sorted(((distance(v['pos'], at), v['name']) for v in npcs(host.bot).values() if not v['dead'] and not v['name'].startswith('G3T_')),
                      key=lambda t: t[0])
        names = [spot] + [n for d, n in near if n != spot][:2]
        out['chain'] = [report_line(host.bot, 'attchain ' + n, ('ATTCHAIN ', 'REFUSED attchain'))[14:] for n in names]
        report[spot] = out
    for c in (host, client):
        start = c.bot.send('p7probe', 20)
        c.bot.line('P7PROBE end', 15, 'p7probe', start)
        report[c.tag + '_probe_end'] = [l[8:] for l in c.bot.log()[start:].splitlines() if l.startswith('P7PROBE name=')]
    report['status_note'] = 'diagnostic only'


def summonmurderdiag(copies, report, negative=False):
    """P7 measurement, a murder by a player's summon in a town (Reddock): G3_WHO=client (default) or host summons
    (G3_SPELL, default It_Spell_SummonSkeleton) next to Brenton, the other hero 15 m off, and sends the creature after a
    town member (npcfight). Records the witness counters and log lines and the tasks and targets around on the host;
    asserts nothing (host = single-player reference)."""
    who = os.environ.get('G3_WHO', 'client')
    spell = os.environ.get('G3_SPELL', 'It_Spell_SummonSkeleton')
    host, client = join_both(copies)
    caster, other = (host, client) if who == 'host' else (client, host)
    clear_raid(host, report)
    town, good = murder_town(host, report, 'Reddock')
    victim = good[1]
    teleport_to(other.bot, victim)
    x, y, z = hero_pos(other.bot)
    move_hero(other.bot, (x + 1500, y, z))
    teleport_to(caster.bot, victim)
    time.sleep(3)
    own = creature_cast(caster, host, spell, report, who)
    report['creatures'] = own
    assert own, 'no summoned creature'
    guid = next(g for g, v in npcs(host.bot).items() if v['name'] == victim)
    pos = npcs(host.bot)[guid]['pos']
    near = sorted({v['name'] for g, v in npcs(host.bot).items() if g != guid and not v['dead'] and not v['name'].startswith('G3T_')
                   and distance(v['pos'], pos) < 1500})
    report_line(host.bot, f'npchp {victim}_30', 'NPCHP ')
    marks = {c.tag: len(c.mod_log()) for c in (host, client)}
    report['fight'] = report_line(caster.bot, f"npcfight {own[0]['name']}_{guid}", ('NPCFIGHT ', 'REFUSED npcfight'))
    try:
        wait_for(lambda: npcs(host.bot).get(guid, {}).get('dead') and ['dead'], 90, f'the summon kills {victim}')
    finally:
        time.sleep(6)
        report['after'] = {n: (lambda c: f"{c.get('task')}>{c.get('target')}:{c.get('reason')}")(crime_of(host.bot, n)) for n in near}
        report['witness'] = {c.tag: witnesses_of(c.bot)[0] for c in (host, client)}
        report['log'] = {c.tag: [l for l in c.mod_log()[marks[c.tag]:].splitlines() if 'witness' in l or 'killed' in l or 'murder' in l][-14:]
                         for c in (host, client)}


def questdumpdiag(copies, report, negative=False):
    """Plan P0 (24.10.), host only: every quest of the game with its journal folder, title, type, status, experience,
    prerequisites and delivery targets, copied into the run folder as quests.tsv (base of the chapter checklist)."""
    host = copies['host'].launch()
    enter_world(host)
    start = host.bot.send('questdump', 30)
    report['questdump'] = host.bot.line('QUESTDUMP ', 30, 'questdump', start)
    shutil.move(host.root/'quests.tsv', host.run/'quests.tsv')  # the install must hold only its own files (isolation check)


def combat_state(bot):
    start = bot.send('hp', 20)
    line = bot.line('HP hero=', 10, 'hp', start)
    return {k: int(v) for k, v in re.findall(r'(\w+)=(-?\d+)', line)}


def hit_npc(bot, guid):
    start = bot.send('hitnpc '+guid, 20)
    line = bot.line('HITNPC ', 10, 'hitnpc', start)
    values = {k: int(v) for k, v in re.findall(r'(before|after|dead|claim_sent)=(-?\d+)', line)}
    values['line'] = line
    return values


def stage3b(copies, report, negative=False):
    """Damage with host authority through the game's own OnDamage: a client hit on a mirrored NPC is undone
    locally and applied by the host (and the result mirrored back, death included); an NPC hit on a puppet
    on the host lands on that player's real hero."""
    host, client = join_both(copies)
    time.sleep(10)
    for copy in (host, client):
        state_ = combat_state(copy.bot)
        report[copy.tag+'_combat_start'] = state_
        assert state_['installed'] == 1, copy.tag+': OnDamage wrapper not installed'
        # Damage comes from the weapon in the hand, as in a real swing.
        start = copy.bot.send('arm '+WEAPON, 20)
        armed = copy.bot.line('ARM ', 10, 'arm', start)
        report[copy.tag+'_arm'] = armed
        assert 'right_hand=none' not in armed, copy.tag+': no weapon in hand: '+armed
    host_npcs, client_npcs = npcs(host.bot), npcs(client.bot)
    candidates = [g for g in set(host_npcs) & set(client_npcs)
                  if client_npcs[g]['mirrored'] and not host_npcs[g]['dead'] and host_npcs[g]['hp'] > 0]
    assert candidates, 'No living NPC mirrored on the client'
    # A sturdy raider, so that the first hit does not already kill and the kill phase takes several hits.
    # Near the client's hero, well inside the 40 m interest radius of both bot reports.
    client_at = remotes(host.bot)[2]['pos']
    near = [g for g in candidates if distance(client_npcs[g]['pos'], client_at) < 2000] or candidates
    orcs = [g for g in near if host_npcs[g]['name'].startswith('Orc_')] or near
    if negative:
        start = client.bot.send('combatoff', 20)
        assert 'ok=1' in client.bot.line('COMBATOFF ', 10, 'combatoff', start)
    # A: one client hit. Undone locally at once, applied on the host, mirrored back.
    # A raider in a fight may parry (task ZS_Parade*): no damage, so nothing to claim. Game behaviour, swing again; one that
    # parried four hits in a row goes on parrying for a while (Scout_01 twice on 24.10.): the next sturdiest then.
    for target in sorted(orcs, key=lambda g: (host_npcs[g]['hp'], g), reverse=True)[:3]:
        report['target'] = {'guid': target, 'name': host_npcs[target]['name'], 'hp': host_npcs[target]['hp']}
        host_before = npcs(host.bot).get(target, host_npcs[target])['hp']
        first = hit_npc(client.bot, target)
        for _ in range(3):
            if first['claim_sent'] or 'task=ZS_Parade' not in first['line']:
                break
            report.setdefault('parried', []).append(first['line'])
            time.sleep(2)
            first = hit_npc(client.bot, target)
        if first['claim_sent'] or 'task=ZS_Parade' not in first['line']:
            break
    report['first_hit'] = first['line']
    assert first['after'] == first['before'], 'Client changed the NPC\'s hit points itself: '+first['line']
    assert first['claim_sent'] == 1, 'No hit claim sent: '+first['line']
    host_after = wait_for(lambda: (npcs(host.bot).get(target, {}).get('hp', host_before) < host_before
                                   and [npcs(host.bot)[target]['hp']]), 10, 'host applies the client hit')[0]
    report['host_hp_after_first_hit'] = host_after
    wait_for(lambda: npcs(client.bot).get(target, {}).get('hp') == npcs(host.bot).get(target, {}).get('hp'), 10,
             'client mirrors the host hit points')
    # B: an NPC hit on the client's puppet on the host lands on the client's hero.
    hero_before = combat_state(client.bot)['hero']
    start = host.bot.send('hitpuppet 2', 20)
    line = host.bot.line('HITPUPPET ', 10, 'hitpuppet', start)
    report['puppet_hit'] = line
    assert 'sent=1' in line, 'Host sent no player hit: '+line
    hero_after = wait_for(lambda: (combat_state(client.bot)['hero'] < hero_before and [combat_state(client.bot)['hero']]), 10,
                          'client hero takes the damage')[0]
    report['client_hero_hp'] = [hero_before, hero_after]
    assert remotes(host.bot).get(2, {}).get('puppet'), 'Puppet gone after the hit'
    # C: hit until the host says dead; the client shows it dead too.
    hits = 1
    # Since stage V1 the host's orcs strike the client too: a swing while it staggers may not land, so count landed ones.
    claimed = first['claim_sent']
    while hits < 80 and not npcs(host.bot).get(target, {}).get('dead'):
        claimed += hit_npc(client.bot, target).get('claim_sent', 0)
        hits += 1
        time.sleep(0.3)
    report['own_hits_claimed'] = claimed
    report['hits_to_kill'] = hits
    assert npcs(host.bot).get(target, {}).get('dead'), f'NPC still alive on the host after {hits} hits'
    wait_for(lambda: npcs(client.bot).get(target, {}).get('dead'), 10, 'client shows the NPC dead')
    time.sleep(2)
    snap(client, 'corpse', report)
    for copy in (host, client):
        report[copy.tag+'_combat_end'] = combat_state(copy.bot)
    assert report['host_combat_end']['claims_applied'] >= 2, 'Host applied too few claims'
    # The kill went through the game's own kill (OnCheatKill) on the host, and the killer earned the experience.
    # In the raid someone else may land the last blow (damage per claim varies, orcs heal, defenders fight); then
    # nobody kills through OnCheatKill, which the host does for every lethal claim, and the client earns nothing.
    if report['host_combat_end']['kills'] == 0:
        assert report['client_combat_end']['xp_awarded'] == 0, 'Client rewarded for a kill it did not make'
        assert report['client_combat_end']['xp'] == report['client_combat_start']['xp'], 'Client earned experience for a kill it did not make'
        report['killed_by'] = 'someone else in the raid'
    else:
        assert report['client_combat_end']['xp_awarded'] >= 1, 'The client was not rewarded for its kill'
        assert report['client_combat_end']['xp'] > report['client_combat_start']['xp'], 'Client experience did not grow'
        report['killed_by'] = 'client'
    assert report['client_combat_end']['player_hits_taken'] >= 1, 'Client hero took no forwarded hit'
    # Only the client's own hits are claimed; hits between host-owned NPCs on the client are dropped.
    assert report['client_combat_end']['claims_sent'] == claimed, \
        f"Client claimed {report['client_combat_end']['claims_sent']} hits for {claimed} own hits that landed ({hits} swings)"


def combatdiag(copies, report, negative=False):
    """Diagnostic, host only: arm the hero and hit nearby NPCs through OnDamage; record what the script did."""
    host = copies['host'].launch()
    enter_world(host)
    start = host.bot.send('arm '+WEAPON, 20)
    report['arm'] = host.bot.line('ARM ', 10, 'arm', start)
    found = npcs(host.bot)
    lines = []
    for rva in ('3f188c', '3f1890'):
        start = host.bot.send('gamestr '+rva, 20)
        lines.append(host.bot.line('GAMESTR ', 10, 'gamestr', start))
    for guid in sorted(found, key=lambda g: found[g]['hp'])[:6]:
        for action, prefix in (('hitnpc', 'HITNPC '),):
            start = host.bot.send(action+' '+guid, 20)
            lines.append(host.bot.line(prefix, 10, action, start))
            for _ in range(3):
                time.sleep(0.5)
                n = npcs(host.bot).get(guid, {})
                lines.append(f"  later hp={n.get('hp')} task={n.get('task')} dead={n.get('dead')}")
    report['hits'] = lines
    report['combat'] = combat_state(host.bot)
    report['status_note'] = 'diagnostic only'


def hitstacks(bot):
    start = bot.send('hitstacks', 20)
    bot.line('HITSTACKS end', 10, 'hitstacks', start)
    return [l for l in bot.log()[start:].splitlines() if l.startswith('HITSTACK')]


def stackdiag(copies, report, negative=False):
    """Diagnostic, host only: which code paths call AddHitPoints during the opening raid, and which one the
    bot's own test hit takes."""
    host = copies['host'].launch()
    enter_world(host)
    time.sleep(45)
    report['raid'] = hitstacks(host.bot)
    start = host.bot.send('arm '+WEAPON, 20)
    report['arm'] = host.bot.line('ARM ', 10, 'arm', start)
    found = npcs(host.bot)
    target = min(found, key=lambda g: (found[g]['dead'], -found[g]['hp']))
    start = host.bot.send('hitnpc '+target, 20)
    report['qa_hit'] = host.bot.line('HITNPC ', 10, 'hitnpc', start)
    report['after_qa_hit'] = hitstacks(host.bot)
    report['combat'] = combat_state(host.bot)
    report['status_note'] = 'diagnostic only'


OPEN, RUNNING, SUCCESS, FAILED = 0, 1, 2, 3
EARLY_QUEST, HOST_QUEST, CLIENT_QUEST, CONTROL_QUEST = 'Geldern_Revolution', 'Montera_Revolution', 'Faring_Revolution', 'Silden_Revolution'


def quest(bot, action, quest_id):
    start = bot.send(f'{action} {quest_id}', 20)
    line = bot.line('QUEST ', 10, f'{action} {quest_id}', start)
    match = re.match(r'QUEST id=(\w+) status=(\d+)', line)
    assert match and match[1] == quest_id, f'{bot.name}: unexpected quest reply: {line}'
    return int(match[2])


def converge(bot, quest_id, expected, seconds=15):
    """Poll a quest until it shows the expected status; returns seconds taken."""
    began = time.monotonic()
    wait_for(lambda: quest(bot, 'quest', quest_id) == expected, seconds, f'{bot.name}: {quest_id} reaches status {expected}')
    return round(time.monotonic()-began, 1)


def story(bot):
    """{kind: {counter: value}} from the mod's STORY lines."""
    start = bot.send('story', 20)
    bot.line('STORY end', 10, 'story', start)
    found = {}
    for line in bot.log()[start:].splitlines():
        match = re.match(r'STORY kind=(\w+) (.*)', line)
        if match:
            found[match[1]] = {k: int(v) for k, v in re.findall(r'(\w+)=(\d+)', match[2])}
    return found


def join_late(copies, before_join):
    """Host into the world, run before_join(host), then the client joins; returns (host, client)."""
    host = copies['host'].launch('host', 'Host')
    enter_world(host)
    start = host.bot.send('releaselock')
    host.bot.line('LOCK scan complete', 10, 'lock scan', start)
    before_join(host)
    client = copies['client'].launch('client', 'Client')
    enter_world(client, startup=120)
    wait_for(lambda: remotes(host.bot).get(2, {}).get('puppet'), 30, 'host shows a puppet for the client')
    return host, client


def story_off(client):
    start = client.bot.send('storyoff', 20)
    assert 'ok=1' in client.bot.line('STORYOFF ', 10, 'storyoff', start)


def stage4(copies, report, negative=False):
    """Shared quest state with host authority: the host's story reaches a late joiner, host changes reach the
    client, client changes reach the host, and untouched quests stay untouched."""
    host = copies['host'].launch('host', 'Host')
    enter_world(host)
    start = host.bot.send('releaselock')
    host.bot.line('LOCK scan complete', 10, 'lock scan', start)
    for quest_id in (EARLY_QUEST, HOST_QUEST, CLIENT_QUEST, CONTROL_QUEST):
        assert quest(host.bot, 'quest', quest_id) == OPEN, f'Host: {quest_id} not Open at start'
    # The host's story so far reaches a client that joins later.
    assert quest(host.bot, 'runquest', EARLY_QUEST) == RUNNING, 'Host could not start '+EARLY_QUEST
    client = copies['client'].launch('client', 'Client')
    enter_world(client, startup=120)
    wait_for(lambda: remotes(host.bot).get(2, {}).get('puppet'), 30, 'host shows a puppet for the client')
    if negative:
        story_off(client)
    report['late_join_seconds'] = converge(client.bot, EARLY_QUEST, RUNNING)
    # Host -> client.
    assert quest(host.bot, 'runquest', HOST_QUEST) == RUNNING, 'Host could not start '+HOST_QUEST
    report['host_to_client_seconds'] = converge(client.bot, HOST_QUEST, RUNNING)
    # Client -> host, start and completion.
    assert quest(client.bot, 'runquest', CLIENT_QUEST) == RUNNING, 'Client could not start '+CLIENT_QUEST
    report['client_start_seconds'] = converge(host.bot, CLIENT_QUEST, RUNNING)
    quest(client.bot, 'succeedquest', CLIENT_QUEST)
    report['client_success_seconds'] = converge(host.bot, CLIENT_QUEST, SUCCESS)
    assert quest(client.bot, 'quest', CLIENT_QUEST) == SUCCESS, 'Client lost its own completion'
    # Control: a quest nobody touched stays Open everywhere.
    for copy in (host, client):
        assert quest(copy.bot, 'quest', CONTROL_QUEST) == OPEN, f'{copy.tag}: untouched quest changed'
        report[copy.tag+'_story'] = story(copy.bot)
    assert report['client_story']['quests']['claims'] >= 2, 'Client sent too few quest changes'


def info(bot, action, name):
    start = bot.send(f'{action} {name}', 20)
    line = bot.line('INFO ', 10, f'{action} {name}', start)
    match = re.match(r'INFO name=(\S+) given=(\d)', line)
    assert match and match[1] == name, f'{bot.name}: unexpected info reply: {line}'
    return int(match[2])


def info_candidates(bot):
    start = bot.send('infolist', 20)
    bot.line('INFOS end', 10, 'infolist', start)
    return [m[1] for m in re.finditer(r'INFOS name=(\S+) owner=(\S+)', bot.log()[start:])]


def stage4b(copies, report, negative=False):
    """Shared dialog state with host authority: which dialog options were already told. A told option reaches
    a late joiner, travels host to client and client to host, and untouched options stay untold."""
    chosen = {}

    def before_join(host):
        names = info_candidates(host.bot)
        assert len(names) >= 4, f'Only {len(names)} untold dialog options found'
        chosen.update(zip(('early', 'host', 'client', 'control'), names[:4]))
        report['infos'] = dict(chosen)
        assert info(host.bot, 'giveinfo', chosen['early']) == 1, 'Host could not mark '+chosen['early']

    host, client = join_late(copies, before_join)
    if negative:
        story_off(client)
    for key in ('host', 'client', 'control'):
        assert info(client.bot, 'info', chosen[key]) == 0, f'Client: {chosen[key]} already told at start'
    report['late_join_seconds'] = converge_info(client.bot, chosen['early'])
    assert info(host.bot, 'giveinfo', chosen['host']) == 1
    report['host_to_client_seconds'] = converge_info(client.bot, chosen['host'])
    assert info(client.bot, 'giveinfo', chosen['client']) == 1
    report['client_to_host_seconds'] = converge_info(host.bot, chosen['client'])
    for copy in (host, client):
        assert info(copy.bot, 'info', chosen['control']) == 0, f'{copy.tag}: untouched dialog option changed'
        report[copy.tag+'_story'] = story(copy.bot)
    assert report['client_story']['infos']['claims'] >= 1, 'Client sent no dialog change'
    for kind in ('quests', 'infos'):
        assert report['host_story'][kind]['longest'] < 63, f'Some {kind} names are too long for a message entry'


def converge_info(bot, name, seconds=15):
    began = time.monotonic()
    wait_for(lambda: info(bot, 'info', name) == 1, seconds, f'{bot.name}: {name} told')
    return round(time.monotonic()-began, 1)


def talkdiag(copies, report, negative=False):
    """Diagnostic: can each side hold a dialog? Host talks to its own Diego, the client to the mirrored one."""
    host, client = join_both(copies)
    time.sleep(5)
    for copy in (host, client):
        lines = []
        start = copy.bot.send('talk Diego', 20)
        lines.append(copy.bot.line('TALK ', 10, 'talk', start))
        for _ in range(4):
            time.sleep(1.5)
            start = copy.bot.send('talkstate', 20)
            lines.append(copy.bot.line('TALKSTATE ', 10, 'talkstate', start))
        start = copy.bot.send('endtalk', 20)
        lines.append(copy.bot.line('TALKSTATE ', 10, 'endtalk', start))
        report[copy.tag+'_talk'] = lines
    report['status_note'] = 'diagnostic only'


def namesdiag(copies, report, negative=False):
    args_names = [t for t in os.environ.get('G3_NAMES', '').split(',') if t]
    """Diagnostic, host only: registered script and AI names around talking."""
    host = copies['host'].launch()
    enter_world(host)
    found = []
    for text in (args_names or ('Talk', 'Dialog', 'Speak', 'Interact', 'Info')):
        start = host.bot.send('names '+text, 20)
        host.bot.line('NAMES end', 10, 'names', start)
        found += [l for l in host.bot.log()[start:].splitlines() if l.startswith('NAME ')]
    report['names'] = sorted(set(found))
    report['status_note'] = 'diagnostic only'


def talkdiag2(copies, report, negative=False):
    """Diagnostic: after the raid, address Diego through AssessTalk on the host and on the client."""
    host, client = join_both(copies)
    calm, kills = [], []
    for _ in range(16):
        start = host.bot.send('killraiders', 20)
        kills.append(host.bot.line('KILLRAIDERS ', 10, 'killraiders', start))
        time.sleep(5)
        found = npcs(host.bot)
        calm.append(sorted(f"{n['name']}:{n['task']}" for n in found.values() if n['name'] in ('Diego', 'Gorn', 'Milten', 'Lester')
                           or (n['name'].startswith('Orc_') and not n['dead'])))
    report['calm'] = calm
    report['raid'] = kills
    for copy in (host, client):
        lines = []
        start = copy.bot.send('assesstalk Diego', 20)
        lines.append(copy.bot.line('ASSESSTALK ', 10, 'assesstalk', start))
        for _ in range(4):
            time.sleep(1.5)
            start = copy.bot.send('talkstate', 20)
            lines.append(copy.bot.line('TALKSTATE ', 10, 'talkstate', start))
        start = copy.bot.send('endtalk', 20)
        lines.append(copy.bot.line('TALKSTATE ', 10, 'endtalk', start))
        report[copy.tag+'_talk'] = lines
        time.sleep(3)
    report['status_note'] = 'diagnostic only'


def clear_raid(host, report):
    """Host: end the opening raid (it comes in waves) so that the people of Ardea calm down and can talk."""
    if RAID_FROM_SAVE:
        line = report_line(host.bot, 'killraiders', 'KILLRAIDERS ')
        report['raid_kills'] = [int(re.search(r'killed=(\d+)', line)[1])]
        report['raid_note'] = 'started from the post-raid save'
        return
    kills, quiet = [], 0
    start = host.bot.send('arm '+WEAPON, 20)
    host.bot.line('ARM ', 10, 'arm', start)
    for _ in range(24):
        start = host.bot.send('killraiders', 20)
        line = host.bot.line('KILLRAIDERS ', 10, 'killraiders', start)
        kills.append(int(re.search(r'killed=(\d+)', line)[1]))
        quiet = quiet + 1 if kills[-1] == 0 else 0
        if quiet >= 3:
            break
        time.sleep(5)
    report['raid_kills'] = kills
    assert quiet >= 3, 'The raid did not end'
    RAID.release()  # the raid is over: another pair may start one


def talkstate(bot):
    start = bot.send('talkstate', 20)
    line = bot.line('TALKSTATE ', 10, 'talkstate', start)
    values = dict(re.findall(r'(\w+)=(\S+)', line))
    return values


def talksync(bot):
    start = bot.send('talksync', 20)
    line = bot.line('TALKSYNC ', 10, 'talksync', start)
    return dict(re.findall(r'(\w+)=(\S+)', line))


def npc_named(bot, name):
    return next(((g, n) for g, n in npcs(bot).items() if n['name'] == name), (None, None))


def stage4c(copies, report, negative=False):
    """A client's hero holds a dialog with an NPC owned by the host: the dialog runs on the client, the host
    holds its NPC meanwhile and releases it afterwards, and what was told reaches the host."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        start = host.bot.send('talkholdoff', 20)
        assert 'ok=1' in host.bot.line('TALKHOLDOFF ', 10, 'talkholdoff', start)
    busy = ('ZS_Attack', 'ZS_QuickStumble', 'ZS_Stumble', 'ZS_SitKnockDown', 'ZS_ParadeStumble')
    wait_for(lambda: (npc_named(host.bot, 'Diego')[1] or {}).get('task', 'ZS_Attack') not in busy, 60, 'Diego calms down on the host')
    # The client walks up to the mirrored Diego and addresses him the way the player's talk action does.
    # The game may refuse for a while (seen right after the raid, when the host's Diego had walked up to the
    # host's hero); a player would simply try again. Every refusal is recorded with the host's view.
    report['refusals'] = []
    for _ in range(10):
        start = client.bot.send('approach Diego', 20)
        report['approach'] = client.bot.line('APPROACH ', 10, 'approach', start)
        time.sleep(2)
        start = client.bot.send('assesstalk Diego', 20)
        report['assesstalk'] = client.bot.line('ASSESSTALK ', 10, 'assesstalk', start)
        if 'result=1' in report['assesstalk']:
            break
        report['refusals'].append({'client': report['assesstalk'], 'host_talk': talkstate(host.bot),
                                   'host_diego_task': (npc_named(host.bot, 'Diego')[1] or {}).get('task')})
        time.sleep(1)
    assert 'result=1' in report['assesstalk'], 'Client could not address Diego: '+report['assesstalk']
    told, states = set(), []

    def watch():
        state = talkstate(client.bot)
        states.append(state)
        if state.get('current', '-') != '-':
            told.add(state['current'])
        return state

    wait_for(lambda: watch().get('partner') == 'Diego' and watch().get('running') == '1', 10, 'client in a dialog with Diego')
    # Meanwhile the host holds its Diego: idle, not moving.
    wait_for(lambda: 'Diego' in talksync(host.bot).get('held_names', ''), 10, 'host holds Diego')
    guid, first = npc_named(host.bot, 'Diego')
    time.sleep(6)
    watch()
    _, later = npc_named(host.bot, 'Diego')
    report['host_diego_held'] = {'task': later['task'], 'moved_cm': round(distance(first['pos'], later['pos']))}
    assert later['task'] == 'ZS_HangAround_DoNothing', 'Host Diego not held: '+later['task']
    assert distance(first['pos'], later['pos']) < 100, 'Host Diego moved during the dialog'
    # Through the dialog: wait for a choice, pick the first option, then end it.
    wait_for(lambda: watch().get('waiting') == '0' and int(watch().get('available', '0')) > 0, 40, 'a dialog choice')
    start = client.bot.send('talkchoose 0', 20)
    report['choice'] = client.bot.line('TALKCHOOSE ', 10, 'talkchoose', start)
    for _ in range(10):
        time.sleep(1)
        watch()
    start = client.bot.send('endtalk', 20)
    client.bot.line('TALKSTATE ', 10, 'endtalk', start)
    wait_for(lambda: watch().get('running') == '0', 10, 'dialog ends on the client')
    report['client_dialog'] = [f"{s.get('current')}/{s.get('available')}/{s.get('waiting')}" for s in states]
    # The host lets Diego go again, and knows what was told.
    wait_for(lambda: talksync(host.bot).get('held') == '0', 10, 'host releases Diego')
    told = sorted(name for name in told if info(client.bot, 'info', name) == 1)
    report['told'] = told
    assert told, 'Nothing was told in the dialog'
    for name in told:
        converge_info(host.bot, name)
    report['host_talksync'] = talksync(host.bot)
    report['client_talksync'] = talksync(client.bot)
    assert report['client_talksync']['begins'] == '1' and report['client_talksync']['ends'] == '1'


def world_items(bot):
    start = bot.send('items', 20)
    bot.line('ITEMS end', 10, 'items', start)
    text = bot.log()[start:]
    found = {m[1]: {'name': m[2], 'distance': float(m[3])} for m in re.finditer(r'ITEM guid=(\S+) name=(\S+) distance_cm=([\d.]+)', text)}
    summary = re.search(r'ITEMS entities=(\d+) world_items_near=(\d+)', text)
    return found, summary[0] if summary else ''


def itemdiag(copies, report, negative=False):
    """Diagnostic, host only: world items near the hero; what taking and decaying one does to its GUID."""
    host = copies['host'].launch()
    enter_world(host)
    found, summary = world_items(host.bot)
    report['summary'] = summary
    report['near'] = [f"{g} {v['name']} {v['distance']:.0f}" for g, v in sorted(found.items(), key=lambda kv: kv[1]['distance'])[:10]]
    ordered = sorted(found, key=lambda g: found[g]['distance'])
    assert len(ordered) >= 2, 'Fewer than two world items near the hero'
    lines = []
    for action, guid in (('itempick', ordered[0]), ('itemdecay', ordered[1])):
        start = host.bot.send(f'{action} {guid}', 20)
        host.bot.line('ITEM', 10, action, start)
        time.sleep(0.5)
        lines += [l for l in host.bot.log()[start:].splitlines() if l.startswith('ITEM')]
        for wait in (1, 5):
            time.sleep(wait)
            start = host.bot.send(f'iteminfo {guid}', 20)
            lines.append(host.bot.line('ITEMINFO ', 10, 'iteminfo', start))
    start = host.bot.send('itemtook', 20)
    lines.append(host.bot.line('ITEMTOOK ', 10, 'itemtook', start))
    report['lines'] = lines
    after, summary_after = world_items(host.bot)
    report['summary_after'] = summary_after
    report['taken_still_listed'] = ordered[0] in after
    report['decayed_still_listed'] = ordered[1] in after
    report['status_note'] = 'diagnostic only'


def item_exists(bot, guid):
    start = bot.send('iteminfo '+guid, 20)
    return 'exists=1' in bot.line('ITEMINFO ', 10, 'iteminfo', start)


def carried(bot, name):
    start = bot.send('invcount '+name, 20)
    return int(re.search(r'amount=(-?\d+)', bot.line('INVCOUNT ', 10, 'invcount', start))[1])


def pick(bot, guid):
    start = bot.send('itempick '+guid, 20)
    return bot.line('ITEMPICK ', 10, 'itempick', start)


def item_counters(bot):
    start = bot.send('itemtook', 20)
    return {k: int(v) for k, v in re.findall(r'(\w+)=(\d+)', bot.line('ITEMTOOK ', 10, 'itemtook', start).split(' last=')[0])}


def stage5(copies, report, negative=False):
    """World items with host authority: what one player takes is gone for everyone, two players grabbing the
    same item end up with one item between them, and untouched items stay."""
    host, client = join_both(copies)
    if negative:
        start = client.bot.send('itemsyncoff', 20)
        assert 'ok=1' in client.bot.line('ITEMSYNCOFF ', 10, 'itemsyncoff', start)
    # The opening raid first: raiders striking a hero break off its reach for an item (StumbleR_Hit during
    # Reach_Begin, red runs 20.10. 21:26/21:34), which is the game's way, not what this stage tests.
    clear_raid(host, report)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    host_items, _ = world_items(host.bot)
    client_items, _ = world_items(client.bot)
    common = sorted((g for g in set(host_items) & set(client_items)), key=lambda g: host_items[g]['distance'])
    assert len(common) >= 4, f'Only {len(common)} world items seen by both'
    by_name = {}
    for g in common:  # four items of different kinds, so that inventory counts tell them apart
        if not host_items[g]['name'].startswith('It_Gold'):  # gold stacks turn into coins of another template
            by_name.setdefault(host_items[g]['name'], g)
    chosen = list(by_name.values())[:5]
    assert len(chosen) == 5, f'Only {len(chosen)} kinds of world items near both heroes'
    a, b, c, d, e = chosen
    name = {g: host_items[g]['name'] for g in chosen}
    report['items'] = {k: f'{g} {name[g]}' for k, g in zip(('client_takes', 'host_takes', 'both_grab', 'untouched', 'late'), chosen)}
    before = {g: (carried(host.bot, name[g]), carried(client.bot, name[g])) for g in chosen}
    # The client takes one: gone from the host's world.
    report['client_pick'] = pick(client.bot, a)
    wait_for(lambda: carried(client.bot, name[a]) > before[a][1], 15, 'client carries what it took')
    wait_for(lambda: not item_exists(host.bot, a), 10, "the client's item disappears on the host")
    # The host takes one: gone from the client's world.
    report['host_pick'] = pick(host.bot, b)
    wait_for(lambda: carried(host.bot, name[b]) > before[b][0], 15, 'host carries what it took')
    wait_for(lambda: not item_exists(client.bot, b), 10, "the host's item disappears on the client")
    # Both grab the same item: one item between them in the end, and it is gone everywhere.
    # Both commands go out before either answer is read; whoever comes second may find the item already gone
    # (the first take reached its machine first), which is a valid outcome of the race.
    starts = (host.bot.send('itempick '+c, 20), client.bot.send('itempick '+c, 20))
    report['race'] = [bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith(('ITEMPICK ', 'REFUSED itempick'))), ''),
                               10, 'itempick', start) for bot, start in zip((host.bot, client.bot), starts)]
    wait_for(lambda: not item_exists(host.bot, c) and not item_exists(client.bot, c), 15, 'raced item gone everywhere')
    time.sleep(5)
    gained = (carried(host.bot, name[c]) - before[c][0], carried(client.bot, name[c]) - before[c][1])
    report['race_gained'] = gained
    assert sum(1 for x in gained if x > 0) == 1, f'Raced item ended up with {gained} (host, client)'
    # Too late: the item already left the host's world without a word (as when an NPC picks it up); the
    # client's claim is refused and the client gives back what it took.
    start = host.bot.send('itemdecay '+e, 20)
    host.bot.line('ITEMDECAY ', 10, 'itemdecay', start)
    report['late_pick'] = pick(client.bot, e)
    wait_for(lambda: item_counters(client.bot)['returned'] >= 1, 15, 'client gives back the refused item')
    report['late_carried'] = carried(client.bot, name[e]) - before[e][1]
    assert report['late_carried'] == 0, f"Client kept {report['late_carried']} of the refused item"
    # Untouched stays.
    assert item_exists(host.bot, d) and item_exists(client.bot, d), 'Untouched item vanished'
    report['host_items'] = item_counters(host.bot)
    report['client_items'] = item_counters(client.bot)


def containers(bot):
    start = bot.send('containers', 20)
    bot.line('CONTAINERS end', 10, 'containers', start)
    found = {}
    for m in re.finditer(r'CONTAINER guid=(\S+) name=(\S+) kind=(\S+) items=(\S+)', bot.log()[start:]):
        found[m[1]] = {'name': m[2], 'kind': m[3], 'items': m[4]}
    return found


def fakebreaddiag(copies, report, negative=False):
    """Diagnostic (23.10.): why It_FakeBread never stays in the client's copy of a corpse ("drifted; set back" every 2 s,
    in runs since 22.10.). The host puts one into a corpse near it (stock); both sides' stacks are dumped before, as it
    arrives and after the client's resyncs. Then the client makes one in its own copy, to see where the game puts it."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    found = containers(host.bot)
    corpses = [g for g, c in found.items() if c['kind'] == 'corpse']
    assert corpses, 'no corpse near the host'
    guid = corpses[0]
    report['corpse'] = found[guid]['name']

    def stacks(bot):
        start = bot.send('stacks '+guid, 20)
        bot.line('STACKS end', 10, 'stacks', start)
        return [l for l in bot.log()[start:].splitlines() if l.startswith(('STACKS ', 'STACK '))]

    report['host_before'], report['client_before'] = stacks(host.bot), stacks(client.bot)
    since = len(client.mod_log())
    report['stock'] = report_line(host.bot, f'stock {guid}_It_FakeBread', 'STOCK ')
    report['host_after_stock'] = stacks(host.bot)
    for wait, label in ((1, 'client_1s'), (2, 'client_3s'), (4, 'client_7s')):
        time.sleep(wait)
        report[label] = stacks(client.bot)
    report['client_contents'] = contents(client.bot, guid)
    report['host_contents'] = contents(host.bot, guid)
    report['client_log'] = [l for l in client.mod_log()[since:].splitlines() if 'containers:' in l][:10]
    report['client_stock'] = report_line(client.bot, f'stock {guid}_It_FakeBread', 'STOCK ')
    report['client_after_own'] = stacks(client.bot)
    report['status_note'] = 'diagnostic only'


def fakebreadlivediag(copies, report, negative=False):
    """Diagnostic (23.10.), the live case of the It_FakeBread drift (stageReloadLoot 20261023-213010: Slave_08 died in the
    raid holding its bread; the client's copy of the corpse drifted every 2 s). A live slave near the host gets
    It_FakeBread into its right hand (holditem, as a slave eats); once the client's copy holds it too, the host kills
    the slave (farkill). The client's copy of the corpse is dumped as it dies and after the container sync."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    here = hero_pos(host.bot)
    alive = {g: v for g, v in npcs(host.bot).items() if not v['dead'] and v['hp'] > 0}

    def dist(guid):
        return sum((a-b)**2 for a, b in zip(alive[guid]['pos'], here)) ** 0.5

    # The host must simulate it and the client mirror it (the case of Slave_08 in run 20261023-213010): a slave the client
    # simulates is a mirror on the host, and the client's state undid the host's farkill (run 20261023-220429, Slave_03).
    mirrored_here = npcs(client.bot)
    slaves = sorted((g for g, v in alive.items() if v['name'].startswith('Slave') and not v['mirrored'] and
                     (mirrored_here.get(g) or {}).get('mirrored')), key=dist)
    report['live_slaves'] = {alive[g]['name']: [alive[g]['mirrored'], (mirrored_here.get(g) or {}).get('mirrored')]
                             for g in alive if alive[g]['name'].startswith('Slave')}
    assert slaves, 'no live slave the host simulates and the client mirrors'
    guid = slaves[0]
    name = alive[guid]['name']
    report['npc'] = {'name': name, 'distance_m': round(dist(guid)/100, 1)}

    def stacks(bot):
        start = bot.send('stacks '+guid, 20)
        bot.line('STACKS end', 10, 'stacks', start)
        return [l for l in bot.log()[start:].splitlines() if l.startswith(('STACKS ', 'STACK '))]

    def holds(lines):
        return any('name=It_Fake' in l and ' slot=0 ' not in l for l in lines)

    report['host_before'], report['client_before'] = stacks(host.bot), stacks(client.bot)
    if not any(' slot=1 ' in l and 'name=It_Fake' in l for l in report['host_before']):  # slaves eat by default; else give it bread
        report['hold'] = report_line(host.bot, f'holditem {guid}_It_FakeBread', 'HOLDITEM ')
    report['host_holding'] = stacks(host.bot)
    try:
        report['client_holding'] = wait_for(lambda: (lambda s: s if holds(s) else None)(stacks(client.bot)), 15, "the client's copy holds the bread")
    except AssertionError:
        report['client_holding'] = stacks(client.bot) + ['(no prop held on the client)']
    since = len(client.mod_log())
    # A lethal claim from the client, applied by the host's OnCheatKill(victim, victim) as a raid kill is; farkill (the hero
    # as killer) only knocked the slave out (run 20261023-223055).
    report['kill'] = report_line(client.bot, f'hitnpcburst {guid}_1', 'HITNPCBURST ')
    report['host_killed'] = wait_mod_log(host, rf'combat: player \d+ killed {name} ', 10, 'the host kills the slave')
    for wait, label in ((1, 'client_1s'), (2, 'client_3s'), (4, 'client_7s'), (5, 'client_12s')):
        time.sleep(wait)
        report[label] = stacks(client.bot)
    report['host_after'] = stacks(host.bot)
    report['host_contents'], report['client_contents'] = contents(host.bot, guid), contents(client.bot, guid)
    report['client_log'] = [l for l in client.mod_log()[since:].splitlines() if name in l][:14]
    report['status_note'] = 'diagnostic only'


def stageCorpseProp(copies, report, negative=False):
    """Stage CorpseProp, a slave that dies holding its bread or cup lies on the client with the host's plunder, the prop
    included, and stays so (run 20261023-213010: Slave_08's It_FakeBread drifted every 2 s on the client; fakebreadlivediag
    23.10.: DeleteStack leaves a stack held in a hand and CreateItems adds to it, so the dead copy's hand held one more
    bread at every rewrite). A slave the host simulates and the client mirrors, holding a prop (slaves eat; else
    holditem), is killed by a lethal claim from the client, as a raid kill is (OnCheatKill on the host). Within 6 s the
    client's copy holds what the host's corpse holds, then 8 s more without a rewrite, and no hand holds a prop.
    Negative control: containersyncoff hands on the client."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    if negative:
        assert 'hands=0' in report_line(client.bot, 'containersyncoff hands', 'CONTAINERSYNCOFF ')
    here = hero_pos(host.bot)
    alive = {g: v for g, v in npcs(host.bot).items() if not v['dead'] and v['hp'] > 0}
    there = npcs(client.bot)
    slaves = sorted((g for g, v in alive.items() if v['name'].startswith('Slave') and not v['mirrored'] and (there.get(g) or {}).get('mirrored')),
                    key=lambda g: sum((a-b)**2 for a, b in zip(alive[g]['pos'], here)))
    assert slaves, 'no live slave the host simulates and the client mirrors'
    guid = slaves[0]
    name = report['slave'] = alive[guid]['name']

    def stacks(bot):
        start = bot.send('stacks '+guid, 20)
        bot.line('STACKS end', 10, 'stacks', start)
        return [l for l in bot.log()[start:].splitlines() if l.startswith('STACK ')]

    def held_prop(lines):
        return [l for l in lines if 'name=It_Fake' in l and (' slot=1 ' in l or ' slot=2 ' in l)]

    if not held_prop(stacks(host.bot)):
        report['hold'] = report_line(host.bot, f'holditem {guid}_It_FakeBread', 'HOLDITEM ')
    report['client_holds'] = wait_for(lambda: held_prop(stacks(client.bot)), 15, "the client's copy holds the prop")
    report['kill'] = report_line(client.bot, f'hitnpcburst {guid}_1', 'HITNPCBURST ')
    report['host_killed'] = wait_mod_log(host, rf'combat: player \d+ killed {name} ', 10, 'the host kills the slave')
    report['host_contents'] = plunder = wait_for(lambda: (lambda c: c if any(k.startswith('It_Fake') for k in c) else None)(contents(host.bot, guid)),
                                                 10, "the prop is among the host corpse's plunder")
    report['client_same'] = wait_for(lambda: contents(client.bot, guid) == plunder and ['yes'], 6, "the client's copy holds the host's plunder")
    since = len(client.mod_log())
    before = containersync(client.bot)
    time.sleep(8)
    after = containersync(client.bot)
    report['client_later'] = later = contents(client.bot, guid)
    report['client_stacks'] = lines = stacks(client.bot)
    report['rewrites'] = after['rewrites'] - before['rewrites']
    report['hands_released'] = after.get('hands_released', 0)
    drift = [l for l in client.mod_log()[since:].splitlines() if f'containers: {name} drifted' in l]
    assert later == plunder, f"the client's copy drifted from the host's plunder: {plunder} -> {later}"
    assert not drift, f"the client's copy kept drifting: {drift[:2]}"
    assert not held_prop(lines), f"the dead copy's hand still holds a prop: {held_prop(lines)}"


def stageTradeKeys(copies, report, negative=False):
    """Stage TradeKeys, keys that are the same are warned about (B 23.10.: partner_key = INSERT also accepted a trade offer).
    The host plays with partner_key = INSERT and offer_decline_key = insert, so all three keys are Insert: one warning names
    all three, in its log at the start and on its screen once the game runs. The client chooses its own trade keys (F9, f10),
    which clash with nothing: no warning, and those are its keys. Negative control: qa_keycheck = off in both INIs (the check
    does not run; honoured only with the QA bot)."""
    off = 'qa_keycheck = off\n' if negative else ''
    copies['host'].ini_extra = 'partner_key = INSERT\noffer_decline_key = insert\n' + off
    copies['client'].ini_extra = 'offer_accept_key = F9\noffer_decline_key = f10\n' + off
    host, client = join_both(copies)
    report['host_keys'] = host_keys = report_line(host.bot, 'keycheck', 'KEYCHECK ')
    report['client_keys'] = client_keys = report_line(client.bot, 'keycheck', 'KEYCHECK ')
    assert 'partner=INSERT accept=INSERT decline=INSERT ' in host_keys, host_keys
    assert 'partner=PAGEDOWN accept=F9 decline=F10 ' in client_keys, client_keys
    clash = 'INSERT is set for partner_key, offer_accept_key, offer_decline_key'
    report['host_log'] = wait_mod_log(host, 'WARN keys: ' + clash, 5, "the host's log warns about Insert")
    report['host_screen'] = wait_mod_log(host, r'hud: \[\d+\] (The key ' + clash + '|Die Taste INSERT ist für partner_key, offer_accept_key, offer_decline_key)',
                                         30, "the host's screen warns about Insert")
    report['host_keys_after'] = after = report_line(host.bot, 'keycheck', 'KEYCHECK ')
    assert 'clashes=1 shown=1' in after, after
    assert 'clashes=0' in client_keys and 'WARN keys:' not in client.mod_log(), f'the client warns without a clash: {client_keys}'


def menutextdiag(copies, report, negative=False):
    """Diagnostic (24.10.): can a player waiting in the main menu read a message of the mod? A client that never reaches
    the host waits there (JOINING.md: stay in the main menu until the host's campaign arrives). Only the client starts,
    nothing listens on its port; in the start menu each of the game's ways to show a text is tried (menutext), with a
    screenshot after each."""
    client = copies['client'].launch('client', 'Client')
    client.bot.line('STATE running=0 paused=1 save_allowed=0 world=G3_StartUp hero=none ', 60, 'startup menu', 0)
    time.sleep(5)
    snap(client, 'menu-before', report)
    for way in ('hud', 'gamemsg', 'subtitle'):
        report['probe_'+way] = report_line(client.bot, 'menutext '+way, ('MENUTEXT ', 'REFUSED menutext'))
        time.sleep(1.5)
        snap(client, 'menu-'+way, report)
        time.sleep(7)  # the game's messages fade after about 6 s
    start = client.bot.send('menu', 30)
    client.bot.line('MENU scan complete', 30, 'menu scan', start)
    report['menu'] = [l for l in client.bot.log()[start:].splitlines() if l.startswith('MENU ')][:80]
    report['net'] = [l for l in client.mod_log().splitlines() if 'net:' in l][:8]
    report['status_note'] = 'diagnostic only'


def stageHostUnreachable(copies, report, negative=False):
    """Stage HostUnreachable, a player whose game never reaches the host is told so where it waits (owner 24.10.,
    BERICHT-2026-10-24 point 6: it saw nothing). It waits in the main menu, where the game's own messages do not show
    (menutextdiag): only the client starts, nothing listens on its port; within 40 s the menu's top line has a second line
    under the Community Patch's version saying the host address is not reachable, and the log says so. Then the host
    starts: the client reaches it, and the line is the version alone again. Then the host refuses it (a hello with another
    protocol version, as in W7): the line says why. Negative controls on the client (honoured only with the QA bot):
    qa_hosthint = off (no menu hints) fails at the first part, G3_HOSTHINT_NEGATIVE=norefusal (no refusal there) at the last."""
    if negative:
        copies['client'].ini_extra = 'qa_hosthint = ' + os.environ.get('G3_HOSTHINT_NEGATIVE', 'off') + '\n'
    client = copies['client'].launch('client', 'Client')
    client.bot.line('STATE running=0 paused=1 save_allowed=0 world=G3_StartUp hero=none ', 60, 'startup menu', 0)

    def line():
        return report_line(client.bot, 'menuline', 'MENULINE ')

    def unreachable(text):
        return 'not reachable' in text or 'nicht erreichbar' in text

    report['menu_line'] = shown = wait_for(lambda: (lambda l: l if unreachable(l) else None)(line()), 40, 'the menu says the host is not reachable')
    assert 'found=1' in shown and 'pb/cpt/sb' in shown and '127.0.0.1:' in shown, f"the line lacks the patch's version or the address: {shown}"
    report['log'] = wait_mod_log(client, r'net: host 127\.0\.0\.1:\d+ not reachable for \d+ s', 5, "the client's log says so")
    snap(client, 'menu-unreachable', report)
    since = len(client.mod_log())
    copies['host'].launch('host', 'Host')
    report['connected'] = wait_mod_log(client, r'net: connected, hello sent', 120, 'the client reaches the host', since)
    report['menu_after'] = after = wait_for(lambda: (lambda l: l if 'found=1' in l and 'changed=0' in l and not unreachable(l) else None)(line()), 20,
                                            'the menu line is the version alone again')
    assert '\\n' not in after, after
    snap(client, 'menu-connected', report)
    # The host refuses this client (another protocol version, as in W7): its reason stays in the menu line.
    since = len(client.mod_log())
    assert 'ok=1' in report_line(client.bot, 'helloversion 1', 'HELLOVERSION ')
    report['reconnect_other_version'] = report_line(client.bot, 'reconnect', 'RECONNECT ')
    report['refusal'] = wait_mod_log(client, r'net: REFUSED by host: different G3 Together version', 30, 'the host refuses the other version', since)
    report['menu_refused'] = refused = wait_for(lambda: (lambda l: l if 'yours: protocol 1' in l or 'deine: Protokoll 1' in l else None)(line()), 10,
                                               'the menu line says why the host refused this client')
    assert 'pb/cpt/sb' in refused and '\\n' in refused, refused
    snap(client, 'menu-refused', report)


def lootdiag(copies, report, negative=False):
    """Diagnostic: chests and corpses near the start, after the raid, on both sides: same contents?"""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(5)
    h, c = containers(host.bot), containers(client.bot)
    report['host_count'], report['client_count'] = len(h), len(c)
    same, differ = [], []
    for guid in sorted(set(h) & set(c)):
        (same if h[guid]['items'] == c[guid]['items'] else differ).append(f"{h[guid]['kind']} {h[guid]['name']}: host {h[guid]['items']} | client {c[guid]['items']}")
    report['same'], report['differ'] = same[:15], differ[:15]
    report['only_host'] = [f"{h[g]['kind']} {h[g]['name']} {h[g]['items']}" for g in set(h) - set(c)][:10]
    report['status_note'] = 'diagnostic only'


def contents(bot, guid):
    start = bot.send('contents '+guid, 20)
    line = bot.line('CONTENTS ', 10, 'contents', start)
    items = line.split(' items=')[1]
    found = {} if items == '-' else {k: int(v) for k, v in (x.split(':') for x in items.split(','))}
    # 'Fist' is the bare-hand weapon an unarmed NPC holds while it fights (host only, it is never plunder and the
    # container sync skips held stacks); it made stage 5b fail whenever the raid killed an unarmed slave.
    found.pop('Fist', None)
    return found


def containersync(bot):
    start = bot.send('containersync', 20)
    return {k: int(v) for k, v in re.findall(r'(\w+)=(\d+)', bot.line('CONTAINERSYNC ', 10, 'containersync', start))}


def loottake(bot, guid, item=None):
    """(name, amount) taken from the container's first stack; (None, 0) if it was empty."""
    start = bot.send('loottake '+guid+('_'+item if item else ''), 20)
    line = bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith(('LOOTTAKE ', 'REFUSED loottake'))), ''), 10,
                    'loottake', start)
    match = re.search(r'name=(\S+) amount=(\d+) ok=(\d)', line)
    return (match[1], int(match[2])) if match else (None, 0)


def stage5b(copies, report, negative=False):
    """Plunder with host authority: corpses show the host's contents on every machine; what one player takes
    from a corpse is gone for everyone; two players taking the same stack end up with it once."""
    host, client = join_both(copies)
    if negative:
        start = client.bot.send('containersyncoff', 20)
        assert 'ok=1' in client.bot.line('CONTAINERSYNCOFF ', 10, 'containersyncoff', start)
    clear_raid(host, report)
    time.sleep(8)
    h, c = containers(host.bot), containers(client.bot)
    corpses = sorted(g for g in set(h) & set(c) if h[g]['kind'] == 'corpse' and h[g]['items'] != '-')
    report['corpses'] = len(corpses)
    assert len(corpses) >= 3, f'Only {len(corpses)} corpses with plunder seen by both'
    # Every corpse shows the host's contents on the client (plunder is rolled per machine otherwise).
    try:
        wait_for(lambda: all(contents(client.bot, g) == contents(host.bot, g) for g in corpses), 15, 'corpses show the host contents')
    except AssertionError:
        # Which corpses differ, and who simulated the NPC (an NPC another machine owned is plundered there).
        owners = {g.replace('-', '')[:12]: v for g, v in npcs(host.bot).items()}
        report['corpse_mismatch'] = [f"{g} host={contents(host.bot, g)} client={contents(client.bot, g)} "
                                     f"host_mirrored={owners.get(g.replace('-', '')[:12], {}).get('mirrored')}"
                                     for g in corpses if contents(client.bot, g) != contents(host.bot, g)]
        raise
    x, y, z = corpses[:3]
    # The client opens a corpse the player's way and takes the first stack: it is gone on the host.
    start = client.bot.send('lootopen '+x, 20)
    report['lootopen'] = client.bot.line('LOOTOPEN ', 10, 'lootopen', start)
    before_x = contents(host.bot, x)
    name, amount = loottake(client.bot, x)
    report['client_took'] = f'{name}:{amount}'
    wait_for(lambda: contents(host.bot, x).get(name, 0) == before_x.get(name, 0) - amount, 10, "the client's take shows on the host")
    post_key(client.process.pid, 0x1B)  # close the loot screen
    # The host takes from another corpse: the client's copy follows.
    name_y, amount_y = loottake(host.bot, y)
    report['host_took'] = f'{name_y}:{amount_y}'
    wait_for(lambda: contents(client.bot, y) == contents(host.bot, y), 10, "the host's take shows on the client")
    # Both take the same first stack: it ends up with exactly one of them.
    report['z_before_open'] = {'client': contents(client.bot, z), 'host': contents(host.bot, z), 'client_gold': carried(client.bot, 'It_Gold')}
    start = client.bot.send('lootopen '+z, 20)
    client.bot.line('LOOTOPEN ', 10, 'lootopen', start)
    time.sleep(1)
    report['z_after_open'] = {'client': contents(client.bot, z), 'host': contents(host.bot, z), 'client_gold': carried(client.bot, 'It_Gold')}
    first = next(iter(contents(host.bot, z)))
    carried_before = (carried(host.bot, first), carried(client.bot, first))
    race = [loottake(host.bot, z, first), loottake(client.bot, z, first)]
    report['race'] = [f'{n}:{a}' for n, a in race]
    time.sleep(6)
    gained = (carried(host.bot, first) - carried_before[0], carried(client.bot, first) - carried_before[1])
    report['race_gained'] = gained
    post_key(client.process.pid, 0x1B)
    assert sum(1 for g in gained if g > 0) == 1, f'Raced stack {first} ended up with {gained} (host, client)'
    wait_for(lambda: contents(client.bot, z) == contents(host.bot, z), 10, 'raced corpse agrees afterwards')
    # Too late: the host's stack left unseen; the client takes it from its stale copy, is refused, gives it back.
    # Loot is random: the next corpse that still holds something on the host (one was empty, stage 5b once), and
    # nothing but ordinary items: one held only 'Fist', the unarmed weapon every hero carries too, so the client's
    # count of it could not show the refused take being undone.
    w = next((k for k in corpses[3:] if contents(host.bot, k) and 'Fist' not in contents(host.bot, k)), None)
    assert w, 'no corpse with ordinary loot left for the late-take check'
    if w:
        start = client.bot.send('lootopen '+w, 20)
        client.bot.line('LOOTOPEN ', 10, 'lootopen', start)
        start = host.bot.send('lootsilent '+w, 20)
        silent = host.bot.line('LOOTSILENT ', 10, 'lootsilent', start)
        late_name = re.search(r'name=(\S+)', silent)[1]
        late_before = carried(client.bot, late_name)
        report['late'] = [silent, '{}:{}'.format(*loottake(client.bot, w, late_name))]
        wait_for(lambda: containersync(client.bot)['tx_denied'] >= 1, 10, "the host refuses the client's late take")
        time.sleep(1)
        report['late_kept'] = carried(client.bot, late_name) - late_before
        post_key(client.process.pid, 0x1B)
        assert report['late_kept'] == 0, f"Client kept {report['late_kept']} of a refused take"
        wait_for(lambda: contents(client.bot, w) == contents(host.bot, w), 10, 'refused corpse agrees afterwards')
    report['host_sync'] = containersync(host.bot)
    report['client_sync'] = containersync(client.bot)


def goods(bot, guid):
    start = bot.send('goods '+guid, 20)
    items = bot.line('GOODS ', 10, 'goods', start).split(' items=')[1]
    return {} if items == '-' else {k: int(v) for k, v in (x.split(':') for x in items.split(','))}


def tradebuy(bot, guid, item=None):
    start = bot.send('tradebuy '+guid+('_'+item if item else ''), 20)
    line = bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith(('TRADEBUY ', 'REFUSED tradebuy'))), ''), 10,
                    'tradebuy', start)
    match = re.search(r'name=(\S+) price=(\d+) got=(\d) paid=(\d)', line)
    assert match and match[3] == '1' and match[4] == '1', f'{bot.name}: {line}'
    return match[1], int(match[2])


def stage5c(copies, report, negative=False):
    """Trade with host authority: the client sees the host's goods of the NPC it trades with, a purchase
    (item one way, gold the other) reaches the host as one transaction, and a refused purchase is undone."""
    host, client = join_both(copies)
    if negative:
        start = client.bot.send('containersyncoff', 20)
        assert 'ok=1' in client.bot.line('CONTAINERSYNCOFF ', 10, 'containersyncoff', start)
    # Diego survives the opening raid; the host stocks him with two kinds of goods for the test.
    trader, diego = npc_named(host.bot, 'Diego')
    assert trader and not diego['dead'], 'Diego not found alive on the host'
    report['trader'] = 'Diego'
    for ware in ('It_Bread', 'It_Water'):
        start = host.bot.send(f'stock {trader}_{ware}', 20)
        host.bot.line('STOCK ', 10, 'stock', start)
    start = client.bot.send('tradeopen '+trader, 20)
    report['tradeopen'] = client.bot.line('TRADEOPEN ', 10, 'tradeopen', start)
    wait_for(lambda: goods(client.bot, trader) == goods(host.bot, trader), 15, "the client sees the host's goods")
    host_before = goods(host.bot, trader)
    buy, late = 'It_Bread', 'It_Water'  # stocked above; item names are not unique ("Appow" vs "Arrow")
    hero_before = {k: carried(client.bot, k) for k in (buy, late, 'It_Gold')}
    report['hero_before'] = hero_before
    # A purchase: the host's trader loses the piece and gains the price.
    name, price = tradebuy(client.bot, trader, buy)
    report['bought'] = f'{name} for {price}'
    wait_for(lambda: goods(host.bot, trader).get(buy, 0) == host_before.get(buy, 0) - 1 and
             goods(host.bot, trader).get('It_Gold', 0) == host_before.get('It_Gold', 0) + price, 10, 'the purchase reaches the host')
    assert carried(client.bot, buy) == hero_before[buy] + 1 and carried(client.bot, 'It_Gold') == hero_before['It_Gold'] - price
    # Refused: the host's trader no longer has it; the client's purchase is undone, gold included.
    start = host.bot.send(f'lootsilent {trader}_{late}', 20)
    report['silent'] = host.bot.line('LOOTSILENT ', 10, 'lootsilent', start)
    gold_before_late = carried(client.bot, 'It_Gold')
    report['late'] = '{} for {}'.format(*tradebuy(client.bot, trader, late))
    wait_for(lambda: containersync(client.bot)['tx_denied'] >= 1, 10, 'the host refuses the late purchase')
    time.sleep(1)
    report['late_kept'] = (carried(client.bot, late) - hero_before[late], carried(client.bot, 'It_Gold') - gold_before_late)
    post_key(client.process.pid, 0x1B)
    assert report['late_kept'] == (0, 0), f"Refused purchase not undone (item, gold): {report['late_kept']}"
    wait_for(lambda: goods(client.bot, trader) == goods(host.bot, trader), 10, 'goods agree afterwards')
    report['client_sync'] = containersync(client.bot)


def invstate(bot, guid):
    line = report_line(bot, 'invstate '+guid, 'INVSTATE ')
    return dict(re.findall(r'(\w+)=(\S+)', line))


def chests(bot):
    start = bot.send('chests', 20)
    bot.line('CHESTS end', 10, 'chests', start)
    return [dict(re.findall(r'(\w+)=(\S+)', l)) for l in bot.log()[start:].splitlines() if l.startswith('CHEST ')]


def chestdiag(copies, report, negative=False):
    """Diagnostic (playtest 21.10.): who rolls a chest's or a trader's inventory from its treasure sets, and what each
    machine shows when the client opens first and the host second (and the other way round)."""
    host, client = join_both(copies)
    if negative:  # does the client open a chest first without the container sync?
        report_line(client.bot, 'containersyncoff', 'CONTAINERSYNCOFF ')
    clear_raid(host, report)
    time.sleep(5)
    h = chests(host.bot)
    report['host_chests'] = [f"{c['guid'][:8]} {c['distance_cm']}cm gen={c['gen_plunder']} locked={c['locked']} sets={c['sets']} items={c['items']}" for c in h][:12]
    fresh = [c['guid'] for c in h if c['sets'] != '-' and c['gen_plunder'] == '0' and c['locked'] != '1']
    report['fresh'] = len(fresh)
    def both(guid):
        return {'host': invstate(host.bot, guid), 'client': invstate(client.bot, guid)}
    def short(state):
        return {k: f"gen={v['gen_plunder']}/{v['gen_trade']} items={v['items']}" for k, v in state.items()}
    def hero(bot, guid):
        return re.search(r'hero_state=(\S+)', report_line(bot, 'useinfo '+guid, 'USEINFO '))[1]
    def watch(bot_opener, guid, seconds):
        """Contents on both machines every half second while bot_opener has the chest open."""
        seen = []
        for _ in range(seconds * 2):
            state = short(both(guid))
            if not seen or seen[-1][1] != state:
                seen.append((hero(bot_opener.bot, guid), state))
            time.sleep(0.5)
        return seen
    for order in ('client_first', 'host_first'):
        first, second = (client, host) if order == 'client_first' else (host, client)
        for guid in list(fresh):
            steps = {'before': short(both(guid))}
            # The player's way: walk up and press the action key (PS_Interact on the chest), not PlayerOpenLoot.
            report_line(first.bot, 'use '+guid, 'USE ')
            for _ in range(16):  # the walk to the chest's front comes first (PS_Interact)
                time.sleep(0.5)
                if hero(first.bot, guid) != 'PS_Interact':
                    break
            if 'Interact_Chest' not in hero(first.bot, guid):
                report.setdefault('unreachable', []).append(f'{order} {guid[:8]}')
                fresh.remove(guid)
                continue
            fresh.remove(guid)
            steps['first'] = watch(first, guid, 6)
            post_key(first.process.pid, 0x1B)
            time.sleep(2)
            report_line(second.bot, 'use '+guid, 'USE ')
            steps['second'] = watch(second, guid, 6)
            post_key(second.process.pid, 0x1B)
            time.sleep(3)
            steps['closed_3s'] = short(both(guid))
            report[order+'_'+guid[:8]] = steps
            break
    # Traders: living NPCs near the host with treasure sets.
    traders = []
    for guid, n in npcs(host.bot).items():
        if n['dead'] or len(guid) != 36:
            continue
        st = invstate(host.bot, guid)
        if 'Trader' in st.get('sets', '-'):
            traders.append((guid, n['name'], st['sets']))
    report['npcs_with_sets'] = [f'{n} {s}' for _, n, s in traders][:15]
    if traders:
        guid, name, _ = traders[0]
        steps = {'before': short(both(guid))}
        report_line(client.bot, 'tradeopen '+guid, 'TRADEOPEN ')
        time.sleep(3)
        steps['client_trades_3s'] = short(both(guid))
        post_key(client.process.pid, 0x1B)
        report_line(host.bot, 'tradeopen '+guid, 'TRADEOPEN ')
        time.sleep(3)
        steps['host_trades_3s'] = short(both(guid))
        post_key(host.process.pid, 0x1B)
        time.sleep(3)
        steps['closed_3s'] = short(both(guid))
        report['trader_'+name] = steps
    report['host_sync'] = containersync(host.bot)
    report['client_sync'] = containersync(client.bot)
    report['status_note'] = 'diagnostic only'


def open_chest(bot, guid):
    """The player's way (action key, PS_Interact -> PS_Interact_Chest); False if the hero never got to the chest."""
    report_line(bot, 'use '+guid, 'USE ')
    for _ in range(16):
        time.sleep(0.5)
        state = re.search(r'hero_state=(\S+)', report_line(bot, 'useinfo '+guid, 'USEINFO '))[1]
        if state != 'PS_Interact':
            break
    return 'Interact_Chest' in state


def chest_items(bot, guid):
    items = invstate(bot, guid)['items']
    return {} if items == '-' else {k: int(v) for k, v in (x.split(':') for x in items.split(','))}


def stageChest(copies, report, negative=False):
    """Chests (playtest 21.10.): only the host rolls a chest from its treasure sets. A client opening a chest first gets
    the host's roll, what it takes is gone on the host, and the host opening it later rolls nothing new; a client
    opening a chest the host rolled before adds nothing of its own."""
    host, client = join_both(copies)
    if negative:
        report_line(client.bot, 'containersyncoff', 'CONTAINERSYNCOFF ')
    clear_raid(host, report)
    time.sleep(5)
    fresh = [c['guid'] for c in chests(host.bot) if c['sets'] != '-' and c['gen_plunder'] == '0' and c['locked'] != '1']
    report['fresh'] = len(fresh)
    assert len(fresh) >= 2, f'{len(fresh)} unrolled chests near the start'
    # 1. The client opens a chest first.
    a = next((g for g in fresh if open_chest(client.bot, g)), None)
    assert a, 'the client got to no chest'
    fresh.remove(a)
    report['a'] = a
    try:
        wait_for(lambda: chest_items(host.bot, a) and chest_items(client.bot, a) == chest_items(host.bot, a), 8,
                 "the host rolled the client's chest and the client shows it")
    finally:
        report['a_opened'] = {'host': invstate(host.bot, a), 'client': invstate(client.bot, a)}
    rolled = chest_items(host.bot, a)
    # Where the client stood at the chest: its front, where the host starts later (from behind, PS_Interact found no way).
    front = tuple(map(float, re.search(r'hero_pos=(\S+)', report_line(client.bot, 'useinfo '+a, 'USEINFO '))[1].split('_')))
    name, amount = loottake(client.bot, a)
    report['a_client_took'] = f'{name}:{amount}'
    assert amount > 0
    wait_for(lambda: chest_items(host.bot, a).get(name, 0) == rolled.get(name, 0) - amount, 10, "the client's take shows on the host")
    post_key(client.process.pid, 0x1B)
    after_take = chest_items(host.bot, a)
    time.sleep(2)
    teleport_to(client.bot, 'Diego')  # the client's puppet stood on the chest's front, where the host has to go
    time.sleep(2)
    move_hero(host.bot, (front[0], front[1] + 50, front[2]))
    time.sleep(1)
    # The host opens it later: nothing new rolled.
    if not open_chest(host.bot, a):
        report['a_host_useinfo'] = report_line(host.bot, 'useinfo '+a, 'USEINFO ')
        report['a_client_useinfo'] = report_line(client.bot, 'useinfo '+a, 'USEINFO ')
        raise AssertionError('the host got not to the chest')
    time.sleep(3)
    report['a_host_opened'] = {'host': chest_items(host.bot, a), 'client': chest_items(client.bot, a)}
    post_key(host.process.pid, 0x1B)
    assert chest_items(host.bot, a) == after_take, f'host rolled again: {chest_items(host.bot, a)} after {after_take}'
    wait_for(lambda: chest_items(client.bot, a) == after_take, 8, 'the client agrees afterwards')
    # 2. The host opens another chest first; the client opens it after.
    b = next((g for g in fresh if open_chest(host.bot, g)), None)
    assert b, 'the host got to no chest'
    report['b'] = b
    front_b = tuple(map(float, re.search(r'hero_pos=(\S+)', report_line(host.bot, 'useinfo '+b, 'USEINFO '))[1].split('_')))
    time.sleep(3)
    post_key(host.process.pid, 0x1B)
    time.sleep(1)
    teleport_to(host.bot, 'Diego')
    b_rolled = chest_items(host.bot, b)
    report['b_rolled'] = b_rolled
    wait_for(lambda: chest_items(client.bot, b) == b_rolled, 8, "the client shows the host's roll")
    move_hero(client.bot, (front_b[0], front_b[1] + 50, front_b[2]))
    time.sleep(1)
    assert open_chest(client.bot, b), 'the client got not to the second chest'
    samples = []
    for _ in range(12):
        samples.append(chest_items(client.bot, b))
        time.sleep(0.5)
    extra = [x for x in samples if any(v > b_rolled.get(k, 0) for k, v in x.items())]
    report['b_client_extra'] = extra[:3]
    assert not extra, f'the client rolled on top of the host: {extra[0]}'
    name, amount = loottake(client.bot, b)
    report['b_client_took'] = f'{name}:{amount}'
    wait_for(lambda: chest_items(host.bot, b).get(name, 0) == b_rolled.get(name, 0) - amount, 10, "the client's take shows on the host")
    post_key(client.process.pid, 0x1B)
    report['host_sync'] = containersync(host.bot)
    report['client_sync'] = containersync(client.bot)


def quicklootdiag(copies, report, negative=False):
    """Diagnostic for 1.2: does a posted right click quick-loot the faced corpse or chest (QuickLoot=true in ge3.ini),
    and does the take reach the host?"""
    from bot import post_rclick
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(8)
    h = containers(host.bot)
    corpses = [g for g, c in h.items() if c['kind'] == 'corpse' and c['items'] != '-']
    report['corpses'] = len(corpses)
    for who, guid in zip(('host', 'client', 'client'), corpses[:3]):
        player = host if who == 'host' else client
        steps = {'who': who, 'before': {'host': contents(host.bot, guid), 'client': contents(client.bot, guid)}}
        steps['face'] = report_line(player.bot, 'face '+guid, 'FACE ')
        time.sleep(1.5)
        steps['useinfo'] = report_line(player.bot, 'useinfo '+guid, 'USEINFO ').split(' hero_task=')[1]
        steps['clicks'] = post_rclick(player.process.pid)
        time.sleep(3)
        steps['after'] = {'host': contents(host.bot, guid), 'client': contents(client.bot, guid)}
        steps['client_sync'] = containersync(client.bot)
        report['corpse_'+guid[:8]] = steps
    report['host_sync'] = containersync(host.bot)
    report['status_note'] = 'diagnostic only'


def quickloot(bot):
    return report_line(bot, 'quickloot', 'QUICKLOOT ')


def stageQuickLoot(copies, report, negative=False):
    """Quick loot (1.2, ge3.ini QuickLoot=true): the game's own quick loot action (queued the way Use2 queues it in
    PS_Normal) takes the faced corpse's stacks. Host first, then a client: what the client takes is gone on the host
    as well, and both machines end with the same contents."""
    host, client = join_both(copies)
    if negative:
        report_line(client.bot, 'containersyncoff', 'CONTAINERSYNCOFF ')
    clear_raid(host, report)
    time.sleep(8)
    h = containers(host.bot)
    corpses = [g for g, c in h.items() if c['kind'] == 'corpse' and c['items'] != '-']
    report['corpses'] = len(corpses)
    assert len(corpses) >= 2, f'{len(corpses)} corpses with plunder after the raid'
    def faced(player, guid):
        # face stands the hero on the side it comes from; corpses lie in heaps and a neighbour or a dropped weapon
        # takes the focus (22.10.), so try the corpse from all four sides.
        pos = npcs(host.bot).get(guid, {}).get('pos')
        seen = []
        for dx, dz in ((0, 0), (300, 0), (-300, 0), (0, 300), (0, -300)):
            if (dx or dz) and pos:
                move_hero(player.bot, (pos[0] + dx, pos[1] + 50, pos[2] + dz))
                time.sleep(1)
            report_line(player.bot, 'face '+guid, 'FACE ')
            for _ in range(2):
                time.sleep(1)
                line = report_line(player.bot, 'useinfo '+guid, 'USEINFO ')
                if 'focus_is_it=1' in line:
                    return True
            seen.append(line.split(' focus=')[1].split(' ')[0])
            if not pos:
                break
        return ','.join(seen)

    for who in ('host', 'client'):
        player = host if who == 'host' else client
        # Someone standing in front of a corpse takes the focus (Hamlar, 22.10.): take the next corpse then.
        misses = []
        while corpses:
            guid = corpses.pop(0)
            if contents(host.bot, guid) and (result := faced(player, guid)) is True:
                break
            misses.append(f"{h[guid]['name']}: {result if contents(host.bot, guid) else 'empty'}")
        else:
            raise AssertionError(f'the {who} could face no corpse with plunder ({misses})')
        step = {'name': h[guid]['name'], 'before': contents(host.bot, guid), 'misses': misses}
        report[who] = step
        if who == 'client' and not negative:
            wait_for(lambda: contents(client.bot, guid) == step['before'], 20, "the client shows the host's contents")
        sync = containersync(player.bot)
        step['queued'] = wait_for(lambda: (lambda l: l if 'queued=1' in l else None)(quickloot(player.bot)), 10,
                                  f'the {who} queues quick loot')
        step['actions_after'] = wait_for(lambda: (lambda l: l if 'count=0' in l else None)(
            report_line(player.bot, 'quickloot count', 'QUICKACTIONS ')), 5, f"the {who}'s game takes the action")
        step['host_after'] = wait_for(lambda: (lambda c: [c] if c != step['before'] else None)(contents(host.bot, guid)), 15,
                                      f"the host's corpse lost what the {who} quick-looted")[0]  # [{}]: all taken
        assert sum(step['host_after'].values()) < sum(step['before'].values()), f'the {who} quick loot added items'
        wait_for(lambda: contents(client.bot, guid) == contents(host.bot, guid), 15, 'both machines show the same rest')
        step['rest'] = contents(host.bot, guid)
        after = containersync(player.bot)
        step['sync'] = {k: after[k] - sync.get(k, 0) for k in ('quick_takes', 'quick_refused', 'tx_sent', 'tx_refused')}
        if who == 'client':
            assert step['sync']['quick_takes'] >= 1, 'the quick loot hook did not see the take'
            assert step['sync']['tx_refused'] == 0, 'the host refused the quick loot'
            # One take is one transaction (12 on 22.10. while "Fist:-1" went out every poll).
            assert step['sync']['tx_sent'] <= 3, f"{step['sync']['tx_sent']} transactions for one quick loot"
    report['host_sync'] = containersync(host.bot)


def summon_short(line):
    """SUMMON line as name owner/puppet_owner task at."""
    f = dict(re.findall(r'(\w+)=(\S+)', line))
    return f"{f.get('name')} owner={f.get('owner')}/{f.get('puppet_owner')} task={f.get('task')} at={f.get('at')}"


def areaspelldiag(copies, report, negative=False):
    """P6 (rest of the magic, 26.10.): what the area spells do when the other player casts them. For each spell of G3_SPELLS
    (default mist, light, time bubble, fire rain) the host casts it at a villager it simulates (as in single-player), then
    the client at another one: for 12 s both machines' Smn_ objects (owner, task, position), the tasks of the host's NPCs
    within 12 m of the target (and of the client's mirrors of them), and the target's hit points. Diagnostic."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    keep = ('Diego', 'Gorn', 'Milten', 'Lester')
    spells = (os.environ.get('G3_SPELLS') or 'It_Spell_Mist,It_Spell_Light,It_Spell_TimeBubble,It_Spell_Firerain').split(',')
    order = ((client, host),) if os.environ.get('G3_CASTER') == 'client' else ((host, client), (client, host))
    used = set()
    for spell in spells:
        for caster, watcher in order:
            tag = f"{spell.replace('It_Spell_', '')}_{caster.tag}"
            living = {g: v for g, v in npcs(host.bot).items() if not v['dead'] and not v['mirrored'] and 'Orc' not in v['name']
                      and v['name'] not in keep and v['name'] != 'PC_Hero' and g not in used}
            if not living:
                report[tag+'_skipped'] = 'no villager left near the host'
                continue
            # The villager with the most others within 10 m: the mist and the bubble act on everyone round the target.
            target = max(living, key=lambda g: sum(1 for v in living.values() if distance(v['pos'], living[g]['pos']) < 1000))
            used.add(target)
            goal = living[target]['pos']
            report[tag+'_target'] = living[target]['name']
            move_hero(caster.bot, (goal[0] + 400, goal[1] + 50, goal[2]))
            move_hero(watcher.bot, (goal[0] + 400, goal[1] + 50, goal[2] + 500))
            time.sleep(3)
            near = {g for g, v in npcs(host.bot).items() if distance(v['pos'], goal) < 1200}
            tasks = lambda bot: {f"{v['name']}:{g[:4]}": v['task'] for g, v in npcs(bot).items() if g in near}
            report[tag+'_before'] = tasks(host.bot)
            hp0 = npcs(host.bot)[target]['hp']
            report_line(caster.bot, 'mana', 'MANA ')
            report[tag+'_ready'] = report_line(caster.bot, 'ready '+spell, 'READY ')
            report_line(caster.bot, 'aimat '+target, 'TARGET ')
            report[tag+'_cast'] = report_line(caster.bot, 'castat '+target, 'CASTAT ')[7:300]
            samples = []
            for i in range(12):
                time.sleep(1)
                h, c = tasks(host.bot), tasks(client.bot)
                samples.append({'t': i + 1, 'caster': [summon_short(l) for l in summons(caster.bot)],
                                'watcher': [summon_short(l) for l in summons(watcher.bot)],
                                'host_npcs': {k: v for k, v in h.items() if v != report[tag+'_before'].get(k)},
                                'client_mirrors': {k: v for k, v in c.items() if v != report[tag+'_before'].get(k)}})
            report[tag+'_samples'] = samples
            report[tag+'_hp'] = (hp0, npcs(host.bot).get(target, {}).get('hp'))
            # Let it end before the next one (a mist lasts about 20 s).
            until = time.monotonic() + 40
            while time.monotonic() < until and any('Smn_' in l for l in summons(caster.bot) + summons(watcher.bot)):
                time.sleep(2)
            report[tag+'_left'] = {'caster': [summon_short(l) for l in summons(caster.bot)], 'watcher': [summon_short(l) for l in summons(watcher.bot)]}
    report['summonstate'] = {c.tag: summon_state(c.bot)[0] for c in (host, client)}
    report['status_note'] = 'diagnostic only'


def stageAreaSpells(copies, report, negative=False):
    """P6 (rest of the magic, 26.10.): the other player's mist, light and time bubble act as in single-player.
    areaspelldiag 26.10.: the watcher's copy of a summoned spell object (Smn_Mist, Smn_Light, Smn_Timebubble) runs the same
    states as the caster's, owned by the caster's puppet; the copy of a client's mist ticks on the host, which simulates
    the NPCs, so they turn ZS_Misted there exactly as for the host's own mist. Checked: the host's mist mists its villagers
    (the single-player reference); the client's mist mists the host's villagers too, and its copy goes when the client's
    mist ends; the client's light has a copy on the host owned by its puppet that stays with the light; the client's
    time bubble has a copy at its place on the host with the time zone (property set 112) that slows what is inside.
    Negative control: summonmirroroff on both (no copies: nothing happens on the host)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    if negative:
        report['negative'] = [report_line(c.bot, 'summonmirroroff', 'SUMMONMIRROROFF') for c in (host, client)]
    keep = ('Diego', 'Gorn', 'Milten', 'Lester')
    used, problems = set(), []
    field = lambda l, k: (re.search(rf'\b{k}=(\S+)', l) or [None, None])[1]
    at = lambda l: tuple(float(v) for v in field(l, 'at').split('_'))

    def cast(caster, watcher, spell, tag):
        living = {g: v for g, v in npcs(host.bot).items() if not v['dead'] and not v['mirrored'] and 'Orc' not in v['name']
                  and v['name'] not in keep and v['name'] != 'PC_Hero' and g not in used}
        assert living, f'{tag}: no villager left near the host'
        target = max(living, key=lambda g: sum(1 for v in living.values() if distance(v['pos'], living[g]['pos']) < 1000))
        used.add(target)
        goal = living[target]['pos']
        report[tag+'_target'] = living[target]['name']
        move_hero(caster.bot, (goal[0] + 400, goal[1] + 50, goal[2]))
        move_hero(watcher.bot, (goal[0] + 400, goal[1] + 50, goal[2] + 500))
        time.sleep(3)
        near = {g for g, v in npcs(host.bot).items() if distance(v['pos'], goal) < 1000 and not v['dead']}
        report_line(caster.bot, 'mana', 'MANA ')
        report_line(caster.bot, 'ready '+spell, 'READY ')
        report_line(caster.bot, 'aimat '+target, 'TARGET ')
        report[tag+'_cast'] = report_line(caster.bot, 'castat '+target, 'CASTAT ')[7:200]
        return near

    kinds = lambda bot, name: [l for l in summons(bot) if f'name={name} ' in l]
    # 1. Mist, the host's (reference) and the client's: the villagers round it turn ZS_Misted on the host.
    for caster, watcher in ((host, client), (client, host)):
        tag = 'mist_' + caster.tag
        near = cast(caster, watcher, 'It_Spell_Mist', tag)
        misted, copy_seen, own_gone, copy_gone = set(), [], None, None
        for i in range(30):
            time.sleep(1)
            misted |= {npcs(host.bot)[g]['name'] for g in near if npcs(host.bot).get(g, {}).get('task') == 'ZS_Misted'}
            own, copy = kinds(caster.bot, 'Smn_Mist'), kinds(watcher.bot, 'Smn_Mist')
            copy_seen += copy[:1]
            if own_gone is None and not own and i > 0:
                own_gone = i
            if copy_gone is None and not copy and copy_seen:
                copy_gone = i
            if own_gone is not None and copy_gone is not None:
                break
        report[tag+'_misted'] = sorted(misted)
        report[tag+'_copy'] = [summon_short(l) for l in copy_seen[:2]]
        report[tag+'_ends'] = {'own_gone_s': own_gone, 'copy_gone_s': copy_gone}
        if not misted:
            problems.append(f'{tag}: no villager of the host was misted')
        if caster is client:
            if not copy_seen or field(copy_seen[0], 'puppet_owner') != '2':
                problems.append(f'{tag}: the host has no copy of the mist owned by the client\'s puppet')
            elif copy_gone is None or own_gone is None or abs(copy_gone - own_gone) > 3:
                problems.append(f'{tag}: the copy did not end with the mist ({report[tag+"_ends"]})')
    # 2. The client's light: a copy on the host owned by its puppet, where the light is.
    cast(client, host, 'It_Spell_Light', 'light')
    gaps = []
    for _ in range(5):
        time.sleep(1)
        own, copy = kinds(client.bot, 'Smn_Light'), kinds(host.bot, 'Smn_Light')
        own = [l for l in own if field(l, 'puppet_owner') == '0']
        copy = [l for l in copy if field(l, 'puppet_owner') == '2']
        gaps.append(round(distance(at(own[0]), at(copy[0]))) if own and copy else None)
    report['light_gaps_cm'] = gaps
    if None in gaps or max(gaps) > 300:
        problems.append(f'light: the host\'s copy is missing or away from the client\'s light ({gaps})')
    # 3. The client's time bubble: a copy at its place on the host, with the time zone.
    cast(client, host, 'It_Spell_TimeBubble', 'bubble')
    time.sleep(2)
    own = [l for l in kinds(client.bot, 'Smn_Timebubble') if field(l, 'puppet_owner') == '0']
    copy = [l for l in kinds(host.bot, 'Smn_Timebubble') if field(l, 'puppet_owner') == '2']
    report['bubble'] = {'own': own[:1], 'copy': copy[:1]}
    if not own:
        problems.append('bubble: the client has no time bubble')
    elif not copy or distance(at(own[0]), at(copy[0])) > 200 or '112,' not in (field(copy[0], 'sets') or ''):
        problems.append('bubble: the host has no copy with a time zone where the client\'s bubble is')
    report['summonstate'] = {c.tag: summon_state(c.bot)[0] for c in (host, client)}
    report['problems'] = problems
    assert not problems, '; '.join(problems)


def held_lines(bot):
    start = bot.send('remotes', 20)
    bot.line('REMOTES end', 10, 'remotes', start)
    return [l for l in bot.log()[start:].splitlines() if l.startswith('HELD ')]


def flamesworddiag(copies, report, negative=False):
    """P6 (PS_Magic_FlameSword): the client conjures the flame sword the player's way (ready the spell, then the game's
    state PS_Magic_FlameSword through QA act), then, after a pause, by castat; after each, for 6 s: what the client says
    it holds and what its puppet holds on the host (QA remotes, HELD). Diagnostic."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    move_hero(client.bot, hero_pos(host.bot))
    time.sleep(2)
    spell = os.environ.get('G3_SPELL', 'It_Spell_Flamesword')
    for way in ('act', 'castat'):
        report_line(client.bot, 'mana', 'MANA ')
        report[way+'_ready'] = report_line(client.bot, 'ready '+spell, 'READY ')
        if way == 'act':
            report[way] = report_line(client.bot, 'act PS_Magic-PS_Magic_FlameSword', ('ACT ', 'REFUSED act'))[:200]
        else:
            target = calm_target(host)
            report[way] = report_line(client.bot, 'castat '+target, 'CASTAT ')[7:220]
        samples = []
        for _ in range(6):
            time.sleep(1)
            samples.append(held_lines(host.bot)[:2])
        report[way+'_held_on_host'] = samples
        report[way+'_client_inventory'] = [l[4:120] for l in report_line(client.bot, 'inventory', 'INV end') and
                                          client.bot.log().splitlines()[-80:] if l.startswith('INV name=') and 'lame' in l]
        report_line(client.bot, 'ready It_Spell_Light', 'READY ')
        time.sleep(4)
    report['status_note'] = 'diagnostic only'


def stageFlameSword(copies, report, negative=False):
    """P6 (PS_Magic_FlameSword): the client conjures the flame sword the player's way (ready the spell, then the game's state
    PS_Magic_FlameSword through QA act); the sword (It_1H_MagicFlameSword_01) is in its hand, and its puppet on the host holds
    the same sword within 6 s (flamesworddiag 26.10.). Negative control: puppetholdoff on the host (the puppet's hands are not
    set from what the player holds)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    if negative:
        report['negative'] = report_line(host.bot, 'puppetholdoff', 'PUPPETHOLDOFF')
    move_hero(client.bot, hero_pos(host.bot))
    time.sleep(2)
    report_line(client.bot, 'mana', 'MANA ')
    report['ready'] = report_line(client.bot, 'ready It_Spell_Flamesword', 'READY ')
    report['act'] = report_line(client.bot, 'act PS_Magic-PS_Magic_FlameSword', ('ACT ', 'REFUSED act'))[:200]
    sword = 'It_1H_MagicFlameSword_01'
    held = []
    for _ in range(6):
        time.sleep(1)
        held = held_lines(host.bot)
        if any(f'remote={sword} puppet={sword}' in l for l in held):
            break
    report['held_on_host'] = held
    assert any(f'remote={sword} ' in l for l in held), f'the client does not hold the flame sword: {held}'
    assert any(f'puppet={sword}' in l for l in held), f"the client's puppet on the host does not hold the flame sword: {held}"


def stageMeleeSwing(copies, report, negative=False):
    """Basis (OnHit, OnTouchDamage, DoLogicalDamage): the client strikes a villager the host simulates with real sword blows
    (QA swing: the game's PS_Melee_Attack, as the attack key), not the QA hit, so the game's own melee path runs on the client
    against its mirror and the combat filter sends the result to the host as the player's claim. Checked: the client's claims
    count up and the villager loses hit points on the host. Reported: which hit scripts the script table called on the client
    (QA scriptwatch; 26.10.: OnHit and OnTouchDamage, DoLogicalDamage and AssessHit are called directly, not through the
    table) and AddHitPoints' callers (QA hitstacks). Negative control: combatoff on the client (no filter: the blows stay on
    the client's mirror)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    if negative:
        report['negative'] = report_line(client.bot, 'combatoff', 'COMBATOFF')
    target = calm_target(host)
    name = report['target'] = npcs(host.bot)[target]['name']
    hp0 = npcs(host.bot)[target]['hp']
    goal = npcs(host.bot)[target]['pos']
    report['arm'] = report_line(client.bot, 'arm ' + WEAPON, 'ARM ')
    # Which of the game's hit scripts run on the client (QA scriptwatch counts their calls through the script table).
    watch = ('OnHit', 'OnTouchDamage', 'DoLogicalDamage', 'AssessHit')
    report['watch'] = [report_line(client.bot, 'scriptwatch ' + n, 'SCRIPTWATCH ') for n in watch]
    before = report['before'] = hp_counters(client.bot)
    swings = []
    for i in range(20):
        now = npcs(host.bot).get(target, {}).get('pos', goal)
        move_hero(client.bot, (now[0] + 110, now[1] + 30, now[2]))
        report_line(client.bot, 'aimat ' + target, 'TARGET ')
        line = report_line(client.bot, 'swing', 'SWING ')
        if i < 3:
            swings.append(line[6:160])
        time.sleep(1.4)
        state = npcs(host.bot).get(target, {})
        if state.get('hp', hp0) < hp0 or state.get('dead'):
            break
    report['swings'] = swings
    report['tries'] = i + 1
    after = report['after'] = hp_counters(client.bot)
    report['host_view'] = npcs(host.bot).get(target, {})
    report['client_view'] = npcs(client.bot).get(target, {})
    start = client.bot.send('scriptwatch', 20)
    client.bot.line('SCRIPTWATCH end', 10, 'scriptwatch', start)
    report['scripts'] = [l[12:200] for l in client.bot.log()[start:].splitlines() if l.startswith('SCRIPTWATCH name=')]
    start = client.bot.send('hitstacks', 20)
    client.bot.line('HITSTACKS end', 10, 'hitstacks', start)
    report['paths'] = [l[9:400] for l in client.bot.log()[start:].splitlines() if l.startswith('HITSTACK ') and f'victim={name} ' in l][:3]
    assert after['claims_sent'] > before['claims_sent'], f"no blow went to the host as the client's claim: {before} -> {after}"
    hp1 = report['host_view'].get('hp', hp0)
    assert hp1 < hp0 or report['host_view'].get('dead'), f"{name} lost no hit points on the host ({hp0} -> {hp1})"


def stageCounterHit(copies, report, negative=False):
    """Basis (OnHit of a mirrored NPC): a wild animal the host simulates (G3_COUNTER_ANIMAL, scavengers north of Ardea, as
    stageControl) attacks the client, who stands still for 30 s (one blow of the client if it does not start). Not the
    post-raid slave any more: struck by the client, a friend now complains (AssessAgressor, combat.cpp, 26.10.), the slave
    complained at every blow and never fought, and the host's AssessTarget did not get it off its bench. The host judges
    the animal's blows on the client's puppet and
    sends them (combat rule 4); the client's mirror of the villager swings too, and OnHit and OnTouchDamage run there (QA
    scriptwatch), but they must not hurt the client's hero a second time. Checked: the client's hero takes the host's blows
    (player_hits_taken as many as the host's player_hits_sent, give or take one in flight) and loses hit points, and it
    stumbles at most once per blow (TRACE motions; counterhitdiag 26.10.: 8 blows, 8 stumbles, 200 -> 96). Negative
    control: combatoff on the host (its blows stay on the puppet)."""
    kind = os.environ.get('G3_COUNTER_ANIMAL', 'Scavenger')
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    if negative:
        report['negative'] = report_line(host.bot, 'combatoff', 'COMBATOFF')
    move_hero(host.bot, (90500.0, 5300.0, -6600.0))  # the host first: it simulates the animals
    herd = wait_for(lambda: [g for g, v in npcs(host.bot).items() if v['name'].startswith(kind) and not v['dead'] and not v['mirrored']],
                    20, f'a {kind} near the host')
    target = herd[0]
    name = npcs(host.bot)[target]['name']
    report['target'] = name
    goal = npcs(host.bot)[target]['pos']
    move_hero(host.bot, (goal[0] + 2500, goal[1] + 30, goal[2]))  # 25 m off: the fight is the client's
    report_line(client.bot, 'sethp 1000', 'SETHP ')
    move_hero(client.bot, (goal[0] + 150, goal[1] + 30, goal[2]))
    for n in ('OnHit', 'OnTouchDamage'):
        report_line(client.bot, 'scriptwatch ' + n, 'SCRIPTWATCH ')
    hp_line = lambda c: dict(re.findall(r' (\w+)=(-?\d+)', report_line(c.bot, 'hp', 'HP ')))
    before = {c.tag: hp_line(c) for c in (host, client)}
    mark = len(client.mod_log())
    fighting = lambda: npcs(host.bot).get(target, {}).get('task') == 'ZS_Attack' and ['attack']

    def provoke():
        for _ in range(3):
            try:
                wait_for(fighting, 6, name + ' attacks')
                break
            except AssertionError:  # one blow of the client: an animal is no friend, it fights back
                report_line(client.bot, 'arm ' + WEAPON, 'ARM ')
                report_line(client.bot, 'aimat ' + target, 'TARGET ')
                report_line(client.bot, 'swing', 'SWING ')
                time.sleep(1.4)
                report_line(client.bot, 'act PS_Normal-PS_Normal_Loop', ('ACT ', 'REFUSED act'))  # weapon away: it only takes the blows
        return npcs(host.bot).get(target, {}).get('task')
    report['provoked'] = provoke()
    # 30 s of blows; an animal that gives up is set on the client once more half-way.
    for half in range(2):
        time.sleep(15)
        if half == 0 and npcs(host.bot).get(target, {}).get('task') != 'ZS_Attack':
            report['provoked_again'] = provoke()
    after = {c.tag: hp_line(c) for c in (host, client)}
    keys = ('hero', 'dropped', 'player_hits_taken', 'player_hits_sent', 'claims_sent')
    report['counters'] = {t: {k: (before[t].get(k), after[t].get(k)) for k in keys} for t in ('host', 'client')}
    motions = re.findall(r'TRACE hero .*?motion=(\S+)', client.mod_log()[mark:])
    reacts = [m for m in motions if any(w in m for w in ('Stumble', 'Hit_', '_Parade', 'Hurt')) and '_Attack_' not in m]
    starts, last = [], None
    for m in motions:
        hit = any(w in m for w in ('Stumble', 'Hit_', '_Parade', 'Hurt')) and '_Attack_' not in m  # not its own blows
        if hit and last is False:
            starts.append(m[:70])
        last = hit
    report['reaction_samples'] = len(reacts)
    report['reaction_starts'] = starts[:12]
    start = client.bot.send('scriptwatch', 20)
    client.bot.line('SCRIPTWATCH end', 10, 'scriptwatch', start)
    report['scripts'] = [l[12:200] for l in client.bot.log()[start:].splitlines() if l.startswith('SCRIPTWATCH name=')]
    report['host_task'] = npcs(host.bot).get(target, {}).get('task')
    c, h = report['counters']['client'], report['counters']['host']
    taken = int(c['player_hits_taken'][1]) - int(c['player_hits_taken'][0])
    sent = int(h['player_hits_sent'][1]) - int(h['player_hits_sent'][0])
    assert taken >= 2, f"the client took {taken} blows of the host ({report['counters']})"
    assert abs(taken - sent) <= 1, f'the host sent {sent} blows, the client took {taken}'
    assert int(c['hero'][1]) < int(c['hero'][0]), f"the client's hero lost no hit points: {c['hero']}"
    assert len(starts) <= taken + 1, f'{len(starts)} stumbles for {taken} blows: the mirror hurt the hero as well'



def counterhitdiag(copies, report, negative=False):
    """Basis (OnHit of a mirrored NPC): the client strikes a villager the host simulates once (as stageMeleeSwing), then
    stands still for 20 s while the villager fights back. The host judges the villager's blows on the client's puppet and
    sends them (rule 4); the client's mirror of the villager swings too, and its OnHit runs there (QA scriptwatch). Reported:
    the client's hit counters (dropped: the mirror's own blows, rule 1; player_hits_taken: the host's), the host's
    player_hits_sent, the client hero's hit points, and how often the client's hero showed a stumble or hit motion (TRACE).
    Diagnostic."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    target = calm_target(host)
    report['target'] = npcs(host.bot)[target]['name']
    for n in ('OnHit', 'OnTouchDamage'):
        report_line(client.bot, 'scriptwatch ' + n, 'SCRIPTWATCH ')
    report['arm'] = report_line(client.bot, 'arm ' + WEAPON, 'ARM ')
    hp_line = lambda c: dict(re.findall(r' (\w+)=(-?\d+)', report_line(c.bot, 'hp', 'HP ')))
    for i in range(10):
        now = npcs(host.bot).get(target, {}).get('pos')
        move_hero(client.bot, (now[0] + 110, now[1] + 30, now[2]))
        report_line(client.bot, 'aimat ' + target, 'TARGET ')
        report_line(client.bot, 'swing', 'SWING ')
        time.sleep(1.4)
        if npcs(host.bot).get(target, {}).get('task') == 'ZS_Attack':
            break
    report['provoked'] = npcs(host.bot).get(target, {}).get('task')
    report_line(client.bot, 'act PS_Normal-PS_Normal_Loop', ('ACT ', 'REFUSED act'))  # weapon away: it only takes the blows
    before = {c.tag: hp_line(c) for c in (host, client)}
    mark = len(client.mod_log())
    time.sleep(20)
    after = {c.tag: hp_line(c) for c in (host, client)}
    keys = ('hero', 'dropped', 'player_hits_taken', 'player_hits_sent', 'claims_sent')
    report['counters'] = {t: {k: (before[t].get(k), after[t].get(k)) for k in keys} for t in ('host', 'client')}
    motions = re.findall(r'TRACE hero .*?motion=(\S+)', client.mod_log()[mark:])
    reacts = [m for m in motions if any(w in m for w in ('Stumble', 'Hit_', '_Parade', 'Hurt'))]
    starts, last = [], None
    for m in motions:
        hit = any(w in m for w in ('Stumble', 'Hit_', '_Parade', 'Hurt'))
        if hit and last is False:
            starts.append(m[:70])
        last = hit
    report['reaction_samples'] = len(reacts)
    report['reaction_starts'] = starts[:12]
    start = client.bot.send('scriptwatch', 20)
    client.bot.line('SCRIPTWATCH end', 10, 'scriptwatch', start)
    report['scripts'] = [l[12:200] for l in client.bot.log()[start:].splitlines() if l.startswith('SCRIPTWATCH name=')]
    report['host_task'] = npcs(host.bot).get(target, {}).get('task')
    report['status_note'] = 'diagnostic only'


def animalsleepdiag(copies, report, negative=False):
    """Basis (OnAnimalSleep): where do animals sleep, and does the client wake one? Both go to G3_SPOT (default Kent: deer,
    scavengers, rippers); the host sets the clock to G3_HOUR (default 23); every 5 s for 40 s the animals near the host with
    their tasks. Then the client walks up to the nearest sleeping one the host simulates while the host stands 25 m off; the
    animal's task on the host over 20 s. Diagnostic."""
    host, client = join_both(copies)
    clear_raid(host, report)
    spot = os.environ.get('G3_SPOT', 'Kent')
    for c in (host, client):
        report[c.tag + '_jump'] = teleport_to(c.bot, spot)[:100]
    report['clock'] = report_line(host.bot, f"clockset {os.environ.get('G3_HOUR', '23')}_00", 'CLOCK ')
    kinds = ('Scavenger', 'Deer', 'Ripper', 'Okara_Ripper', 'Wolf', 'Boar', 'Molerat', 'Rabbit', 'Bison', 'Lurker', 'Snapper')
    seen = []
    sleeper = None
    hours = [h for h in os.environ.get('G3_HOURS', '23,2,5,13').split(',')]
    for k in range(8):
        if k % 2 == 0 and hours:
            # Another hour, and every animal near picks its routine's task for it anew (QA npcroutine: FullStop, ContinueRoutine).
            report_line(host.bot, f'clockset {int(hours.pop(0)):02d}_00', 'CLOCK ')
            for n in sorted({v['name'] for v in npcs(host.bot).values() if v['name'].startswith(kinds) and not v['dead']}):
                report.setdefault('reroutes', []).append(report_line(host.bot, 'npcroutine ' + n, ('NPCROUTINE ', 'REFUSED npcroutine'))[:90])
        time.sleep(5)
        animals = {g: v for g, v in npcs(host.bot).items() if v['name'].startswith(kinds) and not v['dead']}
        seen.append(sorted({f"{v['name']}:{v['task']}/" + (re.search(r' state=(\S+)', find_npc(host.bot, v['name'])) or [None, '-'])[1]
                            for v in animals.values()}))
        asleep = [g for g, v in animals.items() if not v['mirrored'] and any(f"{v['name']}:" in e and 'Sleep' in e for e in seen[-1])]
        if asleep:
            sleeper = asleep[0]
            break
    report['seen'] = seen
    if not sleeper:
        report['status_note'] = 'diagnostic only: no sleeping animal'
        return
    v = npcs(host.bot)[sleeper]
    report['sleeper'] = (v['name'], v['task'], v['pos'])
    move_hero(host.bot, (v['pos'][0] + 2500, v['pos'][1] + 100, v['pos'][2]))
    time.sleep(3)
    samples = []
    for d in (1500, 900, 500, 300, 200):
        move_hero(client.bot, (v['pos'][0] + d, v['pos'][1] + 50, v['pos'][2]))
        time.sleep(4)
        now = npcs(host.bot).get(sleeper, {})
        samples.append((d, now.get('task'), now.get('mirrored')))
    report['client_near'] = samples
    report['status_note'] = 'diagnostic only'


def sectorrefreshdiag(copies, report, negative=False):
    """Basis (OnEnclaveSectorRefresh): after Ardea is freed the old friends leave (Diego to Braga, Gorn to GothaPrison, Milten
    to the monastery: re/out/basis-rest-20261026-b.c). From the post-raid save (Ardea freed): where Diego, Gorn, Milten and
    Lester are on both machines (QA findnpc: position, task, mirrored); then G3_WHO (default client) goes 400 m off and comes
    back after 15 s, then the other one does; the friends on both after each. Diagnostic."""
    host, client = join_both(copies)
    clear_raid(host, report)
    friends = ('Diego', 'Gorn', 'Milten', 'Lester')
    look = lambda c: {n: re.sub(r'FIND name=\S+ ', '', find_npc(c.bot, n))[:150] for n in friends}
    report['start'] = {c.tag: look(c) for c in (host, client)}
    first = host if os.environ.get('G3_WHO') == 'host' else client
    for tag, goer in (('first', first), ('second', client if first is host else host)):
        x, y, z = hero_pos(goer.bot)
        move_hero(goer.bot, (x + 40000, y + 200, z))
        time.sleep(15)
        report[tag + '_away'] = {c.tag: look(c) for c in (host, client)}
        move_hero(goer.bot, (x, y, z))
        time.sleep(12)
        report[tag + '_back'] = {c.tag: look(c) for c in (host, client)}
        report[tag + '_who'] = goer.tag
    report['status_note'] = 'diagnostic only'


def zoneattitudediag(copies, report, negative=False):
    """P7 (GetZoneAttitudeToPlayer and the rest of GetAttitude's chain): an NPC's attitude to the one standing in its
    guarded place. ai.cpp asks GetAttitude about a puppet as about the local hero, so a zone the puppet stands in might
    count as where the host's hero stands. At G3_PLACE (default Muntasir, Ishtar): (A) the client in the place, the host 30 m
    off; (B) the other way round. Each time the host's QA attitude (to its hero and to puppet 2) for G3_JUDGES (default
    Muntasir and the NPCs within 15 m of him), three samples. Diagnostic."""
    host, client = join_both(copies)
    clear_raid(host, report)
    place = os.environ.get('G3_PLACE', 'Muntasir')
    for phase, inside, outside in (('client_in', client, host), ('host_in', host, client)):
        for c in (host, client):
            teleport_to(c.bot, place)
        x, y, z = hero_pos(outside.bot)
        move_hero(outside.bot, (x + 3000, y + 30, z))
        time.sleep(6)
        at = hero_pos(inside.bot)
        judges = os.environ.get('G3_JUDGES')
        names = judges.split(',') if judges else sorted({v['name'] for v in npcs(host.bot).values()
                                                       if distance(v['pos'], at) < 1500 and not v['dead']})[:8]
        samples = []
        for _ in range(3):
            samples.append({n: re.sub(r'ATTITUDE name=\S+ ', '', report_line(host.bot, 'attitude ' + n, ('ATTITUDE ', 'REFUSED attitude')))[:60]
                            for n in names})
            time.sleep(2)
        report[phase] = samples
        report[phase + '_zone'] = {c.tag: hero_zone(c.bot) for c in (host, client)}
    report['status_note'] = 'diagnostic only'


def zonescandiag(copies, report, negative=False):
    """P7 (GetZoneAttitudeToPlayer): where does an NPC take the one in its zone for a threat? For each spot of G3_SPOTS
    (default Muntasir, Topork, Varek, Brenton, Hamlar) the host stands at the spot NPC, the client 30 m off, then the
    other way round; each time QA zoneatt for the spot NPC and the two nearest others on the host (zone and security of
    hero and puppet, GetZoneAttitudeToPlayer and GetAttitudeToPlayer for the hero, for the puppet plainly and as the
    game's player, GetAttitude to the puppet). Diagnostic."""
    host, client = join_both(copies)
    clear_raid(host, report)
    spots = os.environ.get('G3_SPOTS', 'Muntasir,Topork,Varek,Brenton,Hamlar').split(',')
    for spot in spots:
        for phase, inside, outside in (('host_in', host, client), ('client_in', client, host)):
            tag = f'{spot}_{phase}'
            for c in (host, client):
                report[tag + '_' + c.tag] = teleport_to(c.bot, spot)[:60]
            x, y, z = hero_pos(outside.bot)
            move_hero(outside.bot, (x + 3000, y + 30, z))
            time.sleep(5)
            at = hero_pos(inside.bot)
            near = sorted(((distance(v['pos'], at), v['name']) for v in npcs(host.bot).values() if not v['dead']), key=lambda t: t[0])
            names = [spot] + [n for d, n in near if n != spot][:2]
            report[tag] = [re.sub(r'ZONEATT name=', '', report_line(host.bot, 'zoneatt ' + n, ('ZONEATT ', 'REFUSED zoneatt')))[:260] for n in names]
    report['status_note'] = 'diagnostic only'


def spellfuncdiag(copies, report, negative=False):
    """Plan 3.6: which scripts the spell items run (stageNpcSpell asked for It_Spell_Stun, which does not exist)."""
    host = copies['host'].launch('host', 'Host')
    enter_world(host)
    names = os.environ.get('G3_SPELLS') or ('It_Spell_TimeBubble,It_Spell_Mist,It_Spell_DuskToDawn,It_Spell_FrostWave,It_Spell_Hailstorm,It_Spell_IceBomb,'
             'It_Spell_Sleep,It_Spell_Fear,It_Spell_Charm,It_Spell_WordOfControl,It_Spell_TameAnimal,It_Spell_Telekinesis,'
             'It_Spell_HeatWave,It_Spell_Bloodlust,It_Spell_SoulTravel,It_Spell_DestroyEvil,It_Spell_Icelance')  # G3_SPELLS: other list
    report['spells'] = []
    names = names.split(',')
    for name in names:  # one per command: QA arguments allow no commas
        start = host.bot.send('spellfunc '+name, 20)
        host.bot.line('SPELLFUNC end', 20, 'spellfunc', start)
        report['spells'] += [l for l in host.bot.log()[start:].splitlines() if l.startswith('SPELLFUNC item=')]
    report['status_note'] = 'diagnostic only'


def tradediag(copies, report, negative=False):
    """1.4 (playtest 21.10.: whoever talks to a trader first sees its goods, the other does not): one player talks to
    Hamlar in a real dialog (ZS_Talk fills the trade inventory), then the other; the trade state on both machines after
    each. G3_ORDER=client (default) or host picks who talks first."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(5)
    first = os.environ.get('G3_ORDER', 'client')
    guid = next((g for g, n in npcs(host.bot).items() if n['name'] == 'Hamlar' and not n['dead']), None)
    assert guid, 'Hamlar is not near the start'

    def state():
        out = {}
        for tag, bot in (('host', host.bot), ('client', client.bot)):
            st = invstate(bot, guid)
            out[tag] = f"gen_trade={st.get('gen_trade')} stacks={st.get('generated_stacks')} goods={goods(bot, guid)}"
        return out

    report['before'] = state()
    for who in ((client, host) if first == 'client' else (host, client)):
        talk_to(who, 'Hamlar', report, who.tag+'_partners')
        time.sleep(3)
        report[who.tag+'_talks'] = state()
        start = who.bot.send('endtalk', 20)
        who.bot.line('TALKSTATE ', 10, 'endtalk', start)
        wait_for(lambda: talkstate(who.bot).get('running') == '0', 20, who.tag+' dialog ends')
        time.sleep(4)
        report[who.tag+'_after'] = state()
    report['host_sync'] = containersync(host.bot)
    report['client_sync'] = containersync(client.bot)
    report['status_note'] = 'diagnostic only'


def stageTrade(copies, report, negative=False):
    """1.4 (playtest 21.10.: whoever talked to a trader first saw its goods, the other none): only the host fills a
    trader, as soon as a client comes close. One player talks to Hamlar in a real dialog, then the other; after each,
    both machines show the same goods, and not none. G3_ORDER=client (default) or host picks who talks first. Negative
    control: the host does not fill traders before a dialog (containersyncoff trader on the host)."""
    # The client joins next to Hamlar: the negative control must be on before that.
    host, client = join_both(copies, ('containersyncoff trader', 'CONTAINERSYNCOFF ') if negative else None)
    clear_raid(host, report)
    time.sleep(5)
    first = os.environ.get('G3_ORDER', 'client')
    report['order'] = first
    guid = next((g for g, n in npcs(host.bot).items() if n['name'] == 'Hamlar' and not n['dead']), None)
    assert guid, 'Hamlar is not near the start'
    for who in ((client, host) if first == 'client' else (host, client)):
        talk_to(who, 'Hamlar', report, who.tag+'_partners')
        time.sleep(2)
        host_goods = goods(host.bot, guid)
        report[who.tag+'_talks_host'] = host_goods
        assert sum(host_goods.values()) > 0, f"Hamlar has no goods on the host after the {who.tag}'s dialog"
        report[who.tag+'_talks_client'] = wait_for(
            lambda: (lambda c: [c] if c == goods(host.bot, guid) else None)(goods(client.bot, guid)), 15,
            f"the client shows Hamlar's goods after the {who.tag}'s dialog")[0]
        start = who.bot.send('endtalk', 20)
        who.bot.line('TALKSTATE ', 10, 'endtalk', start)
        wait_for(lambda: talkstate(who.bot).get('running') == '0', 20, who.tag+' dialog ends')
        time.sleep(4)
    report['host_state'] = invstate(host.bot, guid)
    report['client_sync'] = containersync(client.bot)


def dialogaudio(bot):
    return {k: (int(v) if v.isdigit() else v) for k, v in re.findall(r'(\w+)=(\S+)', report_line(bot, 'dialogaudio', 'DIALOGAUDIO '))}


def stageDialogAudio(copies, report, negative=False):
    """Dialog lines for the other player (owner 22.10., version 1): the host talks to Hamlar with the client next to it;
    each line the host's hero or Hamlar starts is played on the client at the speaker, while the client is in no dialog
    of its own. Then the other way round. Then the client 30 m away: nothing is played there. Negative control:
    dialogaudiooff on the listener."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(5)
    assert next((g for g, n in npcs(host.bot).items() if n['name'] == 'Hamlar' and not n['dead']), None), 'Hamlar is not near the start'

    def converse(speaker, listener, key, far_after=0):
        """far_after: after that many options the listener walks 30 m away; report[key]['far'] counts from there."""
        if negative:
            report_line(listener.bot, 'dialogaudiooff', 'DIALOGAUDIOOFF ')
        before = {'speaker': dialogaudio(speaker.bot), 'listener': dialogaudio(listener.bot)}
        talk_to(speaker, 'Hamlar', report, key+'_partners')
        # A few lines: the greeting, then the first options.
        listener_dialog, mid = [], None
        for round_ in range(4 if far_after else 2):
            if far_after and round_ == far_after:
                # Away from both speakers (the other player and Hamlar): the side where the nearer one is farthest.
                speakers = [remotes(listener.bot)[1 if listener is client else 2]['pos'],
                            next(n['pos'] for n in npcs(listener.bot).values() if n['name'] == 'Hamlar')]
                base = speakers[0]
                spot = max(((base[0] + dx, base[1] + 50, base[2] + dz) for dx, dz in ((3500, 0), (-3500, 0), (0, 3500), (0, -3500))),
                           key=lambda p: min(distance(p, q) for q in speakers))
                report[key+'_far_spot_m'] = round(min(distance(spot, q) for q in speakers) / 100)
                move_hero(listener.bot, spot)
                time.sleep(1.5)  # a line started just before the move is stopped there (stopped_far); count from here
                mid = {'speaker': dialogaudio(speaker.bot), 'listener': dialogaudio(listener.bot)}
            report_line(speaker.bot, 'talkchoose 0', ('TALKCHOOSE ', 'REFUSED talkchoose'))
            for _ in range(8):
                time.sleep(1)
                listener_dialog.append(talkstate(listener.bot).get('running'))
            if talkstate(speaker.bot).get('running') != '1':
                break
        if talkstate(speaker.bot).get('running') == '1':
            start = speaker.bot.send('endtalk', 20)
            speaker.bot.line('TALKSTATE ', 10, 'endtalk', start)
        wait_for(lambda: talkstate(speaker.bot).get('running') == '0', 20, key+' dialog ends')
        time.sleep(3)
        after = {'speaker': dialogaudio(speaker.bot), 'listener': dialogaudio(listener.bot)}
        diff = {side: {k: after[side][k] - before[side][k] for k in after[side] if isinstance(after[side][k], int)}
                for side in after}
        report[key] = {'diff': diff, 'listener_in_dialog': sorted(set(listener_dialog)),
                       'speaker_last': after['speaker']['last'], 'listener_last': after['listener']['last']}
        if mid:
            report[key]['far'] = {side: {k: after[side][k] - mid[side][k] for k in after[side] if isinstance(after[side][k], int)}
                                  for side in after}
            diff = {side: {k: mid[side][k] - before[side][k] for k in mid[side] if isinstance(mid[side][k], int)} for side in mid}
            report[key]['near'] = diff  # the checks below use the part with the listener close
        return diff, listener_dialog, after

    diff, listener_dialog, after = converse(host, client, 'host_talks')
    assert after['speaker']['hooked'] == 1, 'StartOutput not hooked on the host'
    assert diff['speaker']['sent_starts'] >= 2, f"the host's dialog sent {diff['speaker']['sent_starts']} lines"
    assert diff['listener']['played'] >= diff['speaker']['sent_starts'] - 1, \
        f"the client played {diff['listener']['played']} of {diff['speaker']['sent_starts']} lines"
    assert diff['listener']['dropped_failed'] == 0, 'lines failed to play on the client'
    assert set(listener_dialog) == {'0'}, 'the client was drawn into the dialog'
    assert diff['speaker']['sent_ends'] == diff['speaker']['sent_starts'], 'a line was never ended'
    assert after['listener']['playing'] == 0, 'a channel still plays on the client after the dialog'

    diff, listener_dialog, after = converse(client, host, 'client_talks', far_after=1)
    assert diff['speaker']['sent_starts'] >= 2, f"the client's dialog sent {diff['speaker']['sent_starts']} lines"
    assert diff['listener']['played'] >= diff['speaker']['sent_starts'] - 1, \
        f"the host played {diff['listener']['played']} of {diff['speaker']['sent_starts']} lines"
    assert set(listener_dialog) == {'0'}, 'the host was drawn into the dialog'

    # The host walked 30 m away during the client's dialog: from then on nothing plays there.
    far = report['client_talks'].get('far')
    assert far, 'no far part'
    assert far['speaker']['sent_starts'] >= 1, 'no line after the host walked away'
    assert far['listener']['played'] == 0 and far['listener']['dropped_far'] >= 1, f"30 m away: {far['listener']}"


def stageSubtitles(copies, report, negative=False):
    """Stage Subtitles, overheard dialog lines show their text (owner 24.10., phase 5 pre-step): the host talks to Hamlar with
    the client next to it. Each line the client plays shows as the game shows its own dialog's text (GUI2::StartSay), in the
    client's language: the line's text id travels with it and is looked up there (the client has the game's subtitles on).
    Screenshots of the client while lines play. Negative control: subtitlesoff on the client (lines play without text)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(5)
    assert next((g for g, n in npcs(host.bot).items() if n['name'] == 'Hamlar' and not n['dead']), None), 'Hamlar is not near the start'
    if negative:
        assert 'ok=1' in report_line(client.bot, 'subtitlesoff', 'SUBTITLESOFF ')
    before = {'host': dialogaudio(host.bot), 'client': dialogaudio(client.bot)}
    talk_to(host, 'Hamlar', report, 'partners')
    for round_ in range(3):
        report_line(host.bot, 'talkchoose 0', ('TALKCHOOSE ', 'REFUSED talkchoose'))
        for second in range(7):
            time.sleep(1)
            if second == 2:
                snap(client, f'subtitle{round_}', report)
        if talkstate(host.bot).get('running') != '1':
            break
    if talkstate(host.bot).get('running') == '1':
        start = host.bot.send('endtalk', 20)
        host.bot.line('TALKSTATE ', 10, 'endtalk', start)
    wait_for(lambda: talkstate(host.bot).get('running') == '0', 20, 'the dialog ends')
    time.sleep(3)
    after = {'host': dialogaudio(host.bot), 'client': dialogaudio(client.bot)}
    report['before'], report['after'] = before, after
    played = after['client']['played'] - before['client']['played']
    shown = after['client']['subtitles'] - before['client']['subtitles']
    report['played'], report['shown'] = played, shown
    report['client_log'] = [l for l in client.mod_log().splitlines() if 'dialogaudio:' in l][-8:]
    assert after['client']['subtitle_on'] == 1 or negative, "the client's game has subtitles off: " + str(after['client'])
    assert played >= 2, f'the client played {played} lines'
    assert shown >= played - 1, f'{shown} of {played} lines showed their text on the client'
    assert after['client']['last_subtitle'] != '-', 'no text was shown'


def listendiag(copies, report, negative=False):
    """Listening test with the owner (dialog audio, 22.10.): the speaking game is muted, so what is heard comes from the
    listening game alone, through the mod. Part 1: the host talks to Hamlar, the client (listening) stands by, after
    about 40 s it walks 35 m away. Part 2: the other way round. HUD messages say which part runs."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(5)
    for part, (speaker, listener) in enumerate(((host, client), (client, host)), 1):
        report_line(speaker.bot, 'mutegame 1', 'MUTEGAME ')
        report_line(listener.bot, 'mutegame 0', 'MUTEGAME ')
        emit({'event': 'listen', 'part': part, 'speaker': speaker.tag, 'hear': listener.tag})
        before = dialogaudio(listener.bot)
        talk_to(speaker, 'Hamlar', report, f'part{part}_partners')
        # The listener stands next to the conversation, as a co-op player would (owner 22.10.: from 18 m Hamlar sounded
        # far away, which was right for 18 m).
        hamlar = next(n['pos'] for n in npcs(listener.bot).values() if n['name'] == 'Hamlar')
        move_hero(listener.bot, (hamlar[0] + 300, hamlar[1] + 50, hamlar[2]))
        report[f'part{part}_listener_next_to_hamlar_at'] = time.strftime('%H:%M:%S')
        for round_ in range(7):
            if round_ == 5:
                speakers = [remotes(listener.bot)[1 if listener is client else 2]['pos'],
                            next(n['pos'] for n in npcs(listener.bot).values() if n['name'] == 'Hamlar')]
                base = speakers[0]
                spot = max(((base[0] + dx, base[1] + 50, base[2] + dz) for dx, dz in ((3500, 0), (-3500, 0), (0, 3500), (0, -3500))),
                           key=lambda p: min(distance(p, q) for q in speakers))
                move_hero(listener.bot, spot)
                report[f'part{part}_walked_away_at'] = time.strftime('%H:%M:%S')
            report_line(speaker.bot, 'talkchoose 0', ('TALKCHOOSE ', 'REFUSED talkchoose'))
            time.sleep(8)
            if talkstate(speaker.bot).get('running') != '1':
                talk_to(speaker, 'Hamlar', report, f'part{part}_again')
        if talkstate(speaker.bot).get('running') == '1':
            start = speaker.bot.send('endtalk', 20)
            speaker.bot.line('TALKSTATE ', 10, 'endtalk', start)
        after = dialogaudio(listener.bot)
        report[f'part{part}'] = {k: after[k] - before[k] for k in ('played', 'dropped_far', 'stopped_far')}
        report_line(speaker.bot, 'mutegame 0', 'MUTEGAME ')
        time.sleep(3)
    report['status_note'] = 'listening test with the owner'


def npc_summary(bot):
    start = bot.send('npcsummary', 20)
    bot.line('NPCSUMMARY end', 20, 'npcsummary', start)
    text = bot.log()[start:]
    head = re.search(r'NPCSUMMARY npcs=\d+ entities=\d+ farthest_m=\d+', text)[0]
    rows = [{'name': m[1], 'distance': float(m[2]), 'pos': m[3], 'dead': m[4] == '1'}
            for m in re.finditer(r'NPCAT name=(\S+) distance_m=(\d+) pos=(\S+) dead=(\d)', text)]
    return head, rows


def near(bot, pos):
    start = bot.send('near '+pos, 20)
    bot.line('NEAR end', 20, 'near', start)
    text = bot.log()[start:]
    found = {}
    for m in re.finditer(r'NEAR name=(\S+) pos=(\S+) task=(\S*) hp=(-?\d+) dead=(\d) mirrored=(\d)', text):
        found.setdefault(m[1], []).append({'pos': tuple(map(float, m[2].split('_'))), 'task': m[3], 'dead': m[5] == '1',
                                           'mirrored': m[6] == '1'})
    summary = re.search(r'NEARSUM at=\S+ npcs=(\d+) entities=(\d+)', text)
    return found, {'npcs': int(summary[1]), 'entities': int(summary[2])}


def radiusdiag(copies, report, negative=False):
    """Exploration: does the host have (and run) NPCs where a far-away client stands?"""
    host, client = join_both(copies)
    host_head, host_rows = npc_summary(host.bot)
    client_head, client_rows = npc_summary(client.bot)
    report['host_summary'], report['client_summary'] = host_head, client_head
    # Far targets: NPCs found through the owners of dialog options (GetNPCs only knows the ~45 m around the hero).
    start = host.bot.send('farnpcs', 60)
    host.bot.line('FARNPC end', 60, 'farnpcs', start)
    far_text = host.bot.log()[start:]
    report['farnpcs'] = re.search(r'FARNPCS owners=\d+ resolved=\d+', far_text)[0]
    rows = [{'name': m[1], 'distance': float(m[2]), 'pos': m[3], 'dead': False}
            for m in re.finditer(r'FARNPC name=(\S+) distance_m=(\d+) pos=(\S+)', far_text)]
    report['far_sample'] = [f"{r['name']} {r['distance']:.0f} m" for r in rows[::max(1, len(rows) // 12)]]
    assert rows, 'No far NPCs found'
    def find(bot, name):
        start = bot.send('findnpc '+name, 20)
        return bot.line('FIND ', 10, 'findnpc', start)

    results = []
    for wanted in (600, 1500, 4000):
        target = min((r for r in rows if r['distance'] > 100), key=lambda r: abs(r['distance'] - wanted))
        name = target['name']
        host_far = [find(host.bot, name)]
        time.sleep(10)
        host_far.append(find(host.bot, name))  # does the far NPC move on the host at all?
        x, y, z = (float(v) for v in target['pos'].split('_'))
        pos = f'{x + 200:.0f}_{y + 50:.0f}_{z:.0f}'
        start = client.bot.send('teleportto '+name, 30)
        tele = client.bot.line('TELEPORTTO ', 30, 'teleportto', start)
        samples = []
        for wait in (5, 15, 30):
            time.sleep(wait - (samples[-1]['t'] if samples else 0))
            client_head, _ = npc_summary(client.bot)
            client_found, client_sum = near(client.bot, pos)
            host_found, host_sum = near(host.bot, pos)
            samples.append({'t': wait, 'client_summary': client_head, 'client_near': client_sum,
                            'client_mirrored': sum(1 for v in client_found.values() if v[0]['mirrored']),
                            'client_target': find(client.bot, name), 'host_near': host_sum, 'host_target': find(host.bot, name)})
        results.append({'wanted_m': wanted, 'target': f"{name} at {target['distance']:.0f} m", 'teleport': tele,
                        'host_target_before': host_far, 'samples': samples})
    report['results'] = results
    report['host_after'] = npc_summary(host.bot)[0]
    report['status_note'] = 'diagnostic only'


def frames_seen(copy):
    """The mod's per-second hero reports so far: (count, last frame number)."""
    reports = [l for l in copy.mod_log().splitlines() if ' frame=' in l]  # running or not
    last = re.findall(r'frame=(\d+)', reports[-1]) if reports else ['0']
    return len(reports), int(last[0]), ('running=1' in reports[-1]) if reports else None


def page(bot, what):
    start = bot.send('page '+what, 20)
    return bot.line('PAGE ', 10, 'page', start)


def pausediag(copies, report, negative=False):
    """Exploration: do the inventory and the ESC menu stop the mod's per-frame work or the game, on either side?"""
    host, client = join_both(copies)
    time.sleep(5)
    results = []
    for side, other in ((client, host), (host, client)):
        for what in ('inventory', 'menu'):
            opened = page(side.bot, what)
            before = frames_seen(side)
            npc_before = npcs(host.bot)
            remote_before = remotes(other.bot)
            time.sleep(25)
            # The bot itself can only answer if the side's frame loop still runs.
            try:
                state = page(side.bot, 'state')
            except Exception as error:
                state = f'no answer: {error}'
            after = frames_seen(side)
            npc_after = npcs(host.bot) if side is client else {}
            remote_after = remotes(other.bot)
            moved = sum(1 for g in npc_before if g in npc_after and distance(npc_before[g]['pos'], npc_after[g]['pos']) > 50)
            rid = 2 if side is client else 1
            results.append({'side': side.tag, 'page': what, 'opened': opened, 'state_after_25s': state,
                            'reports_before_after': [before, after],
                            'other_sees_age_ms': [remote_before.get(rid, {}).get('age_ms'), remote_after.get(rid, {}).get('age_ms')],
                            'other_sees_puppet': remote_after.get(rid, {}).get('puppet'),
                            'host_npcs_moved_while_client_paused': moved if side is client else None})
            try:
                page(side.bot, 'close')
            except Exception:
                post_key(side.process.pid, 0x1B)
            time.sleep(5)
    report['results'] = results
    report['status_note'] = 'diagnostic only'


def save_files_of(copy):
    folder = copy.root/'isolated-profile/Documents/gothic3'
    return {f.relative_to(folder).as_posix(): f.stat().st_size for f in folder.rglob('*') if f.is_file()
            and 'paru' not in f.parts and not f.name.startswith(('ge3log', 'Lastlog'))}


def scriptinfo(bot, name):
    start = bot.send('scriptinfo '+name, 20)
    bot.line('SCRIPTINFO ', 10, 'scriptinfo', start)
    return [l for l in bot.log()[start:].splitlines() if l.startswith('SCRIPTINFO address')]


def wait_running(copy, seconds, what):
    """Until the mod reports the hero in the running world again (after a load)."""
    began = len(copy.hero_reports())
    wait_for(lambda: len(copy.hero_reports()) >= began + 3, seconds, what)


def heroes_named(bot):
    _, rows = npc_summary(bot)
    return sum(1 for r in rows if r['name'] == 'PC_Hero')


def savediag(copies, report, negative=False):
    """Exploration: a native save by the host, loaded again by the host and then by the client from a copy."""
    host, client = join_both(copies)
    time.sleep(5)
    before = save_files_of(host)
    start = host.bot.send('nativesave G3T_probe', 60)
    report['save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
    time.sleep(3)
    created = {k: v for k, v in save_files_of(host).items() if k not in before or before[k] != v}
    report['save_files'] = created
    report['hooks_before_load'] = scriptinfo(host.bot, 'AddHitPoints')
    report['host_pc_hero_npcs_before'] = heroes_named(host.bot)
    # The host loads its own save: does the session come back, with the mod's hooks and the client?
    start = host.bot.send('nativeload G3T_probe', 60)
    try:
        report['host_load'] = host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''),
                                            120, 'nativeload result', start)
    except Exception as error:
        report['host_load'] = f'no result: {error}'
    wait_running(host, 180, 'host world running after load')
    time.sleep(10)
    report['host_state_after_load'] = state(host.bot)
    report['hooks_after_load'] = scriptinfo(host.bot, 'AddHitPoints')
    report['host_pc_hero_npcs_after'] = heroes_named(host.bot)
    report['host_sees_client_after_load'] = remotes(host.bot).get(2, {})
    report['host_story_after_load'] = story(host.bot)
    # The client loads a copy of the host's save.
    source, target = host.root/'isolated-profile/Documents/gothic3', client.root/'isolated-profile/Documents/gothic3'
    report['copied'] = []
    for file in source.glob('G3T_probe.*'):  # header and data, changed or not
        shutil.copy2(file, target/file.name)
        report['copied'].append(file.name)
    start = client.bot.send('nativeload G3T_probe', 60)
    try:
        report['client_load'] = client.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''),
                                                120, 'client nativeload result', start)
    except Exception as error:
        report['client_load'] = f'no result: {error}'
    wait_running(client, 180, 'client world running after load')
    time.sleep(10)
    report['client_state_after_load'] = state(client.bot)
    report['client_pc_hero_npcs_after'] = heroes_named(client.bot)
    report['client_hooks_after_load'] = scriptinfo(client.bot, 'AddHitPoints')
    report['host_sees_client_end'] = remotes(host.bot).get(2, {})
    report['client_sees_host_end'] = remotes(client.bot).get(1, {})
    h, c = npcs(host.bot), npcs(client.bot)
    report['npcs_common_end'] = len(set(h) & set(c))
    report['quests_after'] = {'host': quest(host.bot, 'quest', EARLY_QUEST), 'client': quest(client.bot, 'quest', EARLY_QUEST)}
    report['status_note'] = 'diagnostic only'


def find_npc(bot, name):
    start = bot.send('findnpc '+name, 20)
    return bot.line('FIND ', 10, 'findnpc', start)


def farwritediag(copies, report, negative=False):
    """Spike for regional authority: can the host change NPCs it does not process (kill one, move one), and do
    the changes survive its own save and load and the moment the engine starts processing them again?"""
    host = copies['host'].launch()
    enter_world(host)
    start = host.bot.send('farnpcs', 60)
    host.bot.line('FARNPC end', 60, 'farnpcs', start)
    rows = [{'name': m[1], 'distance': float(m[2]), 'pos': tuple(float(v) for v in m[3].split('_'))}
            for m in re.finditer(r'FARNPC name=(\S+) distance_m=(\d+) pos=(\S+)', host.bot.log()[start:])]
    candidates = [r for r in rows if 800 < r['distance'] < 2500]
    assert len(candidates) >= 2, f'{len(candidates)} far NPCs between 800 and 2500 m'
    # Three victims far apart (one sample was a boss), one mover elsewhere.
    picked = []
    for r in candidates:
        if all(distance(r['pos'], p['pos']) > 20000 for p in picked):
            picked.append(r)
        if len(picked) == 4:
            break
    assert len(picked) == 4, f'only {len(picked)} far NPCs 200 m apart'
    victims, mover = picked[:3], picked[3]
    x, y, z = mover['pos']
    target = f'{x + 300:.0f}_{y:.0f}_{z + 300:.0f}'
    names = [v['name'] for v in victims] + [mover['name']]
    probe = lambda tag: {n: find_npc(host.bot, n) for n in names} | {'tag': tag}
    timeline = [probe('before')]
    report['farkill'] = []
    for v in victims:
        start = host.bot.send('farkill '+v['name'], 20)
        report['farkill'].append(host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith(('FARKILL', 'REFUSED farkill'))), ''), 10, 'farkill', start))
    start = host.bot.send(f"farmove {target}_{mover['name']}", 20)
    report['farmove'] = host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith(('FARMOVE', 'REFUSED farmove'))), ''), 10, 'farmove', start)
    timeline.append(probe('after change'))
    time.sleep(15)
    timeline.append(probe('after 15 s'))
    start = host.bot.send('nativesave G3T_far', 60)
    report['save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
    time.sleep(3)
    start = host.bot.send('nativeload G3T_far', 60)
    report['load'] = host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''), 120, 'nativeload', start)
    wait_running(host, 180, 'world running after load')
    time.sleep(10)
    timeline.append(probe('after save and load'))
    # Now let the engine process them: the game's own teleport to each.
    for name in names:
        start = host.bot.send('teleportto '+name, 30)
        host.bot.line('TELEPORTTO ', 30, 'teleportto', start)
        time.sleep(15)
        timeline.append(probe('at '+name))
        if name != mover['name']:  # lazy apply: the same kill once the engine processes the NPC
            start = host.bot.send('farkill '+name, 20)
            host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith(('FARKILL', 'REFUSED farkill'))), ''), 10, 'farkill', start)
            time.sleep(5)
            timeline.append({name: find_npc(host.bot, name), 'tag': 'killed while processed: '+name})
    report['victims'], report['mover'], report['moved_to'] = victims, mover, target
    report['timeline'] = timeline
    report['status_note'] = 'diagnostic only'


def stage3c(copies, report, negative=False):
    """Experience goes to whoever earned it: the host kills the raiders, the client never hits, and the client's
    experience must not grow (its mirrored NPCs die there through the game's death task, which rewards the
    local player unless the NPC is marked as defeated already)."""
    host, client = join_both(copies)
    # Quest rewards are shared on purpose: when the host's quest status reaches the client, the client's quest
    # manager pays the reward too (+500 for Ardea_Revolution, 44 ms after the host's change). Story sync is off
    # here so that only kill experience is measured.
    story_off(client)
    if negative:  # G3_NEGATIVE=farxp: the client's hero is paid for deaths without a killer far from it again
        control = 'farxpoff' if os.environ.get('G3_NEGATIVE') == 'farxp' else 'xpguardoff'
        start = client.bot.send(control, 20)
        assert 'ok=1' in client.bot.line(control.upper() + ' ', 10, control, start)
    report['client_xp_before'] = combat_state(client.bot)['xp']
    report['host_xp_before'] = combat_state(host.bot)['xp']
    clear_raid(host, report)
    # The freed town's script kills the orcs still about (Ardea_Orkboss 15 to 25 s after the town turned, on both
    # machines; 25.10.): those deaths name no killer, and on the client they must pay nothing.
    time.sleep(35)
    report['client_end'] = combat_state(client.bot)
    report['host_end'] = combat_state(host.bot)
    report['client_dead_mirrors'] = sum(1 for v in npcs(client.bot).values() if v.get('dead'))
    assert sum(report['raid_kills']) >= 3 and report['client_dead_mirrors'] >= 3, 'Too few kills to tell'
    assert report['host_end']['xp'] > report['host_xp_before'], 'The host earned nothing for its own kills'
    # Since regional ownership (stage E1) the client may simulate some raiders itself (it processed them first); their
    # deaths then run the death script on the client, its own quest manager counts them and may complete the raid quest
    # there, paying the shared quest reward (+500 for Ardea_Revolution). That reward is allowed; kill experience is not.
    # Its size varies with the kills the quest counted (500, 550 on 20.10.; the host got 600 that run), so the check is
    # the shape: nothing, or one single experience event on the client, the quest reward (a kill on top matches no
    # single event; events logged after the measurement do not count).
    succeeded = quest(client.bot, 'quest', 'Ardea_Revolution') == SUCCESS
    events = [int(m) for m in re.findall(r'xp: \+(\d+) total=', client.mod_log())]
    report['client_xp_events'] = events
    report['client_givexp'] = [l for l in client.mod_log().splitlines() if 'xp: GiveXP ' in l][-12:]
    report['client_quest_succeeded'] = succeeded
    gained = report['client_end']['xp'] - report['client_xp_before']
    assert gained == 0 or (succeeded and gained in events), \
        f"Client earned {gained} XP for kills it had no part in (events {events}, quest succeeded {succeeded})"


def givexpdiag(copies, report, negative=False):
    """Does GiveXP(hero, NPC, -1) through the script admin reward the hero at all (alive and dead NPCs)?"""
    host = copies['host'].launch()
    enter_world(host)
    found = npcs(host.bot)
    alive = [g for g, v in found.items() if not v.get('dead')][:2]
    report['givexp'] = []
    for g in alive:
        start = host.bot.send('givexp '+g, 20)
        report['givexp'].append(host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith(('GIVEXP', 'REFUSED givexp'))), ''), 10, 'givexp', start))
    report['status_note'] = 'diagnostic only'


def herostate(bot):
    start = bot.send('herostate', 20)
    return bot.line('HERO ', 10, 'herostate', start)


def herodiag(copies, report, negative=False):
    """Stage A exploration: what happens today when a player's hero reaches 0 hit points? First the client's
    hero through forwarded hits from the host, then the host's hero hit by an orc through the game's judgement."""
    host, client = join_both(copies)
    report['client_before'] = herostate(client.bot)
    hits = []
    for _ in range(60):
        start = host.bot.send('hitpuppet 2', 20)
        hits.append(host.bot.line('HITPUPPET ', 10, 'hitpuppet', start))
        time.sleep(0.4)
        if combat_state(client.bot)['hero'] <= 0:
            break
    report['client_hits'] = [hits[0], hits[-1], len(hits)]
    report['client_timeline'] = []
    for t in (0, 3, 10, 20, 30):
        time.sleep(t - (report['client_timeline'][-1][0] if report['client_timeline'] else 0))
        report['client_timeline'].append((t, herostate(client.bot), remotes(host.bot).get(2, {}).get('puppet')))
    # The host's hero, hit by a raider through the game's own hit judgement.
    orcs = [g for g, v in npcs(host.bot).items() if 'Orc' in v.get('name', '') and not v.get('dead')]
    report['host_before'] = herostate(host.bot)
    assert orcs, 'no living orc near the host'
    hurts = []
    for _ in range(110):
        start = host.bot.send('hurthero '+orcs[0], 20)
        hurts.append(host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith(('HURTHERO', 'REFUSED hurthero'))), ''), 10, 'hurthero', start))
        time.sleep(0.4)
        state_now = herostate(host.bot)
        if ' hp=0 ' in state_now or 'dead=1' in state_now or 'down=1' in state_now:
            break
    report['host_hurts'] = [hurts[0], hurts[-1], len(hurts)]
    report['host_timeline'] = []
    for t in (0, 3, 10, 20, 30):
        time.sleep(t - (report['host_timeline'][-1][0] if report['host_timeline'] else 0))
        report['host_timeline'].append((t, herostate(host.bot), herostate(client.bot), remotes(client.bot).get(1, {}).get('puppet')))
    report['status_note'] = 'diagnostic only'


def herodeathdiag(copies, report, negative=False):
    """Stage A exploration: the death path of a hero (the game's kill cheat, an orc as killer) on the client and
    then on the host; what does each machine show, and does the session survive?"""
    host, client = join_both(copies)
    for tag, copy, other in (('client', client, host), ('host', host, client)):
        orcs = [g for g, v in npcs(copy.bot).items() if 'Orc' in v.get('name', '') and not v.get('dead')]
        assert orcs, 'no living orc near the '+tag
        start = copy.bot.send('killhero '+orcs[0], 20)
        report[tag+'_kill'] = copy.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith(('KILLHERO', 'REFUSED killhero'))), ''), 10, 'killhero', start)
        timeline = []
        for t in (1, 4, 10, 20, 30):
            time.sleep(t - (timeline[-1][0] if timeline else 0))
            try:
                timeline.append((t, herostate(copy.bot), state(copy.bot), bool(remotes(other.bot).get(2 if tag == 'client' else 1, {}).get('puppet'))))
            except Exception as error:
                timeline.append((t, f'no answer: {error}'))
        report[tag+'_timeline'] = timeline
    report['status_note'] = 'diagnostic only'


def living_orc(bot):
    living = {g: v for g, v in npcs(bot).items() if 'Orc' in v.get('name', '') and not v.get('dead')}
    assert living, 'no living orc near the hero'
    # Prefer an orc this machine simulates itself: a mirror's hits are decided by its owner and dropped here
    # (stage A once picked a raider the client owned: 80 hits, none forwarded).
    own = [g for g, v in living.items() if not v.get('mirrored')]
    return (own or list(living))[0]


def help_dialog(helper, downed_id, report, tag):
    """Owner 23.10.: a downed partner is helped up through its puppet's dialog option "Ich werde dir helfen."
    (G3T_HelpUp_<id>); the helper pays a healing potion when it is chosen."""
    report[tag+'_help_approach'] = report_line(helper.bot, f'approach puppet{downed_id}', ('APPROACH', 'REFUSED approach'))
    for _ in range(5):
        report[tag+'_help_talk'] = report_line(helper.bot, f'assesstalk puppet{downed_id}', ('ASSESSTALK', 'REFUSED assesstalk'))
        time.sleep(1.5)
        if (state := talkstate(helper.bot)).get('running') == '1':
            report[tag+'_help_state'] = state
            break
        time.sleep(1)
    report[tag+'_help_choose'] = report_line(helper.bot, f'talkinfo G3T_HelpUp_{downed_id}', ('TALKINFO', 'REFUSED talkinfo'))
    time.sleep(3)  # the hero says its line
    report_line(helper.bot, 'endtalk', 'TALKSTATE ')


def help_up(helper, downed, report, tag):
    """Since stage A2 a knocked-out player waits for a partner; since 23.10. the helper talks to its puppet and chooses
    the help option (standing next to it no longer helps)."""
    downed_id = 2 if downed.tag == 'client' else 1
    lying = remotes(helper.bot)[downed_id]['pos']
    report[tag+'_helper_moved'] = move_hero(helper.bot, (lying[0] + 100, lying[1], lying[2]))
    help_dialog(helper, downed_id, report, tag)


def stageA(copies, report, negative=False):
    """Stage A, no game over in co-op: a client's hero hit by the host's NPCs is judged on the client (it falls at
    0 hit points instead of walking on); a hero the game would let die is knocked out instead and gets up again,
    on the client and on the host; the session stays intact."""
    host, client = join_both(copies)
    if negative:
        for copy in (host, client):
            start = copy.bot.send('downedoff', 20)
            assert 'ok=1' in copy.bot.line('DOWNEDOFF ', 10, 'downedoff', start)
    up = lambda bot: (lambda s: ' down=0 ' in s and ' dead=0 ' in s and not s.startswith('HERO hp=0 ') and [s])(herostate(bot))
    # 1. An orc on the host beats the client's hero down.
    orc = living_orc(host.bot)
    hits, state_now = 0, ''
    for hits in range(1, 81):
        start = host.bot.send('hitpuppetby '+orc, 20)
        host.bot.line('HITPUPPETBY ', 10, 'hitpuppetby', start)
        time.sleep(0.4)
        state_now = herostate(client.bot)
        if ' down=1 ' in state_now or ' dead=1 ' in state_now:
            break
    report['client_beaten'] = [hits, state_now]
    report['client_combat'] = combat_state(client.bot)
    assert report['client_combat']['forwarded_judged'] >= 1, 'No forwarded hit was judged on the client'
    assert ' down=1 ' in state_now, 'Client hero did not fall: '+state_now
    help_up(host, client, report, 'client')
    report['client_up_again'] = wait_for(lambda: up(client.bot), 60, 'client hero gets up')[0]
    # Everyone sees it: the client's puppet on the host replayed the fall (motion names travel with the state).
    report['host_puppet_fell'] = sum(1 for l in host.mod_log().splitlines() if l.startswith(tuple('0123456789'))
                                     and 'TRACE puppet2 ' in l and ' motion=Hero_LieKnock' in l)  # LieKnockDown or the game's own LieKnockOut
    assert report['host_puppet_fell'] > 0, 'The host never showed the client lying on the ground'
    # 2. The death path (the game's kill cheat, an orc as killer) on each machine.
    for tag, copy in (('client', client), ('host', host)):
        start = copy.bot.send('killhero '+living_orc(copy.bot), 20)
        report[tag+'_kill'] = copy.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith(('KILLHERO', 'REFUSED killhero'))), ''), 10, 'killhero', start)
        time.sleep(2)
        report[tag+'_after_kill'] = herostate(copy.bot)
        assert ' dead=0 ' in report[tag+'_after_kill'] and ' down=1 ' in report[tag+'_after_kill'], \
            f'{tag} hero not knocked out: '+report[tag+'_after_kill']
        help_up(client if copy is host else host, copy, report, tag)
        report[tag+'_up_again'] = wait_for(lambda: up(copy.bot), 60, tag+' hero gets up')[0]
        start = copy.bot.send('downed', 20)
        report[tag+'_downed'] = copy.bot.line('DOWNED ', 10, 'downed', start)
    assert remotes(host.bot).get(2, {}).get('puppet') and remotes(client.bot).get(1, {}).get('puppet'), 'session broke'


def report_line(bot, command, prefix):
    start = bot.send(command, 20)
    return bot.line(prefix, 10, command, start)


def move_hero(bot, pos):
    start = bot.send('teleport ' + '_'.join(f'{v:.0f}' for v in pos), 20)
    return bot.line('TELEPORT ', 10, 'teleport', start)


def stageA2(copies, report, negative=False):
    """Stage A2, a knocked-out player waits for help (owner's rules of 19.10. and 23.10.): it stays down far longer than
    the game's own 5 to 10 s while a partner could help; standing next to it does not help any more; a partner who talks
    to its puppet and chooses "Ich werde dir helfen." pays one healing potion and stands it up; a partner without a
    potion is told so and the player stays down; when nobody can help (everybody down) both die as in the single-player
    game. Everyone is told on screen. Negative control: potionoff (helping up is free)."""
    host, client = join_both(copies)
    if negative:
        report_line(host.bot, 'potionoff', 'POTIONOFF')
    potions = lambda bot: int(re.search(r'health=(\d+)', report_line(bot, 'potions', 'POTIONS end'))[1])
    up = lambda bot: (lambda s: ' down=0 ' in s and ' dead=0 ' in s and [s])(herostate(bot))
    # Helped up: standing, or the client's mod counted the n-th revival. The raid's orcs knocked the revived client down
    # again within 5 s (regression 24.10.: revived at 02:10:01, down at 02:10:06), before a poll saw it standing.
    revived = lambda n: up(client.bot) or (lambda l: f' revives={n} ' in l + ' ' and [l])(report_line(client.bot, 'downed', 'DOWNED '))
    start_pos = remotes(host.bot)[2]['pos']
    away = (start_pos[0] + 1000, start_pos[1], start_pos[2])
    report['host_away'] = move_hero(host.bot, away)
    time.sleep(2)
    # 1. The client goes down while the host is 10 m away.
    report['client_kill'] = report_line(client.bot, 'killhero '+living_orc(client.bot), 'KILLHERO ')
    time.sleep(3)
    report['client_down'] = herostate(client.bot)
    assert ' down=1 ' in report['client_down'], 'client not knocked out: '+report['client_down']
    report['host_told'] = report_line(host.bot, 'hud', 'HUD ')
    assert any(text in report['host_told'] for text in ('is down', 'liegt am Boden')), 'host was not told: '+report['host_told']
    # 2. It stays down well past the game's own timer.
    time.sleep(20)
    report['client_after_20s'] = herostate(client.bot)
    snap(client, 'down-waiting', report)
    snap(host, 'client-down', report)
    report['client_downed_waiting'] = report_line(client.bot, 'downed', 'DOWNED ')
    assert ' down=1 ' in report['client_after_20s'], 'client got up without help: '+report['client_after_20s']
    # 3. The host, with healing potions, stands next to it: up within a few seconds, one potion less.
    report['host_potions_before'] = before = potions(host.bot)
    assert before >= 1, 'the host carries no healing potion'
    lying = remotes(host.bot)[2]['pos']
    report['host_next_to_client'] = move_hero(host.bot, (lying[0] + 100, lying[1], lying[2]))
    time.sleep(6)  # standing next to it helps no more (owner 23.10.: only through the dialog)
    report['client_after_standing_by'] = herostate(client.bot)
    assert ' down=1 ' in report['client_after_standing_by'], 'standing next to the client helped it up: ' + report['client_after_standing_by']
    help_dialog(host, 2, report, 'host')
    report['client_revived'] = wait_for(lambda: revived(1), 15, 'client stands up after help')[0]
    report['client_downed_revived'] = report_line(client.bot, 'downed', 'DOWNED ')
    assert 'revives=1' in report['client_downed_revived'], report['client_downed_revived']
    report['client_hud'] = report_line(client.bot, 'hud', 'HUD ')
    assert any(text in report['client_hud'] for text in ('helped you up', 'aufgeholfen')), report['client_hud']
    time.sleep(6)  # no second potion while the client's state still says "down"
    report['host_potions_after'] = after = potions(host.bot)
    report['host_hud'] = report_line(host.bot, 'hud', 'HUD ')
    assert after == before - 1, f'helping up cost {before - after} potions, not 1'
    # 4. Without a potion the host cannot help: the client stays down and both are told; one potion later it stands.
    report['host_away_2'] = move_hero(host.bot, away)
    time.sleep(2)
    report['client_kill_2'] = report_line(client.bot, 'killhero '+living_orc(client.bot), 'KILLHERO ')
    time.sleep(3)
    assert ' down=1 ' in herostate(client.bot), 'client not knocked out the second time'
    report['host_no_potions'] = report_line(host.bot, 'givepotion It_Potion_Health-0', 'GIVEPOTION ')
    lying = remotes(host.bot)[2]['pos']
    move_hero(host.bot, (lying[0] + 100, lying[1], lying[2]))
    help_dialog(host, 2, report, 'host_no_potion')
    time.sleep(3)
    report['client_without_potion'] = herostate(client.bot)
    report['host_hud_no_potion'] = report_line(host.bot, 'hud', 'HUD ')
    report['client_hud_no_potion'] = report_line(client.bot, 'hud', 'HUD ')
    assert ' down=1 ' in report['client_without_potion'], 'helped up without a potion: ' + report['client_without_potion']
    assert any(text in report['host_hud_no_potion'] for text in ('need a healing potion', 'brauchst einen Heiltrank')), report['host_hud_no_potion']
    assert any(text in report['client_hud_no_potion'] for text in ('has no healing potion', 'keinen Heiltrank')), report['client_hud_no_potion']
    report['host_one_potion'] = report_line(host.bot, 'givepotion It_Potion_Health-1', 'GIVEPOTION ')
    help_dialog(host, 2, report, 'host_with_potion')
    report['client_revived_2'] = wait_for(lambda: revived(2), 15, 'client stands up once the host has a potion')[0]
    # 5. Nobody can help: both down at once, both die as in the single-player game.
    report['host_potions_restored'] = report_line(host.bot, 'givepotion It_Potion_Health-5', 'GIVEPOTION ')
    report['host_away_again'] = move_hero(host.bot, away)
    time.sleep(2)
    report_line(client.bot, 'killhero '+living_orc(client.bot), 'KILLHERO ')
    time.sleep(2)
    report_line(host.bot, 'killhero '+living_orc(host.bot), 'KILLHERO ')
    time.sleep(2)
    report['both_down'] = [herostate(host.bot), herostate(client.bot)]
    assert all(' down=1 ' in s for s in report['both_down']), report['both_down']
    dead = lambda: all(' dead=1 ' in herostate(c.bot) for c in (host, client)) and ['both dead']
    report['both_dead'] = wait_for(dead, 20, 'both die')[0]
    snap(host, 'both-dead', report)
    snap(client, 'both-dead', report)
    report['downed_end'] = [report_line(c.bot, 'downed', 'DOWNED ') for c in (host, client)]
    assert all('deaths=1' in l for l in report['downed_end']), report['downed_end']


def stageB(copies, report, negative=False):
    """Stage B, distance warning: a client 60 m from the host is told that its surroundings are not kept in step
    and the host is told who is far away; back next to the host, both get the all-clear."""
    host, client = join_both(copies)
    clear_raid(host, report)  # no raid needed: starts from the post-raid save (25.10.)
    if negative:
        for copy in (host, client):
            report_line(copy.bot, 'rangeoff', 'RANGEOFF ')
    start_pos = remotes(host.bot)[2]['pos']
    report['client_away'] = move_hero(client.bot, (start_pos[0] + 6000, start_pos[1], start_pos[2]))
    time.sleep(4)
    snap(client, 'far', report)
    snap(host, 'far', report)
    report['client_range_far'] = report_line(client.bot, 'range', 'RANGE ')
    report['host_range_far'] = report_line(host.bot, 'range', 'RANGE ')
    report['client_hud_far'] = report_line(client.bot, 'hud', 'HUD ')
    report['host_hud_far'] = report_line(host.bot, 'hud', 'HUD ')
    assert ' far=1 ' in report['client_range_far'] and ' far=1 ' in report['host_range_far'], 'far away not noticed'
    assert any(text in report['client_hud_far'] for text in ('Far from host', 'Weit weg vom Host')), report['client_hud_far']
    assert any(text in report['host_hud_far'] for text in ('is far away', 'weit entfernt')), report['host_hud_far']
    report['client_back'] = move_hero(client.bot, start_pos)
    time.sleep(4)
    report['client_range_near'] = report_line(client.bot, 'range', 'RANGE ')
    report['client_hud_near'] = report_line(client.bot, 'hud', 'HUD ')
    report['host_hud_near'] = report_line(host.bot, 'hud', 'HUD ')
    assert ' far=0 ' in report['client_range_near'] and 'all_clears=1' in report['client_range_near'], report['client_range_near']
    assert any(text in report['client_hud_near'] for text in ('Back near the host', 'Wieder beim Host')), report['client_hud_near']
    assert any(text in report['host_hud_near'] for text in ('is nearby again', 'wieder in der N')), report['host_hud_near']


def far_rows(bot):
    start = bot.send('farnpcs', 60)
    bot.line('FARNPC end', 60, 'farnpcs', start)
    return [{'name': m[1], 'distance': float(m[2]), 'pos': tuple(float(v) for v in m[3].split('_'))}
            for m in re.finditer(r'FARNPC name=(\S+) distance_m=(\d+) pos=(\S+)', bot.log()[start:])]


def teleport_to(bot, name):
    start = bot.send('teleportto '+name, 30)
    return bot.line('TELEPORTTO ', 30, 'teleportto', start)


def stageC(copies, report, negative=False):
    """Stage C, world journal for deaths: a client kills an NPC far from the host; the host records it and, once it
    gets there itself, finds it dead. The other way round: the host kills an NPC in its region and leaves; the
    client, arriving later where the host no longer is, finds it dead too."""
    host, client = join_both(copies)
    if negative:
        report_line(client.bot, 'journaloff', 'JOURNALOFF ')
    rows = [r for r in far_rows(host.bot) if 600 < r['distance'] < 1500]
    assert len(rows) >= 2, f'{len(rows)} far NPCs between 600 and 1500 m'
    t1 = rows[0]
    t2 = next(r for r in rows[1:] if distance(r['pos'], t1['pos']) > 30000)
    report['targets'] = [t1['name'], t2['name']]
    # 1. The client kills t1 far from the host.
    report['client_teleport'] = teleport_to(client.bot, t1['name'])
    time.sleep(8)
    report['client_t1_before'] = find_npc(client.bot, t1['name'])
    assert 'mirrored=0' in report['client_t1_before'].split(), 'target is mirrored on the client: '+report['client_t1_before']
    report['client_kill'] = report_line(client.bot, 'farkill '+t1['name'], 'FARKILL ')
    time.sleep(3)
    report['client_t1_after'] = find_npc(client.bot, t1['name'])
    assert 'dead=1' in report['client_t1_after'].split(), 'the client did not kill it: '+report['client_t1_after']
    report['client_journal'] = report_line(client.bot, 'journal', 'JOURNAL ')
    report['host_entry'] = report_line(host.bot, 'jentry '+t1['name'], 'JENTRY ')
    report['host_t1_far'] = find_npc(host.bot, t1['name'])
    assert 'dead=0' in report['host_t1_far'].split(), 'dead on the host before it got there: '+report['host_t1_far']
    report['host_teleport'] = teleport_to(host.bot, t1['name'])
    time.sleep(8)
    report['host_t1_there'] = find_npc(host.bot, t1['name'])
    report['host_journal'] = report_line(host.bot, 'journal', 'JOURNAL ')
    assert 'dead=1' in report['host_t1_there'].split(), 'alive on the host after it got there: '+report['host_t1_there']
    # 2. The host kills t2 and leaves; the client gets there alone.
    teleport_to(host.bot, t2['name'])
    time.sleep(8)
    report['host_kill'] = report_line(host.bot, 'farkill '+t2['name'], 'FARKILL ')
    time.sleep(3)
    report['host_t2_after'] = find_npc(host.bot, t2['name'])
    assert 'dead=1' in report['host_t2_after'].split(), 'the host did not kill it: '+report['host_t2_after']
    report['client_entry'] = report_line(client.bot, 'jentry '+t2['name'], 'JENTRY ')
    teleport_to(host.bot, t1['name'])  # away from t2 again
    time.sleep(8)
    teleport_to(client.bot, t2['name'])
    time.sleep(8)
    report['client_t2_there'] = find_npc(client.bot, t2['name'])
    report['client_journal_end'] = report_line(client.bot, 'journal', 'JOURNAL ')
    assert 'mirrored=0' in report['client_t2_there'].split(), 'still mirrored, journal not tested: '+report['client_t2_there']
    assert 'dead=1' in report['client_t2_there'].split(), 'alive on the client: '+report['client_t2_there']


def stageD1(copies, report, negative=False):
    """Stage D1, the journal goes with the host's save: a death the host recorded but has not applied yet survives
    the host's own save and load (sidecar <save>.g3t); the save is taken with the client's puppet in the world
    (removed by the save hook, as for a save from the menu) and loads."""
    host, client = join_both(copies)
    if negative:
        report_line(host.bot, 'persistoff', 'PERSISTOFF ')
    t1 = next(r for r in far_rows(host.bot) if 600 < r['distance'] < 1500)
    report['target'] = t1['name']
    teleport_to(client.bot, t1['name'])
    time.sleep(8)
    report['client_kill'] = report_line(client.bot, 'farkill '+t1['name'], 'FARKILL ')
    time.sleep(3)
    report['host_entry'] = report_line(host.bot, 'jentry '+t1['name'], 'JENTRY ')
    assert 'found=1' in report['host_entry'] and 'applied=0' in report['host_entry'], report['host_entry']
    report['puppet_before_save'] = remotes(host.bot).get(2, {}).get('puppet')
    for old in (host.root/'isolated-profile/Documents/gothic3').glob('G3T_D1.*'):
        old.unlink(missing_ok=True)
    start = host.bot.send('nativesave G3T_D1', 60)
    report['save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
    assert 'ok=1' in report['save'], report['save']
    sidecar = host.root/'isolated-profile/Documents/gothic3/G3T_D1.g3t'
    report['sidecar'] = sidecar.read_text(errors='replace')[:400] if sidecar.exists() else None
    report['persist_after_save'] = report_line(host.bot, 'persist', 'PERSIST ')
    start = host.bot.send('nativeload G3T_D1', 60)
    report['load'] = host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''), 120, 'nativeload', start)
    wait_running(host, 180, 'host world running after load')
    time.sleep(10)
    report['persist_after_load'] = report_line(host.bot, 'persist', 'PERSIST ')
    report['host_entry_after_load'] = report_line(host.bot, 'jentry '+t1['name'], 'JENTRY ')
    report['host_heroes_after_load'] = heroes_named(host.bot)
    assert 'found=1' in report['host_entry_after_load'], 'journal lost with the load: '+report['host_entry_after_load']
    teleport_to(host.bot, t1['name'])
    time.sleep(8)
    report['host_t1_there'] = find_npc(host.bot, t1['name'])
    assert 'dead=1' in report['host_t1_there'].split(), 'alive on the host after the load: '+report['host_t1_there']


def stageD2(copies, report, negative=False):
    """Stage D2, a player's own hero as a record: saved, then the hero changes (a sword, experience, a wound),
    then the record is loaded and the hero is exactly as saved (level, experience, learn points, attribute base
    values and maxima, inventory with quality and equipment)."""
    host = copies['host'].launch()
    enter_world(host)
    if negative:
        report_line(host.bot, 'herorestoreoff', 'HERORESTOREOFF ')
    report['dump_saved'] = report_line(host.bot, 'herodump', 'HERODUMP ')
    report['save'] = report_line(host.bot, 'herosave stageD2', 'HEROSAVE ')
    assert 'ok=1' in report['save'], report['save']
    report['arm'] = report_line(host.bot, 'arm '+WEAPON, 'ARM ')
    npc = next(g for g, v in npcs(host.bot).items() if not v.get('dead'))
    report['givexp'] = report_line(host.bot, 'givexp '+npc, 'GIVEXP ')
    report['hurt'] = report_line(host.bot, 'hurthero '+living_orc(host.bot), 'HURTHERO ')
    time.sleep(2)
    report['dump_changed'] = report_line(host.bot, 'herodump', 'HERODUMP ')
    assert report['dump_changed'] != report['dump_saved'], 'the changes did not change the hero'
    report['load'] = report_line(host.bot, 'heroload stageD2', 'HEROLOAD ')
    time.sleep(2)
    report['dump_restored'] = report_line(host.bot, 'herodump', 'HERODUMP ')
    assert report['dump_restored'] == report['dump_saved'], 'hero differs from the record:\n' + report['dump_saved'] + '\n' + report['dump_restored']


def stageD3a(copies, report, negative=False):
    """Stage D3a, a player's own hero over the host's save: when the host saves, every client keeps its own hero
    for that save; a client that loads (a copy of) the host's save gets its own hero back, not the host's."""
    host, client = join_both(copies)
    # Since puppets collide (host NPCs can hit a client), the opening raid would hurt the client's hero between the
    # dumps; the hero record is the subject here, so the raiders go first.
    clear_raid(host, report)
    if negative:
        report_line(client.bot, 'herorestoreoff', 'HERORESTOREOFF ')
    report_line(client.bot, 'arm '+WEAPON, 'ARM ')
    npc = next(g for g, v in npcs(client.bot).items() if not v.get('dead'))
    for _ in range(3):
        report_line(client.bot, 'givexp '+npc, 'GIVEXP ')
    time.sleep(1)
    report['client_dump'] = report_line(client.bot, 'herodump', 'HERODUMP ')
    report['host_dump'] = report_line(host.bot, 'herodump', 'HERODUMP ')
    assert report['client_dump'] != report['host_dump'], 'the two heroes do not differ'
    for old in list((host.root/'isolated-profile/Documents/gothic3').glob('G3T_D3.*')) + \
            list((client.root/'isolated-profile/Documents/gothic3').glob('*G3T_D3.*')) + \
            list((client.root/'isolated-profile/Documents/gothic3').glob('g3t-hero-*G3T_D3.txt')):
        old.unlink(missing_ok=True)
    start = host.bot.send('nativesave G3T_D3', 60)
    report['save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
    assert 'ok=1' in report['save'], report['save']
    time.sleep(3)
    report['client_persist_after_save'] = report_line(client.bot, 'persist', 'PERSIST ')
    assert 'records_written=1' in report['client_persist_after_save'], report['client_persist_after_save']
    source = host.root/'isolated-profile/Documents/gothic3'
    target = client.root/'isolated-profile/Documents/gothic3'
    report['copied'] = []
    for f in source.glob('G3T_D3.*'):
        shutil.copy2(f, target/('G3T_host_' + f.name))
        report['copied'].append(f.name)
    start = client.bot.send('nativeload G3T_host_G3T_D3', 60)
    report['client_load'] = client.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''), 120, 'client nativeload', start)
    wait_running(client, 180, 'client world running after load')
    time.sleep(10)
    report['client_persist_after_load'] = report_line(client.bot, 'persist', 'PERSIST ')
    report['client_dump_after_load'] = report_line(client.bot, 'herodump', 'HERODUMP ')
    assert report['client_dump_after_load'] == report['client_dump'], \
        "client hero after loading the host's save:\n" + report['client_dump_after_load'] + '\nwanted:\n' + report['client_dump']


def stageD3b(copies, report, negative=False):
    """Stage D3b, joining a campaign: the host plays a saved campaign; a player who starts the game and connects
    (without starting a game of its own) gets the host's save over the network, loads it by itself and is in the
    host's world: an item the host took before saving is gone there too."""
    host = copies['host'].launch('host', 'Host')
    enter_world(host)
    if negative:
        report_line(host.bot, 'campaignoff', 'CAMPAIGNOFF ')
    found, _ = world_items(host.bot)
    item = next(g for g, v in sorted(found.items(), key=lambda kv: kv[1]['distance']) if not v['name'].startswith('It_Gold'))
    report['item'] = f"{item} {found[item]['name']}"
    report['host_pick'] = pick(host.bot, item)
    wait_for(lambda: not item_exists(host.bot, item), 15, 'the host took the item')
    for old in list((host.root/'isolated-profile/Documents/gothic3').glob('G3T_D3B.*')) + \
            list((copies['client'].root/'isolated-profile/Documents/gothic3').glob('*G3T_D3B*')):
        old.unlink(missing_ok=True)
    start = host.bot.send('nativesave G3T_D3B', 60)
    report['save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
    assert 'ok=1' in report['save'], report['save']
    start = host.bot.send('releaselock')
    host.bot.line('LOCK scan complete', 10, 'lock scan', start)
    client = copies['client'].launch('client', 'Client')
    wait_running(client, 300, "client world running from the host's campaign")
    time.sleep(10)
    report['client_persist'] = report_line(client.bot, 'persist', 'PERSIST ')
    report['host_persist'] = report_line(host.bot, 'persist', 'PERSIST ')
    report['client_state'] = state(client.bot)
    assert 'last_received=G3T_host_G3T_D3B' in report['client_persist'], report['client_persist']
    report['client_item_exists'] = item_exists(client.bot, item)
    assert not report['client_item_exists'], "the item the host took is still in the client's world"
    report['host_sees_client'] = wait_for(lambda: remotes(host.bot).get(2, {}).get('puppet'), 60, 'host shows the client')


def clock_of(bot):
    line = report_line(bot, 'clock', 'CLOCK ')
    return line, {k: int(v) for k, v in re.findall(r'(\w+)=(-?\d+)', line)}


def stageG(copies, report, negative=False):
    """Stage G, one time of day: the host sets its clock six hours on; within one update the client has the
    same hour and minute (two game minutes of tolerance)."""
    host, client = join_both(copies)
    clear_raid(host, report)  # no raid needed: starts from the post-raid save (25.10.)
    if negative:
        report_line(client.bot, 'clockoff', 'CLOCKOFF ')
    report['host_before'], before = clock_of(host.bot)
    report['client_before'] = clock_of(client.bot)[0]
    target_hour = (before['hour'] + 6) % 24
    report['host_set'] = report_line(host.bot, f"clockset {target_hour:02d}_{before['minute']:02d}", 'CLOCK ')
    time.sleep(12)
    report['host_after'], host_now = clock_of(host.bot)
    report['client_after'], client_now = clock_of(client.bot)
    gap = abs(host_now['timestamp'] - client_now['timestamp'])
    report['gap_s'] = gap
    assert client_now['hour'] == host_now['hour'] and gap <= 180, f'client clock {gap} s off the host'


def stageG2(copies, report, negative=False):
    """Stage G2, a sleep moves time on for everyone: the client's clock jumps six hours (as a sleep does); the
    host follows within seconds instead of setting the client back."""
    host, client = join_both(copies)
    clear_raid(host, report)  # no raid needed: starts from the post-raid save (25.10.)
    if negative:
        report_line(client.bot, 'clockoff', 'CLOCKOFF ')
    time.sleep(6)  # first updates settle
    report['host_before'], before = clock_of(host.bot)
    target_hour = (before['hour'] + 6) % 24
    report['client_sleep'] = report_line(client.bot, f"clockset {target_hour:02d}_{before['minute']:02d}", 'CLOCK ')
    time.sleep(8)
    report['host_after'], host_now = clock_of(host.bot)
    report['client_after'], client_now = clock_of(client.bot)
    report['gap_s'] = abs(host_now['timestamp'] - client_now['timestamp'])
    assert host_now['hour'] == target_hour or host_now['hour'] == (target_hour + 1) % 24, f"host stayed at {host_now['hour']}h"
    assert client_now['hour'] == host_now['hour'] and report['gap_s'] <= 180, 'clocks apart after the sleep'


def stageWait(copies, report, negative=False):
    """P2, waiting until morning ("Bis zum Morgen", state PS_DuskToDawn) in co-op: the game runs the waiter's clock fast
    and slows its whole simulation; the mod jumps the clock to 09:00 instead and the host moves time on for everyone.
    The wait ends on its own (the game refills the waiter's hit points) and both clocks show the next morning.
    G3_WHO=host: the host waits. Negative control: waitoff on the waiter (the host sets a waiting client back)."""
    host, client = join_both(copies)
    waiter = host if os.environ.get('G3_WHO') == 'host' else client
    if negative:
        report_line(waiter.bot, 'waitoff', 'WAITOFF ')
    time.sleep(6)  # first updates settle
    report['host_evening'] = report_line(host.bot, 'clockset 20_00', 'CLOCK ')
    time.sleep(8)  # the client follows the host's evening
    _, evening = clock_of(host.bot)
    report['waiter_hp'] = report_line(waiter.bot, 'sethp 20', 'SETHP ')
    task = re.search(r'task=(\S+)', herostate(waiter.bot)).group(1)
    report['act'] = report_line(waiter.bot, f'act {task}-PS_DuskToDawn', 'ACT ')
    report['waiter_after'] = wait_for(lambda: (lambda h: [h] if 'state=PS_DuskToDawn' not in h else None)(herostate(waiter.bot)),
                                      30, 'the wait ends')[0]
    time.sleep(8)  # the host's answer and the next update
    report['host_after'], host_now = clock_of(host.bot)
    report['client_after'], client_now = clock_of(client.bot)
    report['gap_s'] = abs(host_now['timestamp'] - client_now['timestamp'])
    assert host_now['hour'] in (9, 10) and host_now['timestamp'] - evening['timestamp'] >= 12 * 3600, f"host at {host_now['hour']}h"
    assert client_now['hour'] == host_now['hour'] and report['gap_s'] <= 180, 'clocks apart after the wait'
    hp = re.search(r'hp=(\d+) max=(\d+)', report['waiter_after']).groups()
    assert hp[0] == hp[1], f'the wait did not refill hit points ({hp[0]}/{hp[1]})'


def infodiag(copies, report, negative=False):
    """Stage H exploration: does the AI wrapper see IS_Running_ProcessInfo, and which dialog commands run while
    the client holds the dialogs of stage 4c?"""
    report['where_host'] = scriptinfo_table(copies)
    stage4c(copies, report, negative)
    for tag in ('host', 'client'):
        report[tag+'_infocmd'] = report_line(copies[tag].bot, 'infocmd', 'INFOCMD ')
        report[tag+'_commands'] = [l for l in copies[tag].mod_log().splitlines() if 'infocmd:' in l][:40]
    report['host_knows_diego_event'] = event_known(copies['host'].bot, 'Diego_WarIsLost')
    report['status_note'] = 'diagnostic only'


def scriptinfo_table(copies):
    return None


def event_known(bot, name):
    return 'known=1' in report_line(bot, 'event '+name, 'EVENT ')


def stageH1(copies, report, negative=False):
    """Stage H1, shared story events: what a dialog makes one hero know (SetGameEvent) every hero knows, a
    cleared event is cleared everywhere, both ways (story sync, third domain)."""
    host, client = join_both(copies)
    clear_raid(host, report)  # no raid needed: starts from the post-raid save (25.10.)
    if negative:
        story_off(client)
    name = 'G3T_StageH1'
    report['client_set'] = report_line(client.bot, 'setevent '+name, 'EVENT ')
    report['host_knows'] = wait_for(lambda: event_known(host.bot, name) and ['yes'], 10, 'host knows the event')[0]
    report['client_clear'] = report_line(client.bot, 'clearevent '+name, 'EVENT ')
    wait_for(lambda: not event_known(host.bot, name), 10, 'host forgot the cleared event')
    report['host_set'] = report_line(host.bot, 'setevent '+name+'_host', 'EVENT ')
    wait_for(lambda: event_known(client.bot, name+'_host'), 10, 'client knows the host event')
    report['story_client'] = story(client.bot)


def npc_state(bot, name):
    line = report_line(bot, 'npcstate '+name, ('NPCSTATE ', 'REFUSED npcstate'))
    return line, (re.search(r'trade=(\d) teach=\d guard=(\d+)', line).groups() if line.startswith('NPCSTATE') else None)


def stageH2(copies, report, negative=False):
    """Stage H2, the world state after a dialog command reaches everyone: a dialog command on one machine changes
    an NPC (trade flag, guard status, as SetTradeEnabled / SetGuardStatus do); its resulting state goes to the host
    and on, and every copy of that NPC takes it, both ways."""
    host, client = join_both(copies)
    clear_raid(host, report)  # no raid needed: starts from the post-raid save (25.10.)
    if negative:
        report_line(client.bot, 'infocmdoff', 'INFOCMDOFF ')
    name = None
    for v in sorted(npcs(host.bot).values(), key=lambda v: v['name']):
        if v.get('dead'):
            continue
        line, state_now = npc_state(host.bot, v['name'])
        if state_now:
            name = v['name']
            break
    assert name, 'no NPC with a dialog near the host'
    report['npc'] = name
    report['before'] = [npc_state(host.bot, name)[0], npc_state(client.bot, name)[0]]
    report['client_change'] = report_line(client.bot, 'simdialog '+name, ('NPCSTATE ', 'REFUSED'))
    wanted = npc_state(client.bot, name)[1]
    report['host_follows'] = wait_for(lambda: npc_state(host.bot, name)[1] == wanted and [npc_state(host.bot, name)[0]], 10,
                                      "host takes the client's dialog result")[0]
    report['host_change'] = report_line(host.bot, 'simdialog '+name, ('NPCSTATE ', 'REFUSED'))
    wanted = npc_state(host.bot, name)[1]
    report['client_follows'] = wait_for(lambda: npc_state(client.bot, name)[1] == wanted and [npc_state(client.bot, name)[0]], 10,
                                        "client takes the host's dialog result")[0]
    report['infocmd'] = [report_line(c.bot, 'infocmd', 'INFOCMD ') for c in (host, client)]


def working_set_mb(pid):
    class Counters(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong), ('PeakWorkingSetSize', ctypes.c_size_t),
                    ('WorkingSetSize', ctypes.c_size_t), ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPagedPoolUsage', ctypes.c_size_t), ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaNonPagedPoolUsage', ctypes.c_size_t), ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32')
    handle = kernel.OpenProcess(0x1000 | 0x0400, False, pid)  # QUERY_LIMITED_INFORMATION | QUERY_INFORMATION
    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    ok = ctypes.WinDLL('psapi').GetProcessMemoryInfo(ctypes.c_void_p(handle), ctypes.byref(counters), counters.cb)
    kernel.CloseHandle(handle)
    return round(counters.WorkingSetSize / 1048576) if ok else -1


def soak(copies, report, negative=False):
    """Long session: host and client do a seeded random mix of what players do (walk, part and meet again, fight,
    loot, pick up, fall and help up, change NPCs through dialog results, sleep, save, load), with a health check
    after every action: both games run, see each other and stay connected. Memory is logged."""
    import random
    host, client = join_both(copies)
    rng = random.Random(1018)
    clear_raid(host, report)
    budget_end = time.monotonic() + max(120, copies['host'].deadline - time.monotonic() - 90)
    log, errors, health = [], [], []
    saved = False

    def act(name):
        nonlocal saved
        if name == 'walk':
            # Small steps (direct moves). The game's own AI goto to arbitrary circle points crashed the game in
            # gCNavZone_PS::DetectCollisionCircleExit even with the host alone (gotodiag2): not a player action.
            copy = rng.choice((host, client))
            for _ in range(3):
                report_line(copy.bot, f'step {rng.randrange(8)}', 'STEP ')
                time.sleep(0.5)
        elif name == 'part':
            pos = remotes(host.bot)[2]['pos']
            move_hero(client.bot, (pos[0] + 6000, pos[1], pos[2]))
            time.sleep(5)
            move_hero(client.bot, pos)
        elif name == 'hit':
            alive = [g for g, v in npcs(client.bot).items() if v.get('mirrored') and not v.get('dead')]
            if alive:
                hit_npc(client.bot, rng.choice(alive))
        elif name == 'pick':
            found, _ = world_items(client.bot)
            if found:
                pick(client.bot, min(found, key=lambda g: found[g]['distance']))
        elif name == 'fall':
            report_line(client.bot, 'killhero '+living_orc(client.bot), 'KILLHERO ') if any('Orc' in v.get('name', '') and not v.get('dead') for v in npcs(client.bot).values()) else None
            time.sleep(3)
            report_line(host.bot, 'givepotion It_Potion_Health-1', 'GIVEPOTION ')  # helping up costs a potion (stage A2)
            help_up(host, client, report, 'soak_fall')  # through the puppet's help option (owner 23.10.)
            wait_for(lambda: ' down=0 ' in herostate(client.bot) and ['up'], 30, 'client helped up')
        elif name == 'dialog':
            names = [v['name'] for v in npcs(host.bot).values() if not v.get('dead')]
            if names:
                report_line(rng.choice((host, client)).bot, 'simdialog '+rng.choice(names), ('NPCSTATE ', 'REFUSED'))
        elif name == 'sleep':
            hour = rng.randrange(24)
            report_line(rng.choice((host, client)).bot, f'clockset {hour:02d}_00', 'CLOCK ')
        elif name == 'save':
            start = host.bot.send('nativesave G3T_SOAK', 60)
            host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
            saved = True
        elif name == 'load' and saved:
            start = host.bot.send('nativeload G3T_SOAK', 60)
            host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''), 120, 'nativeload', start)
            wait_running(host, 180, 'host running after load')

    actions = ['walk'] * 4 + ['part', 'hit', 'hit', 'pick', 'fall', 'dialog', 'sleep', 'save'] + ['load']
    n = 0
    try:
        while time.monotonic() < budget_end:
            n += 1
            name = rng.choice(actions)
            began = time.monotonic()
            try:
                act(name)
                outcome = 'ok'
            except Exception as error:
                outcome = f'{type(error).__name__}: {str(error)[:120]}'
                errors.append(f'{n} {name}: {outcome}')
            # Health: both processes alive, both in the world, both see each other (a load may take a while).
            assert host.process.poll() is None and client.process.poll() is None, f'a game ended during action {n} ({name})'
            seen = wait_for(lambda: remotes(host.bot).get(2, {}).get('puppet') and remotes(client.bot).get(1, {}).get('puppet') and ['ok'], 90,
                            f'players see each other after action {n} ({name})')
            log.append((n, name, outcome, round(time.monotonic() - began, 1)))
            if n % 5 == 0:
                health.append((n, working_set_mb(host.process.pid), working_set_mb(client.process.pid)))
    finally:
        report['actions'] = n
        report['action_log'] = log
        report['action_errors'] = errors
        report['memory_mb'] = health
    report['states'] = [state(host.bot), state(client.bot)]


def navdiag(copies, report, negative=False):
    """Soak crash repro: the host crashed in gCNavZone_PS::DetectCollisionCircleExit (null nav zone) while its hero
    ran an AI goto and the client's puppet walked after the client had jumped 60 m away and back. G3_NAVMODE picks
    what repeats: 'part' (client jumps away and back), 'walk' (host AI goto), 'both'."""
    mode = os.environ.get('G3_NAVMODE', 'both')
    host, client = join_both(copies)
    clear_raid(host, report)
    cycles = []
    for i in range(20):
        if mode in ('part', 'both'):
            pos = remotes(host.bot)[2]['pos']
            move_hero(client.bot, (pos[0] + 6000, pos[1], pos[2]))
            time.sleep(3)
            move_hero(client.bot, pos)
        if mode in ('walk', 'both'):
            report_line(host.bot, f'walkto {i % 8}', 'WALKTO ')
        time.sleep(4)
        assert host.process.poll() is None and client.process.poll() is None, f'a game ended in cycle {i}'
        cycles.append((i, state(host.bot)[:40]))
    report['mode'] = mode
    report['cycles'] = len(cycles)
    report['status_note'] = 'diagnostic only'


def gotodiag(copies, report, negative=False):
    """Soak crash, alone: the host's hero by itself (no client, no puppet) goes from circle point 2 to point 4
    with the game's own AI goto (Entity::StartGoto), as before both soak crashes."""
    host = copies['host'].launch()
    enter_world(host)
    clear_raid(host, report)
    legs = []
    for i in range(6):
        for point in (2, 4):
            legs.append(report_line(host.bot, f'walkto {point}', 'WALKTO '))
            time.sleep(8)
            assert host.process.poll() is None, f'the game ended on leg {len(legs)} (walkto {point})'
            state(host.bot)
    report['legs'] = len(legs)
    report['status_note'] = 'diagnostic only'


def walkdiag(copies, report, negative=False):
    """Soak crash repro with both players: host and client both run AI gotos to random circle points (as the
    soak's walk actions do), so the host's hero paths while the client's puppet walks nearby."""
    import random
    host, client = join_both(copies)
    clear_raid(host, report)
    rng = random.Random(1018)
    legs = []
    for i in range(40):
        copy = rng.choice((host, client))
        point = rng.randrange(8)
        legs.append((i, copy.tag, point))
        report_line(copy.bot, f'walkto {point}', 'WALKTO ')
        time.sleep(1.5)
        assert host.process.poll() is None and client.process.poll() is None, f'a game ended after leg {i}: {legs[-3:]}'
        remotes(host.bot)
    report['legs'] = legs
    report['status_note'] = 'diagnostic only'


def gotodiag2(copies, report, negative=False):
    """Soak crash, alone, random points: the host's hero by itself runs AI gotos to 60 random circle points; the
    crash came when its navigation's zone entity was 'Nav_NavZone 1 Edge' (no nav zone), with or without a
    puppet near."""
    import random
    host = copies['host'].launch()
    enter_world(host)
    clear_raid(host, report)
    rng = random.Random(1018)
    legs = []
    for i in range(60):
        point = rng.randrange(8)
        legs.append(point)
        report_line(host.bot, f'walkto {point}', 'WALKTO ')
        time.sleep(1.5)
        assert host.process.poll() is None, f'the game ended after leg {i}: {legs[-4:]}'
        state(host.bot)
    report['legs'] = legs
    report['status_note'] = 'diagnostic only'


def infoscandiag(copies, report, negative=False):
    """Stage H3 exploration: which dialogs near the start give items, start fights, recruit companions or spawn?"""
    host = copies['host'].launch()
    enter_world(host)
    for command in ('Give', 'GiveAll', 'Grant', 'Erase', 'Attack', 'JoinNPC', 'JoinPlayer', 'Spawn'):
        start = host.bot.send('infoscan '+command, 30)
        host.bot.line('INFOSCAN end', 30, 'infoscan', start)
        report[command] = [l for l in host.bot.log()[start:].splitlines() if l.startswith('INFOSCAN')][:12]
    report['status_note'] = 'diagnostic only'


def stageH3b(copies, report, negative=False):
    """Stage H3b, a fight a dialog starts: the client holds Topork's duel dialog (Attack ... Duel) with the host's
    Topork; Topork attacks the client on the host (AssessTarget against the client's puppet), and the hits reach
    the client's hero."""
    host, client = join_both(copies)
    if negative:
        report_line(client.bot, 'infocmdoff', 'INFOCMDOFF ')
    clear_raid(host, report)
    report['host_to_topork'] = teleport_to(host.bot, 'Topork')
    time.sleep(6)
    report['client_to_topork'] = teleport_to(client.bot, 'Topork')
    time.sleep(8)
    wait_for(lambda: npc_named(client.bot, 'Topork')[1].get('mirrored') and ['yes'], 20, 'Topork mirrored on the client')
    report['client_combat_before'] = combat_state(client.bot)
    # Other NPCs may cut in with an important info (Hamlar threatened once); talk until the partner is Topork.
    report['partners'] = []
    for attempt in range(6):
        start = client.bot.send('assesstalk Topork', 20)
        report['assesstalk'] = client.bot.line('ASSESSTALK ', 10, 'assesstalk', start)
        if 'result=1' not in report['assesstalk']:
            time.sleep(2)
            continue
        try:
            wait_for(lambda: talkstate(client.bot).get('running') == '1', 15, 'client in a dialog')
        except AssertionError:
            continue
        partner = talkstate(client.bot).get('partner')
        report['partners'].append(partner)
        if partner == 'Topork':
            break
        start = client.bot.send('endtalk', 20)
        client.bot.line('TALKSTATE ', 10, 'endtalk', start)
        wait_for(lambda: talkstate(client.bot).get('running') == '0', 15, 'other dialog ends')
        time.sleep(3)
    assert report['partners'] and report['partners'][-1] == 'Topork', 'never in a dialog with Topork: ' + str(report['partners'])
    wait_for(lambda: (lambda t: t.get('waiting') == '0' and int(t.get('available', '0')) > 0)(talkstate(client.bot)) and ['ok'], 40, 'a dialog choice')
    report['talkinfo'] = report_line(client.bot, 'talkinfo BPANKRATZ31298', ('TALKINFO ', 'REFUSED talkinfo'))
    # The option's spoken lines come first; its Attack line ends the dialog by itself.
    wait_for(lambda: talkstate(client.bot).get('running') == '0', 60, 'the duel dialog ends')
    report['host_fight'] = wait_for(lambda: ('attacks player 2' in host.mod_log()) and [next(l for l in host.mod_log().splitlines() if 'attacks player 2' in l)],
                                    25, "host starts Topork's fight against the client")[0]
    report['host_topork'] = npc_named(host.bot, 'Topork')[1]
    samples = []
    for _ in range(8):
        time.sleep(2)
        _, t = npc_named(host.bot, 'Topork')
        puppet = remotes(host.bot).get(2, {}).get('puppet_pos')
        samples.append((t.get('task'), round(distance(t['pos'], puppet)) if puppet else None, combat_state(host.bot)['player_hits_sent']))
    report['host_topork_samples'] = samples
    report['client_hit'] = wait_for(lambda: combat_state(client.bot)['player_hits_taken'] > report['client_combat_before']['player_hits_taken'] and
                                    [combat_state(client.bot)], 40, 'Topork hits the client')[0]
    report['infocmd'] = [report_line(c.bot, 'infocmd', 'INFOCMD ') for c in (host, client)]


def aggrodiag(copies, report, negative=False):
    """Do the host's NPCs attack a player's puppet by themselves? An orc raider is set on the client's puppet with
    reason Enemy (12), then Topork with reason Duel (17); the host's hits sent to the client and the attacker's
    AI state are sampled."""
    host, client = join_both(copies)
    results = {}
    for name, reason in (('Orc_GameStartRaider_Warrior_01', 12), ('Topork', 17)):
        if name == 'Topork':
            clear_raid(host, report)
            teleport_to(host.bot, 'Topork')
            time.sleep(6)
            teleport_to(client.bot, 'Topork')
            time.sleep(8)
        else:
            npc = npc_named(host.bot, name)[1]
            if npc:
                move_hero(client.bot, (npc['pos'][0] + 300, npc['pos'][1], npc['pos'][2]))
                time.sleep(3)
        before = combat_state(host.bot)['player_hits_sent']
        line = report_line(host.bot, f'aggro {name}_{reason}', ('AGGRO ', 'REFUSED aggro'))
        samples = []
        for _ in range(10):
            time.sleep(2)
            samples.append((find_npc(host.bot, name)[5:260], combat_state(host.bot)['player_hits_sent'] - before, herostate(host.bot)[5:20]))
        results[name] = {'aggro': line, 'samples': samples}
    report['results'] = results
    report['status_note'] = 'diagnostic only'


def stageE1(copies, report, negative=False):
    """Stage E1, regional ownership: a client far from the host simulates its surroundings and sends those NPCs to
    the host; when the host arrives there it shows the client's NPCs (mirrors, own AI off, same places) instead of
    simulating them a second time; when the client leaves, the host simulates them again."""
    host, client = join_both(copies)
    clear_raid(host, report)  # no raid needed: starts from the post-raid save (25.10.)
    if negative:
        report_line(client.bot, 'ownoff', 'OWNOFF ')
    target = next(r for r in far_rows(host.bot) if 600 < r['distance'] < 1500)
    report['region'] = target['name']
    teleport_to(client.bot, target['name'])
    time.sleep(10)
    client_view = npcs(client.bot)
    report['client_own'] = sum(1 for v in client_view.values() if not v['mirrored'] and not v['dead'])
    teleport_to(host.bot, target['name'])
    time.sleep(10)
    host_view = npcs(host.bot)
    client_view = npcs(client.bot)
    from_client = [g for g, v in host_view.items() if v['mirrored'] and v['owner'] == 2]
    report['host_mirrors_of_client'] = len(from_client)
    report['host_own'] = sum(1 for v in host_view.values() if not v['mirrored'])
    assert len(from_client) >= 3, f"host shows {len(from_client)} of the client's NPCs: {[v['name'] for v in host_view.values()][:10]}"
    gaps = [distance(host_view[g]['pos'], client_view[g]['pos']) for g in from_client if g in client_view]
    report['position_gaps_cm'] = sorted(round(x) for x in gaps)
    assert gaps and sorted(gaps)[len(gaps) // 2] < 150, f'positions apart: {report["position_gaps_cm"]}'
    double = [host_view[g]['name'] for g in from_client if g in client_view and client_view[g]['mirrored']]
    assert not double, f'mirrored on both machines (nobody simulates them): {double}'
    # The client leaves: after the stale time the host simulates them itself.
    teleport_to(client.bot, 'Diego')
    time.sleep(10)
    after = npcs(host.bot)
    report['host_after_client_left'] = sum(1 for g in from_client if g in after and not after[g]['mirrored'])
    assert report['host_after_client_left'] >= len(from_client) // 2, 'host did not take its NPCs back'


def stageE2(copies, report, negative=False):
    """Stage E2, fights across owners: in the client's region (its NPCs mirrored on the host) the host's hits on
    such an NPC go to the client, which applies them; a kill pays the host. A client-owned NPC attacking the host
    hits the host's real hero (judged there)."""
    host, client = join_both(copies)
    clear_raid(host, report)  # no raid needed: starts from the post-raid save (25.10.)
    target = next(r for r in far_rows(host.bot) if 600 < r['distance'] < 1500)
    report['region'] = target['name']
    teleport_to(client.bot, target['name'])
    time.sleep(10)
    teleport_to(host.bot, target['name'])
    time.sleep(10)
    host_view = npcs(host.bot)
    from_client = [(g, v) for g, v in host_view.items() if v['mirrored'] and v['owner'] == 2 and not v['dead']]
    assert len(from_client) >= 2, f'host shows {len(from_client)} client-owned NPCs'
    victim_guid, victim = min(from_client, key=lambda gv: distance(gv[1]['pos'], remotes(client.bot)[1]['pos']))
    report['victim'] = victim['name']
    if negative:
        report_line(host.bot, 'combatoff', 'COMBATOFF')  # the host applies its hits to its mirror only
    # 1. The host's hero hits the client's NPC until it dies on the client.
    report_line(host.bot, 'arm '+WEAPON, 'ARM ')
    before_client = npcs(client.bot).get(victim_guid, {}).get('hp')
    hits = 0
    for hits in range(1, 61):
        hit_npc(host.bot, victim_guid)
        time.sleep(0.3)
        if npcs(client.bot).get(victim_guid, {}).get('dead'):
            break
    report['hits'] = hits
    report['client_victim'] = npcs(client.bot).get(victim_guid)
    report['host_combat'] = combat_state(host.bot)
    report['client_combat'] = combat_state(client.bot)
    assert report['host_combat']['claims_sent'] >= 1, 'host sent no hit to the owner'
    assert report['client_victim'] and report['client_victim']['dead'], f"client's NPC not dead after {hits} host hits (hp {before_client} -> {report['client_victim']})"
    wait_for(lambda: npcs(host.bot).get(victim_guid, {}).get('dead'), 10, 'host shows it dead')
    assert report['host_combat']['xp_awarded'] >= 1 or combat_state(host.bot)['xp_awarded'] >= 1, 'the host was not rewarded'
    # 2. The client's NPCs fight the host by themselves there: their hits reach the host's hero from the client
    #    (forwarded and judged on the host; the host's own NPCs hit it directly and are not counted here).
    report['host_hits_from_client_npcs'] = combat_state(host.bot)['forwarded_judged']
    if report['host_hits_from_client_npcs'] == 0:
        other = next((v['name'] for g, v in from_client if g != victim_guid), None)
        report['aggro'] = report_line(client.bot, f'aggro {other}_12', ('AGGRO ', 'REFUSED aggro'))
        wait_for(lambda: combat_state(host.bot)['forwarded_judged'] > 0 and ['hit'], 40, 'a client-owned NPC hits the host')


def talk_to(client, name, report, key):
    """Address an NPC until it is the dialog partner (others may cut in), then wait for a choice."""
    report[key] = []
    for attempt in range(6):
        start = client.bot.send('assesstalk '+name, 20)
        if 'result=1' not in client.bot.line('ASSESSTALK ', 10, 'assesstalk', start):
            time.sleep(2)
            continue
        try:
            wait_for(lambda: talkstate(client.bot).get('running') == '1', 15, 'client in a dialog')
        except AssertionError:
            continue
        partner = talkstate(client.bot).get('partner')
        report[key].append(partner)
        if partner == name:
            break
        start = client.bot.send('endtalk', 20)
        client.bot.line('TALKSTATE ', 10, 'endtalk', start)
        wait_for(lambda: talkstate(client.bot).get('running') == '0', 15, 'other dialog ends')
        time.sleep(3)
    assert report[key] and report[key][-1] == name, f'never in a dialog with {name}: {report[key]}'
    wait_for(lambda: (lambda t: t.get('waiting') == '0' and int(t.get('available', '0')) > 0)(talkstate(client.bot)) and ['ok'], 40, 'a dialog choice')


def stageE3(copies, report, negative=False):
    """Stage E3, a companion: the client recruits the host's Lester in a dialog (JoinPlayer true); the client takes
    him over (its machine simulates him, so the game's follow state follows the client's hero) and the host mirrors
    him; when the client walks on, Lester follows the client on both machines."""
    host, client = join_both(copies)
    if negative:
        report_line(client.bot, 'infocmdoff', 'INFOCMDOFF ')
    clear_raid(host, report)
    teleport_to(host.bot, 'Lester')
    time.sleep(6)
    teleport_to(client.bot, 'Lester')
    time.sleep(8)
    wait_for(lambda: npc_named(client.bot, 'Lester')[1].get('mirrored') and ['yes'], 20, 'Lester mirrored on the client')
    # NEWM1055 needs the story event GetLester (normally from an earlier dialog).
    report['event'] = report_line(client.bot, 'setevent GetLester', 'EVENT ')
    talk_to(client, 'Lester', report, 'partners')
    report['talkinfo'] = report_line(client.bot, 'talkinfo NEWM1055', ('TALKINFO ', 'REFUSED talkinfo'))
    wait_for(lambda: talkstate(client.bot).get('running') == '0', 90, 'the recruiting dialog ends')
    time.sleep(3)
    report['client_lester'] = npc_named(client.bot, 'Lester')[1]
    report['host_lester'] = find_npc(host.bot, 'Lester')
    assert 'mirrored=1' in report['host_lester'] and 'owner=2' in report['host_lester'].split(), f"host does not show the client's Lester: {report['host_lester']}"
    assert not report['client_lester']['mirrored'], 'Lester still mirrored on the client'
    # The client walks on; Lester follows it on both machines.
    def pos_of(line):
        return tuple(float(v) for v in re.search(r'pos=(\S+)', line)[1].split('_'))

    lester_start = pos_of(find_npc(client.bot, 'Lester'))

    def following():
        # The follow state stops a few metres short (7 m seen); he must have walked after the client.
        c_hero = remotes(host.bot)[2]['pos']
        h_lester, c_lester = find_npc(host.bot, 'Lester'), find_npc(client.bot, 'Lester')
        near = distance(pos_of(h_lester), c_hero) < 1000 and distance(pos_of(c_lester), c_hero) < 1000
        walked = distance(pos_of(c_lester), lester_start) > 300
        return near and walked and [h_lester, c_lester]
    start_pos = remotes(host.bot)[2]['pos']
    move_hero(client.bot, (start_pos[0] + 1200, start_pos[1], start_pos[2]))
    report['following'] = wait_for(following, 40, 'Lester follows the client')[0]
    # Handed back (JoinPlayer false): the host simulates him again and the client mirrors him.
    report['giveback'] = report_line(client.bot, 'join Lester_0', ('JOIN ', 'REFUSED join'))
    assert 'changed=1' in report['giveback'], report['giveback']
    def back_on_host():
        h, c = find_npc(host.bot, 'Lester'), find_npc(client.bot, 'Lester')
        return 'mirrored=0' in h and 'mirrored=1' in c and 'owner=1' in c.split() and [h, c]
    report['given_back'] = wait_for(back_on_host, 20, 'Lester back with the host')
    # Taken over again, then the client goes silent: the host ends the claim after 15 s and tells the client.
    report['rejoin'] = report_line(client.bot, 'join Lester_1', ('JOIN ', 'REFUSED join'))
    wait_for(lambda: 'owner=2' in find_npc(host.bot, 'Lester').split() and ['yes'], 15, 'the client has Lester again')
    report['ownoff'] = report_line(client.bot, 'ownoff', 'OWNOFF ')
    report['revoked'] = wait_for(back_on_host, 40, 'the host takes back a silent claim')
    assert 'the host took back Lester' in client.mod_log(), 'the client was not told'


def stagePartyJoin(copies, report, negative=False):
    """P9, joining the player's party (Infos.pak: SKALVERAM336 "CmdPartyJoin", the info every party-enabled NPC offers,
    condition 14; FILLER960 "PartyDismiss", condition 15). G3_PARTY (default Diego) is made party-enabled as a dialog's
    SetPartyEnabled true does (QA partyenable on the host, passed on). The player (G3_WHO=host: the host alone, the
    single-player control; default the client) talks to him, chooses the join: he follows the player (the client takes him
    over, the host mirrors him), walks after it; then the dismiss: he leaves the party (the client gives him back). Counted on
    the player's machine: JoinPlayerParty, CanJoinPlayerParty, EndGoto (scriptwatch). Negative control: guidesyncoff on the
    client (it does not take its new follower over; the host's game keeps him)."""
    host, client = join_both(copies)
    player = host if os.environ.get('G3_WHO') == 'host' else client
    npc = os.environ.get('G3_PARTY', 'Diego')
    if negative:
        report_line(client.bot, 'guidesyncoff', 'GUIDESYNCOFF ')
    clear_raid(host, report)
    teleport_to(host.bot, npc)
    if player is client:
        teleport_to(client.bot, npc)
        time.sleep(8)
        wait_for(lambda: (npc_named(client.bot, npc)[1] or {}).get('mirrored') and ['yes'], 20, f'{npc} mirrored on the client')
    else:
        x, y, z = hero_pos(host.bot)
        move_hero(client.bot, (x + 3000, y + 50, z))
        time.sleep(4)
    mob = os.environ.get('G3_JOIN') == 'mob'  # the mob instead (KALVERAMX228 "CmdMobJoin", condition 28; KALVERAMX232 dismisses)
    join_info, dismiss_info = ('KALVERAMX228', 'KALVERAMX232') if mob else ('SKALVERAM336', 'FILLER960')
    report['watch'] = [report_line(player.bot, 'scriptwatch ' + n, 'SCRIPTWATCH ')[12:80]
                       for n in ('JoinPlayerMob' if mob else 'JoinPlayerParty', 'CanJoinPlayerParty', 'EndGoto')]
    report['enable'] = report_line(host.bot, 'partyenable ' + npc + ('_mob' if mob else ''), ('PARTYENABLE ', 'REFUSED partyenable'))
    time.sleep(3)

    def choose(info, key):
        for _ in range(4):
            teleport_to(player.bot, npc)
            time.sleep(2)
            line, infos = talk_infos(player, npc)
            report.setdefault(key + '_offered', []).append(line[:200])
            if info in infos:
                report[key] = report_line(player.bot, 'talkinfo ' + info, ('TALKINFO ', 'REFUSED talkinfo'))
                wait_for(lambda: talkstate(player.bot).get('running') == '0', 60, f'the dialog ({info}) ends')
                time.sleep(3)
                return True
            report_line(player.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))
            time.sleep(3)
        return False

    assert choose(join_info, 'join'), f"{npc} never offered the join: {report.get('join_offered')}"
    report['joined'] = {c.tag: find_npc(c.bot, npc)[:300] for c in (host, client)}
    pos_of = lambda line: tuple(float(v) for v in re.search(r'pos=(\S+)', line)[1].split('_'))
    start = hero_pos(player.bot)
    npc_start = pos_of(find_npc(player.bot, npc))
    move_hero(player.bot, (start[0] + 1200, start[1], start[2]))
    if player is host:
        # The single-player control: he joins and follows at once (ZS_FollowPlayer). After the host's teleport he stood in
        # talk and ambient tasks for 60 s (twice, 26.10.), so the walk is not part of the control.
        report['follow_task'] = re.search(r' task=(\S+)', report['joined']['host'])[1]
        start = player.bot.send('scriptwatch', 20)
        player.bot.line('SCRIPTWATCH end', 10, 'scriptwatch', start)
        report['scripts'] = [l[12:] for l in player.bot.log()[start:].splitlines() if l.startswith('SCRIPTWATCH name=')]
        assert 'Follow' in report['follow_task'], f"{npc} does not follow: {report['follow_task']}"
        return
    # He walks after the player (the follow state stops some metres short).
    walk = 300
    report['following'] = wait_for(lambda: (lambda l: ' task=ZS_FollowPlayer' in l and distance(pos_of(l), hero_pos(player.bot)) < 1200 and
                                            distance(pos_of(l), npc_start) >= walk and [l])(find_npc(player.bot, npc)), 60, f'{npc} follows')[0][:200]
    report['follow_task'] = re.search(r' task=(\S+)', report['following'])[1]
    report['seen'] = {c.tag: find_npc(c.bot, npc)[:300] for c in (host, client)}
    report['dismissed'] = choose(dismiss_info, 'dismiss')
    time.sleep(4)
    report['after'] = {c.tag: find_npc(c.bot, npc)[:300] for c in (host, client)}
    report['after_task'] = (npc_named(player.bot, npc)[1] or {}).get('task')
    start = player.bot.send('scriptwatch', 20)
    player.bot.line('SCRIPTWATCH end', 10, 'scriptwatch', start)
    report['scripts'] = [l[12:] for l in player.bot.log()[start:].splitlines() if l.startswith('SCRIPTWATCH name=')]
    assert 'Follow' in report['follow_task'], f"{npc} does not follow: {report['follow_task']}"
    joined = 'JoinPlayerMob' if mob else 'JoinPlayerParty'
    assert any(l.startswith(f'name={joined} calls=1 ') for l in report['scripts']), f"{joined} did not run once on the player's machine: {report['scripts']}"
    if player is client:
        assert 'owner=2' in report['seen']['host'].split(), f"the host does not show the client's {npc}: {report['seen']['host']}"


def stageCompanionWait(copies, report, negative=False):
    """P9, a companion told to wait (Infos.pak: KALVERAMX231, the party member's info "wait here": SaySVM CmdWait,
    SayHoldingPosition). The player (G3_WHO=host: the host alone, the single-player control; default
    the client, which takes Lester over when he joins) recruits Lester (NEWM1055, as in E3), walks 12 m (he follows), tells him
    to wait, walks 15 m on: he waits there (ZS_WaitForPlayer on the machine that simulates him) and stays put on both
    machines. Counted on the player's machine: the P9 scripts (scriptwatch) and ZS_WaitForPlayer
    (aicount). Negative control: infocmdoff on the client (as in E3)."""
    host, client = join_both(copies)
    player = host if os.environ.get('G3_WHO') == 'host' else client
    other = client if player is host else host
    if negative:
        report_line(client.bot, 'infocmdoff', 'INFOCMDOFF ')
    clear_raid(host, report)
    teleport_to(host.bot, 'Lester')
    if player is client:
        teleport_to(client.bot, 'Lester')
        time.sleep(8)
        wait_for(lambda: npc_named(client.bot, 'Lester')[1].get('mirrored') and ['yes'], 20, 'Lester mirrored on the client')
    else:
        x, y, z = hero_pos(host.bot)
        move_hero(client.bot, (x + 3000, y + 50, z))
        time.sleep(4)
    report['watch'] = [report_line(player.bot, 'scriptwatch ' + n, 'SCRIPTWATCH ')[12:80]
                       for n in ('JoinPlayerParty', 'CanJoinPlayerParty', 'CanHealSelf', 'SelectRoutineDestination', 'EndGoto')]
    report_line(player.bot, 'aicount ZS_WaitForPlayer', ('AICOUNT end',))
    report['event'] = report_line(player.bot, 'setevent GetLester', 'EVENT ')

    def say(info, key):
        talk_to(player, 'Lester', report, key + '_talk')
        report[key] = report_line(player.bot, 'talkinfo ' + info, ('TALKINFO ', 'REFUSED talkinfo'))
        wait_for(lambda: talkstate(player.bot).get('running') == '0', 90, f'the dialog ({info}) ends')
        time.sleep(3)

    say('NEWM1055', 'recruit')
    pos_of = lambda line: tuple(float(v) for v in re.search(r'pos=(\S+)', line)[1].split('_'))
    start = hero_pos(player.bot)
    move_hero(player.bot, (start[0] + 1200, start[1], start[2]))
    report['following'] = wait_for(lambda: (lambda l: distance(pos_of(l), hero_pos(player.bot)) < 1200 and [l])(
        find_npc(player.bot, 'Lester')), 60, 'Lester follows')[0][:200]
    say('KALVERAMX231', 'wait')
    lester_at = pos_of(find_npc(player.bot, 'Lester'))
    here = hero_pos(player.bot)
    move_hero(player.bot, (here[0] + 1500, here[1], here[2]))
    time.sleep(12)
    report['waiting'] = {c.tag: find_npc(c.bot, 'Lester')[:220] for c in (host, client)}
    report['waiting_task'] = (npc_named(player.bot, 'Lester')[1] or {}).get('task')
    report['moved'] = {c.tag: round(distance(pos_of(find_npc(c.bot, 'Lester')), lester_at)) for c in (host, client)}
    start = player.bot.send('scriptwatch', 20)
    player.bot.line('SCRIPTWATCH end', 10, 'scriptwatch', start)
    report['scripts'] = [l[12:] for l in player.bot.log()[start:].splitlines() if l.startswith('SCRIPTWATCH name=')]
    start = player.bot.send('aicount', 20)
    player.bot.line('AICOUNT end', 10, 'aicount', start)
    report['states'] = [l[8:] for l in player.bot.log()[start:].splitlines() if l.startswith('AICOUNT name=')]
    assert 'ZS_WaitForPlayer' in (report['waiting_task'] or ''), f"Lester does not wait: {report['waiting_task']}"
    assert all(d < 300 for d in report['moved'].values()), f"Lester walked off while told to wait: {report['moved']}"


def native_watch(bot, names):
    """QA nativewatch: count every call of these scripts at their code (also the game's direct calls)."""
    return [report_line(bot, 'nativewatch ' + n, 'NATIVEWATCH ')[12:100] for n in names]


def native_counts(bot, only=None):
    """{script: {'calls', 'nonzero', 'hero_self', 'mirrored_self', 'puppet_player', 'selves': {name: {...}}}} since the last reset;
    only: selves lists that caller alone (else the eight most frequent)."""
    start = bot.send('nativewatch only_' + only if only else 'nativewatch', 20)
    bot.line('NATIVEWATCH end', 10, 'nativewatch', start)
    out = {}
    for line in bot.log()[start:].splitlines():
        m = re.match(r'NATIVEWATCH name=(\S+) calls=(\d+) nonzero=(\d+) hero_self=(\d+) mirrored_self=(\d+) puppet_player=(\d+) '
                     r'self=(\S+) other=(\S+) selves=(\S+)', line)
        if not m:
            continue
        selves = {}
        for part in ([] if m[9] == '-' else m[9].split(',')):
            who, calls, nonzero, last, puppet = part.rsplit(':', 4)
            selves[who] = {'calls': int(calls), 'nonzero': int(nonzero), 'last': int(last), 'puppet_player': int(puppet)}
        out[m[1]] = {'calls': int(m[2]), 'nonzero': int(m[3]), 'hero_self': int(m[4]), 'mirrored_self': int(m[5]),
                     'puppet_player': int(m[6]), 'selves': selves}
    return out


def native_calls(counts, script, who):
    return counts.get(script, {}).get('selves', {}).get(who, {}).get('calls', 0)


def p9smalldiag(copies, report, negative=False):
    """P9/Basis remainder (26.10., pair 2): where sleeping animals and taken destinations are. The host lists every animal
    it has with a sleeping point (QA sleepers) at Ardea, Kent and the scavenger meadow north of Ardea at 23 o'clock, and
    the routine destinations of the NPCs near it at Ardea (QA waitfree NAME_show); EndGoto, SelectRoutineDestination,
    CanHealSelf counted at their code on both machines over 20 s (QA nativewatch). Diagnostic."""
    host, client = join_both(copies)
    clear_raid(host, report)
    names = ('CanHealSelf', 'SelectRoutineDestination', 'EndGoto')
    for c in (host, client):
        report[c.tag + '_watch'] = native_watch(c.bot, names)
    report['clock'] = report_line(host.bot, 'clockset 23_00', 'CLOCK ')

    def sleepers(bot, arg=''):
        start = bot.send('sleepers ' + arg if arg else 'sleepers', 30)
        bot.line('SLEEPERS end', 20, 'sleepers', start)
        return [l[8:300] for l in bot.log()[start:].splitlines() if l.startswith(('SLEEPER ', 'SLEEPERS end'))]
    report['ardea_all'] = sleepers(host.bot, 'all')
    report['ardea_dest'] = []
    for v in sorted({v['name'] for v in npcs(host.bot).values() if not v['dead']})[:25]:
        report['ardea_dest'].append(report_line(host.bot, f'waitfree {v}_show', ('WAITFREE ', 'REFUSED waitfree'))[9:200])
    time.sleep(20)
    report['counts_ardea'] = {c.tag: native_counts(c.bot) for c in (host, client)}
    for place in os.environ.get('G3_PLACES', 'Kent,meadow').split(','):
        if place == 'meadow':
            move_hero(host.bot, (90500.0, 5300.0, -6600.0))
        else:
            teleport_to(host.bot, place)
        time.sleep(12)
        report_line(host.bot, 'clockset 23_00', 'CLOCK ')
        for n in sorted({v['name'] for v in npcs(host.bot).values() if not v['dead'] and v['name'] not in ('PC_Hero',)}):
            report_line(host.bot, 'npcroutine ' + n, ('NPCROUTINE ', 'REFUSED npcroutine'))
        time.sleep(5)
        report['sleepers_' + place] = sleepers(host.bot, 'all')
    report['status_note'] = 'diagnostic only'


def stageGuideRoutine(copies, report, negative=False):
    """P9, the routine functions the game calls directly, not by name (CanHealSelf, SelectRoutineDestination: ContinueRoutine,
    re/out/hero2-scriptgame.c; EndGoto: the routine tasks, hero5-scriptgame.c), so scriptwatch never saw them (p9-rest-b.md);
    QA nativewatch counts them at their code. (1) Region: the player (G3_WHO=host: the host alone, the single-player
    control; default the client) goes to a place 300-1500 m from the other one; for 25 s the NPCs there run their routines
    on the player's machine: EndGoto with them as self there, never on the other machine. (2) Guide: the player takes
    Mitch_Trolls (as in stageGuide); Mitch guides it, simulated on the player's machine. CanHealSelf answers yes when
    GetPlayer's party leader is the NPC (re/findings/p8-classification-followup-20261024-c.md: the guide). Mitch at 30 % of
    his hit points chooses his routine again (QA npcroutine: FullStop, ContinueRoutine asks CanHealSelf below 61 %): yes on
    the player's machine; at full hit points: SelectRoutineDestination for him there, and he guides on. The other machine
    runs neither for him. Negative control: guidesyncoff on the client (it does not take Mitch over)."""
    host, client = join_both(copies)
    player = host if os.environ.get('G3_WHO') == 'host' else client
    other = client if player is host else host
    quest, npc = 'Mitch_Trolls', 'Mitch'
    if negative:
        report_line(client.bot, 'guidesyncoff', 'GUIDESYNCOFF ')
    clear_raid(host, report)
    names = ('CanHealSelf', 'SelectRoutineDestination', 'EndGoto')
    for c in (host, client):
        report[c.tag + '_watch'] = native_watch(c.bot, names)
    # (1) The player's own region.
    rows = [r for r in far_rows(other.bot) if 300 < r['distance'] < 1500 and r['name'] != npc]
    assert rows, 'no NPC 300-1500 m away'
    place = report['region'] = rows[0]['name']
    teleport_to(player.bot, place)
    time.sleep(8)
    for c in (host, client):
        report_line(c.bot, 'nativewatch reset', 'NATIVEWATCH ')
    time.sleep(25)
    region = {c.tag: native_counts(c.bot) for c in (host, client)}
    local_names = {v['name'] for v in npcs(player.bot).values() if not v['mirrored']}
    goto_selves = {w: s['calls'] for w, s in region[player.tag].get('EndGoto', {}).get('selves', {}).items() if w in local_names}
    report['region_endgoto'] = goto_selves
    report['region_other_endgoto'] = {w: native_calls(native_counts(other.bot, w), 'EndGoto', w) for w in goto_selves}
    # (2) The guide.
    teleport_to(other.bot, npc)
    x, y, z = hero_pos(other.bot)
    move_hero(other.bot, (x + 3000, y, z))
    take_quest(player, npc, ['SKALVERAM3409', 'SKALVERAM3395'], report, runquests=['Zakosh_KillTrolls'], events=['Mitch_WhereTrolls'])
    report['quest_run'] = {c.tag: quest_status(c.bot, quest) for c in (host, client)}
    if player is client:
        report['guiding'] = wait_for(lambda: (lambda g: g.get('claimed') == '1' and g.get('task', '').startswith('ZS_GuidePlayer') and [g])(
            guide_state(client.bot, npc)), 40, f'{npc} guides the client on the client')[0]
    else:
        report['guiding'] = wait_for(lambda: (lambda g: g.get('hero_leader') == npc and g.get('task', '').startswith('ZS_GuidePlayer') and [g])(
            guide_state(host.bot, npc)), 90, f'{npc} guides the host')[0]

    def again(tag):
        for c in (host, client):
            report_line(c.bot, 'nativewatch reset', 'NATIVEWATCH ')
        report[tag + '_routine'] = report_line(player.bot, 'npcroutine ' + npc, ('NPCROUTINE ', 'REFUSED npcroutine'))
        time.sleep(4)
        report[tag + '_after'] = find_npc(player.bot, npc)[:260]
        report[tag] = {c.tag: native_counts(c.bot, npc) for c in (host, client)}
    hp = int(re.search(r' hp=(-?\d+)', find_npc(player.bot, npc))[1])
    report['hp_set'] = report_line(player.bot, f'npchp {npc}_{max(1, hp * 3 // 10)}', ('NPCHP ', 'REFUSED npchp'))
    again('hurt')
    report['hp_back'] = report_line(player.bot, f'npchp {npc}_{hp}', ('NPCHP ', 'REFUSED npchp'))
    again('full')
    time.sleep(4)
    report['guide_after'] = guide_state(player.bot, npc)
    summary = report['summary'] = {
        'heal_player': report['hurt'][player.tag].get('CanHealSelf', {}).get('selves', {}).get(npc, {}),
        'heal_other': native_calls(report['hurt'][other.tag], 'CanHealSelf', npc),
        'select_player': native_calls(report['full'][player.tag], 'SelectRoutineDestination', npc),
        'select_other': native_calls(report['full'][other.tag], 'SelectRoutineDestination', npc),
        'region_endgoto_npcs': len(goto_selves), 'region_other_endgoto': sum(report['region_other_endgoto'].values())}
    assert summary['heal_player'].get('calls', 0) >= 1 and summary['heal_player'].get('last') == 1, \
        f"CanHealSelf did not say yes for {npc} on the player's machine: {summary}"
    assert summary['select_player'] >= 1, f"SelectRoutineDestination did not run for {npc} on the player's machine: {summary}"
    assert not summary['select_other'] and not summary['heal_other'], f"the other machine ran {npc}'s routine too: {summary}"
    assert goto_selves, f"no EndGoto for the NPCs at {place} on the player's machine: {region[player.tag].get('EndGoto')}"
    assert not summary['region_other_endgoto'], f"the other machine ran EndGoto for the player's NPCs at {place}: {report['region_other_endgoto']}"
    assert report['guide_after'].get('task', '').startswith('ZS_GuidePlayer'), f"{npc} no longer guides: {report['guide_after']}"


def followtimediag(copies, report, negative=False):
    """P8 klein (GetFollowTime, GetDiffToPlayerLevel): GetFollowTime (called directly by the callbacks OnAttack_Goto and
    OnWatchFight_Goto, Script_Game 0x159fa, 0x28b4d) gives a wolf 15 s to follow a sprinting Entity::GetPlayer, else 20 s
    (other species 9 or 5 without the player); GetDiffToPlayerLevel has no caller (only its registration refers to it). The
    player (G3_WHO=host: the host alone; default the client) goes up to the nearest wolf on the meadow north of Ardea (the
    host 25 m off simulates it), lets it attack, then runs 25 m off twice (QA runto: running, not sprinting); both counted
    at their code on both machines (QA nativewatch). Diagnostic."""
    host, client = join_both(copies)
    clear_raid(host, report)
    player = host if os.environ.get('G3_WHO') == 'host' else client
    other = client if player is host else host
    names = ('GetFollowTime', 'GetDiffToPlayerLevel')
    for c in (host, client):
        report[c.tag + '_watch'] = native_watch(c.bot, names)
    at = (90500.0, 5300.0, -6600.0)
    for c in (host, client):
        move_hero(c.bot, at)
    wolves = []
    until = time.monotonic() + 20
    while not wolves and time.monotonic() < until:
        time.sleep(2)
        wolves = sorted(((g, v) for g, v in npcs(host.bot).items() if v['name'].startswith(os.environ.get('G3_ANIMAL', 'Wolf')) and not v['dead']),
                        key=lambda gv: distance(gv[1]['pos'], at))
    assert wolves, 'no wolf near the host'
    guid, wolf = wolves[0]
    report['wolf'] = (wolf['name'], guid, [round(c) for c in wolf['pos']])
    pos = wolf['pos']
    if player is client:
        move_hero(host.bot, (pos[0] - 2500, pos[1] + 50, pos[2]))
    else:
        move_hero(client.bot, (pos[0] - 6000, pos[1] + 50, pos[2]))
    move_hero(player.bot, (pos[0] + 400, pos[1] + 50, pos[2]))
    samples = []
    for k in range(3):
        time.sleep(6)
        here = npcs(host.bot).get(guid, {})
        samples.append((here.get('task'), [round(c) for c in here.get('pos', ())]))
        w = here.get('pos', pos)
        report_line(player.bot, f'runto {w[0] + 2500:.0f}_{w[1] + 50:.0f}_{w[2] + 800 * (k - 1):.0f}', ('RUNTO ', 'REFUSED runto'))
    time.sleep(8)
    samples.append((npcs(host.bot).get(guid, {}).get('task'), None))
    report['samples'] = samples
    report['counts'] = {c.tag: native_counts(c.bot) for c in (host, client)}
    report['status_note'] = 'diagnostic only'


def stageAnimalWake(copies, report, negative=False):
    """Basis (OnAnimalSleep): a sleeping animal (ZS_AnimalSleep, AI mode 7) checks in its task callback whether
    Entity::GetPlayer comes within 10 m (re/out/animalsleep-20261026-b.c) and wakes and attacks it unless friendly or
    neutral: it woke only for the hero of the machine that simulates it. guards.cpp: while it still sleeps after that,
    each puppet within 11 m is the game's player for the same check. No animal at Ardea, Kent or the meadow north of Ardea
    has a sleeping point (p9smalldiag), so a hostile animal (G3_ANIMAL, default Wolf,Scavenger, near G3_ANIMAL_POS, the meadow)
    is put to sleep as ZS_AnimalSleep ends (QA animalsleep), the other animals within 30 m killed first (QA farkill). The player (G3_WHO=host: the host alone, the single-player
    control; default the client) waits 15 m off (it sleeps on), then walks up to 4 m: within 10 s it is awake and has the
    player as its target on the host. Negative control: animalwakeoff on the host."""
    host, client = join_both(copies)
    player = host if os.environ.get('G3_WHO') == 'host' else client
    other = client if player is host else host
    if negative:
        report_line(host.bot, 'animalwakeoff', 'ANIMALWAKEOFF ')
    clear_raid(host, report)
    at = tuple(float(v) for v in os.environ.get('G3_ANIMAL_POS', '90500_5300_-6600').split('_'))
    for c in (host, client):
        move_hero(c.bot, at)
    kinds = tuple(os.environ.get('G3_ANIMAL', 'Wolf,Scavenger').split(','))
    herd = []
    until = time.monotonic() + 20
    while not herd and time.monotonic() < until:
        time.sleep(2)
        herd = sorted(((g, v) for g, v in npcs(host.bot).items() if v['name'].startswith(kinds) and not v['dead'] and not v['mirrored']),
                      key=lambda gv: distance(gv[1]['pos'], at))
    assert herd, f'no {kinds} near the host'
    guid, animal = herd[0]
    report['animal'] = (animal['name'], guid, [round(c) for c in animal['pos']], animal['task'])
    pos = animal['pos']
    # The other player 30 m off (the host stays within its range: it simulates the animal), the player 15 m off.
    move_hero(other.bot, (pos[0] - 2500, pos[1] + 50, pos[2] + 1500))
    move_hero(player.bot, (pos[0] + 1500, pos[1] + 50, pos[2]))
    time.sleep(3)
    # No other animal within 30 m of it (26.10. the single-player control's scavenger went for a wolf meanwhile).
    others = [g for g, v in npcs(host.bot).items() if g != guid and not v['dead'] and not v['mirrored'] and distance(v['pos'], pos) < 3000
              and v['name'].startswith(('Wolf', 'Scavenger', 'Boar', 'Deer', 'Ripper', 'Okara', 'Molerat', 'Rabbit', 'Bison', 'Lurker', 'Snapper'))]
    report['cleared'] = [report_line(host.bot, 'farkill ' + g, ('FARKILL', 'REFUSED farkill'))[:80] for g in others]
    time.sleep(2 if others else 0)
    report['guards_before'] = report_line(host.bot, 'guards', 'GUARDS ')
    report['sleep'] = report_line(host.bot, 'animalsleep ' + guid, ('ANIMALSLEEP ', 'REFUSED animalsleep'))
    time.sleep(5)
    show = lambda: dict(re.findall(r'(\w+)=(\S+)', report_line(host.bot, f'animalsleep {guid}_show', ('ANIMALSLEEP ', 'REFUSED animalsleep'))))
    report['asleep'] = show()
    assert 'Sleep' in report['asleep'].get('state', ''), f"the animal does not sleep with nobody near: {report['asleep']}"
    pos = npcs(host.bot).get(guid, {}).get('pos', pos)
    move_hero(player.bot, (pos[0] + 400, pos[1] + 50, pos[2]))
    woke = []
    for _ in range(10):
        time.sleep(1)
        woke.append(show())
        if 'Sleep' not in woke[-1].get('state', ''):
            break
    report['woke'] = [f"{w.get('state')}|{w.get('ai_mode')}|{w.get('target')}|{w.get('target_puppet')}|{w.get('target_hero')}" for w in woke]
    report['guards'] = report_line(host.bot, 'guards', 'GUARDS ')
    last = woke[-1]
    assert 'Sleep' not in last.get('state', ''), f'the animal slept on with the player at 4 m: {report["woke"]}'
    whom = last.get('target_puppet') == '2' if player is client else last.get('target_hero') == '1'
    assert whom, f'the animal woke but does not go for the player: {report["woke"]}'


def stageFreeDestination(copies, report, negative=False):
    """Basis (ZS_WaitForFreeDestination_Loop): an NPC whose way ends at a taken destination waits (EndGoto, goto status 6,
    and the routine tasks' IsFreeDestination: ZS_WaitForFreeDestination); its loop (Script_Game 0x42db0) goes on with the
    routine once Entity::GetPlayer is more than 1.5 m from the destination, so it waited only for the hero of the machine
    that simulates it. guards.cpp: while another player stands nearer the destination, that puppet is the game's player
    for the loop. The player (G3_WHO=host: the host alone, the single-player control; default the client) stands on the
    routine destination of an NPC the host simulates, the host 8 m off; the NPC is sent to wait for it (QA waitfree: FullStop,
    task ZS_WaitForFreeDestination with the destination it had, as EndGoto does; the NPC moved 4 m off its destination first,
    QA farmove): the last 4 of 6 one-second samples it waits, and it started waiting at most twice (without the puppet as the
    player its loop went on every second and its routine found the destination taken again); the player walks 6 m off:
    within 8 s it goes on with its routine. The guards' zone and weapon checks against puppets are off on the host
    (trespassoff: at Ardea everyone observed the client standing in Hamlar's place). Negative control: freedestoff on the host."""
    host, client = join_both(copies)
    player = host if os.environ.get('G3_WHO') == 'host' else client
    if negative:
        report_line(host.bot, 'freedestoff', 'FREEDESTOFF ')
    clear_raid(host, report)
    report['trespassoff'] = report_line(host.bot, 'trespassoff', 'TRESPASSOFF ')
    keep = ('Diego', 'Gorn', 'Milten', 'Lester', 'PC_Hero')
    here = npcs(host.bot)
    names = [v['name'] for v in here.values()]
    candidates = []
    for name in sorted({n for n in names if names.count(n) == 1 and n not in keep and not n.startswith('Orc')}):
        line = report_line(host.bot, f'waitfree {name}_show', ('WAITFREE ', 'REFUSED waitfree'))
        f = dict(re.findall(r'(\w+)=(\S+)', line))
        if f.get('dest', '-') != '-' and f.get('mirrored') == '0' and f.get('task') and not f['task'].startswith(('ZS_Attack', 'ZS_Talk', 'ZS_Flee')) \
                and float(f.get('dest_off_cm', '1e9')) < 2000:
            candidates.append((float(f['dest_off_cm']) < 150, f))
    report['candidates'] = [f"{f.get('name')}:{f.get('dest')}:{f.get('dest_use')}:{f.get('dest_off_cm')}:{f.get('task')}" for _, f in candidates][:12]
    assert candidates, 'no NPC near the host with a routine destination'
    chosen = sorted(candidates, key=lambda c: c[0])[0][1]  # one that is not standing on its destination first
    npc = report['npc'] = chosen['name']
    dest = tuple(float(v) for v in chosen['dest_pos'].split('_'))
    report['dest'] = (chosen['dest'], chosen['dest_use'], chosen['dest_pos'])
    if player is client:
        move_hero(host.bot, (dest[0] + 800, dest[1] + 50, dest[2]))
    else:
        move_hero(client.bot, (dest[0] + 3500, dest[1] + 50, dest[2]))
    report['npc_moved'] = report_line(host.bot, f'farmove {dest[0] + 400:.0f}_{dest[1] + 50:.0f}_{dest[2]:.0f}_{npc}', ('FARMOVE ', 'REFUSED farmove'))
    time.sleep(1)
    # The destination it has now (a pacing boss has several Pace points, the current one changes; 26.10. the SP control
    # stood 1.6 m from the one the wait used and Hamlar rightly went on).
    pos_of = lambda line: tuple(float(v) for v in re.search(r'dest_pos=(\S+)', line)[1].split('_'))
    dest = pos_of(report_line(host.bot, f'waitfree {npc}_show', ('WAITFREE ', 'REFUSED waitfree')))
    move_hero(player.bot, (dest[0], dest[1] + 30, dest[2]))
    time.sleep(3)
    report['guards_before'] = report_line(host.bot, 'guards', 'GUARDS ')
    report_line(host.bot, 'aicount ZS_WaitForFreeDestination', ('AICOUNT end',))
    report['wait'] = report_line(host.bot, f'waitfree {npc}', ('WAITFREE ', 'REFUSED waitfree'))
    if distance(pos_of(report['wait']), dest) > 50:  # it chose another one meanwhile: stand there
        dest = pos_of(report['wait'])
        move_hero(player.bot, (dest[0], dest[1] + 30, dest[2]))
    report['dest_used'] = [round(c) for c in dest]
    waiting = []
    for _ in range(6):
        time.sleep(1)
        waiting.append((re.search(r' task=(\S*)', find_npc(host.bot, npc)) or [None, '?'])[1])
    report['waiting'] = waiting
    start = host.bot.send('aicount', 20)
    host.bot.line('AICOUNT end', 10, 'aicount', start)
    report['states_waiting'] = [l[8:] for l in host.bot.log()[start:].splitlines() if l.startswith('AICOUNT name=')]
    report['player_off_cm'] = round(distance(hero_pos(player.bot), dest))
    move_hero(player.bot, (dest[0] + 600, dest[1] + 50, dest[2]))
    after = []
    for _ in range(8):
        time.sleep(1)
        after.append((re.search(r' task=(\S*)', find_npc(host.bot, npc)) or [None, '?'])[1])
        if after[-1] != 'ZS_WaitForFreeDestination':
            break
    report['after'] = after
    report['guards'] = report_line(host.bot, 'guards', 'GUARDS ')
    start = host.bot.send('aicount', 20)
    host.bot.line('AICOUNT end', 10, 'aicount', start)
    report['states'] = [l[8:] for l in host.bot.log()[start:].splitlines() if l.startswith('AICOUNT name=')]
    entries = next((int(re.search(r'others=(\d+)', l)[1]) for l in report['states_waiting'] if l.startswith('name=ZS_WaitForFreeDestination ')), 0)
    report['wait_entries'] = entries
    assert all(t == 'ZS_WaitForFreeDestination' for t in waiting[-4:]) and entries <= 2, \
        f'{npc} did not wait while the player stood on its destination: {waiting}, started waiting {entries} times'
    assert after[-1] != 'ZS_WaitForFreeDestination', f'{npc} still waits after the player left its destination: {after}'


def stageE4(copies, report, negative=False):
    """Stage E4, NPC against NPC where two regions meet: in the client's region (its NPCs mirrored on the host) one of
    the host's NPCs (Lester, moved there) attacks a client-owned NPC. Lester's hits go to the client, which applies
    them to its NPC; that NPC's hits on Lester come back to the host."""
    host, client = join_both(copies)
    clear_raid(host, report)  # no raid needed: starts from the post-raid save (25.10.)
    target = next(r for r in far_rows(host.bot) if 600 < r['distance'] < 1500)
    victim = report['victim'] = target['name']
    teleport_to(client.bot, victim)
    time.sleep(10)
    teleport_to(host.bot, victim)
    time.sleep(10)
    report['host_victim'] = find_npc(host.bot, victim)
    assert 'mirrored=1' in report['host_victim'] and 'owner=2' in report['host_victim'].split(), f"{victim} is not the client's: {report['host_victim']}"

    def pos_of(line):
        return tuple(float(v) for v in re.search(r'pos=(\S+)', line)[1].split('_'))
    v = pos_of(report['host_victim'])
    report['farmove'] = report_line(host.bot, f'farmove {v[0] + 300:.0f}_{v[1] + 50:.0f}_{v[2]:.0f}_Lester', ('FARMOVE ', 'REFUSED farmove'))
    time.sleep(4)
    wait_for(lambda: 'mirrored=0' in find_npc(host.bot, 'Lester') and 'mirrored=1' in find_npc(client.bot, 'Lester') and ['yes'], 20,
             "the host's Lester mirrored on the client")
    report['host_lester'] = find_npc(host.bot, 'Lester')
    report['client_lester'] = find_npc(client.bot, 'Lester')
    if negative:
        report_line(host.bot, 'npchitsoff', 'NPCHITSOFF')
    hp_before = int(re.search(r' hp=(-?\d+)', find_npc(client.bot, victim))[1])
    report['fight'] = report_line(host.bot, f'npcfight Lester_{victim}', ('NPCFIGHT ', 'REFUSED npcfight'))

    nudges = report['lester_nudges'] = []

    def hurt():
        line = find_npc(client.bot, victim)
        hp = int(re.search(r' hp=(-?\d+)', line)[1])
        if combat_state(client.bot)['npc_hits_applied'] > 0 and hp < hp_before:
            return [line]
        # The client's NPC fights the host's puppet and moves; Lester's way after it can stall in ZS_Attack_Goto (7 m
        # short for 45 s, 20.10.). What is tested is where his hits go, not his path: put him next to it again.
        here, there = pos_of(find_npc(host.bot, 'Lester')), pos_of(find_npc(host.bot, victim))
        if distance(here, there) > 400 and (not nudges or time.monotonic() - nudges[-1][0] > 10):
            nudges.append((time.monotonic(), round(distance(here, there))))
            report_line(host.bot, f'farmove {there[0] + 150:.0f}_{there[1] + 50:.0f}_{there[2]:.0f}_Lester', ('FARMOVE ', 'REFUSED farmove'))
        return None
    try:
        report['client_victim'] = wait_for(hurt, 45, "Lester's hits reach the client's NPC")[0]
    except AssertionError:
        # Where the fight stands when no hit arrived (red after the merge 7355acc, 20.10.).
        report['timeout_host_lester'] = find_npc(host.bot, 'Lester')
        report['timeout_host_victim'] = find_npc(host.bot, victim)
        report['timeout_client_victim'] = find_npc(client.bot, victim)
        report['timeout_host_combat'] = combat_state(host.bot)
        report['timeout_client_combat'] = combat_state(client.bot)
        raise
    report['host_combat'] = combat_state(host.bot)
    report['client_combat'] = combat_state(client.bot)
    assert report['host_combat']['npc_hits_sent'] >= 1, 'the host sent no NPC hit'
    # The client's NPC fights back: its hits on Lester go to the host (reported, not required: it may die first).
    time.sleep(10)
    report['host_npc_hits_applied'] = combat_state(host.bot)['npc_hits_applied']
    report['host_lester_after'] = find_npc(host.bot, 'Lester')
    report['client_victim_after'] = find_npc(client.bot, victim)
    report['host_victim_after'] = find_npc(host.bot, victim)


def questdiag(copies, report, negative=False):
    """Diagnostic: kill and defeat quests with their target names and counters, at the start and after the raid."""
    host = copies['host'].launch()
    enter_world(host)

    def listing(tag):
        start = host.bot.send('questcounters', 20)
        host.bot.line('QUESTCOUNTER end', 15, 'questcounters', start)
        report[tag] = [l for l in host.bot.log()[start:].splitlines() if l.startswith('QUESTCOUNTER name=')]
    listing('start')
    clear_raid(host, report)
    listing('after_raid')
    report['status_note'] = 'diagnostic only'


def stageQ(copies, report, negative=False):
    """Stage Q, kill quests across regions: with Zakosh_KillTrolls running (three Faring_Troll_01), the client kills a
    troll in its own region (the death script Kill runs there, so only the client's quest manager saw it); the host's
    own quest counter for that target goes 0 -> 1 when the client reports the death, and stays 1 when the host gets
    there itself (the death is applied as a state, no second count)."""
    host, client = join_both(copies)
    clear_raid(host, report)  # no raid needed: starts from the post-raid save (25.10.)
    if negative:
        report_line(host.bot, 'questcountoff', 'QUESTCOUNTOFF ')
    quest_id, target = 'Zakosh_KillTrolls', 'Faring_Troll_01'
    report['run'] = quest(host.bot, 'runquest', quest_id)
    assert report['run'] == RUNNING, f'host could not start {quest_id}'

    def counter(bot):
        start = bot.send('questcounters ' + quest_id, 20)
        line = bot.line('QUESTCOUNTER name=', 15, 'questcounters', start)
        return int(re.search(target + r':(-?\d+)/', line)[1]), line
    wait_for(lambda: quest(client.bot, 'quest', quest_id) == RUNNING and ['yes'], 20, 'the quest runs on the client too')
    report['host_before'] = counter(host.bot)[1]
    assert counter(host.bot)[0] == 0, report['host_before']
    teleport_to(client.bot, target)
    time.sleep(8)
    report['client_target'] = find_npc(client.bot, target)
    assert 'mirrored=0' in report['client_target'].split(), 'the troll is not simulated by the client: ' + report['client_target']
    report['client_kill'] = report_line(client.bot, 'farkill ' + target, 'FARKILL ')
    report['client_counter'] = counter(client.bot)[1]
    report['host_counted'] = wait_for(lambda: counter(host.bot)[0] == 1 and [counter(host.bot)[1]], 20,
                                      "the host's quest counts the client's kill")[0]
    # The host gets there: the troll is dead there as well, and the kill is not counted again.
    teleport_to(host.bot, target)
    time.sleep(8)
    report['host_there'] = find_npc(host.bot, target)
    report['host_after'] = counter(host.bot)[1]
    assert counter(host.bot)[0] == 1, 'counted again on arrival: ' + report['host_after']


def stageE5(copies, report, negative=False):
    """Stage E5, a companion across the host's save: the client recruits Lester (as in E3); the host saves, the client
    leaves, the host loads that save and the client joins again (it gets and loads the host's campaign). The host's
    sidecar remembers whose companion Lester is, hands him to the client again, and he follows the client."""
    host, client = join_both(copies)
    if negative:
        report_line(host.bot, 'companionsoff', 'COMPANIONSOFF ')
    clear_raid(host, report)
    teleport_to(host.bot, 'Lester')
    time.sleep(6)
    teleport_to(client.bot, 'Lester')
    time.sleep(8)
    wait_for(lambda: npc_named(client.bot, 'Lester')[1].get('mirrored') and ['yes'], 20, 'Lester mirrored on the client')
    report['event'] = report_line(client.bot, 'setevent GetLester', 'EVENT ')
    talk_to(client, 'Lester', report, 'partners')
    report['talkinfo'] = report_line(client.bot, 'talkinfo NEWM1055', ('TALKINFO ', 'REFUSED talkinfo'))
    wait_for(lambda: talkstate(client.bot).get('running') == '0', 90, 'the recruiting dialog ends')
    wait_for(lambda: 'owner=2' in find_npc(host.bot, 'Lester').split() and ['yes'], 15, 'the client has Lester')
    # The host saves; its sidecar names Lester's player.
    folder = host.root/'isolated-profile/Documents/gothic3'
    for old in list(folder.glob('G3T_E5.*')) + list((client.root/'isolated-profile/Documents/gothic3').glob('*G3T_E5*')):
        old.unlink(missing_ok=True)
    start = host.bot.send('nativesave G3T_E5', 60)
    report['save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
    assert 'ok=1' in report['save'], report['save']
    sidecar = (folder/'G3T_E5.g3t').read_text(errors='replace')
    report['sidecar_companions'] = [l for l in sidecar.splitlines() if l.startswith('companion ')]
    assert any(l.endswith(' Client') for l in report['sidecar_companions']), sidecar
    # The client leaves; the host loads the save.
    assert close(client), 'the client did not close'
    client.collect()
    (client.run/'client').rename(client.run/'client-first')
    wait_for(lambda: 2 not in remotes(host.bot), 30, 'host drops the client')
    start = host.bot.send('nativeload G3T_E5', 60)
    report['host_load'] = host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''), 120,
                                        'host nativeload', start)
    wait_running(host, 180, 'host world running after load')
    time.sleep(5)
    report['host_lester_loaded'] = find_npc(host.bot, 'Lester')
    # The client joins again and loads the host's campaign.
    start = host.bot.send('releaselock')
    host.bot.line('LOCK scan complete', 10, 'lock scan', start)
    client.launch('client', 'Client')
    wait_running(client, 300, "client world running from the host's campaign")
    time.sleep(5)

    def adopted():
        h, c = find_npc(host.bot, 'Lester'), find_npc(client.bot, 'Lester')
        return 'owner=2' in h.split() and 'mirrored=0' in c.split() and 'task=ZS_FollowPlayer' in c.split() and [h, c]
    report['adopted'] = wait_for(adopted, 45, "Lester is the client's companion again")

    def pos_of(line):
        return tuple(float(v) for v in re.search(r'pos=(\S+)', line)[1].split('_'))
    lester_start = pos_of(find_npc(client.bot, 'Lester'))
    start_pos = remotes(host.bot)[2]['pos']
    move_hero(client.bot, (start_pos[0] + 1200, start_pos[1], start_pos[2]))

    def following():
        c_hero = remotes(host.bot)[2]['pos']
        h_lester, c_lester = find_npc(host.bot, 'Lester'), find_npc(client.bot, 'Lester')
        near = distance(pos_of(h_lester), c_hero) < 1000 and distance(pos_of(c_lester), c_hero) < 1000
        # Kept for a timeout: how far each Lester is from the client and from where he stood (24.10.: timed out unseen).
        report['following_last'] = {'host_m': round(distance(pos_of(h_lester), c_hero) / 100, 1),
                                    'client_m': round(distance(pos_of(c_lester), c_hero) / 100, 1),
                                    'moved_m': round(distance(pos_of(c_lester), lester_start) / 100, 1), 'host': h_lester, 'client': c_lester}
        return near and distance(pos_of(c_lester), lester_start) > 300 and [h_lester, c_lester]
    report['following'] = wait_for(following, 40, 'Lester follows the client after the load')[0]


def near_rows(bot):
    start = bot.send('nearnpcs', 20)
    bot.line('NEARX end', 10, 'nearnpcs', start)
    rows = {}
    for m in re.finditer(r'NEARX guid=(\S+) name=(\S+) dead=(\d) mirrored=(\d) owner=(\d+) target=(\S+) target_hero=(\d) '
                         r'target_puppet=(\d+) task=(\S*) ani=(-?\d+) hand=(\S+) motion=(\S+)', bot.log()[start:]):
        rows[m[1]] = {'name': m[2], 'dead': m[3] == '1', 'mirrored': m[4] == '1', 'owner': int(m[5]), 'target': m[6],
                      'target_hero': m[7] == '1', 'target_puppet': int(m[8]), 'task': m[9], 'ani': int(m[10]), 'hand': m[11], 'motion': m[12]}
    return rows


def viewdiag(copies, report, negative=False):
    """Diagnostic for the first human test: during the opening raid, whom the host's NPCs attack (its hero or the
    client's puppet) and whether the client shows the same motions (attacks) on its mirrors of them."""
    host, client = join_both(copies)
    samples = []
    for _ in range(25):
        samples.append((near_rows(host.bot), near_rows(client.bot)))
        time.sleep(1.5)
    targets_hero = targets_puppet = 0
    attack_host = attack_same = 0
    examples = []
    for h, c in samples:
        for guid, row in h.items():
            if row['dead']:
                continue
            targets_hero += row['target_hero']
            targets_puppet += 1 if row['target_puppet'] else 0
            if any(k in row['motion'] for k in ('Attack', 'Parade', 'Combo')) and guid in c:
                attack_host += 1
                same = c[guid]['motion'] == row['motion']
                attack_same += same
                if len(examples) < 12:
                    examples.append(f"{row['name']}: host ani={row['ani']} hand={row['hand']} {row['motion']} | client ani={c[guid]['ani']} hand={c[guid]['hand']} {c[guid]['motion']}")
    attacky = ('Attack', 'Parade_Begin', 'Combo')
    report['host_attack_samples'] = sum(1 for h, _ in samples for r in h.values() if not r['dead'] and any(k in r['motion'] for k in attacky))
    report['client_attack_samples'] = sum(1 for _, c in samples for r in c.values() if not r['dead'] and any(k in r['motion'] for k in attacky))
    report['client_armed_mirrors'] = sum(1 for r in samples[-1][1].values() if r['mirrored'] and r['hand'] != '-')
    report['host_armed'] = sum(1 for r in samples[-1][0].values() if r['hand'] != '-')
    report['host_targets_hero'] = targets_hero
    report['host_targets_puppet'] = targets_puppet
    report['host_attack_motions'] = attack_host
    report['client_same_motion'] = attack_same
    report['examples'] = examples
    report['last_host'] = [f"{r['name']} t={r['target']} {r['task']} {r['motion']}" for r in samples[-1][0].values()][:20]
    report['last_client'] = [f"{r['name']} m={r['mirrored']} o={r['owner']} t={r['target']} {r['motion']}" for r in samples[-1][1].values()][:20]
    report['status_note'] = 'diagnostic only'


def pose_rows(bot):
    """NEARX rows with the slot-0 state: {guid: {'name', 'dead', 'mirrored', 'motion', 'run0', 'weight0'}}."""
    start = bot.send('nearnpcs', 20)
    bot.line('NEARX end', 10, 'nearnpcs', start)
    rows = {}
    for m in re.finditer(r'NEARX guid=(\S+) name=(\S+) dead=(\d) mirrored=(\d) .*? motion=(\S+) state=(\S*) run0=(-?\d+) weight0=(-?\d+)(?: yaw=(-?\d+))?',
                         bot.log()[start:]):
        rows[m[1]] = {'name': m[2], 'dead': m[3] == '1', 'mirrored': m[4] == '1', 'motion': m[5], 'run0': int(m[7]), 'weight0': int(m[8]),
                      'yaw': int(m[9]) if m[9] else None}
    return rows


def motion_kind(motion):
    for kind, words in (('hit', ('Stumble', '_Hit_N_Back', 'KnockDown')), ('attack', ('Attack', 'Combo')),
                        ('move', ('_Move_',)), ('parade', ('Parade',)), ('stand', ('Ambient',))):
        if any(w in motion for w in words):
            return kind
    return 'other'


def posediag(copies, report, negative=False):
    """Owner's look after V2 (T-posing orcs on the client, hits not shown): how many living NPCs stand with nothing
    playing on slot 0 (bind pose) on each machine, and how often the client shows the same kind of motion as the host."""
    host, client = join_both(copies)
    if os.environ.get('G3_MOTION_OWNER'):
        report['motion_owner'] = report_line(client.bot, 'motionowner ' + os.environ['G3_MOTION_OWNER'], 'MOTIONOWNER')
    if os.environ.get('G3_COMBAT_REPLAY_OFF'):
        report['combat_replay'] = report_line(client.bot, 'combatreplayoff', 'COMBATREPLAYOFF')
    host_rows = host_still = client_rows = client_still = pairs = same = 0
    kinds, still_examples = {}, []
    for _ in range(25):
        h, c = pose_rows(host.bot), pose_rows(client.bot)
        for g, r in h.items():
            if r['dead']:
                continue
            host_rows += 1
            host_still += r['run0'] == 0
            m = c.get(g)
            if not m or m['dead'] or not m['mirrored']:
                continue
            client_rows += 1
            client_still += m['run0'] == 0
            if m['run0'] == 0 and len(still_examples) < 12:
                still_examples.append(f"{m['name']} client={m['motion']} w={m['weight0']} | host={r['motion']}")
            pairs += 1
            hk, ck = motion_kind(r['motion']), motion_kind(m['motion'])
            same += hk == ck
            report['same_kind_running'] = report.get('same_kind_running', 0) + (hk == ck and m['run0'] == 1)
            report['host_attacks'] = report.get('host_attacks', 0) + (hk == 'attack')
            report['attacks_running_on_client'] = report.get('attacks_running_on_client', 0) + (hk == 'attack' and ck == 'attack' and m['run0'] == 1)
            kinds[f'{hk}->{ck}'] = kinds.get(f'{hk}->{ck}', 0) + 1
        time.sleep(1.5)
    start = client.bot.send('nearnpcs', 20)
    report['client_end'] = client.bot.line('NEARX end', 10, 'nearnpcs', start)
    report.update(host_rows=host_rows, host_still=host_still, client_rows=client_rows, client_still=client_still,
                  pairs=pairs, same_kind=same, kinds=dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
                  still_examples=still_examples, status_note='diagnostic only')


def sync_sample(host, client, client_pos, tally, examples):
    """One look at every living host NPC near the client: is it mirrored there, does a motion run, is it the same
    kind of motion, how far apart are the two bodies. A second host look right after the client's (same delay as
    between host and client) gives the agreement that timing alone allows."""
    h, hp = pose_rows(host.bot), npcs(host.bot)
    c, cp = pose_rows(client.bot), npcs(client.bot)
    h2 = pose_rows(host.bot)
    hpos = {g.replace('-', '')[:12]: v for g, v in hp.items()}
    cpos = {g.replace('-', '')[:12]: v for g, v in cp.items()}
    for g, r in h.items():
        if r['dead'] or g not in hpos or hpos[g]['dead']:
            continue
        if distance(hpos[g]['pos'], client_pos) > 3500:  # NEARX covers 40 m around each hero
            continue
        tally['host'] += 1
        tally['host_still'] += r['run0'] == 0
        m = c.get(g)
        if not m or m['dead']:
            tally['missing' if not m else 'dead_on_client'] += 1
            if len(examples.setdefault('missing', [])) < 8:
                examples['missing'].append(f"{r['name']} host={r['motion'][:50]}")
            continue
        if not m['mirrored']:
            # The client simulates it. Fine if the host mirrors it (the client's region); if the host simulates it
            # too, both run their own AI and show different things.
            tally['own_ai'] += 1
            tally['own_ai_both'] += not r['mirrored']
            tally['own_ai_names'].add(r['name'] + ('' if r['mirrored'] else ' (host too)'))
            if len(examples.setdefault('own_ai', [])) < 8:
                examples['own_ai'].append(f"{r['name']} host_mirrored={int(r['mirrored'])} host={r['motion'][:50]} client={m['motion'][:50]}")
            continue
        tally['pairs'] += 1
        hk, ck = motion_kind(r['motion']), motion_kind(m['motion'])
        tally['still'] += m['run0'] == 0
        tally['same_kind'] += hk == ck
        if g in h2:
            tally['baseline_pairs'] += 1
            bk = motion_kind(h2[g]['motion'])
            tally['baseline_same'] += bk == hk
            tally['baseline_kinds'][f'{hk}->{bk}'] = tally['baseline_kinds'].get(f'{hk}->{bk}', 0) + 1
        tally['kinds'][f'{hk}->{ck}'] = tally['kinds'].get(f'{hk}->{ck}', 0) + 1
        if hk != ck or m['run0'] == 0:
            key = 'still' if m['run0'] == 0 else f'{hk}->{ck}'
            if len(examples.setdefault(key, [])) < 6:
                examples[key].append(f"{r['name']} host={r['motion'][:60]} client={m['motion'][:60]}")
        if r['yaw'] is not None and m['yaw'] is not None:
            turn = abs((r['yaw'] - m['yaw'] + 180) % 360 - 180)
            tally['yaw_moving' if hk == 'move' else 'yaw_other'].append(turn)
            if turn > 60 and len(examples.setdefault('facing', [])) < 8:
                examples['facing'].append(f"{r['name']} {turn} deg host={r['motion'][:50]} client={m['motion'][:50]}")
        if g in cpos:
            d = distance(hpos[g]['pos'], cpos[g]['pos'])
            tally['pos'].append(d)
            if d > 300 and len(examples.setdefault('far', [])) < 8:
                examples['far'].append(f"{r['name']} {d:.0f} cm host={r['motion'][:40]} client={m['motion'][:40]}")


def sync_phase(host, client, rounds):
    tally = {k: 0 for k in ('host', 'host_still', 'missing', 'dead_on_client', 'own_ai', 'own_ai_both', 'pairs', 'still',
                            'same_kind', 'baseline_pairs', 'baseline_same')}
    tally['kinds'], tally['baseline_kinds'], tally['pos'], tally['own_ai_names'] = {}, {}, [], set()
    tally['yaw_moving'], tally['yaw_other'] = [], []
    examples = {}
    for _ in range(rounds):
        client_pos = remotes(host.bot)[2]['pos']
        sync_sample(host, client, client_pos, tally, examples)
        time.sleep(1.0)
    pos = sorted(tally.pop('pos'))
    pct = lambda q: round(pos[min(len(pos) - 1, int(q * len(pos)))]) if pos else None
    tally['pos_cm'] = {'n': len(pos), 'median': pct(0.5), 'p90': pct(0.9), 'max': round(pos[-1]) if pos else None,
                       'over_1m': sum(d > 100 for d in pos), 'over_3m': sum(d > 300 for d in pos)}
    for key in ('yaw_moving', 'yaw_other'):
        turns = sorted(tally.pop(key))
        tally[key + '_deg'] = {'n': len(turns), 'median': turns[len(turns) // 2] if turns else None,
                               'p90': turns[min(len(turns) - 1, int(0.9 * len(turns)))] if turns else None,
                               'over_45': sum(t > 45 for t in turns), 'over_90': sum(t > 90 for t in turns)}
    tally['kinds'] = dict(sorted(tally['kinds'].items(), key=lambda kv: -kv[1]))
    tally['baseline_kinds'] = dict(sorted(tally['baseline_kinds'].items(), key=lambda kv: -kv[1]))
    tally['own_ai_names'] = sorted(tally['own_ai_names'])
    tally['examples'] = examples
    return tally


def syncdiag(copies, report, negative=False):
    """Owner's look (19.10.): not every character seemed to move right on the client. Two phases, the opening raid
    and the village afterwards: every living host NPC within 35 m of the client, compared with the client's body."""
    host, client = join_both(copies)
    if os.environ.get('G3_MOTION_OWNER'):
        report['motion_owner'] = report_line(client.bot, 'motionowner ' + os.environ['G3_MOTION_OWNER'], 'MOTIONOWNER')
    for flag, command, prefix in (('G3_SCRIPT_ANI_OFF', 'scriptanioff', 'SCRIPTANIOFF'), ('G3_LOOP_RESTART_OFF', 'looprestartoff', 'LOOPRESTARTOFF')):
        if os.environ.get(flag):
            report[command] = report_line(client.bot, command, prefix)
    report['raid'] = sync_phase(host, client, int(os.environ.get('G3_SYNC_ROUNDS', '20')))
    start = client.bot.send('nearnpcs', 20)
    report['client_end_raid'] = client.bot.line('NEARX end', 10, 'nearnpcs', start)
    clear_raid(host, report)
    time.sleep(5)
    report['village'] = sync_phase(host, client, int(os.environ.get('G3_SYNC_ROUNDS', '20')))
    for tag, copy in (('host', host), ('client', client)):
        start = copy.bot.send('nearnpcs', 20)
        report[tag+'_end'] = copy.bot.line('NEARX end', 10, 'nearnpcs', start)
    report['client_body_log'] = [l for l in client.mod_log().splitlines() if l.startswith('body: ')][-15:]
    report['status_note'] = 'diagnostic only'


def glide_counts(bot):
    start = bot.send('glide', 20)
    bot.line('GLIDE kind=puppet ', 10, 'glide', start)
    found = {}
    for m in re.finditer(r'GLIDE kind=(\w+) moving_ms=(\d+) glide_ms=(\d+) steer_ms=(\d+) place_ms=(\d+) snaps=(\d+) stance_stops=(\d+)', bot.log()[start:]):
        found[m[1]] = dict(zip(('moving_ms', 'glide_ms', 'steer_ms', 'place_ms', 'snaps', 'stance_stops'), map(int, m.groups()[1:])))
    for m in re.finditer(r'GLIDEMOTION kind=(\w+) ms=(\d+) motion=(\S*)', bot.log()[start:]):
        found.setdefault(m[1], {}).setdefault('motions', []).append((int(m[2]), m[3]))
    return found


def stageGlide(copies, report, negative=False):
    """Owner's look (first evening 21.10.): characters glide instead of walking. Each player runs eight 11 m legs (game
    AI goto) while the other machine counts how long its puppet travelled faster than 1 m/s and how much of that slot 0
    showed no movement motion (QA glide). Meant to run under G3_NETSIM (Hamachi-like). Mirrored NPCs on the client are
    reported too. Passes when each puppet glides at most G3_GLIDE_MAX (default 5 %: accepted runs 23.10. 0.6-2.4 %, negative
    controls 7-64 %) of its travel time. Negative control:
    glideold (speed from arrival times, a replayed stance left running when the body sets off: as before 23.10.)."""
    host, client = join_both(copies)
    if negative:
        for copy in (host, client):
            report_line(copy.bot, 'glideold', 'GLIDEOLD ')
    elif os.environ.get('G3_GLIDE_STOPOFF'):  # comparison: on set-off only StopPlayAni, the slot's motion runs on
        for copy in (host, client):
            report_line(copy.bot, 'glidestopoff', 'GLIDESTOPOFF ')
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((host, client), (client, host)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    report['moved'] = {'host': move_hero(host.bot, OpenGround),
                       'client': move_hero(client.bot, (OpenGround[0] + 300, OpenGround[1], OpenGround[2]))}
    time.sleep(3)
    limit = float(os.environ.get('G3_GLIDE_MAX', '0.05'))
    problems = []
    for mover, watcher in ((client, host), (host, client)):
        report_line(watcher.bot, 'glidereset', 'GLIDERESET ')
        legs = []
        for point in range(8):
            legs.append(report_line(mover.bot, f'walkto {point}', 'WALKTO '))
            time.sleep(5)
        counts = glide_counts(watcher.bot)
        puppet = counts.get('puppet', {})
        share = puppet.get('glide_ms', 0) / puppet['moving_ms'] if puppet.get('moving_ms') else None
        report[mover.tag+'_legs_started'] = sum('started=1' in l for l in legs)
        report[mover.tag+'_seen_by_'+watcher.tag] = counts
        report[mover.tag+'_glide_share'] = round(share, 3) if share is not None else None
        report[mover.tag+'_place_share'] = round(puppet['place_ms'] / puppet['moving_ms'], 3) if puppet.get('moving_ms') else None
        if not puppet.get('moving_ms'):
            problems.append(f'{watcher.tag} never saw the puppet of {mover.tag} travel')
        elif share > limit:
            problems.append(f'puppet of {mover.tag} on {watcher.tag} glided {share:.0%} of {puppet["moving_ms"]} ms travel (max {limit:.0%})')
    report['problems'] = problems
    assert not problems, '; '.join(problems)


def wear_line(bot, player_id):
    start = bot.send('remotes', 20)
    bot.line('REMOTES end', 10, 'remotes', start)
    m = re.search(rf'WEAR id={player_id} remote_armor=(\S+) remote_helmet=(\S+) puppet_armor=(\S+) puppet_helmet=(\S+) look=(\S+) template=(\S+)',
                  bot.log()[start:])
    return dict(zip(('remote_armor', 'remote_helmet', 'puppet_armor', 'puppet_helmet', 'look', 'template'), m.groups())) if m else {}


def wear_hero(bot, argument):
    """QA wear: the WEARHERO line, or the REFUSED line that says why."""
    start = bot.send('wear ' + argument, 20)
    return bot.line(('WEARHERO ', 'REFUSED wear'), 10, 'wear ' + argument, start)


def stageWear(copies, report, negative=False):
    """Owner (21.10./23.10.): what a player wears shows on the other machine, and each player picks its own look in its
    INI. The client plays with look = Diego: the host shows its puppet as Diego, the client shows the host (no look) as
    puppet_template. Then each player puts on peasant clothes (Body_Peasant) and a leather helmet and takes them off
    again; the other machine's puppet follows within 5 s; screenshots of the watcher while the puppet wears both. Negative control: wearoff and lookoff on both machines."""
    copies['client'].ini_extra = 'look = Diego\n'
    host, client = join_both(copies)
    if negative:
        for copy in (host, client):
            report_line(copy.bot, 'wearoff', 'WEAROFF ')
            report_line(copy.bot, 'lookoff', 'LOOKOFF ')
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((host, client), (client, host)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    problems = []
    try:
        report['look_host_sees_client'] = wait_for(lambda: (w := wear_line(host.bot, 2)) and w['template'] == 'Diego' and [w], 15,
                                                    'host shows the client as Diego')[0]
    except AssertionError:
        report['look_host_sees_client'] = wear_line(host.bot, 2)
        problems.append(f"host shows the client as {report['look_host_sees_client'].get('template')}, not its own look Diego")
    report['look_client_sees_host'] = wear_line(client.bot, 1)
    if report['look_client_sees_host'].get('template') != PUPPET_TEMPLATE:
        problems.append(f"client shows the host as {report['look_client_sees_host'].get('template')}, not {PUPPET_TEMPLATE}")
    # Names from the game's Templates.pak: a wearable armor is a Body_* item (body slot 17); It_Armor_* are only the packets
    # one picks up (re/findings/armor-equip-c.md).
    items = [('armor', os.environ.get('G3_WEAR_ARMOR', 'Body_Peasant')), ('helmet', os.environ.get('G3_WEAR_HELMET', 'It_Helmet_Leather_01'))]
    report['items'] = items
    for wearer, watcher, wearer_id in ((client, host, 2), (host, client, 1)):
        for put_on in (True, False):
            for slot, item in items:
                line = wear_hero(wearer.bot, item if put_on else '-' + slot)
                wanted = item if put_on else '-'
                step = f"{wearer.tag} {'puts on' if put_on else 'takes off'} {item}"
                report.setdefault('steps', []).append(f'{step}: {line}')
                if f'{slot}={wanted}' not in line:
                    problems.append(f'{step}: the hero shows {line}')
                    continue
                try:
                    wait_for(lambda: wear_line(watcher.bot, wearer_id).get('puppet_'+slot) == wanted and ['ok'], 5,
                             f'{watcher.tag} puppet follows: {step}')
                except AssertionError:
                    problems.append(f"{step}: {watcher.tag}'s puppet shows {wear_line(watcher.bot, wearer_id)}")
            # For a look (owner or agent): the watcher 4 m in front of the puppet, dressed and undressed.
            report.setdefault('lookat', []).append(report_line(watcher.bot, f'lookat puppet{wearer_id}', ('LOOKAT ', 'REFUSED lookat')))
            time.sleep(2)
            snap(watcher, f"puppet{wearer_id}-{'dressed' if put_on else 'bare'}", report)
    report['problems'] = problems
    assert not problems, '; '.join(problems[:4])


def puppettalkdiag(copies, report, negative=False):
    """Phase 2 groundwork (ENTWURF-HANDEL-2026-10-23): the trade options the game has (condition type 17) and how one is
    built; whether the client can address the host's puppet the player's way (AssessTalk); whether a copy of a trader's
    option, owned by the puppet under a name of its own (QA puppetinfo), is offered and opens the trade screen."""
    copies['client'].ini_extra = 'look = Diego\n'  # the host then addresses an NPC-template puppet (Diego), not PC_Hero
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((host, client), (client, host)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    start = client.bot.send('infoscan type_17', 20)
    client.bot.line('INFOSCAN end', 10, 'infoscan', start)
    rows = [l for l in client.bot.log()[start:].splitlines() if l.startswith('INFOSCAN ') and not l.startswith('INFOSCAN end')]
    report['trade_infos'] = rows[:12]
    source = os.environ.get('G3_TRADE_INFO') or (re.search(r'info=(\S+)', rows[0])[1] if rows else '')
    report['source_info'] = source
    if source:
        start = client.bot.send('infodesc ' + source, 20)
        client.bot.line('INFODESC end', 10, 'infodesc', start)
        report['source_desc'] = [l for l in client.bot.log()[start:].splitlines() if l.startswith(('INFODESC', 'INFOLINE'))]
    # The player's way first, before the puppet has any option.
    report['approach'] = report_line(client.bot, 'approach puppet1', ('APPROACH', 'REFUSED approach'))
    time.sleep(1)
    report['assesstalk_bare'] = report_line(client.bot, 'assesstalk puppet1', ('ASSESSTALK', 'REFUSED assesstalk'))
    time.sleep(2)
    report['talkstate_bare'] = talkstate(client.bot)
    report_line(client.bot, 'endtalk', 'TALKSTATE ')
    time.sleep(1)
    for copy, other, tag in ((client, 1, 'client'), (host, 2, 'host')):
        report[tag+'_approach'] = report_line(copy.bot, f'approach puppet{other}', ('APPROACH', 'REFUSED approach'))
        report[tag+'_tradeon'] = report_line(copy.bot, f'puppettradeon {other}', ('PUPPETTRADE', 'REFUSED puppettradeon'))
        report[tag+'_assesstalk'] = report_line(copy.bot, f'assesstalk puppet{other}', ('ASSESSTALK', 'REFUSED assesstalk'))
        time.sleep(2)
        report[tag+'_talkstate'] = talkstate(copy.bot)
        snap(copy, 'menu', report)
        trade = os.environ.get('G3_TRADE_OPTION', 'FILLER27')
        report[tag+'_choose'] = report_line(copy.bot, 'talkinfo ' + trade, ('TALKINFO', 'REFUSED talkinfo'))
        time.sleep(5)
        report[tag+'_talkstate_after'] = talkstate(copy.bot)
        report[tag+'_containersync'] = report_line(copy.bot, 'containersync', 'CONTAINERSYNC ')
        snap(copy, 'trade', report)
        report_line(copy.bot, 'endtalk', 'TALKSTATE ')
        time.sleep(2)
    report['host_remotes'] = [l for l in anim_lines(host.bot, 'REMOTE ')]
    report['status_note'] = 'diagnostic only'


def postraidsave(copies, report, negative=False):
    """Fixture, not a test: a new game, the opening raid cleared, both heroes up; the host saves it as G3T_PostRaid and the
    client writes its hero record. The host's save and record and the client's copy and record go to
    runtime/fixtures/postraid-p<pair>, from which join_both starts the stages that do not need the raid. Run it again after
    a change to what a save holds (the mod's sidecars)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((host, client), (client, host)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    time.sleep(5)
    clear_slot(copies, POSTRAID_SLOT)
    since = len(client.mod_log())
    start = host.bot.send(f'nativesave {POSTRAID_SLOT}', 60)
    report['save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
    assert 'ok=1' in report['save'], report['save']
    report['record_written'] = wait_mod_log(client, r'persist: the host saved '+POSTRAID_SLOT+r'; own hero record written', 30,
                                            'the client writes its hero record', since)
    time.sleep(5)  # the game finishes writing a save after SaveGame returns
    if POSTRAID_FIXTURE.exists():
        shutil.rmtree(POSTRAID_FIXTURE)
    files = {}
    for tag in ('host', 'client'):
        (POSTRAID_FIXTURE/tag).mkdir(parents=True)
        folder = copies[tag].root/'isolated-profile/Documents/gothic3'
        for item in sorted(set(folder.glob('*'+POSTRAID_SLOT+'*'))):
            if item.is_file():
                shutil.copy2(item, POSTRAID_FIXTURE/tag/item.name)
                files.setdefault(tag, []).append(item.name)
    assert files.get('host') and files.get('client'), f'save files missing: {files}'
    report['files'] = files
    (POSTRAID_FIXTURE/'meta.json').write_text(json.dumps({'made': time.strftime('%Y-%m-%d %H:%M:%S'), 'mod_sha256': report.get('mod_sha256'),
                                                          'files': files}, indent=2), encoding='utf-8')


def player_goods(bot, which):
    return report_line(bot, f'playergoods {which}', 'PLAYERGOODS ')


def close_trade(copy, report):
    """Close the trade screen as a player does (Escape), then end the dialog: left open, the next stage of a session could
    not talk to anyone (stageTrade and stageH3b after a trade with a puppet, 23.10.)."""
    presses = 0
    for presses in range(1, 6):  # the screen's tip box takes a key of its own
        post_key(copy.process.pid, 0x1B)
        time.sleep(1.5)
        if ' state=PS_HUD' not in herostate(copy.bot):
            break
    report[copy.tag+'_escapes'] = presses
    if ' state=PS_HUD' in herostate(copy.bot):  # the bot's Escape did not reach the trade window (23.10.)
        report[copy.tag+'_tradeclose'] = report_line(copy.bot, 'tradeclose', ('TRADECLOSE', 'REFUSED tradeclose'))
    report_line(copy.bot, 'endtalk', 'TALKSTATE ')
    report[copy.tag+'_after_trade'] = talkstate(copy.bot)
    # The puppet runs no AI: the hero stayed in ZS_Talk after the dialog and could talk to nobody (23.10.); the mod lets it go.
    report[copy.tag+'_hero_after_trade'] = wait_for(lambda: (h := herostate(copy.bot)) and ' task=ZS_Talk ' not in h and [h], 10,
                                                     copy.tag+' hero out of the dialog task after the trade')[0]


def hero_count(bot, name):
    return int(re.search(r'total=(-?\d+)', report_line(bot, f'herocount {name}', 'HEROCOUNT '))[1])


def trade_counts(host, client, item):
    return {'client_item': hero_count(client.bot, item), 'client_gold': hero_count(client.bot, 'It_Gold'),
            'host_item': hero_count(host.bot, item), 'host_gold': hero_count(host.bot, 'It_Gold')}


def tradesync(bot):
    return dict((k, int(v)) for k, v in re.findall(r'(\w+)=(-?\d+)', report_line(bot, 'tradesync', 'TRADESYNC ')))


def stagePlayerTrade(copies, report, negative=False):
    """Phase 2 step 4 end to end (owner 23.10.: the game's trade window, every trade confirmed by the other player, the
    same price both ways). The client opens trade with the host's puppet (PlayerOpenTrade) and swaps as the window's
    Tauschen does (QA tradebuy: the hunting bow for its value). Closing the window sends one offer; until the host's
    answer the client has neither the bow nor its gold. The host accepts: the bow leaves the host's hero, the gold
    arrives there, the client gets the bow. A second window (a fireball scroll; the host sold its bow) is declined: the
    client's gold comes back and nothing changes on the host. Negative control: tradeholdoff on the client (it keeps the
    item before the answer)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report_line(client.bot, 'tradeholdoff', 'TRADEHOLDOFF ')
    report['client_gold'] = report_line(client.bot, 'givepotion It_Gold-500', 'GIVEPOTION ')  # the bow costs 200, a hero starts with 123
    for goods in ('It_Bow_Hunting', 'It_Scroll_Fireball'):  # one more each, so a run after another in a session finds them
        report_line(host.bot, f'givepotion {goods}-1', 'GIVEPOTION ')
    wait_for(lambda: (l := player_goods(client.bot, '1_It_Bow_Hunting')) and 'item=It_Bow_Hunting:0 ' not in l and 'unknown' not in l and [l],
             30, "the host's goods reach the client")
    problems = []
    for answer, item in (('accept', 'It_Bow_Hunting'), ('reject', 'It_Scroll_Fireball')):
        before = report[answer+'_before'] = trade_counts(host, client, item)
        synced = tradesync(client.bot)
        report[answer+'_approach'] = report_line(client.bot, 'approach puppet1', ('APPROACH', 'REFUSED approach'))
        report[answer+'_open'] = report_line(client.bot, 'tradeopen puppet1', ('TRADEOPEN', 'REFUSED tradeopen'))
        time.sleep(2)
        report[answer+'_hero_in_window'] = herostate(client.bot)
        report[answer+'_buy'] = buy = report_line(client.bot, f'tradebuy puppet1_{item}', ('TRADEBUY', 'REFUSED tradebuy'))
        price = int(re.search(r'price=(\d+)', buy)[1]) if 'price=' in buy else 0
        time.sleep(2)
        close_trade(client, report)
        report[answer+'_sent'] = wait_for(lambda: (t := tradesync(client.bot))['offers'] > synced['offers'] and [t], 15,
                                          'the closed window went out as an offer')[0]
        held = report[answer+'_held'] = trade_counts(host, client, item)
        if held['client_item'] != before['client_item'] or held['client_gold'] != before['client_gold'] - price:
            problems.append(f'{answer}: before the answer the client has {held}, expected {item} held back and {price} gold paid ({before})')
        wait_for(lambda: ' pending=1 ' in report_line(host.bot, 'offerstate', 'OFFERSTATE '), 15, 'the offer reaches the host')
        report[answer+'_decide'] = report_line(host.bot, 'offer'+answer, ('OFFER', 'REFUSED'))
        key = 'accepted' if answer == 'accept' else 'declined'
        wait_for(lambda: tradesync(client.bot)[key] > synced[key] and ['answered'], 20, f'the client learns the host {key}')
        time.sleep(2)
        after = report[answer+'_after'] = trade_counts(host, client, item)
        if answer == 'accept':
            wanted = {'client_item': before['client_item'] + 1, 'client_gold': before['client_gold'] - price,
                      'host_item': before['host_item'] - 1, 'host_gold': before['host_gold'] + price}
        else:
            wanted = dict(before)
        if after != wanted:
            problems.append(f'{answer}: after the answer {after}, expected {wanted} (price {price})')
    report['problems'] = problems
    assert not problems, '; '.join(problems)


def map_marks(bot):
    """The MAPMARKS line and player 2's drawing in it: position x/z, map (drawn, u, v) and compass (drawn, degrees)."""
    line = report_line(bot, 'mapmarks', 'MAPMARKS ')
    counts = dict((k, int(v)) for k, v in re.findall(r'(map_paints|compass_paints|map_drawn|compass_drawn|off_map)=(-?\d+)', line))
    view = re.search(r' view=(-?[\d.]+),(-?[\d.]+)', line)
    p2 = re.search(r' p2=(-?\d+),(-?\d+) map=(\d):(-?[\d.]+),(-?[\d.]+)@(-?\d+),(-?\d+) compass=(\d):(-?\d+)@', line)
    player = None
    if p2:
        player = {'x': float(p2[1]), 'z': float(p2[2]), 'on_map': p2[3] == '1', 'u': float(p2[4]), 'v': float(p2[5]),
                  'on_compass': p2[8] == '1', 'degrees': float(p2[9])}
    return line, counts, (float(view[1]), float(view[2])) if view else None, player


def witnesses_of(bot):
    line = report_line(bot, 'witnesses', 'WITNESS ')
    return line, {k: int(v) for k, v in re.findall(r'(\w+)=(-?\d+)', line)}


def stageWitness(copies, report, negative=False):
    """Stage Witness, an NPC the host runs reacts to the client's crime as in single-player (owner 24.10.). The game judges a
    crime only against its local player (AssessTakeItem, MemorizePlayerCrime ask GetPlayer), so it judges the client's theft on
    the client, where the host's NPC is a mirror whose reaction never runs. There G3_WITNESS (default Hamlar) judges the
    client's hero a thief (AssessTarget with the attack reason Theft, the last step of AssessTakeItem); the host's real NPC
    then goes after the client's puppet (its target puppet2, its attack reason Theft). Negative control: witnessoff on the
    client (nothing is passed on)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    name = os.environ.get('G3_WITNESS', 'Hamlar')
    if negative:
        assert 'ok=1' in report_line(client.bot, 'witnessoff', 'WITNESSOFF ')
    teleport_to(host.bot, name)
    teleport_to(client.bot, name)
    report_line(client.bot, 'lookat ' + name, 'LOOKAT ')
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 600, y, z))
    time.sleep(4)
    report['host_before'] = crime_of(host.bot, name)
    assert report['host_before'], f'no NPC {name} on the host'
    report['saw'] = report_line(client.bot, f'witness {name}_4', ('WITNESSSAW ', 'REFUSED witness'))
    assert 'mirrored=1' in report['saw'], f"{name} is not the host's on the client: " + report['saw']
    try:
        report['host_after'] = wait_for(lambda: (lambda c: c.get('target') == 'puppet2' and c.get('reason') == '4' and c)(crime_of(host.bot, name)),
                                        10, f"{name} goes after the client on the host")
    finally:
        report['host_last'] = crime_of(host.bot, name)
        report['witness'] = {c.tag: witnesses_of(c.bot)[0] for c in (host, client)}
        report['host_log'] = [l for l in host.mod_log().splitlines() if 'witness:' in l][-4:]
        report['client_log'] = [l for l in client.mod_log().splitlines() if 'witness:' in l][-4:]


def stageTogether(copies, report, negative=False):
    """Stage Together, reputation = together (owner 24.10.: shared guilt): what an NPC holds against one player, it holds
    against both. The client's hero is judged a thief as in stageWitness by NPCs near the start without a town (the game
    remembers a theft at the NPC itself then: MemorizeEnclaveCrime takes it only for a town's boss or guard) until the client's
    copy of one holds it (LastPlayerCrime Theft = 3, it refuses to talk to the client); the host's own NPC then holds it
    against the host's player too. Negative control: reputation = shared, the default (the host's NPC holds nothing)."""
    copies['host'].ini_extra = '' if negative else 'reputation = together\n'
    host, client = join_both(copies)
    clear_raid(host, report)
    report['mode'] = {c.tag: report_line(c.bot, 'enclavesync', 'ENCLAVESYNC ') for c in (host, client)}
    companions = {'Diego', 'Gorn', 'Milten', 'Lester'}
    here = hero_pos(client.bot)
    # The host's NPCs as the client mirrors them, nearest first (members of a town too: MemorizeEnclaveCrime takes a theft
    # only from its boss or a guard, so an ordinary member remembers it itself).
    rows = sorted((v for v in npcs(client.bot).values() if v['mirrored'] and not v['dead'] and v['name'] not in companions
                   and not v['name'].startswith(('Orc', 'G3T_', 'Wolf', 'Scavenger', 'Boar', 'Rat', 'Bird'))),
                  key=lambda v: distance(v['pos'], here))
    candidates = [v['name'] for v in rows][:15]
    report['candidates'] = candidates
    chosen, report['tries'] = None, []
    for name in candidates:
        before = crime_of(client.bot, name)
        if not before or before.get('last_crime') != '0':
            report['tries'].append(f'{name}: skipped {before}')
            continue
        saw = report_line(client.bot, f'witness {name}_4', ('WITNESSSAW ', 'REFUSED witness'))
        report['tries'].append(saw)
        if 'mirrored=1' in saw and 'last_crime=3' in saw:
            chosen = name
            break
    assert chosen, f"no NPC near the start held the client's theft on the client: {report['tries']}"
    report['chosen'] = chosen
    report['client_after'] = crime_of(client.bot, chosen)
    time.sleep(3)
    report['host_after'] = crime_of(host.bot, chosen)
    report['witness'] = {c.tag: witnesses_of(c.bot)[0] for c in (host, client)}
    report['host_log'] = [l for l in host.mod_log().splitlines() if 'witness:' in l][-4:]
    assert report['host_after'].get('last_crime') == '3', \
        f"{chosen} holds nothing against the host's player: {report['host_after']}"


def ransack_of(bot):
    line = report_line(bot, 'ransack', 'RANSACK ')
    return line, {k: int(v) for k, v in re.findall(r'(\w+)=(-?\d+)', line)}


def npc_gold(bot, name):
    return int(re.search(r'gold=(-?\d+)', report_line(bot, 'npcgold ' + name, 'NPCGOLD '))[1])


def knock_out(attacker_bot, hit_command, victim_bot, report, key):
    """Blows of the attacker (hurthero / hitpuppetby <guid>) until the victim's hero is down; returns its HERO line."""
    report[key] = []
    for _ in range(40):
        report[key].append(report_line(attacker_bot, hit_command, ('HURTHERO ', 'HITPUPPETBY ', 'REFUSED'))[-100:])
        if ' down=1 ' in herostate(victim_bot):
            break
        time.sleep(0.5)
    return wait_for(lambda: (lambda s: ' down=1 ' in s and [s])(herostate(victim_bot)), 10, 'the hero is knocked out')[0]


def stageShieldBlock(copies, report, negative=False):
    """P4, the hit judgement (AssessHit) for the player as in single-player: a hero who parries with a shield and the shield
    talent (Perk_Shield_2) pays a blocked blow with stamina, half of it with the talent (Script_Game SG+2c69c..2c70b, the
    branch only the player gets), an unblocked one with hit points. G3_STRIKER (default Hamlar) strikes G3_BLOWS (default 4)
    blows each: (1) on the parrying host hero (hurthero, single-player on the host: shows that blocks happen); (2) on the
    parrying client hero, judged on the client alone (hurthero on the client: the client's own single-player judgement, the NPC
    scaled to the client's level as the game scales it to its player); (3) on the client's puppet from the host (hitpuppetby,
    the co-op path; the client holds its parade every frame, as with the key held down, and its puppet parries on the host
    at the blow, as the snapshots show it: hitpuppetby <guid>_parade). (3) must reach the client every time, block at least once and cost what (2) cost. (1) is shown, not required
    (whether the host's own hero faced him varies). G3_SHIELD (default It_Shield_Wood_01), G3_SHIELDPERKS (default
    Perk_Shield_1,Perk_Shield_2; empty: none). Negative control: puppetparadeoff on the host (the host judges the parrying
    puppet's block itself, as before 25.10.: the blows never reach the client, its block costs nothing)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    striker = os.environ.get('G3_STRIKER', 'Hamlar')
    shield = os.environ.get('G3_SHIELD', 'It_Shield_Wood_01')
    perks = [p for p in os.environ.get('G3_SHIELDPERKS', 'Perk_Shield_1,Perk_Shield_2').split(',') if p]
    blows = int(os.environ.get('G3_BLOWS', '4'))
    for c in (host, client):
        report_line(c.bot, 'arm It_1H_Sword_Rust_01', 'ARM ')
        for perk in perks:
            report.setdefault(c.tag + '_perks', []).append(report_line(c.bot, 'learnperk ' + perk, ('LEARNPERK ', 'REFUSED learnperk'))[:90])
    teleport_to(host.bot, striker)
    time.sleep(4)
    guid = next(g for g, v in npcs(host.bot).items() if v['name'] == striker)
    value = lambda line, key: int(re.search(rf' {key}=(-?\d+)', line)[1])

    def blow(victim, hitter, command):
        """One blow on the parrying victim; its cost as the game judged it on the victim's machine (stamina comes back
        within seconds, so it is read at the judgement): (result, stamina, hit points), or None when it never arrived."""
        report_line(victim.bot, 'sethp 1000', 'SETHP ')
        report_line(host.bot, 'npcstill ' + guid, ('NPCSTILL ', 'REFUSED npcstill'))
        time.sleep(1)
        report_line(victim.bot, 'face ' + guid, 'FACE ')
        time.sleep(1)
        guard = report_line(victim.bot, 'guard ' + shield, ('GUARD ', 'REFUSED guard'))
        assert f'left_hand={shield}' in guard, guard
        probe = report_line(victim.bot, 'hitprobe ' + guid, ('HITPROBE ', 'REFUSED hitprobe'))
        waits = []
        if command == 'hitpuppetby ':  # until the host sees the puppet parry him, as the client's held parade shows it there
            for _ in range(10):
                waits.append(report_line(host.bot, 'puppetparade ' + guid, ('PUPPETPARADE ', 'REFUSED puppetparade'))[13:60])
                if 'parade=1 fov=1' in waits[-1]:
                    break
                time.sleep(0.4)
        since = len(victim.mod_log())
        hit = report_line(hitter.bot, command + guid + ('_parade' if command == 'hitpuppetby ' else ''), ('HURTHERO ', 'HITPUPPETBY ', 'REFUSED'))
        time.sleep(2.5)
        row = {'probe': probe[9:200], 'hit': hit[:330], 'waits': waits}
        if hit.startswith('HURTHERO '):
            row['cost'] = (re.search(r'result=(\S+)', hit)[1], value(hit, 'sp_after') - value(hit, 'sp_before'), value(hit, 'after') - value(hit, 'before'))
        else:
            judged = [l for l in victim.mod_log()[since:].splitlines() if 'forwarded blow judged here' in l]
            m = judged and re.search(r'result=(\S+) pending=\d+ hp (-?\d+) -> (-?\d+) sp (-?\d+) -> (-?\d+)', judged[-1])
            row['cost'] = (m[1], int(m[5]) - int(m[4]), int(m[3]) - int(m[2])) if m else None
            # Whether the host's game saw the puppet parry this blow (the case the host used to take for itself).
            row['host_parried'] = ' puppet_parade=1 puppet_fov=1 ' in hit
        return row

    report['host_face'] = report_line(host.bot, 'face ' + guid, 'FACE ')[:80]
    report['host'] = [blow(host, host, 'hurthero ') for _ in range(blows)]
    # The host steps aside, the client takes its place in front of him.
    x, y, z = hero_pos(host.bot)
    teleport_to(client.bot, striker)
    move_hero(host.bot, (x + 600, y, z))
    time.sleep(4)
    report['client_alone'] = [blow(client, client, 'hurthero ') for _ in range(blows)]
    for _ in range(4):  # the client's own block cost, the reference: more blows while none was blocked (its turn varies)
        if any(b['cost'][0] == '0x14' for b in report['client_alone']):
            break
        report['client_alone'].append(blow(client, client, 'hurthero '))
    if negative:
        assert 'ok=1' in report_line(host.bot, 'puppetparadeoff', 'PUPPETPARADEOFF ')
    report['client'] = [blow(client, host, 'hitpuppetby ') for _ in range(blows)]
    report['parade'] = report_line(host.bot, 'parade', 'PARADE ')
    report['cost'] = {key: [b['cost'] for b in report[key]] for key in ('host', 'client_alone', 'client')}
    blocks = lambda key: {c[1:] for c in report['cost'][key] if c and c[0] == '0x14'}
    hits = lambda key: {c[1:] for c in report['cost'][key] if c and c[0] != '0x14'}
    assert blocks('client_alone'), f"no block in the client's single-player control: {report['cost']} (test setup)"
    assert all(report['cost']['client']), f"a blow never reached the client: {report['cost']['client']}"
    assert any(b['host_parried'] for b in report['client']), f"the host never saw the puppet parry: {[b['hit'][-120:] for b in report['client']]} (test setup)"
    assert blocks('client'), f"the client never blocked through the co-op path: {report['cost']['client']}"
    assert blocks('client') <= blocks('client_alone') and hits('client') <= hits('client_alone') | {(0, 0)}, \
        f"co-op {report['cost']['client']}, the client alone {report['cost']['client_alone']}"


def stageNpcBlock(copies, report, negative=False):
    """P4, the hit judgement the other way round: an NPC of the host parries a player's blow. Single-player control: G3_BLOCKER
    (default Hamlar) parries the host hero (npcguard: facing it, parade held) and the host strikes (hitnpc): the game blocks
    (ZS_ParadeStumble, stamina, no hit points). Then he parries the client's puppet and the client strikes its mirror of him,
    G3_BLOWS (default 6) times: on the host every blow the game there judges parried (parrying, able to parry it, facing the
    puppet) must be blocked as in the control (the owner's block: stamina, no hit points), every other one hurt him; at least
    two must be parried. Negative control: npcblockoff on both (judged on the mirror, as before 25.10.)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    blocker = os.environ.get('G3_BLOCKER', 'Hamlar')
    blows = int(os.environ.get('G3_BLOWS', '6'))
    for c in (host, client):
        report_line(c.bot, 'arm It_1H_Sword_Rust_01', 'ARM ')
    teleport_to(host.bot, blocker)
    time.sleep(4)
    guid = next(g for g, v in npcs(host.bot).items() if v['name'] == blocker)
    state = lambda bot: report_line(bot, 'npcparade ' + guid, ('NPCPARADE ', 'REFUSED npcparade'))
    value = lambda line, key: int(re.search(rf' {key}=(-?\d+)', line)[1])

    def blow(striker, facing):
        report_line(host.bot, 'npcstill ' + guid, ('NPCSTILL ', 'REFUSED npcstill'))
        time.sleep(1)
        report_line(striker.bot, 'face ' + guid, 'FACE ')
        time.sleep(1)
        guard = report_line(host.bot, f'npcguard {guid}_{facing}', ('NPCGUARD ', 'REFUSED npcguard'))
        time.sleep(0.5)
        since = len(host.mod_log())
        hit = report_line(striker.bot, 'hitnpc ' + guid, ('HITNPC ', 'REFUSED hitnpc'))
        time.sleep(0.6)
        after = state(host.bot)
        blocked = [l[-90:] for l in host.mod_log()[since:].splitlines() if 'blocked a player' in l]
        row_log = [l[-110:] for l in host.mod_log()[since:].splitlines() if 'combat:' in l][-4:]
        time.sleep(3)
        row = {'guard': guard[8:160], 'hit': re.sub(r'guid=\S+ |source=\S+ owner=\S+ ', '', hit)[:260], 'host_after': after[9:160],
               'host_blocked': blocked, 'host_log': row_log,
               # Whether the game on the host said the NPC parries this blow (parrying, able to, facing it): then it must block.
               'parrying': any('parade_mode=1 can_parade=1 fov=1' in l for l in row_log)}
        if striker is host:
            row['cost'] = (re.search(r'result=(\S+)', hit)[1], value(hit, 'sp_after') - value(hit, 'sp_before'), value(hit, 'after') - value(hit, 'before'))
        else:
            m = blocked and re.search(r'stamina (-?\d+) -> (-?\d+) hit points (-?\d+) -> (-?\d+)', blocked[-1])
            row['cost'] = ('0x14', int(m[2]) - int(m[1]), int(m[4]) - int(m[3])) if m else ('hit', 0, value(after, 'hp') - value(guard, 'hp'))
        return row

    report['host'] = [blow(host, 'hero') for _ in range(blows)]
    x, y, z = hero_pos(host.bot)
    teleport_to(client.bot, blocker)
    move_hero(host.bot, (x + 600, y, z))
    time.sleep(4)
    if negative:
        for c in (host, client):
            assert 'ok=1' in report_line(c.bot, 'npcblockoff', 'NPCBLOCKOFF ')
    report['client'] = [blow(client, 'puppet2') for _ in range(blows)]
    report['parade'] = {c.tag: report_line(c.bot, 'parade', 'PARADE ') for c in (host, client)}
    report['cost'] = {key: [b['cost'] for b in report[key]] for key in ('host', 'client')}
    assert any(c[0] == '0x14' and c[1] < 0 and c[2] == 0 for c in report['cost']['host']), f"the control blocked nothing: {report['cost']} (test setup)"
    assert all(' claim_sent=1 ' in b['hit'] for b in report['client']), f"a blow never went to the host: {[b['hit'][:150] for b in report['client']]}"
    parried = [b for b in report['client'] if b['parrying']]
    assert len(parried) >= 2, f"the host's game saw him parry only {len(parried)} blows (test setup): {[b['host_log'] for b in report['client']]}"
    for b in parried:
        assert b['cost'][0] == '0x14' and b['cost'][1] < 0 and b['cost'][2] == 0, f"a parried blow hurt him on the host: {report['cost']['client']}"
    for b in report['client']:
        if not b['parrying']:
            assert b['cost'][0] == 'hit', f"a blow he did not parry was blocked: {report['cost']['client']}"


def stageRansack(copies, report, negative=False):
    """Stage Ransack, a guard of the host robs a knocked-out partner as in single-player (owner 24.10.; C
    re/findings/defeat-arena-c.md, p3-load-autoload-ransack-contract-c-20261023.md). G3_ROBBER (default Hamlar) attacks the
    client's puppet (AssessTarget, reason G3_ROBREASON, default 7 Angry); his blows go to the client and knock it out by the
    game's own rules (not a death the mod turns into a knock-out: nobody is told to help). On the host the puppet lies down for
    the AI and carries the client's gold, he robs it as the game does (ZS_RansackBody), and the client pays from its real
    hero: of G3_ROBGOLD (default 11) gold half rounded down, at least one piece (11 -> 6); he gains as much on the host. The
    client stands up by itself. With G3_ROB_BASELINE=1 he first knocks out and robs the host's own hero (the game alone).
    Negative control: ransackoff on the host (he takes the puppet's gold, the client pays nothing)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    robber = os.environ.get('G3_ROBBER', 'Hamlar')
    reason = int(os.environ.get('G3_ROBREASON', '7'))
    gold = int(os.environ.get('G3_ROBGOLD', '11'))
    owed = 0 if gold <= 0 else (gold // 2 if gold // 2 >= 1 else gold)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'ransackoff', 'RANSACKOFF ')
    teleport_to(host.bot, robber)
    time.sleep(4)
    guid = next(g for g, v in npcs(host.bot).items() if v['name'] == robber)
    if os.environ.get('G3_ROB_BASELINE') == '1':
        report_line(host.bot, 'givepotion It_Gold-0', 'GIVEPOTION ')
        report_line(host.bot, f'givepotion It_Gold-{gold}', 'GIVEPOTION ')
        report['base_robber_gold'] = npc_gold(host.bot, robber)
        report['base_sethp'] = report_line(host.bot, 'sethp 20', 'SETHP ')
        report['base_assess'] = report_line(host.bot, f'assesshero {robber}_{reason}', ('ASSESSHERO ', 'REFUSED'))
        report['base_down'] = knock_out(host.bot, 'hurthero ' + guid, host.bot, report, 'base_blows')
        try:
            report['base_robbed'] = wait_for(lambda: (lambda g: g < gold and [g])(carried(host.bot, 'It_Gold')), 30, 'the host is robbed')[0]
        except AssertionError:
            report['base_robbed'] = None
        report['base_robber_after'] = report_line(host.bot, 'npcgold ' + robber, 'NPCGOLD ')
        report['base_up'] = wait_for(lambda: (lambda s: ' down=0 ' in s and [s])(herostate(host.bot)), 60, 'the host stands up')[0]
        report_line(host.bot, 'sethp 1000', 'SETHP ')
        time.sleep(8)
    # The client, with its gold, next to him; the host a few metres away (its region: the host runs him).
    report_line(client.bot, 'givepotion It_Gold-0', 'GIVEPOTION ')
    if gold > 0:
        report_line(client.bot, f'givepotion It_Gold-{gold}', 'GIVEPOTION ')
    report['client_gold_before'] = carried(client.bot, 'It_Gold')
    assert report['client_gold_before'] == gold, f"the client carries {report['client_gold_before']} gold, not {gold}"
    teleport_to(client.bot, robber)
    report_line(client.bot, 'lookat ' + robber, 'LOOKAT ')
    # The host steps aside: on the client's spot it kept him from reaching the body (ZS_WaitForFreeDestination, run
    # 20261024-092931: the client stood up meanwhile and he gave up, as the game does).
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 600, y, z))
    time.sleep(4)
    report['robber_gold_before'] = npc_gold(host.bot, robber)
    report['client_sethp'] = report_line(client.bot, 'sethp 20', 'SETHP ')
    since_host, since_client = len(host.mod_log()), len(client.mod_log())
    client_lines = lambda: client.mod_log()[since_client:].splitlines()
    host_lines = lambda: host.mod_log()[since_host:].splitlines()
    report['aggro'] = report_line(host.bot, f'aggro {robber}_{reason}', ('AGGRO ', 'REFUSED aggro'))
    assert 'result=' in report['aggro'], report['aggro']
    try:
        report['down'] = knock_out(host.bot, 'hitpuppetby ' + guid, client.bot, report, 'blows')
        assert not any('would die' in l for l in client_lines()), 'the client died (a death turned into a knock-out), not knocked out: test setup'
        wait_for(lambda: (lambda r: r[1]['gold_taken'] >= 1 and r[0])(ransack_of(host.bot)), 40, 'he robs the puppet')
        # The host's hero cannot plunder the knocked-out partner (its puppet lies down like a plunderable body).
        report['host_loot'] = report_line(host.bot, 'lootopen puppet2', ('LOOTOPEN ', 'REFUSED lootopen'))
    finally:
        report['host_ransack'] = ransack_of(host.bot)[0]
        report['client_ransack'] = ransack_of(client.bot)[0]
        report['robber_after'] = report_line(host.bot, 'npcgold ' + robber, 'NPCGOLD ')
        report['host_log'] = [l for l in host_lines() if 'ransack:' in l or 'combat:' in l][-12:]
        report['client_log'] = [l for l in client_lines() if 'ransack:' in l or 'downed:' in l or 'hud:' in l][-10:]
    if negative:
        time.sleep(5)
        report['client_gold_after'] = carried(client.bot, 'It_Gold')
    else:
        report['client_gold_after'] = wait_for(lambda: (lambda g: g != gold and [g])(carried(client.bot, 'It_Gold')), 15, 'the client pays')[0]
    report['robber_gold_after'] = npc_gold(host.bot, robber)
    assert report['client_gold_after'] == gold - owed, f"the client has {report['client_gold_after']} gold, expected {gold - owed}"
    assert report['robber_gold_after'] - report['robber_gold_before'] == owed, \
        f"{robber} gained {report['robber_gold_after'] - report['robber_gold_before']} gold, expected {owed}"
    report['up'] = wait_for(lambda: (lambda s: ' down=0 ' in s and ' dead=0 ' in s and [s])(herostate(client.bot)), 60,
                            'the client stands up by itself')[0]
    report['client_log'] = [l for l in client_lines() if 'ransack:' in l or 'downed:' in l or 'hud:' in l][-10:]
    assert not any('waiting for a partner' in l for l in report['client_log']), 'the knock-out was held for help'
    assert 'result=0' in report['host_loot'] and any('loot of G3T_Player2 refused' in l for l in host_lines()),         "the host's hero could plunder the knocked-out partner: " + report['host_loot']


def stageMapMarks(copies, report, negative=False):
    """Phase 3, owner's point 9: the other player on the map and the compass (C re/findings/map-compass-c.md: drawn after the
    game's own paint of its map control and compass, nothing put into the map's saved marker list). The client stands 15 m
    from the host: the host's compass shows it in its direction as seen from the camera. The host opens its world map: the
    client is drawn at its place; the client goes to Reddock and its mark follows (u and v change as x and z do).
    Screenshots of the compass and both map views. Negative control: mapmarksoff on the host (nothing drawn)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report_line(host.bot, 'mapmarksoff', 'MAPMARKSOFF ')
    line, counts, view, player = map_marks(host.bot)
    report['hooks'] = line
    assert 'map_hooked=1' in line and 'compass_hooked=1' in line, 'the paints are not hooked: ' + line
    at = hero_pos(host.bot)
    report['client_moved'] = move_hero(client.bot, (at[0] + 1500, at[1], at[2] + 500))
    time.sleep(4)
    problems = []
    # Compass: drawn in the client's direction from the camera (the needle's own reference).
    before = counts
    report['compass'], counts, view, player = map_marks(host.bot)
    snap(host, 'compass', report)
    host_at = hero_pos(host.bot)
    if not player or not player['on_compass'] or counts.get('compass_drawn', 0) <= before.get('compass_drawn', 0):
        problems.append('the client is not drawn on the compass: ' + report['compass'])
    elif view:
        dx, dz = player['x'] - host_at[0], player['z'] - host_at[2]
        expected = math.degrees(math.atan2(view[1] * dx - view[0] * dz, view[0] * dx + view[1] * dz))
        report['compass_expected_degrees'] = round(expected)
        if abs((player['degrees'] - expected + 180) % 360 - 180) > 15:
            problems.append(f"compass direction {player['degrees']} degrees, expected {expected:.0f}")
    # Map: the world map opened by the host.
    report['map_open'] = report_line(host.bot, 'mapopen', ('MAPOPEN', 'REFUSED mapopen'))
    time.sleep(2)
    from bot import post_click
    report['tip_ok_clicks'] = post_click(host.process.pid, 0.5, 0.65)  # the first document view's tip box covers the map
    time.sleep(1.5)
    report['map_near'], counts, view, near = map_marks(host.bot)
    snap(host, 'map-near', report)
    if not near or not near['on_map']:
        problems.append('the client is not drawn on the map: ' + report['map_near'])
    report['client_far'] = teleport_to(client.bot, os.environ.get('G3_FAR_PLACE', 'Brenton'))
    time.sleep(6)
    report['map_far'], counts, view, far = map_marks(host.bot)
    snap(host, 'map-far', report)
    if near and near['on_map'] and far:
        if not far['on_map']:
            problems.append('the client at Reddock is not on the map: ' + report['map_far'])
        else:
            dx, dz, du, dv = far['x'] - near['x'], far['z'] - near['z'], far['u'] - near['u'], far['v'] - near['v']
            report['map_follow'] = {'dx': dx, 'dz': dz, 'du': round(du, 4), 'dv': round(dv, 4)}
            # u grows with x, v falls with z (gCMap_PS::CalcUV, C); a move of more than 50 m must show.
            if abs(dx) > 5000 and (du > 0) != (dx > 0):
                problems.append(f'the mark moved {du:+.3f} in u while the client moved {dx:+.0f} in x')
            if abs(dz) > 5000 and (dv < 0) != (dz > 0):
                problems.append(f'the mark moved {dv:+.3f} in v while the client moved {dz:+.0f} in z')
            if abs(dx) <= 5000 and abs(dz) <= 5000:
                problems.append(f'the client moved only {dx:.0f}/{dz:.0f}; no check of the map')
    report['map_close'] = report_line(host.bot, 'mapclose', ('MAPCLOSE', 'REFUSED mapclose'))
    report['problems'] = problems
    assert not problems, '; '.join(problems)


def quality_count(bot, name):
    """{quality: amount} of the item on the hero (QA qualitycount)."""
    line = report_line(bot, f'qualitycount {name}', ('QUALITYCOUNT', 'REFUSED qualitycount'))
    return {int(q): int(n) for q, n in re.findall(r' q(\d+)=(-?\d+)', line)}


def stageTradeQuality(copies, report, negative=False):
    """Player trade keeps item quality (24.10.; C's list of 23.10.: traded items arrived at quality 0, a sharp or forged
    weapon lost what made it so). The host carries a sharp hunting bow (quality 32), the client a forged rusty sword
    (quality 128). In one trade window the client buys the sharp bow from the host's puppet (its stack there is sharp) and
    sells it the forged sword; the host accepts. Afterwards the client has one more sharp bow, the host one less, and the
    host has the forged sword, the client not. Negative control: tradequalityoff on the client (what it gets and gives
    arrives at quality 0, as before)."""
    bow, sword, sharp, forged = 'It_Bow_Hunting', WEAPON, 32, 128
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report_line(client.bot, 'tradequalityoff', 'TRADEQUALITYOFF ')
    for copy in (host, client):  # the bow costs 200 and the puppet pays for the sword with the host's gold
        report[copy.tag+'_gold'] = report_line(copy.bot, 'givepotion It_Gold-500', 'GIVEPOTION ')
    report['host_sharp_bow'] = report_line(host.bot, f'givequality {bow}_{sharp}', ('GIVEQUALITY', 'REFUSED givequality'))
    report['client_forged_sword'] = report_line(client.bot, f'givequality {sword}_{forged}', ('GIVEQUALITY', 'REFUSED givequality'))
    assert f'got={sharp}' in report['host_sharp_bow'] and f'got={forged}' in report['client_forged_sword'], 'the quality items were not made'
    wait_for(lambda: (l := player_goods(client.bot, f'1_{bow}-q{sharp}')) and f'item={bow}:0 ' not in l and 'unknown' not in l and [l], 30,
             "the host's sharp bow reaches the client's goods")

    def counts():
        h, c = quality_count(host.bot, bow), quality_count(client.bot, bow)
        hs, cs = quality_count(host.bot, sword), quality_count(client.bot, sword)
        return {'host_sharp_bow': h.get(sharp, 0), 'client_sharp_bow': c.get(sharp, 0),
                'host_forged_sword': hs.get(forged, 0), 'client_forged_sword': cs.get(forged, 0)}

    before = report['before'] = counts()
    synced = tradesync(client.bot)
    report['approach'] = report_line(client.bot, 'approach puppet1', ('APPROACH', 'REFUSED approach'))
    report['open'] = report_line(client.bot, 'tradeopen puppet1', ('TRADEOPEN', 'REFUSED tradeopen'))
    time.sleep(2)
    report['puppet_goods'] = player_goods(client.bot, 'puppet1')
    report['buy'] = report_line(client.bot, f'tradebuy puppet1_{bow}-q{sharp}', ('TRADEBUY', 'REFUSED tradebuy'))
    report['sell'] = report_line(client.bot, f'tradesell puppet1_{sword}-q{forged}', ('TRADESELL', 'REFUSED tradesell'))
    time.sleep(2)
    close_trade(client, report)
    report['sent'] = wait_for(lambda: (t := tradesync(client.bot))['offers'] > synced['offers'] and [t], 15,
                              'the closed window went out as an offer')[0]
    wait_for(lambda: ' pending=1 ' in report_line(host.bot, 'offerstate', 'OFFERSTATE '), 15, 'the offer reaches the host')
    report['decide'] = report_line(host.bot, 'offeraccept', ('OFFER', 'REFUSED'))
    wait_for(lambda: tradesync(client.bot)['accepted'] > synced['accepted'] and ['answered'], 20, 'the client learns the host accepted')
    time.sleep(2)
    after = report['after'] = counts()
    wanted = report['wanted'] = {'host_sharp_bow': before['host_sharp_bow'] - 1, 'client_sharp_bow': before['client_sharp_bow'] + 1,
                                 'host_forged_sword': before['host_forged_sword'] + 1, 'client_forged_sword': before['client_forged_sword'] - 1}
    report['host_bow_qualities'], report['client_bow_qualities'] = quality_count(host.bot, bow), quality_count(client.bot, bow)
    report['host_sword_qualities'], report['client_sword_qualities'] = quality_count(host.bot, sword), quality_count(client.bot, sword)
    assert after == wanted, f'after the trade {after}, expected {wanted}'


def stageTradePrice(copies, report, negative=False):
    """Phase 2, player trade step 5 (owner 23.10.: the same price both ways): with another player's puppet the game's price
    script (OnPlayerGetGoldCost, which the trade screen asks) gives an item's value both when buying it from the puppet
    and when selling one to it; with the trader Hamlar selling still brings less than the value (traders unchanged).
    Negative control: tradepricesoff on the client (the puppet prices like a trader)."""
    item = 'It_Bow_Hunting'  # every test hero carries one, unequipped, worth more than 1 gold
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report_line(client.bot, 'tradepricesoff', 'TRADEPRICESOFF ')
    wait_for(lambda: (l := player_goods(client.bot, '1_'+item)) and f'item={item}:0 ' not in l and 'unknown' not in l and [l], 30,
             "the host's goods reach the client")
    report['approach'] = report_line(client.bot, 'approach puppet1', ('APPROACH', 'REFUSED approach'))
    report['tradeon'] = report_line(client.bot, 'puppettradeon 1', ('PUPPETTRADE', 'REFUSED puppettradeon'))
    report['assesstalk'] = report_line(client.bot, 'assesstalk puppet1', ('ASSESSTALK', 'REFUSED assesstalk'))
    time.sleep(2)  # the dialog fills the puppet with the host's goods
    report['puppet_price'] = line = report_line(client.bot, f'tradeprice puppet1_{item}', ('TRADEPRICE', 'REFUSED tradeprice'))
    close_trade(client, report)
    values = dict(re.findall(r'(buy|sell|value)=(-?\d+)', line))
    report['trader_price'] = trader = report_line(client.bot, f'tradeprice Hamlar_{item}', ('TRADEPRICE', 'REFUSED tradeprice'))
    trader_values = dict(re.findall(r'(sell|value)=(-?\d+)', trader))
    problems = []
    if not values or int(values['value']) <= 1:
        problems.append(f'no price for {item}: {line}')
    elif not (values['buy'] == values['sell'] == values['value']):
        problems.append(f'with the puppet buy, sell and value differ: {line}')
    if trader_values and int(trader_values.get('sell', -1)) >= int(trader_values.get('value', 0)):
        problems.append(f'the trader Hamlar pays the full value now: {trader}')
    report['problems'] = problems
    assert not problems, '; '.join(problems)


def stageTradeGoods(copies, report, negative=False):
    """Phase 2, player trade step 4a (ENTWURF-HANDEL-2026-10-23): what a player can trade reaches the other machine
    (MsgGoods), and trading with its puppet (the game's own trade option FILLER27 on it) fills the puppet with exactly
    those goods (every puppet offers trade by itself, no QA switch). After the first trade's tip box is closed with its
    Ok, the window shows the seller's goods (screenshots). One more of an item, given to the host, counts on the client and in the client's trade screen (the test
    heroes carry every spell already); the puppet's gold is the host's.
    Negative control: tradefilloff on the client (the puppet keeps what its template carries)."""
    item = 'It_Spell_TimeBubble'
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report_line(client.bot, 'tradefilloff', 'TRADEFILLOFF ')
    report['give'] = report_line(host.bot, f'givepotion {item}-1', 'GIVEPOTION ')
    # Gold of its own for the host, so its side of the trade screen tells itself from the client's (same start gold).
    report['give_gold'] = report_line(host.bot, 'givepotion It_Gold-654', 'GIVEPOTION ')
    count = int(re.search(r' now=(\d+)', report['give'])[1])
    host_gold = re.search(r' now=(\d+)', report['give_gold'])[1]
    wanted = f'item={item}:{count} '
    report['host_goods'] = wait_for(lambda: (l := player_goods(host.bot, '0_'+item)) and wanted in l and f' gold={host_gold}' in l and [l], 10,
                                    'the host counts the item and the gold among its goods')[0]
    report['client_sees'] = wait_for(lambda: (l := player_goods(client.bot, '1_'+item)) and wanted in l and f' gold={host_gold}' in l and [l], 30,
                                     "the host's goods reach the client")[0]
    report['approach'] = report_line(client.bot, 'approach puppet1', ('APPROACH', 'REFUSED approach'))
    if os.environ.get('G3_TRADE_VIA') == 'open':  # comparison: the trade screen through PlayerOpenTrade, as for an NPC trader
        report['tradeopen'] = report_line(client.bot, 'tradeopen puppet1', ('TRADEOPEN', 'REFUSED tradeopen'))
    else:
        report['assesstalk'] = report_line(client.bot, 'assesstalk puppet1', ('ASSESSTALK', 'REFUSED assesstalk'))
        time.sleep(2)
        report['choose'] = report_line(client.bot, 'talkinfo FILLER27', ('TALKINFO', 'REFUSED talkinfo'))
    time.sleep(1)
    snap(client, 'trade-tip', report)
    # The first trade shows a tip box; the screen binds the new seller only when its Ok is clicked (C
    # trade-vendor-empty-followup-c.md: BeginTrade binds the partner to the tip page, which ignores it).
    from bot import post_click
    report['tip_ok_clicks'] = post_click(client.process.pid, 0.5, 0.65)
    time.sleep(2)
    snap(client, 'trade-after-ok', report)
    time.sleep(1)
    report['puppet_goods'] = player_goods(client.bot, 'puppet1_'+item)
    report['tradesync'] = report_line(client.bot, 'tradesync', 'TRADESYNC ')
    snap(client, 'trade', report)
    close_trade(client, report)
    problems = []
    if wanted not in report['puppet_goods']:
        problems.append(f"the host's puppet does not offer {count} {item}: {report['puppet_goods']}")
    if f' gold={host_gold}' not in report['puppet_goods']:
        problems.append(f"the host's puppet does not have the host's gold {host_gold}: {report['puppet_goods']}")
    report['problems'] = problems
    assert not problems, '; '.join(problems)


def puppet_pose(bot, player_id):
    start = bot.send('remotes', 20)
    bot.line('NET ', 10, 'remotes', start)
    time.sleep(0.05)
    m = re.search(rf'PUPPETPOSE id={player_id} remote=(\S+) shows=(\S+) run0=(-?\d+)(?: shape=(\S+) body=(\S+))?', bot.log()[start:])
    return {'remote': m[1], 'shows': m[2], 'run0': int(m[3]), 'shape': m[4], 'body': m[5]} if m else None


def swings_seen(swinger, watcher, watched_id, report, tag, count=15):
    """The swinger's hero attacks `count` times; how often the watcher's puppet of it shows a running attack."""
    report[tag+'_arm'] = report_line(swinger.bot, 'arm ' + WEAPON, 'ARM ')
    seen, lines = 0, []
    for i in range(count):
        line = report_line(swinger.bot, 'swing', 'SWING ')
        if i < 3:
            lines.append(line)
        shown = False
        for _ in range(6):
            pose = puppet_pose(watcher.bot, watched_id)
            if pose and len(report.setdefault(tag+'_poses', [])) < 24:
                report[tag+'_poses'].append(f"remote={pose['remote'][:60]} shows={pose['shows'][:60]} run0={pose['run0']}")
            if pose and motion_kind(pose['shows']) == 'attack' and pose['run0'] == 1:
                shown = True
                break
            time.sleep(0.2)
        seen += shown
        time.sleep(1.2)
    report[tag+'_swing_lines'] = lines
    report[tag+'_seen'] = f'{seen}/{count}'
    poses = [puppet_pose(watcher.bot, watched_id) for _ in range(10)]
    report[tag+'_frozen_after'] = sum(1 for p in poses if p and p['run0'] == 0)
    return seen


def puppetposediag(copies, report, negative=False):
    """During the raid (orcs strike both players): how often each machine's puppet stands with nothing running."""
    host, client = join_both(copies)
    if os.environ.get('G3_PUPPET_COMBAT_OFF'):
        for copy in (host, client):
            report_line(copy.bot, 'puppetcombatoff', 'PUPPETCOMBATOFF')
    frozen, samples, examples = {'host': 0, 'client': 0}, {'host': 0, 'client': 0}, []
    for _ in range(40):
        for tag, copy, pid in (('host', host, 2), ('client', client, 1)):
            pose = puppet_pose(copy.bot, pid)
            if not pose:
                continue
            samples[tag] += 1
            if pose['run0'] == 0:
                frozen[tag] += 1
                if len(examples) < 10:
                    examples.append(f"{tag}: remote={pose['remote'][:60]} shows={pose['shows'][:60]}")
        time.sleep(0.5)
    report.update(frozen=frozen, samples=samples, examples=examples, status_note='diagnostic only')


def stageV4(copies, report, negative=False):
    """Stage V4 (owner's look: the players' own attacks were hardly visible): when one player's hero swings its
    weapon, the other machine's puppet of that player plays a running attack."""
    host, client = join_both(copies)
    if negative or os.environ.get('G3_PUPPET_COMBAT_OFF'):
        for copy in (host, client):
            report_line(copy.bot, 'puppetcombatoff', 'PUPPETCOMBATOFF')
    # The raid's orcs struck the swinging client (stumbles, weapon lost), so it could not swing: clear the raid first.
    clear_raid(host, report)
    time.sleep(3)
    count = 15
    by_client = swings_seen(client, host, 2, report, 'client', count)
    by_host = swings_seen(host, client, 1, report, 'host', count)
    for tag, copy in (('host', host), ('client', client)):
        start = copy.bot.send('nearnpcs', 20)
        report[tag+'_end'] = copy.bot.line('NEARX end', 10, 'nearnpcs', start)
    assert by_client >= 0.6 * count, f"the host saw {by_client} of {count} client swings"
    assert by_host >= 0.6 * count, f"the client saw {by_host} of {count} host swings"


def stageV1(copies, report, negative=False):
    """Stage V1 (first human test: the client was invisible to the world): during the opening raid the host's orcs
    choose the client's puppet as a target by themselves and their hits reach the client's real hero."""
    host, client = join_both(copies)
    # The puppet no longer joins the host hero's party (that put it into the follower list); the GetAttitude wrapper
    # makes NPCs judge it as the hero. The negative control switches that wrapper off.
    if negative:
        report_line(host.bot, 'attitudeoff', 'ATTITUDEOFF')
    # Orcs that already fight the host's hero keep that target (they re-judge others only now and then), so in two
    # of three raids the client stood where no free orc saw it. Put the client next to the orc that is farthest from
    # the host's hero and not fighting it: that orc must now pick the puppet by its own perception.
    if os.environ.get('G3_V1_ARM'):  # experiment: does an orc strike an armed puppet but not an unarmed one?
        report['client_arm'] = report_line(client.bot, 'arm ' + WEAPON, 'ARM ')
    rows = near_rows(host.bot)
    places = {g.replace('-', '')[:12]: v for g, v in npcs(host.bot).items()}  # NEARX carries a short guid
    host_pos = remotes(client.bot)[1]['pos']
    free = [(distance(places[g]['pos'], host_pos), g) for g, r in rows.items()
            if 'Orc' in r['name'] and not r['dead'] and not r['target_hero'] and g in places]
    if free:
        gap, orc = max(free)
        p = places[orc]['pos']
        # 1.5 m from the orc on the line towards the host's hero: walkable ground (a fixed offset put the client into
        # walls and onto roofs, where the orc only walked and turned), and nearer to the orc than the host's hero.
        f = min(1.0, 150.0 / gap) if gap else 0.0
        spot = tuple(a + (b - a) * f for a, b in zip(p, host_pos))
        report['client_placed_by'] = rows[orc]['name']
        report['client_moved'] = move_hero(client.bot, spot)
    before = combat_state(client.bot)['player_hits_taken']
    targeted = 0
    for _ in range(60):  # orcs circle a long time between blows; stop as soon as one landed
        targeted += sum(1 for r in near_rows(host.bot).values() if r['target_puppet'] and not r['dead'])
        if targeted and combat_state(client.bot)['player_hits_taken'] > before:
            break
        time.sleep(1.5)
    report['host_targets_puppet'] = targeted
    report['client_hits_taken'] = combat_state(client.bot)['player_hits_taken'] - before
    start = host.bot.send('nearnpcs', 20)
    report['host_near_end'] = host.bot.line('NEARX end', 10, 'nearnpcs', start)
    assert targeted > 0, "no NPC of the host chose the client's puppet as a target"
    assert report['client_hits_taken'] > 0, "the host's NPCs never hit the client"


def stageV2(copies, report, negative=False):
    """Stage V2 (first human test: the client saw no fight; owner's second look: T-posing orcs): the client's mirrors
    of the host's fighting NPCs hold the same weapons, nothing stands frozen with no motion running (bind pose), and
    the host's attacks really play on the client (same kind of motion, running). The earlier check "the motion name
    is still there 150 ms later" passed while the bodies were frozen. Negative control: attacks by motion name only."""
    host, client = join_both(copies)
    if negative:
        report_line(client.bot, 'combatreplayoff', 'COMBATREPLAYOFF')
    armed_pairs = armed_host = 0
    host_rows = host_still = client_rows = client_still = host_attacks = client_attacks = 0
    for _ in range(25):
        h, c = near_rows(host.bot), near_rows(client.bot)
        for guid, row in h.items():
            if row['dead'] or guid not in c or not c[guid]['mirrored'] or row['hand'] == '-':
                continue
            armed_host += 1
            armed_pairs += c[guid]['hand'] == row['hand']
        hp, cp = pose_rows(host.bot), pose_rows(client.bot)
        for guid, row in hp.items():
            if row['dead']:
                continue
            host_rows += 1
            host_still += row['run0'] == 0
            m = cp.get(guid)
            if not m or m['dead'] or not m['mirrored']:
                continue
            client_rows += 1
            client_still += m['run0'] == 0
            if motion_kind(row['motion']) == 'attack':
                host_attacks += 1
                client_attacks += motion_kind(m['motion']) == 'attack' and m['run0'] == 1
        time.sleep(1.0)
    start = client.bot.send('nearnpcs', 20)
    report['client_end'] = client.bot.line('NEARX end', 10, 'nearnpcs', start)
    report.update(armed_host=armed_host, armed_same=armed_pairs, host_still=f'{host_still}/{host_rows}',
                  client_still=f'{client_still}/{client_rows}', attacks_running_on_client=f'{client_attacks}/{host_attacks}')
    assert armed_host >= 20, f'too few armed NPCs on the host to judge ({armed_host})'
    assert armed_pairs >= 0.8 * armed_host, f'the client holds the same weapon in {armed_pairs} of {armed_host} cases'
    assert client_rows >= 100 and client_still <= 0.1 * client_rows, f'{client_still} of {client_rows} mirrors had no motion running'
    assert host_attacks >= 30 and client_attacks >= 0.25 * host_attacks, \
        f'{client_attacks} of {host_attacks} host attacks played on the client'


def stageV3(copies, report, negative=False):
    """Stage V3 (human look: the host saw the client's character unarmed): the client draws a weapon; its puppet on
    the host holds the same weapon."""
    host, client = join_both(copies)
    clear_raid(host, report)  # no raid needed: starts from the post-raid save (25.10.)
    if negative:
        report_line(host.bot, 'puppetholdoff', 'PUPPETHOLDOFF')
    report['arm'] = report_line(client.bot, 'arm '+WEAPON, 'ARM ')

    def held():
        start = host.bot.send('remotes', 20)
        host.bot.line('REMOTES end', 10, 'remotes', start)
        line = next((l for l in host.bot.log()[start:].splitlines() if l.startswith('HELD id=2 ')), '')
        m = re.search(r'remote=(\S+) puppet=(\S+)', line)
        return m and m[1] != '-' and m[1] == m[2] and [line]
    report['held'] = wait_for(held, 20, "the client's puppet holds its weapon")[0]


def ownerdiag(copies, report, negative=False):
    """Sync check 19.10.: can an NPC end up simulated on both machines? (npcs.cpp OnNpcs: each side refuses the other's
    snapshots of an NPC it sent itself.) Both players arrive somewhere at the same moment; count NPCs that neither
    machine mirrors. Not reproduced so far (3 spots, 73-75 NPCs, either order), so diagnostic only."""
    host, client = join_both(copies)
    rows = [r for r in far_rows(host.bot) if 300 < r['distance'] < 1500]
    spots = []
    for r in rows:
        if all(distance(r['pos'], s['pos']) > 30000 for s in spots):
            spots.append(r)
        if len(spots) == 3:
            break
    assert len(spots) == 3, f'{len(spots)} far spots'
    doubles, checked, lines = [], 0, []
    for spot in spots:
        if os.environ.get('G3_CLIENT_FIRST'):
            sc = client.bot.send('teleportto '+spot['name'], 30)
            sh = host.bot.send('teleportto '+spot['name'], 30)
        else:
            sh = host.bot.send('teleportto '+spot['name'], 30)
            sc = client.bot.send('teleportto '+spot['name'], 30)
        lines.append(host.bot.line('TELEPORTTO ', 30, 'teleportto', sh))
        client.bot.line('TELEPORTTO ', 30, 'teleportto', sc)
        time.sleep(12)
        h, c = npcs(host.bot), npcs(client.bot)
        for g, v in h.items():
            if v['dead'] or g not in c or c[g]['dead']:
                continue
            checked += 1
            if not v['mirrored'] and not c[g]['mirrored']:
                doubles.append(f"{spot['name']}: {v['name']}")
    report.update(spots=[s['name'] for s in spots], teleports=lines, npcs_checked=checked, simulated_on_both=doubles,
                  client_yields=[l for l in client.mod_log().splitlines() if 'the host keeps it' in l][:10])
    report['status_note'] = 'diagnostic only'
    assert checked >= 5, f'only {checked} NPCs near both players'


def stageV5(copies, report, negative=False):
    """Stage V5 (static review of the GetAttitude wrapper, re/findings/attitude-callers.md): an NPC fleeing from the
    client's puppet keeps judging it as Panic and goes on fleeing; with the hero in the puppet's place it turned on
    it. Negative control: fleekeepoff."""
    host, client = join_both(copies)
    if negative:
        report_line(host.bot, 'fleekeepoff', 'FLEEKEEPOFF')
    orc = living_orc(host.bot)
    report['client_moved'] = move_hero(client.bot, npcs(host.bot)[orc]['pos'])
    time.sleep(2.0)
    report['flee'] = report_line(host.bot, 'flee '+orc, 'FLEE ')
    short = orc.replace('-', '')[:12]
    turned, seen = [], 0
    for _ in range(6):  # the perception scan would put the puppet into the Hostile slot within a few seconds
        time.sleep(1.0)
        row = {g.replace('-', '')[:12]: r for g, r in near_rows(host.bot).items()}.get(short)
        if not row or row['dead']:
            continue
        seen += 1
        if row['target_puppet'] and 'Attack' in row['task']:
            turned.append(row['task'])
    start = host.bot.send('nearnpcs', 20)
    report['host_near_end'] = host.bot.line('NEARX end', 10, 'nearnpcs', start)
    report.update(samples=seen, turned=turned)
    assert ' attitude=6 ' in report['flee'], f"the fleeing NPC does not judge the puppet as Panic: {report['flee']}"
    assert seen and not turned, f'the fleeing NPC turned on the puppet ({turned}) or was not seen ({seen})'


POTION_CANDIDATES = ['It_Potion_Health'] + [f'It_Potion_Health_{i:02d}' for i in range(1, 7)] + \
    [f'It_Potion_Health_{s}' for s in ('Small', 'Medium', 'Large', 'Big', 'Elixir', 'Elexier')]


def owned_items(bot, radius=6000):
    """QA owned: world items near the hero whose AI zone has an owner or a security level (theft counts there)."""
    start = bot.send(f'owned {radius}', 20)
    bot.line('OWNED end', 15, 'owned', start)
    rows = []
    for l in bot.log()[start:].splitlines():
        if l.startswith('OWNED guid='):
            rows.append(dict(re.findall(r'(\w+)=(\S+)', l)))
    return rows


def theftscan(copies, report, negative=False):
    """Diagnostic for A1 (theft): after the raid, which items near Ardea count as someone's property, and what their
    zone owners and the nearby NPCs remember (QA crime). One player."""
    host = copies['host'].launch()
    enter_world(host)
    clear_raid(host, report)
    time.sleep(3)
    report['owned'] = owned_items(host.bot)
    owners = sorted({r['zone_owner'] for r in report['owned'] if r.get('zone_owner', '-') != '-'})
    report['owners'] = owners
    report['crime'] = {name: report_line(host.bot, 'crime '+name, ('CRIME ', 'REFUSED crime')) for name in owners[:12]}
    me = hero_pos(host.bot)
    report['hero_pos'] = me
    near = sorted(((g, r) for g, r in npcs(host.bot).items() if not r['dead']), key=lambda x: distance(x[1]['pos'], me))[:6]
    report['near_crime'] = [report_line(host.bot, 'crime '+r['name'], ('CRIME ', 'REFUSED crime')) for _, r in near]
    # Where the property lies: go to one member of each larger enclave and look for owned items around it.
    start = host.bot.send('enclaves', 20)
    host.bot.line('ENCLAVES end', 15, 'enclaves', start)
    rows = [dict(re.findall(r'(\w+)=(\S+)', l)) for l in host.bot.log()[start:].splitlines() if l.startswith('ENCLAVE ')]
    report['enclaves'] = rows
    picks = [r for r in rows if r.get('first', '-') != '-' and int(r.get('members', 0)) >= 5][:int(os.environ.get('G3_THEFT_MAX', '8'))]
    report['towns'] = {}
    for r in picks:
        moved = teleport_to(host.bot, r['first'])
        time.sleep(8)
        found = owned_items(host.bot, 4000)
        report['towns'][r['enclave']] = {'member': r['first'], 'teleport': moved, 'owned': len(found), 'sample': found[:6],
                                         'crime': report_line(host.bot, 'crime '+r['first'], ('CRIME ', 'REFUSED crime'))}
    report['status_note'] = 'diagnostic only'



def crime_of(bot, name):
    """QA crime as a dict (last_crime, reason, target, task, enclave, crimes ...); {} when refused."""
    line = report_line(bot, 'crime '+name, ('CRIME ', 'REFUSED crime'))
    return dict(re.findall(r'(\w+)=(\S+)', line)) if line.startswith('CRIME ') else {}


def enclave_counts(bot, name):
    """Crime counters of one enclave (QA enclaves) as a list of ints, crime index 3 = theft, 4 = murder."""
    start = bot.send('enclaves', 20)
    bot.line('ENCLAVES end', 15, 'enclaves', start)
    for l in bot.log()[start:].splitlines():
        if l.startswith('ENCLAVE ') and f' enclave={name} ' in l:
            return [int(v) for v in re.search(r' crimes=(\S+)', l)[1].split(',')]
    return None


def theftdiag(copies, report, negative=False, check=False):
    """Diagnostic for A1 (re/findings/social-coop-auftraege-c.md): after the raid a player takes an item that belongs to
    Ardea (QA owned: indoor owner zone) the game's way (itempick: PS_Normal_Take) with weapons put away, the other
    player 3 m off. On both machines: Ardea's crime counters (theft = index 3) and what the nearest Ardea NPCs do and
    whom they target, before and for 10 s after. G3_WHO=host|client|both (who takes)."""
    host, client = join_both(copies)
    if negative:
        for c in (host, client):
            assert 'ok=1' in report_line(c.bot, 'enclavesyncoff', 'ENCLAVESYNCOFF ')
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    time.sleep(2)
    report['ardea_before'] = {c.tag: enclave_counts(c.bot, 'Ardea') for c in (host, client)}
    items = [r for r in owned_items(host.bot, 4000) if r.get('zone_owner') == 'Ardea' and 'Chest' not in r['name']]
    report['owned'] = len(items)
    assert items, 'no item of Ardea near the host'
    item = min(items, key=lambda r: float(r['distance_cm']))
    report['item'] = item
    at = tuple(float(v) for v in item['pos'].split('_'))
    who = os.environ.get('G3_WHO', 'host')
    actors = [host] if who == 'host' else [client] if who == 'client' else [host, client]
    for i, c in enumerate((host, client)):
        spot = (at[0] + (120 if c in actors else 300), at[1] + 60, at[2] + 80 * i)
        report[c.tag+'_moved'] = move_hero(c.bot, spot)
        report[c.tag+'_unarm'] = report_line(c.bot, 'unarm', ('UNARM ', 'REFUSED unarm'))
    time.sleep(4)
    # Witnesses: the living NPCs of Ardea nearest to the item (the host's view).
    rows = sorted(((g, r) for g, r in npcs(host.bot).items() if not r['dead']), key=lambda x: distance(x[1]['pos'], at))[:10]
    witnesses = [r['name'] for _, r in rows if crime_of(host.bot, r['name']).get('enclave') == 'Ardea'][:4]
    report['witnesses'] = witnesses
    report['before'] = {c.tag: {w: crime_of(c.bot, w) for w in witnesses} for c in (host, client)}
    report['pick'] = {c.tag: report_line(c.bot, 'itempick '+item['guid'], ('ITEMPICK ', 'REFUSED itempick')) for c in actors}
    samples = []
    for _ in range(10):
        time.sleep(1)
        samples.append({c.tag: {w: {k: v for k, v in crime_of(c.bot, w).items() if k in ('task', 'target', 'reason', 'last_crime')}
                                for w in witnesses} for c in (host, client)})
    report['samples'] = samples
    report['after'] = {c.tag: {w: crime_of(c.bot, w) for w in witnesses} for c in (host, client)}
    report['attitude_after'] = {c.tag: {w: report_line(c.bot, 'attitude ' + w, ('ATTITUDE ', 'REFUSED attitude'))[9:]
                                        for w in witnesses} for c in (host, client)}
    report['took'] = {c.tag: report_line(c.bot, 'itemtook', 'ITEMTOOK ') for c in (host, client)}
    report['item_after'] = {c.tag: report_line(c.bot, 'iteminfo '+item['guid'], ('ITEMINFO ', 'REFUSED iteminfo')) for c in (host, client)}
    first = witnesses[0] if witnesses else None
    report['theft_count'] = {c.tag: [report['before'][c.tag].get(first, {}).get('crimes'), report['after'][c.tag].get(first, {}).get('crimes')]
                             for c in (host, client)} if first else None
    time.sleep(5)  # a client holds a theft back up to 3 s until its take is settled
    report['ardea_after'] = {c.tag: enclave_counts(c.bot, 'Ardea') for c in (host, client)}
    report['enclavesync'] = {c.tag: report_line(c.bot, 'enclavesync', 'ENCLAVESYNC ') for c in (host, client)}
    if not check:
        report['status_note'] = 'diagnostic only'
        return
    # Stage R1 (enclaves.cpp, reputation = shared): both machines keep the host's counters of Ardea, and the theft
    # counts exactly once, whoever took the item (with both reaching for it: only the one who got it).
    before, after = report['ardea_before']['host'], report['ardea_after']
    assert before and after['host'], 'Ardea not found'
    same = after['host'] == after['client']
    report['same'] = same
    report['theft_added'] = after['host'][3] - before[3]
    if negative:
        assert not same, f'Ardea is the same on both although reputation sync is off: {after}'
        return
    assert same, f"Ardea differs between the machines: {after}"
    assert report['theft_added'] == 1, f"the theft counted {report['theft_added']} times on the host: {before} -> {after['host']}"


def wait_mod_log(copy, pattern, seconds, what, since=0):
    """Until a line of the mod's log after character `since` matches `pattern`; returns the line."""
    def found():
        return next((l for l in copy.mod_log()[since:].splitlines() if re.search(pattern, l)), '')
    return wait_for(found, seconds, what)


def hero_pos(bot):
    return tuple(map(float, re.search(r'pos=([-\d.]+),([-\d.]+),([-\d.]+)', report_line(bot, 'heropos', 'HEROPOS ')).groups()))


def hero_zone(bot):
    return re.search(r' zone=(\S+)', report_line(bot, 'heropos', 'HEROPOS '))[1]


def clear_slot(copies, slot):
    """Remove a save slot and every copy of it (G3T_host_<slot>, hero records) from both test copies."""
    for tag in ('host', 'client'):
        folder = copies[tag].root/'isolated-profile/Documents/gothic3'
        for old in list(folder.glob(slot+'.*')) + list(folder.glob('*'+slot+'.*')) + list(folder.glob('g3t-hero-*'+slot+'.txt')):
            old.unlink(missing_ok=True)


def nearest_item(bot):
    found, _ = world_items(bot)
    guid = next(g for g, v in sorted(found.items(), key=lambda kv: kv[1]['distance']) if not v['name'].startswith('It_Gold'))
    return guid, found[guid]['name']


def check_in_saved_world(host, client, report, slot, saved_pos, since):
    """Both after a load of `slot` by the host: the client loaded the host's copy of it, stands where the save put the
    hero and both see each other again."""
    report['client_load_line'] = wait_mod_log(client, r'persist: loading G3T_host_'+slot+r'\b', 120,
                                              "the client loads the host's save", since)
    wait_running(client, 180, "client world running after the host's load")
    time.sleep(8)
    report['client_persist'] = report_line(client.bot, 'persist', 'PERSIST ')
    report['client_pos_after'] = client_pos = hero_pos(client.bot)
    report['client_from_saved_cm'] = round(distance(client_pos, saved_pos))
    assert report['client_from_saved_cm'] < 500, f"the client is not where the save put the hero: {client_pos}, save {saved_pos}"
    report['host_sees_client'] = wait_for(lambda: remotes(host.bot).get(2, {}).get('puppet'), 60, 'host shows the client again')
    report['client_sees_host'] = wait_for(lambda: remotes(client.bot).get(1, {}).get('puppet'), 60, 'client shows the host again')
    report['client_hud'] = [l for l in client.mod_log()[since:].splitlines() if 'hud:' in l][-4:]


def stageS1(copies, report, negative=False):
    """Stage S1, the host loads a save during the session (PLAN-2026-10-22 0.1, spieltest 21.10. 20:58): every connected
    player loads that save with the host and so is in the saved world again. Before, only the host went back and the
    client kept its own world. Checked on the client: an item the host took after saving is back, the hero stands
    where the save put it (not where the client had walked to), both show each other again. Negative control:
    worldsyncoff on the host (a load is not passed on)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'worldsyncoff', 'WORLDSYNCOFF ')
    clear_slot(copies, 'G3T_S1')
    item, name = nearest_item(host.bot)
    report['item'] = f'{item} {name}'
    assert item_exists(client.bot, item), 'the item is not in the client world to begin with'
    report['saved_pos'] = saved_pos = hero_pos(host.bot)
    start = host.bot.send('nativesave G3T_S1', 60)
    report['save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
    assert 'ok=1' in report['save'], report['save']
    time.sleep(3)
    report['host_pick'] = pick(host.bot, item)
    wait_for(lambda: not item_exists(host.bot, item), 15, 'the host took the item after saving')
    wait_for(lambda: not item_exists(client.bot, item), 15, 'the item is gone for the client too')
    away = (saved_pos[0] + 2500, saved_pos[1] + 200, saved_pos[2])
    report['client_move'] = move_hero(client.bot, away)
    time.sleep(3)
    report['client_pos_before'] = client_before = hero_pos(client.bot)
    assert distance(client_before, saved_pos) > 1500, f'the client did not walk away from the saved place: {client_before}'
    since = len(client.mod_log())
    start = host.bot.send('nativeload G3T_S1', 60)
    report['load'] = host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''), 120, 'nativeload', start)
    wait_running(host, 180, 'host world running after load')
    assert item_exists(host.bot, item), 'the item is not back in the host world after the load'
    check_in_saved_world(host, client, report, 'G3T_S1', saved_pos, since)
    report['client_item_back'] = item_exists(client.bot, item)
    assert report['client_item_back'], "the item the host took after saving is still missing in the client's world"


def stageS2(copies, report, negative=False):
    """Stage S2, a client in a new game joins a host that is still in the main menu and loads a save afterwards
    (PLAN-2026-10-22 0.2, spieltest 21.10. 21:04): the client must end up in the host's saved world, never in a second
    one. Before, the campaign went out only at the join and only if the host had one: the client stayed in its new
    game (it fought orcs the host did not have). The host first makes the save in a game of its own and restarts.
    Checked on the client: an item the host took before saving is gone, the hero stands where the save put it, both
    show each other. Negative control: worldsyncoff on the host after its restart."""
    host = copies['host'].launch('host', 'Host')
    enter_world(host)
    clear_slot(copies, 'G3T_S2')
    item, name = nearest_item(host.bot)
    report['item'] = f'{item} {name}'
    report['host_pick'] = pick(host.bot, item)
    wait_for(lambda: not item_exists(host.bot, item), 15, 'the host took the item')
    report['saved_pos'] = saved_pos = hero_pos(host.bot)
    start = host.bot.send('nativesave G3T_S2', 60)
    report['save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
    assert 'ok=1' in report['save'], report['save']
    report['host_first_closed'] = close(host)
    assert report['host_first_closed'], 'the host game did not close after saving'
    shutil.copy2(host.root/'g3together.log', host.run/'host-first-g3together.log')
    host = copies['host'].launch('host', 'Host')
    host.bot.line('STATE running=0 paused=1 save_allowed=0 world=G3_StartUp hero=none ', 60, 'host in the startup menu', 0)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'worldsyncoff', 'WORLDSYNCOFF ')
    start = host.bot.send('releaselock')
    host.bot.line('LOCK scan complete', 10, 'lock scan', start)
    client = copies['client'].launch('client', 'Client')
    enter_world(client, startup=120)
    report['client_welcomed'] = wait_mod_log(client, r'net: welcomed as player', 60, 'the client joins the host in its menu')
    assert item_exists(client.bot, item), "the item is not in the client's new game to begin with"
    time.sleep(5)
    since = len(client.mod_log())
    start = host.bot.send('nativeload G3T_S2', 60)
    report['load'] = host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''), 120, 'nativeload', start)
    assert 'ok=1' in report['load'], report['load']
    wait_running(host, 180, 'host world running after load')
    check_in_saved_world(host, client, report, 'G3T_S2', saved_pos, since)
    report['client_item_exists'] = item_exists(client.bot, item)
    assert not report['client_item_exists'], "the item the host took before saving is still in the client's world: two worlds"


def stageU1(copies, report, negative=False):
    """A unique item across a rejoin and a load (owner 22.10., after re/findings/myrtana-accord-c.md): the host puts an
    item nobody has into a corpse; the client takes it; the host saves; the client quits and joins again (it is loaded
    into the host's save); then the host loads the save once more. After each step the item exists exactly once: in the
    client hero's inventory, not in the corpse on either machine, not with the host. Negative control: containersyncoff
    on the client before the take (the corpse keeps its copy on the host: two of them)."""
    item, slot = 'It_Spell_TimeBubble', 'G3T_U1'
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(5)
    clear_slot(copies, slot)
    found = containers(host.bot)
    corpse = next((g for g, c in found.items() if c['kind'] == 'corpse'), None)
    assert corpse, 'no corpse near the host'
    report['corpse'] = found[corpse]['name']

    def where(client_copy):
        return {'host_corpse': contents(host.bot, corpse).get(item, 0), 'host_hero': carried(host.bot, item),
                'client_hero': carried(client_copy.bot, item), 'client_corpse': contents(client_copy.bot, corpse).get(item, 0)}

    def exactly_once(client_copy, when):
        """One more of the item than at the start, and that one with the client's hero."""
        counts = where(client_copy)
        added = {k: counts[k] - base[k] for k in counts}
        report[when] = {'counts': counts, 'added': added}
        assert added['host_corpse'] + added['host_hero'] + added['client_hero'] == 1 and added['client_hero'] == 1             and added['client_corpse'] == 0, f'{when}: {added}'

    base = report['start'] = where(client)  # the host's hero may carry one from the start (seen with this spell)
    report['stock'] = report_line(host.bot, f'stock {corpse}_{item}', 'STOCK ')
    wait_for(lambda: contents(client.bot, corpse).get(item, 0) == 1, 20, 'the client sees the item in the corpse')
    if negative:
        report_line(client.bot, 'containersyncoff', 'CONTAINERSYNCOFF ')
    report['open'] = report_line(client.bot, 'lootopen '+corpse, 'LOOTOPEN ')  # the player's way: open, then take
    report['take'] = loottake(client.bot, corpse, item)
    wait_for(lambda: carried(client.bot, item) == base['client_hero'] + 1, 10, 'the client carries the item')
    time.sleep(5)  # the transaction reaches the host
    exactly_once(client, 'after_take')

    start = host.bot.send(f'nativesave {slot}', 60)
    report['save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
    assert 'ok=1' in report['save'], report['save']
    # The client quits and joins again: it waits in the main menu and is loaded into the host's save.
    report['client_closed'] = close(client)
    shutil.copy2(client.root/'g3together.log', client.run/'client-first-g3together.log')
    time.sleep(5)
    client = copies['client'].launch('client', 'Client')
    report['rejoin_load'] = wait_mod_log(client, r'persist: loading G3T_host_'+slot+r'\b', 180, "the rejoining client loads the host's save")
    wait_running(client, 180, 'client world running after the rejoin')
    time.sleep(8)
    exactly_once(client, 'after_rejoin')

    since = len(client.mod_log())
    start = host.bot.send(f'nativeload {slot}', 60)
    report['load'] = host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''), 120,
                                   'nativeload', start)
    assert 'ok=1' in report['load'], report['load']
    wait_running(host, 180, 'host world running after the load')
    wait_mod_log(client, r'persist: loading G3T_host_'+slot+r'\b', 120, "the client loads the host's save again", since)
    wait_running(client, 180, 'client world running after the load')
    time.sleep(8)
    exactly_once(client, 'after_load')


def stageBigTrade(copies, report, negative=False):
    """A trader with more than 48 different goods (Codex review R4): the host stocks 45 more kinds into Hamlar; the
    client must see every one. Before, contents of more than 48 entries were silently not sent. Negative control:
    containersyncoff on the client (it takes no contents)."""
    kinds = ['It_Spell_'+n for n in ('TimeBubble', 'Mist', 'DuskToDawn', 'FrostWave', 'Hailstorm', 'IceBomb', 'Sleep', 'Fear',
             'Charm', 'WordOfControl', 'TameAnimal', 'Telekinesis', 'HeatWave', 'Bloodlust', 'SoulTravel', 'DestroyEvil', 'Icelance',
             'AnimalForm', 'ArmyOfDarkness', 'BlessWeapon', 'CallAnimals', 'CureDisease', 'CurePoison', 'Disease', 'Fireball',
             'Firerain', 'Flamesword', 'HealOther', 'Healing', 'Light', 'Lightningbolt', 'Meteor', 'OpenLocks', 'Poison',
             'SummonDemon', 'SummonGoblin', 'SummonGolem', 'SummonLightning', 'SummonSkeleton')] +         ['It_Scroll_Fireball', 'It_Scroll_HealOther', 'It_Scroll_Light', 'It_Water', 'It_Bread', 'It_Booze']
    host, client = join_both(copies)
    if negative:
        report_line(client.bot, 'containersyncoff', 'CONTAINERSYNCOFF ')
    clear_raid(host, report)
    time.sleep(5)
    guid = next((g for g, n in npcs(host.bot).items() if n['name'] == 'Hamlar' and not n['dead']), None)
    assert guid, 'Hamlar is not near the start'
    wait_for(lambda: sum(goods(host.bot, guid).values()) > 0, 30, 'the host filled Hamlar')
    for kind in kinds:
        report_line(host.bot, f'stock {guid}_{kind}', ('STOCK ', 'REFUSED stock'))
    # The host sends a trader's goods while a client stands within 15 m (or trades): the client walks up to Hamlar.
    pos = npcs(host.bot)[guid]['pos']
    move_hero(client.bot, (pos[0] + 250, pos[1] + 50, pos[2]))
    host_goods = goods(host.bot, guid)
    report['host_kinds'] = len(host_goods)
    assert len(host_goods) > 48, f'only {len(host_goods)} kinds on the host'
    report['client_kinds'] = len(wait_for(lambda: (lambda c: [c] if c == goods(host.bot, guid) else None)(goods(client.bot, guid)), 30,
                                          f"the client shows all {len(host_goods)} kinds of Hamlar's goods")[0])
    report['host_sync'] = containersync(host.bot)


def stageReloadLoot(copies, report, negative=False):
    """A load while a client loots (Codex review R5): the host saves; the client opens a corpse and takes a stack; the
    host loads the save. Afterwards the corpse holds what the save holds on both machines, and stays so: the client
    must not report the reloaded difference as a deposit. Negative control: containersyncoff forget on the client
    (its container state survives the load, as before)."""
    slot = 'G3T_R5'
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(5)
    clear_slot(copies, slot)
    found = containers(host.bot)
    corpse = next((g for g, c in found.items() if c['kind'] == 'corpse' and c['items'] != '-'), None)
    assert corpse, 'no corpse with plunder near the host'
    report['corpse'] = found[corpse]['name']
    saved = report['saved'] = contents(host.bot, corpse)
    wait_for(lambda: contents(client.bot, corpse) == saved, 20, "the client shows the host's contents")
    start = host.bot.send(f'nativesave {slot}', 60)
    report['save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
    assert 'ok=1' in report['save'], report['save']
    if negative:
        report_line(client.bot, 'containersyncoff forget', 'CONTAINERSYNCOFF ')
    report['open'] = report_line(client.bot, 'lootopen '+corpse, 'LOOTOPEN ')
    report['take'] = loottake(client.bot, corpse)
    wait_for(lambda: contents(host.bot, corpse) != saved, 15, 'the take reaches the host')
    since = len(client.mod_log())
    start = host.bot.send(f'nativeload {slot}', 60)
    report['load'] = host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''), 120,
                                   'nativeload', start)
    assert 'ok=1' in report['load'], report['load']
    wait_running(host, 180, 'host world running after the load')
    wait_mod_log(client, r'persist: loading G3T_host_'+slot+r'\b', 120, "the client loads the host's save", since)
    wait_running(client, 180, 'client world running after the load')
    time.sleep(15)  # several polls: a stale copy would have been reported by now
    report['host_after'] = after = contents(host.bot, corpse)
    report['client_after'] = contents(client.bot, corpse)
    assert after == saved, f'the corpse on the host changed after the load: {after}, saved {saved}'
    assert report['client_after'] == saved, f"the client's corpse differs: {report['client_after']}"


def world_line(bot):
    line = report_line(bot, 'world', 'WORLD ')
    return line, dict(re.findall(r'(\w+)=(\S+)', line))


def stageS3(copies, report, negative=False):
    """Stage S3, never two worlds without a save (PLAN-2026-10-22 0.2): the host plays a new game it has not saved; the
    client loads a save of its own. The client is then not let into the host's world (no puppet on either side for 15 s,
    the client's messages stop at the gate) until the host saves; then it gets the host's save, loads it and is back.
    Negative control: worldsyncoff on the host (the client counts as in the host's world whatever it loaded)."""
    host, client = join_both(copies)
    clear_raid(host, report)  # alone in its own save, the client would be beaten down by the raid there
    if negative:
        assert 'ok=1' in report_line(host.bot, 'worldsyncoff', 'WORLDSYNCOFF ')
    clear_slot(copies, 'G3T_S3own')
    clear_slot(copies, 'G3T_S3')
    start = client.bot.send('nativesave G3T_S3own', 60)
    report['client_save'] = client.bot.line('NATIVESAVE ', 60, 'client nativesave', start)
    assert 'ok=1' in report['client_save'], report['client_save']
    since = len(client.mod_log())
    for _ in range(15):  # the game finishes writing a save after SaveGame returns: right away the load found it missing (exists=0)
        start = client.bot.send('nativeload G3T_S3own', 60)
        report['client_load'] = client.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''), 120, 'client nativeload', start)
        if 'exists=0' not in report['client_load']:
            break
        time.sleep(1)
    assert 'ok=1' in report['client_load'], report['client_load']
    wait_running(client, 180, 'client world running after its own load')
    time.sleep(15)
    report['client_world'], cw = world_line(client.bot)
    report['host_world'], _ = world_line(host.bot)
    report['host_puppet_for_client'] = bool(remotes(host.bot).get(2, {}).get('puppet'))
    report['client_puppet_for_host'] = bool(remotes(client.bot).get(1, {}).get('puppet'))
    assert not report['host_puppet_for_client'], 'the host shows the client although it plays a save of its own'
    assert not report['client_puppet_for_host'], 'the client shows the host although it plays a save of its own'
    assert cw.get('in_world') == '0', report['client_world']
    report['client_hud'] = [l for l in client.mod_log()[since:].splitlines() if 'hud:' in l][-3:]
    report['host_saved_pos'] = saved_pos = hero_pos(host.bot)
    since = len(client.mod_log())
    start = host.bot.send('nativesave G3T_S3', 60)
    report['host_save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
    assert 'ok=1' in report['host_save'], report['host_save']
    check_in_saved_world(host, client, report, 'G3T_S3', saved_pos, since)
    report['client_world_after'], cw = world_line(client.bot)
    assert cw.get('in_world') == '1', report['client_world_after']


def corpse_poses(bot):
    pattern = r'DEADPOSE mirror=(\S+) killed=(\d) running=(\d) height=(-?[\d.]+) ani=(-?\d+) motion=(\S+) slots=(\S*)'
    start = bot.send('deadpose', 20)
    summary = bot.line('DEADPOSE enabled', 10, 'deadpose', start)
    return summary, {m[0]: {'killed': m[1] == '1', 'running': m[2] == '1', 'height': float(m[3]), 'motion': m[5], 'slots': m[6]}
                     for m in re.findall(pattern, bot.log()[start:])}


def lies(pose):
    """Lying as deaddiag measures it: a lying motion (LieDead, or the knock-out's LieKnockDown) runs on slot 0, nothing on the
    locomotion slots 1-3."""
    return bool(pose) and '_Lie' in pose['motion'] and pose['running'] and ':run:' not in pose['slots']


def look_at_corpses(observer, victims, report, tag):
    """The observer looks at each dead victim (hero 4 m before it) and measures its pose; returns the names that stand."""
    after = npcs(observer.bot)
    for entry in victims:
        entry[tag + '_dead'] = after.get(entry['guid'], {}).get('dead')
        report_line(observer.bot, 'lookat ' + entry['name'], 'LOOKAT ')
        time.sleep(2)
        _, seen = corpse_poses(observer.bot)
        entry[tag + '_pose'] = seen.get(entry['name'])
        if not entry[tag + '_pose']:  # not laid down by the mod at all (a DLL before 22.10. lists only mirrors): what the body shows
            start = observer.bot.send('nearnpcs', 20)
            observer.bot.line('NEARX end', 10, 'nearnpcs', start)
            shows = {m[0]: (m[1], m[2] == '1') for m in re.findall(r'NEARX guid=\S+ name=(\S+) .*? motion=(\S+) state=\S* run0=(-?\d+)',
                                                                    observer.bot.log()[start:])}
            motion, running = shows.get(entry['name'], ('-', False))
            entry[tag + '_pose'] = {'motion': motion, 'running': running, 'slots': '', 'from': 'nearnpcs'}
        snap(observer, 'corpse-' + entry['name'], report)
    return [e['name'] for e in victims if e[tag + '_dead'] and not lies(e[tag + '_pose'])],            [e['name'] for e in victims if e[tag + '_dead']]


def kill_by_hits(bot, victims):
    for entry in victims:
        hits = []
        for _ in range(40):
            hit = hit_npc(bot, entry['guid'])
            hits.append(hit['line'][-60:])
            if hit.get('dead'):
                break
            time.sleep(0.3)
        entry['hits'], entry['last_hit'] = len(hits), hits[-1]


def pick_victims(rows, near, limit, kinds=('Orc_', 'Slave', 'Scavenger', 'Boar', 'Wolf')):
    """One victim per name, nearest first, but at least one of each kind found (orcs, humans, animals)."""
    names = {}
    for g in sorted(rows, key=lambda g: distance(rows[g]['pos'], near)):
        names.setdefault(rows[g]['name'], g)
    chosen = []
    for kind in kinds:
        chosen += [n for n in names if kind in n and n not in chosen][:2]
    chosen += [n for n in names if n not in chosen]
    return [{'name': n, 'guid': names[n], 'distance_m': round(distance(rows[names[n]]['pos'], near) / 100, 1)} for n in chosen[:limit]]


def stageK1(copies, report, negative=False):
    """Stage K1, corpses lie on the other machine long after the death (PLAN-2026-10-22 1.5; owner 22.10.: on the play evening
    far more corpses stood than the scavengers and Brenton). Part 1, at the game's start: the host kills up to eight living
    mirrored NPCs near the client (orcs, slaves, others; the game's damage path, hitnpc); the client looks at each corpse
    45 s later (G3_CORPSE_WAIT). Part 2, at Reddock (animals, people): the client kills NPCs of its own region more than
    60 m from the host and leaves; the host comes afterwards and applies those deaths from the world journal, then looks
    at each corpse 45 s later. Every corpse must lie (lying-dead motion running on slot 0, slots 1-3 still); the mod's
    "corpse X after 45 s" lines are reported. Negative control: deadposeoff on both."""
    host, client = join_both(copies)
    for copy in (host, client):
        report_line(copy.bot, 'arm '+WEAPON, 'ARM ')
    time.sleep(8)
    if negative:
        for copy in (host, client):
            assert 'ok=1' in report_line(copy.bot, 'deadposeoff', 'DEADPOSEOFF ')
    wait = int(os.environ.get('G3_CORPSE_WAIT', '45'))
    # Part 1: the host's NPCs, mirrored on the client.
    host_npcs, client_npcs = npcs(host.bot), npcs(client.bot)
    client_at = remotes(host.bot)[2]['pos']
    alive = {g: client_npcs[g] for g in set(host_npcs) & set(client_npcs)
             if client_npcs[g]['mirrored'] and not host_npcs[g]['dead'] and host_npcs[g]['hp'] > 0 and host_npcs[g]['name'] != 'PC_Hero'
             and distance(client_npcs[g]['pos'], client_at) < 3000}
    alive = {g: v for g, v in alive.items() if v['name'] not in ('Gorn', 'Diego', 'Milten', 'Lester')}  # companions do not die
    part1 = pick_victims(alive, client_at, int(os.environ.get('G3_VICTIMS', '8')))
    assert part1, 'no living mirrored NPC near the client'
    kill_by_hits(host.bot, part1)
    time.sleep(wait)
    standing1, dead1 = look_at_corpses(client, part1, report, 'client')
    report['part1'] = part1
    # Part 2: the client's own region at Reddock, the host far away; the host comes afterwards.
    place = os.environ.get('G3_FAR_PLACE', 'Brenton')  # Reddock: boars, scavengers and rebels round him on the play evening
    report['far_place'] = place
    report['far_teleport'] = teleport_to(client.bot, place)
    time.sleep(8)
    far_at = hero_pos(client.bot)
    report['host_from_far_m'] = round(distance(hero_pos(host.bot), far_at) / 100)
    assert report['host_from_far_m'] > 60, f"the host is only {report['host_from_far_m']} m away"
    own = {g: v for g, v in npcs(client.bot).items() if not v['mirrored'] and not v['dead'] and v['hp'] > 0 and v['name'] != 'PC_Hero'
           and distance(v['pos'], far_at) < 3000}
    part2 = pick_victims(own, far_at, int(os.environ.get('G3_FAR_VICTIMS', '5')), kinds=('Boar', 'Scavenger', 'Wolf', 'Reddock_Rebel', 'Reb', 'Orc'))
    assert part2, "no NPC of the client's own region near it"
    kill_by_hits(client.bot, part2)
    time.sleep(3)
    report['client_journal'] = [l for l in client.mod_log().splitlines() if 'journal:' in l and 'died here' in l][-6:]
    # Through the game's own teleport (Entity::Teleport): moving the hero 139 m with MoveTo left the host's processing
    # range behind, so it saw no NPC there at all (run 20261022-101805).
    report['client_leaves'] = teleport_to(client.bot, os.environ.get('G3_HOME_PLACE', 'Diego'))  # before the host comes, as with Brenton
    time.sleep(6)
    report['host_comes'] = teleport_to(host.bot, place)
    time.sleep(wait)
    standing2, dead2 = look_at_corpses(host, part2, report, 'host')
    report['part2'] = part2
    report['host_journal'] = [l for l in host.mod_log().splitlines() if 'journal: applied' in l][-6:]
    report['corpse_lines'] = {c.tag: [l for l in c.mod_log().splitlines() if 'npcs: corpse ' in l][-40:] for c in (host, client)}
    report['standing'] = {'client_part1': standing1, 'host_part2': standing2}
    report['dead'] = {'client_part1': dead1, 'host_part2': dead2}
    assert dead1 and dead2, f'victims did not die: part 1 {dead1}, part 2 {dead2}'
    if negative:
        assert standing1 or standing2, 'every corpse lies although deadposeoff'
    else:
        assert not standing1 and not standing2, f'corpses stand: {report["standing"]}'


# Names the game's Respawn lets come back (Script_Game 0xac3c0, re/out/hero1-scriptgame.c): 60 % or 40 %.
RESPAWN_ANIMALS = ('Alligator', 'Bloodfly', 'Boar', 'Deer', 'Lurker', 'SandLurker', 'Scavenger', 'Snapper', 'GreenSnapper', 'SwampLurker',
                   'Waran', 'Bison', 'LonghairBison', 'Goblin', 'Ripper', 'Lion', 'Lioness', 'Rhino', 'LonghairRhino', 'Sabertooth',
                   'Shadowbeast', 'IceWolf', 'Schakal', 'Warg', 'Wolf')


def respawndiag(copies, report, negative=False, stage=False):
    """P3 exploration (re/findings/hero-death.md 4): what happens when animals killed in the client's region come back?
    The client kills animals at Reddock (G3_FAR_PLACE), leaves, the host moves time on 6 days (G3_RESPAWN_DAYS; Respawn
    needs 120 game hours since the last fight), the client comes back: which victims stand again there, which on the
    host when it comes too; a victim that stands again is killed a second time: is that death reported?"""
    host, client = join_both(copies)
    report_line(client.bot, 'arm '+WEAPON, 'ARM ')
    if negative:
        for c in (host, client):
            report_line(c.bot, 'respawnoff', 'RESPAWNOFF ')
    time.sleep(6)
    place, home = os.environ.get('G3_FAR_PLACE', 'Brenton'), os.environ.get('G3_HOME_PLACE', 'Diego')
    report['far_teleport'] = teleport_to(client.bot, place)
    time.sleep(8)
    far_at = hero_pos(client.bot)
    report['host_from_far_m'] = round(distance(hero_pos(host.bot), far_at) / 100)
    own = {g: v for g, v in npcs(client.bot).items() if not v['mirrored'] and not v['dead'] and v['hp'] > 0
           and distance(v['pos'], far_at) < int(os.environ.get('G3_FAR_RADIUS', '6000')) and any(k in v['name'] for k in RESPAWN_ANIMALS + ('Boar',))}
    victims = [{'name': own[g]['name'], 'guid': g, 'distance_m': round(distance(own[g]['pos'], far_at) / 100, 1)}
               # The game lets only some names come back (Boar, Scavenger, Wolf ...; Reddock_Boar_01 never): those first.
               for g in sorted(own, key=lambda g: (own[g]['name'] not in RESPAWN_ANIMALS, distance(own[g]['pos'], far_at)))
               [:int(os.environ.get('G3_FAR_VICTIMS', '4'))]]
    assert victims, "no animal of the client's own region near it"
    for v in victims:
        report_line(client.bot, f"npchp {v['guid']}_10", ('STATUSOF ', 'NPCHP '))  # one hit of the game's damage path kills
    kill_by_hits(client.bot, victims)
    time.sleep(3)
    guids = [v['guid'] for v in victims]
    view = lambda bot: {g[:8]: report_line(bot, 'statusof '+g, 'STATUSOF ')[9:] for g in guids}
    report['victims'] = victims
    report['client_after_kill'] = view(client.bot)
    report['client_leaves'] = teleport_to(client.bot, home)
    time.sleep(6)
    report['host_clock_before'] = report_line(host.bot, 'clock', 'CLOCK ')
    report['host_days'] = report_line(host.bot, 'clockdays '+os.environ.get('G3_RESPAWN_DAYS', '6'), ('CLOCK ', 'REFUSED'))
    time.sleep(10)
    report['client_clock'] = report_line(client.bot, 'clock', 'CLOCK ')
    report['client_back'] = teleport_to(client.bot, place)
    samples = []
    for _ in range(8):
        time.sleep(5)
        samples.append(view(client.bot))
    report['client_back_samples'] = samples
    back = samples[-1]
    again = [v for v in victims if ' dead=0 ' in back.get(v['guid'][:8], '')]
    report['respawned_on_client'] = [v['name'] for v in again]
    if again:
        for v in again:
            v.pop('hits', None)
            report_line(client.bot, f"npchp {v['guid']}_10", ('STATUSOF ', 'NPCHP '))
        kill_by_hits(client.bot, again)
        time.sleep(3)
        report['client_after_second_kill'] = view(client.bot)
    report['client_journal'] = [l for l in client.mod_log().splitlines() if 'journal:' in l][-20:]
    report['client_leaves_again'] = teleport_to(client.bot, home)
    time.sleep(6)
    report['host_comes'] = teleport_to(host.bot, place)
    time.sleep(20)
    report['host_view'] = view(host.bot)
    report['host_journal'] = [l for l in host.mod_log().splitlines() if 'journal:' in l][-20:]
    report['client_respawnsync'] = report_line(client.bot, 'respawnsync', 'RESPAWNSYNC ')
    if not stage:
        report['status_note'] = 'diagnostic only'
        return
    # Both machines must show the same: killed again after standing up (the second death reached the host), or gone.
    gone = lambda line: re.search(r'pos=(-?\d+)', line) and int(re.search(r'pos=(-?\d+)', line)[1]) < -100000
    rolled = [v for v in victims if ' dead=0 ' in back.get(v['guid'][:8], '') or gone(back.get(v['guid'][:8], ''))]
    assert rolled, 'no victim came back or went on the client (no roll: too early or too close)'
    final = report['host_view']
    for v in rolled:
        line = final.get(v['guid'][:8], '')
        if gone(back[v['guid'][:8]]):
            assert gone(line), f"{v['name']} went on the client, the host still shows it: {line}"
        else:
            assert ' dead=1 ' in report['client_after_second_kill'][v['guid'][:8]], f"{v['name']} did not die a second time on the client"
            assert ' dead=1 ' in line and not gone(line), f"{v['name']} died twice on the client, the host shows: {line}"


def stageRespawn(copies, report, negative=False):
    """P3, animals coming back (re/findings/hero-death.md 4): the client kills animals of its own region at Reddock, the
    clock moves on 6 days, the client comes back and the game rolls for each victim (Respawn). The host must show what the
    client's roll gave: an animal standing again (and dead after the client kills it a second time: that death is
    reported again), or one taken out of the world. Negative control: respawnoff on both (each machine rolls for itself)."""
    respawndiag(copies, report, negative, stage=True)


def stageQ2(copies, report, negative=False):
    """Stage Q2, experience from handing in a quest goes to every player, gold only to the one who hands in (PLAN-2026-10-22
    2.1, owner's decision 21.10.; spieltest 21.10. 21:13: Brenton's reward reached only the client). As on the play evening:
    the client takes Brenton's boar quest in his dialog (BPANKRATZ31290 sets Brenton_BoarsThere, about 9 s in), kills the
    four boars in its own region (the quest succeeds on both machines), then hands in (BPANKRATZ31293: Give It_Gold 30,
    GiveXP 50). The host's hero then gains the same experience as the client's, and no gold. Negative control: dialogxpoff
    on the client."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(client.bot, 'dialogxpoff', 'DIALOGXPOFF ')
    npc, info_name, quest_id = 'Brenton', 'BPANKRATZ31293', 'Brenton_KillBoars'
    for copy in (host, client):  # an NPC talks to nobody with a weapon in hand (Brenton reacted to it and attacked)
        report_line(copy.bot, 'unarm', 'UNARM')
    teleport_to(client.bot, npc)
    time.sleep(6)
    report['talk_quest'] = report_line(client.bot, 'talk ' + npc, 'TALK ')
    report['take_quest'] = report_line(client.bot, 'talkinfo BPANKRATZ31290', 'TALKINFO ')
    report['knows'] = wait_for(lambda: (lambda e: 'known=1' in e and [e])(report_line(client.bot, 'event Brenton_BoarsThere', 'EVENT ')), 40,
                               'the dialog tells the client Brenton_BoarsThere')[0]
    time.sleep(2)
    report['end_talk'] = report_line(client.bot, 'talkinfo BPANKRATZ30', 'TALKINFO ')  # farewell
    start = client.bot.send('questcounters ' + quest_id, 20)
    report['counters'] = client.bot.line('QUESTCOUNTER name=', 15, 'questcounters', start)
    report['boars_killed'] = kill_quest_targets(client, report['counters'])
    for copy in (host, client):
        wait_for(lambda: quest(copy.bot, 'quest', quest_id) == SUCCESS and ['yes'], 40, f'the quest succeeds on the {copy.tag}')
    report_line(client.bot, 'unarm', 'UNARM')
    time.sleep(8)  # the quest's own experience has arrived on both
    report['host_xp_before'], report['client_xp_before'] = xp_of(host.bot), xp_of(client.bot)
    report['host_gold_before'], report['client_gold_before'] = carried(host.bot, 'It_Gold'), carried(client.bot, 'It_Gold')
    for _ in range(6):  # Brenton walks on after a dialog: next to him again before each try
        teleport_to(client.bot, npc)
        time.sleep(2)
        report['talk'] = report_line(client.bot, 'talk ' + npc, 'TALK ')
        if info_name in report['talk']:
            break
        time.sleep(3)
    assert info_name + ':xp' in report['talk'], f'{npc} does not offer the hand-in: ' + report['talk']
    since = len(client.mod_log())
    report['talkinfo'] = report_line(client.bot, 'talkinfo ' + info_name, 'TALKINFO ')
    assert 'ok=1' in report['talkinfo'], report['talkinfo']
    report['client_givexp'] = wait_mod_log(client, r'infocmd: GiveXP\|', 60, 'the hand-in runs GiveXP on the client', since)
    time.sleep(3)
    report['client_xp_after'] = xp_of(client.bot)
    gained = report['client_gained'] = report['client_xp_after'] - report['client_xp_before']
    assert gained > 0, 'the hand-in gave the client no experience'
    try:
        wait_for(lambda: xp_of(host.bot) - report['host_xp_before'] >= gained, 15, "the host's hero gains the same experience")
    except AssertionError:
        pass
    report['host_xp_after'] = xp_of(host.bot)
    report['host_gained'] = report['host_xp_after'] - report['host_xp_before']
    report['host_gold_after'], report['client_gold_after'] = carried(host.bot, 'It_Gold'), carried(client.bot, 'It_Gold')
    report['host_log'] = [l for l in host.mod_log().splitlines() if 'dialog experience' in l][-3:]
    report['infocmd_client'] = report_line(client.bot, 'infocmd', 'INFOCMD ')
    assert report['client_gold_after'] > report['client_gold_before'], 'the client got no gold from the hand-in'
    assert report['host_gold_after'] == report['host_gold_before'], f"the host got gold too: {report['host_gold_before']} -> {report['host_gold_after']}"
    assert report['host_gained'] == gained, f"host gained {report['host_gained']} experience, the client {gained}"


def stageA3(copies, report, negative=False):
    """Stage A3, a knock-out of the game's own takes the game's course (PLAN-2026-10-22 2.2; spieltest 21.10. 21:35: guards
    knocked a player out and the help-up rule took over, nobody robbed him). An NPC fights the host's hero in a duel
    (G3_DUELIST, default Diego; attack reason Duel, which the game ends with Defeat, a knock-out). The hero is not held:
    the log has no "waiting for a partner", the client is not told to help, and the hero stands up by itself within 45 s.
    Negative control: lethalonlyoff on the host (every knock-out waits for help, as before)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'lethalonlyoff', 'LETHALONLYOFF ')
    fighter = os.environ.get('G3_DUELIST', 'Hamlar')  # Diego, a companion, never struck the host's hero in 120 s
    teleport_to(host.bot, fighter)
    report_line(host.bot, 'lookat ' + fighter, 'LOOKAT ')  # 4 m before him, not on his spot
    time.sleep(4)
    report['sethp'] = report_line(host.bot, 'sethp 20', 'SETHP ')  # down within a few blows (full hit points: not in 120 s)
    report['arm'] = report_line(host.bot, 'arm ' + WEAPON, 'ARM ')  # unarmed, the duelist never struck (run 20261022-110427)
    report['gold_before'] = carried(host.bot, 'It_Gold')
    since_host, since_client = len(host.mod_log()), len(client.mod_log())
    report['duel'] = report_line(host.bot, 'duelhero ' + fighter, 'DUELHERO ')
    assert 'result=' in report['duel'], report['duel']
    # The duelist's own swings never landed in a bot run (hero standing still at 20 hit points for 120 s); his blows go
    # through the game's damage path instead (hurthero), judged with his attack reason Duel.
    duelist = next(g for g, v in npcs(host.bot).items() if v['name'] == fighter)
    report['blows'] = []
    for _ in range(30):
        report['blows'].append(report_line(host.bot, 'hurthero ' + duelist, 'HURTHERO ')[-90:])
        if ' down=1 ' in herostate(host.bot):
            break
        time.sleep(0.5)
    report['down'] = wait_for(lambda: (lambda s: ' down=1 ' in s and [s])(herostate(host.bot)), 10,
                              "the host's hero is knocked out in the duel")[0]
    report['downed_while_down'] = report_line(host.bot, 'downed', 'DOWNED ')
    host_lines = lambda: host.mod_log()[since_host:].splitlines()
    report['host_downed_log'] = [l for l in host_lines() if 'downed:' in l][-6:]
    assert not any('would die' in l for l in host_lines()), 'the duel ended in a death, not a knock-out: test setup'
    report['up'] = wait_for(lambda: (lambda s: ' down=0 ' in s and ' dead=0 ' in s and [s])(herostate(host.bot)), 45,
                            'the hero stands up by itself')[0]
    report['gold_after'] = carried(host.bot, 'It_Gold')
    report['host_downed_log'] = [l for l in host_lines() if 'downed:' in l][-6:]
    report['client_hud'] = [l for l in client.mod_log()[since_client:].splitlines() if 'hud:' in l][-4:]
    assert not any('waiting for a partner' in l for l in host_lines()), 'the knock-out was held for help'
    assert not any('liegt am Boden' in l for l in report['client_hud']), 'the client was told to help'


def arena_of(bot, name):
    line = report_line(bot, 'arena ' + name, 'ARENA ')
    return line, dict(re.findall(r'(\w+)=(\S+)', line))


def stageA4(copies, report, negative=False):
    """Stage A4, duels and arena bouts belong to their two fighters (PLAN-2026-10-22 2.3, owner's decision 22.10., variant b;
    spieltest 21.10.: a second player joining Reddock's arena bout counted as murder). Part 1: G3_DUELIST (default Diego)
    duels the host's hero; the client hits him ten times; none of those hits counts on the host (his hit points stay, the
    host logs them as not counted). Part 2: he duels the client instead (started on the host, as a dialog's duel is); the
    client hits him down; the host ends the bout with a knock-out, he is not dead and nobody's murder count rises.
    Negative control: arenaruleoff on the host (the client's hits count again; its last hit kills)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'arenaruleoff', 'ARENARULEOFF ')
    fighter = os.environ.get('G3_DUELIST', 'Diego')
    for copy in (host, client):
        report_line(copy.bot, 'arm ' + WEAPON, 'ARM ')
    teleport_to(host.bot, fighter)
    teleport_to(client.bot, fighter)
    time.sleep(5)
    guid = next(g for g, v in npcs(host.bot).items() if v['name'] == fighter)
    # Part 1: the bout is the host's.
    report['duel_host'] = report_line(host.bot, 'duelhero ' + fighter, 'DUELHERO ')
    report['bout1'] = wait_for(lambda: (lambda a: a[1].get('in_bout') == '1' and a[1].get('target_player') == '1' and [a[0]])(arena_of(host.bot, fighter)), 20,
                               f'{fighter} fights the host in a duel')[0]
    hp_before = int(arena_of(host.bot, fighter)[1]['hp'])
    for _ in range(10):
        hit_npc(client.bot, guid)
        time.sleep(0.3)
    time.sleep(2)
    report['after_client_hits'], a = arena_of(host.bot, fighter)
    report['hp_before'], report['hp_after'] = hp_before, int(a['hp'])
    if not negative:
        assert int(a['hp']) == hp_before, f"the client's hits on the host's duel partner counted: {hp_before} -> {a['hp']}"
        assert int(a['refused']) > 0, 'the host refused no hit: ' + report['after_client_hits']
    # The host's bout ends: the host walks away and the duelist calms down (or the next part takes him over).
    time.sleep(3)
    # Part 2: the bout is the client's.
    report['crime_before'] = report_line(host.bot, 'crime ' + fighter, 'CRIME ')
    report['duel_client'] = report_line(client.bot, 'duelhero ' + fighter, 'DUELHERO ')
    report['bout2'] = wait_for(lambda: (lambda a: a[1].get('in_bout') == '1' and a[1].get('target_player') == '2' and [a[0]])(arena_of(host.bot, fighter)),
                               20, f'{fighter} fights the client in a duel')[0]
    report['npchp'] = report_line(host.bot, f'npchp {fighter}_40', 'NPCHP ')  # Diego has 932: the client's 60 hits took 312
    report_line(client.bot, 'lookat ' + fighter, 'LOOKAT ')
    report['client_arm'] = report_line(client.bot, 'arm ' + WEAPON, 'ARM ')  # bare hands did no damage below 8 hit points
    for _ in range(60):
        report_line(client.bot, 'arm ' + WEAPON, 'ARM ')  # the duelist's blows knocked the sword out of the hand after two hits
        hit = hit_npc(client.bot, guid)
        line, a = arena_of(host.bot, fighter)
        if a.get('down') == '1' or a.get('dead') == '1':
            break
        # A hit on a stumbling or parrying duelist does no damage; hit again every 0.3 s and he never stood again
        # (run 20261022-224400: two hits counted, then 56 in ZS_QuickStumble/ZS_ParadeStumble at 7 hit points).
        time.sleep(1.5 if not hit.get('claim_sent') and 'Stumble' in hit['line'] else 0.3)
    time.sleep(3)
    report['end'], a = arena_of(host.bot, fighter)
    report['crime_after'] = report_line(host.bot, 'crime ' + fighter, 'CRIME ')
    report['host_combat_log'] = [l for l in host.mod_log().splitlines() if 'duel or arena' in l or 'OnCheatKill' in l][-6:]
    if negative:
        assert a.get('dead') == '1' or int(report['hp_after']) < hp_before, 'without the rule the client still could not hurt him'
    else:
        assert a.get('dead') == '0', f'{fighter} died in the duel: ' + report['end']
        assert int(a.get('knockouts', 0)) >= 1, 'the bout did not end with a knock-out: ' + report['end']


def xpinfodiag(copies, report, negative=False):
    """Diagnostic, host only (stage Q2 set-up): the conditions of Brenton's hand-in option BPANKRATZ31293 and of his quest
    option 31290, and which people at the game's start offer an option that gives experience right away."""
    host = copies['host'].launch()
    enter_world(host)
    clear_raid(host, report)
    start = host.bot.send('xpinfos', 60)
    host.bot.line('XPINFOS end', 60, 'xpinfos', start)
    rows = [l for l in host.bot.log()[start:].splitlines() if l.startswith('XPINFO name=')]
    report['xpinfos_near'] = sorted(rows, key=lambda l: float(re.search(r'distance_m=(-?[\d.]+)', l)[1]) % 1e9)[:40]
    report['xpinfos_count'] = len(rows)
    report['desc'] = {}
    for name in ('BPANKRATZ31293', 'BPANKRATZ31290', 'BPANKRATZ31289'):
        start = host.bot.send('infodesc ' + name, 20)
        host.bot.line('INFODESC end', 10, 'infodesc', start)
        report['desc'][name] = [l for l in host.bot.log()[start:].splitlines() if l.startswith('INFO')]
    report['offers'] = {}
    for npc in os.environ.get('G3_TALKERS', 'Diego,Gorn,Milten,Lester,Hamlar,Brenton').split(','):
        teleport_to(host.bot, npc)
        time.sleep(3)
        line = report_line(host.bot, 'talk ' + npc, 'TALK ')
        report['offers'][npc] = line
        offered = re.search(r' infos=(\S+)', line)
        if offered and offered[1] != '-':
            report_line(host.bot, 'talkinfo ' + offered[1].split(',')[-1].split(':')[0], 'TALKINFO ')  # the last one: usually farewell
        time.sleep(6)
    report['status_note'] = 'diagnostic only'


def kill_quest_targets(copy, counters):
    """Kill every living NPC named as a target in a QUESTCOUNTER line (four boars share the name Reddock_Boar_01), by GUID
    and the game's damage path, with a sword in hand. Returns the names killed."""
    targets = set(re.findall(r'([A-Za-z]\w*):-?\d+/', counters))
    killed = []
    for target in sorted(targets):
        teleport_to(copy.bot, target)  # Brenton's boars graze farther than 40 m from him
        time.sleep(4)
        report_line(copy.bot, 'arm ' + WEAPON, 'ARM ')
        for guid, row in npcs(copy.bot).items():
            if row['name'] not in targets or row['dead']:
                continue
            for _ in range(40):
                if hit_npc(copy.bot, guid).get('dead'):
                    killed.append(row['name'])
                    break
                time.sleep(0.2)
    return killed


def brentondiag(copies, report, negative=False):
    """Diagnostic, host only (stage Q2 set-up): Brenton's quest option 31290, the quest run and succeeded by the QA, then
    whether he offers the hand-in 31293 (condition Success of Brenton_KillBoars, knows Brenton_BoarsThere)."""
    host = copies['host'].launch()
    enter_world(host)
    clear_raid(host, report)
    report['unarm'] = report_line(host.bot, 'unarm', 'UNARM')  # clear_raid armed the hero: Brenton reacted to the weapon (ZS_ReactToWeapon, ZS_Attack)
    teleport_to(host.bot, 'Brenton')
    time.sleep(3)
    report['talk1'] = report_line(host.bot, 'talk Brenton', 'TALK ')
    report['take'] = report_line(host.bot, 'talkinfo BPANKRATZ31290', 'TALKINFO ')
    time.sleep(10)
    report['end'] = report_line(host.bot, 'talkinfo BPANKRATZ30', 'TALKINFO ')
    report['events'] = report_line(host.bot, 'talkstate', 'TALKSTATE ')
    report['knows_after_dialog'] = report_line(host.bot, 'event Brenton_BoarsThere', 'EVENT ')
    # Not run first: on the play evening the quest went from nothing straight to success when the boars died (21:12).
    # The game's own way to finish it: its targets die (a success set by the QA's succeedquest made no hand-in appear).
    start = host.bot.send('questcounters Brenton_KillBoars', 20)
    report['counters'] = host.bot.line('QUESTCOUNTER name=', 15, 'questcounters', start)
    report['boars_killed'] = kill_quest_targets(host, report['counters'])
    wait_for(lambda: quest(host.bot, 'quest', 'Brenton_KillBoars') == SUCCESS and ['yes'], 30, 'the quest succeeds by its kills')
    report_line(host.bot, 'unarm', 'UNARM')
    report['done'] = quest(host.bot, 'quest', 'Brenton_KillBoars')
    time.sleep(5)
    report['knows_before_talk'] = report_line(host.bot, 'event Brenton_BoarsThere', 'EVENT ')
    if 'known=0' in report['knows_before_talk']:
        report['knows_set'] = report_line(host.bot, 'setevent Brenton_BoarsThere', 'EVENT ')
    # The player's own talk action (as stage 4c addresses Diego), not StartInfoManager: a second StartInfoManager never started.
    teleport_to(host.bot, 'Brenton')
    time.sleep(2)
    report['approach'] = report_line(host.bot, 'approach Brenton', 'APPROACH')
    time.sleep(3)
    report['assesstalk'] = report_line(host.bot, 'assesstalk Brenton', 'ASSESSTALK ')
    time.sleep(3)
    report['talkstate2'] = report_line(host.bot, 'talkstate', 'TALKSTATE ')
    report['talk2_0'] = report_line(host.bot, 'talk Brenton', 'TALK ')
    start = host.bot.send('infodesc BPANKRATZ31293', 20)
    host.bot.line('INFODESC end', 10, 'infodesc', start)
    report['desc_after'] = [l for l in host.bot.log()[start:].splitlines() if l.startswith('INFODESC name')]
    report['status_note'] = 'diagnostic only'


def released_line(copy, since, what):
    line = wait_mod_log(copy, r'remote: \d+ NPCs let go of removed puppet 2', 20, what, since)
    return line, int(re.search(r'remote: (\d+) NPCs let go', line)[1])


def stageF1(copies, report, negative=False):
    """Stage F1, a puppet that goes is let go by every NPC that fought it, without walking Entity::GetNPCs (that walk met a
    freed NPC still in its per-frame list while the host saved, run 20261022-080317; re/findings/save-forget-npclist-c.md).
    During the opening raid the host's orcs fight the client's puppet. The host saves then (the save removes puppets first):
    at least every NPC that held the puppet as its target lets go of it, from the contacts noted while it was alive, and the
    save works. The client leaves: the NPCs let go again, and the host keeps running. Negative control: contactsoff on the
    host (nothing is noted, so nobody lets go)."""
    host, client = join_both(copies)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'contactsoff', 'CONTACTSOFF ')
    fighting = lambda: [r['name'] for r in near_rows(host.bot).values() if r['target_puppet'] == 2 and not r['dead']]
    # The opening raid's orcs often fight only the villagers and companions (runs 20261022-221558, -231510: none took the
    # puppet in 90 s); then two of them are set on it the way a dialog's Attack does (aggro).
    try:
        report['fighting_before_save'] = wait_for(fighting, 30, "host NPCs fight the client's puppet")
    except AssertionError:
        orcs = [r['name'] for r in near_rows(host.bot).values() if r['name'].startswith('Orc_') and not r['dead'] and not r['mirrored']][:2]
        report['aggro'] = [report_line(host.bot, f'aggro {name}_12', ('AGGRO ', 'REFUSED aggro')) for name in orcs]
        report['fighting_before_save'] = wait_for(fighting, 60, "host NPCs fight the client's puppet")
    clear_slot(copies, 'G3T_F1')
    since = len(host.mod_log())
    start = host.bot.send('nativesave G3T_F1', 60)
    report['save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
    assert 'ok=1' in report['save'], report['save']
    report['save_forget'], released = released_line(host, since, 'the save made the NPCs let go of the puppet')
    report['released_at_save'] = released
    assert released >= len(report['fighting_before_save']), \
        f"{released} NPCs let go at the save, {len(report['fighting_before_save'])} held the puppet: {report['fighting_before_save']}"
    report['puppet_back'] = wait_for(lambda: remotes(host.bot).get(2, {}).get('puppet'), 30, 'the puppet is back after the save')
    report['fighting_before_leave'] = fighting()
    since = len(host.mod_log())
    report['client_closed'] = close(client)
    report['leave_forget'], report['released_at_leave'] = released_line(host, since, 'the NPCs let go of the leaving puppet')
    frames = lambda: int(re.findall(r'frame=(\d+)', host.mod_log())[-1])
    before = frames()
    time.sleep(15)
    report['host_frames_after_leave'] = frames() - before
    assert report['host_frames_after_leave'] > 300, f"the host stopped after the client left: {report['host_frames_after_leave']} frames in 15 s"


def stageFormatTrunc(copies, report, negative=False):
    """A line too long for its buffer is cut off instead of ending the game (review 26.10.): sprintf_s into a fixed buffer
    ends the process with 0xC0000409 and no log line when the text does not fit (QA findnpc, stageEscortFar 26.10.;
    TALKSYNC, HITNPC and others close to their size). Every formatted line of the mod goes through Format now. On both
    machines a 300-character line into a 64-byte buffer (QA formatprobe) comes back cut to 63 characters, and the game
    answers the next command. Negative control: formatstrict on the host (sprintf_s as before): the host's game ends."""
    host, client = join_both(copies)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'formatstrict', 'FORMATSTRICT ')
    for c in (client, host):
        report[c.tag+'_short'] = short = report_line(c.bot, 'formatprobe 20', 'FORMATPROBE ')
        assert 'written=32 ' in short, short
        report[c.tag+'_long'] = long = report_line(c.bot, 'formatprobe 300', 'FORMATPROBE ')
        assert 'written=63 ' in long, long
        report[c.tag+'_after'] = report_line(c.bot, 'heropos', 'HEROPOS ')
        assert 'pos=' in report[c.tag+'_after'], report[c.tag+'_after']


def stageW1(copies, report, negative=False):
    """Stage W1, a player who leaves and joins again within one frame of the host's world bookkeeping is told the host's
    world again (Codex review 22.10. R7): the host's state per player (world told, save sent) started over only when the
    bookkeeping saw the slot empty, and a new connection in the same slot was never told the world; it waited outside.
    The host's bookkeeping pauses for 5 s (persistpause) while the client reconnects, so that leaving and joining fall
    into one of its frames. Negative control: generationoff on the host (a slot starts over only once it is empty)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'generationoff', 'GENERATIONOFF ')
    since_client, since_host = len(client.mod_log()), len(host.mod_log())
    report['pause'] = report_line(host.bot, 'persistpause 5000', 'PERSISTPAUSE ')
    assert 'ok=1' in report['pause'], report['pause']
    report['reconnect'] = report_line(client.bot, 'reconnect', 'RECONNECT ')
    assert 'ok=1' in report['reconnect'], report['reconnect']
    report['rejoined'] = wait_mod_log(host, r'net: Client joined as player', 15, 'the client joins again', since_host)
    report['client_in_world'] = wait_mod_log(client, r"persist: in the host's world", 30, "the client is in the host's world again",
                                             since_client)
    report['new_connection'] = wait_mod_log(host, r'persist: player \d+ is a new connection', 5, 'the host starts the new connection over',
                                            since_host)
    wait_for(lambda: remotes(host.bot).get(2, {}).get('puppet') and remotes(client.bot).get(1, {}).get('puppet'), 30,
             'both see each other again')


def stageW2(copies, report, negative=False):
    """Stage W2, a player's hero record is replaced only once it is written completely, and a record that broke off is
    refused before it changes the hero (Codex review 22.10. R1: one with only its first line wiped the inventory and set
    level and experience to zero). On the client: a record is written (no .tmp left, its end line names its stacks); a
    write that breaks off (recordwritefault) leaves the record before it; records cut in the middle of their stacks and
    after their first line are refused and the hero stays as it was; the complete record loads, and the hero is the same.
    Negative control: recordcheckoff on the client (records are applied as read)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    folder = client.root/'isolated-profile/Documents/gothic3'
    record = folder/'g3t-hero-G3TW2.txt'
    record.unlink(missing_ok=True)
    if negative:
        assert 'ok=1' in report_line(client.bot, 'recordcheckoff', 'RECORDCHECKOFF ')
    time.sleep(3)

    def dump():
        return report_line(client.bot, 'herodump', 'HERODUMP ')

    report['before'] = before = dump()
    report['save'] = report_line(client.bot, 'herosave G3TW2', 'HEROSAVE ')
    assert 'ok=1' in report['save'], report['save']
    text = record.read_text()
    lines = text.splitlines()
    stacks = sum(1 for line in lines if line.startswith('stack '))
    report['record'] = {'lines': len(lines), 'stacks': stacks, 'first': lines[0], 'last': lines[-1]}
    assert lines[0] == 'G3THERO 2' and lines[-1] == f'end {stacks}' and stacks > 1, report['record']
    assert not list(folder.glob('*.tmp')), 'a .tmp file was left after the write'
    report['fault'] = report_line(client.bot, 'recordwritefault', 'RECORDWRITEFAULT ')
    report['save_fault'] = report_line(client.bot, 'herosave G3TW2', 'HEROSAVE ')
    assert 'ok=0' in report['save_fault'], report['save_fault']
    assert record.read_text() == text, 'the record changed although its write broke off'
    assert not list(folder.glob('*.tmp')), 'a .tmp file was left after the broken write'
    for kind in ('middle', 'header'):
        record.write_text(text)
        report['cut_'+kind] = cut = report_line(client.bot, 'herocut G3TW2' + ('_header' if kind == 'header' else ''), 'HEROCUT ')
        assert 'ok=1' in cut, cut
        report['load_cut_'+kind] = load = report_line(client.bot, 'heroload G3TW2', 'HEROLOAD ')
        report['after_cut_'+kind] = after = dump()
        assert after == before, f'the hero changed when a record cut ({kind}) was loaded: {before} -> {after}'
        assert 'ok=0' in load, load
    record.write_text(text)
    report['load_whole'] = load = report_line(client.bot, 'heroload G3TW2', 'HEROLOAD ')
    assert 'ok=1' in load, load
    report['after_whole'] = after = dump()
    assert after == before, f'the hero is not the same after loading its whole record: {before} -> {after}'


def stageW3(copies, report, negative=False):
    """Stage W3, while a client loads, its world gate is closed from the start of the load until its own hero is back
    (Codex review 22.10. R6): during a load Evaluate returned before it closed the gate, so the host's messages came through
    the world change, and the gate opened ~3 s before the player's hero record was applied. The host saves (the client
    writes its hero record for that save) and loads the save: the client is loaded into it and its record is applied
    before it is in the host's world. Then the client loads that save itself while in the host's world: the gate closes as
    the load begins. Negative control: worldgatelate on the client."""
    host, client = join_both(copies)
    clear_raid(host, report)
    slot = 'G3T_W3'
    clear_slot(copies, slot)
    if negative:
        assert 'ok=1' in report_line(client.bot, 'worldgatelate', 'WORLDGATELATE ')
    since = len(client.mod_log())
    start = host.bot.send(f'nativesave {slot}', 60)
    report['save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
    assert 'ok=1' in report['save'], report['save']
    report['record_written'] = wait_mod_log(client, r'persist: the host saved '+slot+r'; own hero record written', 20,
                                            'the client writes its hero record for the save', since)
    time.sleep(3)
    since = len(client.mod_log())
    start = host.bot.send(f'nativeload {slot}', 60)
    report['load'] = host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''), 120,
                                   'nativeload', start)
    assert 'ok=1' in report['load'], report['load']
    wait_running(host, 180, 'host world running after the load')
    wait_mod_log(client, r'persist: loading G3T_host_'+slot+r'\b', 120, "the client is loaded into the host's save", since)
    wait_running(client, 180, 'client world running after the load')
    report['applied'] = wait_mod_log(client, r'persist: own hero record .*G3T_host_'+slot+r'\.txt applied: 1', 30,
                                     "the client's own hero record is applied", since)
    report['in_world'] = wait_mod_log(client, r"persist: in the host's world", 30, "the client is in the host's world", since)
    lines = client.mod_log()[since:].splitlines()
    first = lambda pattern: next((i for i, l in enumerate(lines) if re.search(pattern, l)), None)
    report['host_load_order'] = order = {'load_returned': first(r"persist: the game's load of G3T_host_.* returned"),
                                         'record_applied': first(r'own hero record .* applied: 1'), 'in_world': first(r"persist: in the host's world")}
    assert order['record_applied'] < order['in_world'], f"the gate opened before the client's hero record was applied: {order}"
    # The game's load runs frames of its own, in which the mod ran on (C 22.10.): the gate opens only after it returned.
    assert order['load_returned'] is not None and order['load_returned'] < order['in_world'], f'the gate opened inside the load: {order}'
    time.sleep(5)
    since = len(client.mod_log())
    start = client.bot.send(f'nativeload G3T_host_{slot}', 60)
    report['client_load'] = client.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''),
                                            120, 'client nativeload', start)
    assert 'ok=1' in report['client_load'], report['client_load']
    wait_running(client, 180, 'client world running after its own load')
    report['in_world_again'] = wait_mod_log(client, r"persist: in the host's world", 40, "the client is in the host's world again", since)
    lines = client.mod_log()[since:].splitlines()
    report['own_load_order'] = order = {'gate_closed': first(r"net: this player is out of the host's world"),
                                        'loading': first(r'persist: loading G3T_host_'), 'load_returned': first(r"persist: the game's load of G3T_host_.* returned"),
                                        'record_applied': first(r'own hero record .* applied: 1'), 'in_world': first(r"persist: in the host's world")}
    assert order['gate_closed'] is not None and order['gate_closed'] < order['loading'], f'the gate stayed open into the load: {order}'
    assert order['record_applied'] < order['in_world'], f"the gate opened before the client's hero record was applied: {order}"
    assert order['load_returned'] is not None and order['load_returned'] < order['in_world'], f'the gate opened inside the load: {order}'
    assert order['load_returned'] < order['record_applied'], f'the hero record was applied inside the load: {order}'


def stageW4(copies, report, negative=False):
    """Stage W4, a story event the host cleared after its save is cleared for a player who joins later (Codex review
    22.10. R8): the full state for a joining client held only values other than 0, and the client had the event set from
    the older save it was loaded into. The client leaves; the host sets an event, saves and clears the event; the client
    joins again and is loaded into that save; after the full state the event is cleared on the client too. Negative
    control: storyzerosoff on the host."""
    host, client = join_both(copies)
    clear_raid(host, report)
    slot, event = 'G3T_W4', 'G3T_W4_Event'
    clear_slot(copies, slot)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'storyzerosoff', 'STORYZEROSOFF ')
    report['client_closed'] = close(client)
    shutil.copy2(client.root/'g3together.log', client.run/'client-first-g3together.log')
    report['set'] = report_line(host.bot, f'setevent {event}', 'EVENT ')
    assert 'known=1' in report['set'], report['set']
    time.sleep(2)  # the host's story poll (500 ms) sees it set
    start = host.bot.send(f'nativesave {slot}', 60)
    report['save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
    assert 'ok=1' in report['save'], report['save']
    time.sleep(3)
    report['clear'] = report_line(host.bot, f'clearevent {event}', 'EVENT ')
    assert 'known=0' in report['clear'], report['clear']
    time.sleep(2)
    since_host = len(host.mod_log())
    client = copies['client'].launch('client', 'Client')
    report['join_load'] = wait_mod_log(client, r'persist: loading G3T_host_'+slot+r'\b', 180, "the joining client loads the host's save")
    wait_running(client, 180, 'client world running after the join')
    report['full_state'] = wait_mod_log(host, r'story: full events state', 60, 'the host sends the full event state', since_host)
    time.sleep(5)
    report['client_event'] = report_line(client.bot, f'event {event}', 'EVENT ')
    assert 'known=0' in report['client_event'], f"the event the host cleared after its save is set on the client: {report['client_event']}"


def stageStoryReload(copies, report, negative=False):
    """A client that is loaded into the host's save takes the loaded story state as it is and does not send it back as its
    own changes (review 26.10.): its known values were those of the world before the load, and the first poll in the new
    world sent every difference to the loaded save to the host, which applied them, so what the host changed during the
    client's load went back to the save's value. The host saves with an event cleared, sets it (the client learns it),
    loads the save (cleared again) and sets the event once more while the client is still loading: after the client is
    back, the event is still set on the host and on the client, and the client claimed nothing. Negative control:
    storyreloadoff on the client."""
    host, client = join_both(copies)
    clear_raid(host, report)
    slot, event = 'G3T_STORYRL', 'G3T_StoryReload_Event'
    clear_slot(copies, slot)
    if negative:
        assert 'ok=1' in report_line(client.bot, 'storyreloadoff', 'STORYRELOADOFF ')
    report['cleared'] = report_line(host.bot, f'clearevent {event}', 'EVENT ')
    time.sleep(2)
    start = host.bot.send(f'nativesave {slot}', 60)
    report['save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
    assert 'ok=1' in report['save'], report['save']
    time.sleep(3)
    report['set'] = report_line(host.bot, f'setevent {event}', 'EVENT ')
    wait_for(lambda: 'known=1' in report_line(client.bot, f'event {event}', 'EVENT '), 15, 'the client learns the event')
    since = len(client.mod_log())
    start = host.bot.send(f'nativeload {slot}', 60)
    report['load'] = host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''), 120,
                                   'nativeload', start)
    assert 'ok=1' in report['load'], report['load']
    wait_running(host, 180, 'host world running after the load')
    report['host_loaded'] = report_line(host.bot, f'event {event}', 'EVENT ')
    report['set_again'] = report_line(host.bot, f'setevent {event}', 'EVENT ')
    report['client_still_loading'] = not re.search(r"persist: in the host's world", client.mod_log()[since:])
    wait_mod_log(client, r'persist: loading G3T_host_'+slot+r'\b', 120, "the client is loaded into the host's save", since)
    wait_running(client, 180, 'client world running after the load')
    report['in_world'] = wait_mod_log(client, r"persist: in the host's world", 60, "the client is in the host's world", since)
    before = story(client.bot).get('events', {})
    time.sleep(8)
    after = story(client.bot).get('events', {})
    report['client_story'] = {k: after.get(k) for k in ('claims', 'requests', 'reloads', 'received')}
    report['host_event'] = report_line(host.bot, f'event {event}', 'EVENT ')
    report['client_event'] = report_line(client.bot, f'event {event}', 'EVENT ')
    report['claims'] = [l[13:] for l in client.mod_log()[since:].splitlines() if 'story: local events' in l][-6:]
    assert 'known=0' in report['host_loaded'], f'the loaded save has the event set: {report["host_loaded"]}'
    assert report['client_still_loading'], 'the client was back before the host set the event again'
    assert 'known=1' in report['host_event'], f"the client's load cleared the event the host set meanwhile: {report['host_event']}"
    assert 'known=1' in report['client_event'], f"the client does not have the host's event after its load: {report['client_event']}"
    assert not report['claims'], f'the client sent the loaded state as its own changes: {report["claims"]}'


def stageW5(copies, report, negative=False):
    """Stage W5, a kill earns its reward once, also when several lethal hits on the NPC reach the host within one network
    tick (Codex review 22.10. R9): IsDead() turns true only when the death task sets its AI mode, so every hit of such a
    burst killed and rewarded again, and the client's GiveXP had no check of its own. The client sends five lethal hit
    claims on a living host NPC near it in one frame (hitnpcburst): the host kills it once, the client is rewarded once.
    Negative control: killonceoff on both."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        for copy in (host, client):
            assert 'ok=1' in report_line(copy.bot, 'killonceoff', 'KILLONCEOFF ')
    time.sleep(3)
    host_npcs, client_npcs = npcs(host.bot), npcs(client.bot)
    client_at = remotes(host.bot)[2]['pos']
    alive = {g: client_npcs[g] for g in set(host_npcs) & set(client_npcs)
             if client_npcs[g]['mirrored'] and not host_npcs[g]['dead'] and host_npcs[g]['hp'] > 0 and host_npcs[g]['name'] != 'PC_Hero'
             and distance(client_npcs[g]['pos'], client_at) < 3000}
    alive = {g: v for g, v in alive.items() if v['name'] not in ('Gorn', 'Diego', 'Milten', 'Lester')}  # companions do not die
    victims = pick_victims(alive, client_at, 1)
    assert victims, 'no living mirrored NPC near the client'
    victim = report['victim'] = victims[0]
    since_client, since_host = len(client.mod_log()), len(host.mod_log())
    report['burst'] = report_line(client.bot, f"hitnpcburst {victim['guid']}_5", 'HITNPCBURST ')
    assert 'sent=5' in report['burst'], report['burst']
    wait_mod_log(client, r'combat: rewarded the kill of '+re.escape(victim['name']), 20, 'the client is rewarded for the kill', since_client)
    time.sleep(5)
    report['host_kills'] = kills = [l for l in host.mod_log()[since_host:].splitlines() if 'killed '+victim['name']+' (OnCheatKill)' in l]
    report['client_rewards'] = rewards = [l for l in client.mod_log()[since_client:].splitlines() if 'rewarded the kill of '+victim['name'] in l]
    report['host_killonce'] = report_line(host.bot, 'killreport', 'KILLONCE ')
    report['client_killonce'] = report_line(client.bot, 'killreport', 'KILLONCE ')
    assert len(rewards) == 1, f"the client was rewarded {len(rewards)} times for one kill: {rewards}"
    assert len(kills) == 1, f"the host killed {victim['name']} {len(kills)} times: {kills}"


def stageW6(copies, report, negative=False):
    """Stage W6, nothing of the mod runs inside the game's own load, and a client's autoload of the host's campaign starts
    outside the module's frame (C 22./23.10., re/findings/p3-load-autoload-ransack-contract-c-20261023.md): the load runs
    frames of its own (Session::Start), in which remote, npcs and the other ticks ran on in the half-made world, and the
    autoload ran from persist::Tick, so the ticks after it ran on in the new world within the same frame. The host saves
    (the client writes its hero record for it) and loads the save; the client gets it and loads it by itself. On both, the
    load ran frames of its own and none of the mod's ticks in them; on the client the autoload was posted and ran at process
    depth 0, before its load, and its hero record still comes back before it is in the host's world. A held old order is
    first invalidated by an isolated local WelcomeSerial change; a real reconnect must then increment the serial through
    the wire Welcome path and create a fresh order, which alone may run after release. Negative control: loadticksoff on
    both."""
    host, client = join_both(copies)
    clear_raid(host, report)
    slot = 'G3T_W6'
    clear_slot(copies, slot)
    held = False
    if negative:
        for copy in (host, client):
            assert 'ok=1' in report_line(copy.bot, 'loadticksoff', 'LOADTICKSOFF ')
    else:
        assert 'hold=1' in report_line(client.bot, 'loadticksoff hold', 'LOADTICKSOFF ')
        held = True
    try:
        since = len(client.mod_log())
        start = host.bot.send(f'nativesave {slot}', 60)
        report['save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
        assert 'ok=1' in report['save'], report['save']
        report['record_written'] = wait_mod_log(client, r'persist: the host saved '+slot+r'; own hero record written', 20,
                                                'the client writes its hero record for the save', since)
        time.sleep(3)
        since_host, since = len(host.mod_log()), len(client.mod_log())
        start = host.bot.send(f'nativeload {slot}', 60)
        report['load'] = host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''), 120,
                                       'nativeload', start)
        assert 'ok=1' in report['load'], report['load']
        wait_running(host, 180, 'host world running after the load')

        fresh_order = fresh_welcome = None
        if not negative:
            posted_re = r'persist: autoload of G3T_host_'+slot+r' posted \(host world [0-9a-f]+, welcome (\d+), loads \d+\), order (\d+)'
            report['old_posted'] = old_posted = wait_mod_log(client, posted_re, 120, 'the old autoload order is posted and held', since)
            old_match = re.search(posted_re, old_posted)
            old_welcome, old_order = int(old_match.group(1)), int(old_match.group(2))
            report['serial_probe'] = serial_probe = report_line(client.bot, 'loadticksoff welcome', 'LOADTICKSOFF ')
            serial_match = re.search(r'welcome=(\d+)->(\d+)', serial_probe)
            assert serial_match and int(serial_match.group(1)) == old_welcome and int(serial_match.group(2)) == old_welcome + 1, serial_probe
            stale_re = r'persist: autoload of G3T_host_'+slot+r' is stale \(another welcome\), order '+str(old_order)+r'; not loaded'
            report['old_stale'] = wait_mod_log(client, stale_re, 20, 'the serial change invalidates exactly the old order', since)
            assert not any(re.search(r"runs outside the module's frame .* order "+str(old_order)+r'$', l)
                           for l in client.mod_log()[since:].splitlines()), f'old autoload order {old_order} ran'

            since_rejoin = len(client.mod_log())
            report['reconnect'] = report_line(client.bot, 'reconnect', 'RECONNECT ')
            assert 'ok=1' in report['reconnect'], report['reconnect']
            report['real_welcome'] = real_welcome = wait_mod_log(client, r'net: welcomed as player \d+ \(welcome \d+\)', 30,
                                                                  'the client receives a real new Welcome', since_rejoin)
            actual_welcome = int(re.search(r'\(welcome (\d+)\)', real_welcome).group(1))
            assert actual_welcome == old_welcome + 2, f'unexpected real Welcome serial: {old_welcome} -> {actual_welcome}'
            report['fresh_posted'] = fresh_posted = wait_mod_log(client, posted_re, 120, 'the rejoined client gets a fresh autoload order', since_rejoin)
            fresh_match = re.search(posted_re, fresh_posted)
            fresh_welcome, fresh_order = int(fresh_match.group(1)), int(fresh_match.group(2))
            assert fresh_welcome == actual_welcome and fresh_order > old_order, \
                f'fresh order is not bound to the real Welcome: old={old_order}/{old_welcome}, fresh={fresh_order}/{fresh_welcome}, actual={actual_welcome}'
            report['release'] = report_line(client.bot, 'loadticksoff release', 'LOADTICKSOFF ')
            assert 'hold=0' in report['release'], report['release']
            held = False

        wait_mod_log(client, r'persist: loading G3T_host_'+slot+r'\b', 120, "the client is loaded into the host's save", since)
        wait_running(client, 180, 'client world running after the load')
        report['in_world'] = wait_mod_log(client, r"persist: in the host's world", 40, "the client is in the host's world", since)
        report['host_persist'] = report_line(host.bot, 'persist', 'PERSIST ')
        report['client_persist'] = report_line(client.bot, 'persist', 'PERSIST ')
        returned = r"persist: the game's load of (\S+) returned \((\d)\) after (\d+) frames of its own, mod ticks in (\d+)"
        for copy, at in ((host, since_host), (client, since)):
            loads = [m for m in (re.search(returned, l) for l in copy.mod_log()[at:].splitlines()) if m]
            report[copy.tag+'_loads'] = [m.group(0) for m in loads]
            assert loads, f'{copy.tag}: no load reported its own frames'
            for m in loads:
                assert int(m.group(3)) >= 1, f'{copy.tag}: the load ran no frames of its own, so nothing was tested: {m.group(0)}'
                assert int(m.group(4)) == 0, f"{copy.tag}: the mod's ticks ran inside the load's own frames: {m.group(0)}"
            # Seen apart from the mod's own count: Frame() writes a frame= line every second, none may fall inside a load.
            inside, frames_inside = False, 0
            for l in copy.mod_log()[at:].splitlines():
                if re.search(r'persist: loading \S+, (sidecar read|no sidecar)', l): inside = True
                elif re.search(returned, l): inside = False
                elif inside and re.search(r'\bframe=\d+', l): frames_inside += 1
            report[copy.tag+'_frame_lines_inside_loads'] = frames_inside
            assert frames_inside == 0, f"{copy.tag}: {frames_inside} frame= lines were written inside a load"
        lines = client.mod_log()[since:].splitlines()
        first = lambda pattern: next((i for i, l in enumerate(lines) if re.search(pattern, l)), None)
        posted_pattern = r'persist: autoload of G3T_host_'+slot+r' posted' if negative else \
            r'persist: autoload of G3T_host_'+slot+r' posted .* order '+str(fresh_order)+r'$'
        outside_pattern = r"persist: autoload of G3T_host_"+slot+r" runs outside the module's frame \(process depth 0, welcome \d+\)" if negative else \
            r"persist: autoload of G3T_host_"+slot+r" runs outside the module's frame \(process depth 0, welcome "+str(fresh_welcome)+r"\), order "+str(fresh_order)+r'$'
        report['client_order'] = order = {
            'posted': first(posted_pattern),
            'outside': first(outside_pattern),
            'loading': first(r'persist: loading G3T_host_'+slot+r'\b'),
            'returned': first(r"persist: the game's load of G3T_host_"+slot+r" returned \(1\)"),
            'record_applied': first(r'own hero record .*G3T_host_'+slot+r'\.txt applied: 1'),
            'in_world': first(r"persist: in the host's world")}
        report['client_stale'] = [l for l in lines if re.search(r'persist: autoload of \S+ is stale', l)]
        assert order['posted'] is not None and order['outside'] is not None, f"the autoload did not run outside the module's frame: {order}"
        assert None not in order.values(), f'a step of the client load is missing: {order}'
        assert order['posted'] < order['outside'] < order['loading'] < order['returned'], f'the autoload ran out of order: {order}'
        assert order['returned'] < order['record_applied'] < order['in_world'], f"the client's hero record came back out of order: {order}"
        if not negative:
            assert not any(re.search(r"runs outside the module's frame .* order "+str(old_order)+r'$', l) for l in lines), \
                f'old autoload order {old_order} ran after the real rejoin'
        report['crash_lines'] = crashes = {c.tag: [l for l in c.mod_log().splitlines() if ' CRASH ' in l][:6] for c in (host, client)}
        assert not any(crashes.values()), f'CRASH lines: {crashes}'
    finally:
        if held:
            try:
                report['release_cleanup'] = report_line(client.bot, 'loadticksoff release', 'LOADTICKSOFF ')
            except Exception as error:  # preserve the original failure; the game process is disposed after the suite
                report['release_cleanup'] = 'failed: '+repr(error)


def stageW7(copies, report, negative=False):
    """Stage W7, a refused own hero and the first screen messages (owner 23.10., points 1 and 10). Part 1: the host saves
    (the client writes its hero record for the save), the client's record is cut as a write that broke off leaves it, and
    the host loads the save: the client is loaded into it, its record is refused, and it stays outside the host's world
    with the reason on the screen (no copy of the host's hero); once the whole record is back (repaired) it is applied, the
    client is in the host's world and sees that its hero is back. Part 2: the connection drops and comes back (reconnect),
    the host sends a campaign the client must not store (badtransfer), and a hello with another protocol version is refused
    by the host: a message for each on the client. Negative control: recordwaitoff on the client (a refused record opened
    the gate, as before 23.10.). The host learns that the client waits for its hero (WorldWaiting) and shows it."""
    host, client = join_both(copies)
    clear_raid(host, report)
    slot = 'G3T_W7'
    clear_slot(copies, slot)
    record = client.root/'isolated-profile/Documents/gothic3'/f'g3t-hero-G3T_host_{slot}.txt'
    if negative:
        assert 'ok=1' in report_line(client.bot, 'recordwaitoff', 'RECORDWAITOFF ')
    since = len(client.mod_log())
    start = host.bot.send(f'nativesave {slot}', 60)
    report['save'] = host.bot.line('NATIVESAVE ', 60, 'nativesave', start)
    assert 'ok=1' in report['save'], report['save']
    report['record_written'] = wait_mod_log(client, r'persist: the host saved '+slot+r'; own hero record written', 20,
                                            'the client writes its hero record for the save', since)
    whole = record.read_text()
    report['cut'] = report_line(client.bot, f'herocut G3T_host_{slot}', 'HEROCUT ')
    assert 'ok=1' in report['cut'], report['cut']
    time.sleep(3)
    since, since_host = len(client.mod_log()), len(host.mod_log())
    start = host.bot.send(f'nativeload {slot}', 60)
    report['load'] = host.bot.wait(lambda t: next((l for l in t.splitlines() if l.startswith('NATIVELOAD') and 'ok=' in l), ''), 120,
                                   'nativeload', start)
    assert 'ok=1' in report['load'], report['load']
    wait_running(host, 180, 'host world running after the load')
    wait_mod_log(client, r'persist: loading G3T_host_'+slot+r'\b', 120, "the client is loaded into the host's save", since)
    wait_running(client, 180, 'client world running after the load')
    report['restore'] = wait_mod_log(client, r'persist: own hero record .*G3T_host_'+slot+r'\.txt applied: 0', 30,
                                     "the client's own hero record is refused", since)
    time.sleep(15)
    lines = client.mod_log()[since:].splitlines()
    report['in_world_while_refused'] = [l for l in lines if "persist: in the host's world" in l]
    assert not report['in_world_while_refused'], f"the client entered the host's world with a refused hero record: {report['in_world_while_refused']}"
    report['waiting'] = report_line(client.bot, 'recordwait', 'RECORDWAIT ')
    assert 'waiting=1' in report['waiting'], report['waiting']
    report['wait_message'] = wait_mod_log(client, r'hud: \[13\] .*(could not be loaded|nicht laden)', 5, 'the client sees why it waits', since)
    report['host_sees_client'] = remotes(host.bot).get(2, {}).get('puppet')
    assert not report['host_sees_client'], 'the host shows the waiting client'
    # The host learns why (WorldWaiting) and tells its player.
    report['host_knows'] = wait_mod_log(host, r"persist: player 2 \(Client\) waits outside the host's world", 5, 'the host learns the client waits',
                                        since_host)
    report['host_message'] = wait_mod_log(host, r'hud: \[12\] Client (waits outside your game|wartet au)', 5, 'the host sees why the client waits',
                                          since_host)
    since = len(client.mod_log())
    record.write_text(whole)  # repaired
    report['repaired'] = wait_mod_log(client, r'persist: own hero record .* applied after it changed: 1', 20,
                                      "the repaired record is applied", since)
    report['back_message'] = wait_mod_log(client, r'hud: \[8\] .*(own hero is back|eigener Held ist zur)', 10, 'the client sees its hero is back', since)
    report['in_world'] = wait_mod_log(client, r"persist: in the host's world", 20, "the client is in the host's world", since)
    wait_for(lambda: remotes(host.bot).get(2, {}).get('puppet') and ['yes'], 30, 'the host sees the client again')

    since = len(client.mod_log())
    report['reconnect'] = report_line(client.bot, 'reconnect', 'RECONNECT ')
    assert 'ok=1' in report['reconnect'], report['reconnect']
    report['lost_message'] = wait_mod_log(client, r'hud: \[12\] .*(Connection to the host lost|Verbindung zum Host verloren)', 10,
                                          'the client sees the connection is lost', since)
    report['connected_message'] = wait_mod_log(client, r'hud: \[8\] .*(Connected to the host again|Wieder mit dem Host verbunden)', 30,
                                               'the client sees it is connected again', since)
    wait_mod_log(client, r"persist: in the host's world", 30, "the client is in the host's world after the reconnect", since)
    since = len(client.mod_log())
    report['badtransfer'] = report_line(host.bot, 'badtransfer', 'BADTRANSFER ')
    assert 'sent=1' in report['badtransfer'], report['badtransfer']
    report['damaged_message'] = wait_mod_log(client, r'hud: \[12\] .*(did not arrive intact|nicht vollst)', 20,
                                             "the client sees the host's save was not stored", since)
    since = len(client.mod_log())
    assert 'ok=1' in report_line(client.bot, 'helloversion 1', 'HELLOVERSION ')
    report['reconnect_other_version'] = report_line(client.bot, 'reconnect', 'RECONNECT ')
    report['refusal'] = wait_mod_log(client, r'net: REFUSED by host: different G3 Together version', 30, 'the host refuses the other version', since)
    report['refused_message'] = wait_mod_log(client, r'hud: \[13\] .*(another G3 Together version \(yours: protocol 1|andere G3-Together-Version \(deine: Protokoll 1)', 10,
                                             'the client sees why the host refused it', since)


def stageW8(copies, report, negative=False):
    """Stage W8, a mirror whose NPC is not here at its first snapshot is found once it appears, and one that never appears
    goes once its owner stops sending it (Codex review 22.10.: it stayed missing and stayed for good; only summons were bound
    again, by Rebind). Part 1: the host summons a creature (G3_SPELL, default It_Spell_SummonSkeleton); the client makes its
    copy 4 s late and does not rebind it (summonlatecopy), so the host's snapshots of the creature arrive first: the
    client's mirror is missing, then found and mirrored. Part 2: the client sends the host one NPC record of an entity that is
    no NPC there (badrecord, as in X1): the host's mirror entry is missing, then gone once stale. Negative control:
    mirrorretryoff on both."""
    spell = os.environ.get('G3_SPELL', 'It_Spell_SummonSkeleton')
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    if negative:
        for c in (host, client):
            assert 'ok=1' in report_line(c.bot, 'mirrorretryoff', 'MIRRORRETRYOFF ')
    report['late_copy'] = report_line(client.bot, 'summonlatecopy 4000', 'SUMMONLATECOPY ')
    assert 'ms=4000' in report['late_copy'], report['late_copy']
    since = len(client.mod_log())
    own = creature_cast(host, host, spell, report, 'host')
    report['host_creatures'] = own
    assert own, 'no summoned creature after the host cast'
    guid = own[0]['guid']
    report['copy_waits'] = wait_mod_log(client, r'summon: creature net \S+ from player 1: its copy waits 4000 ms', 15, "the client's copy waits", since)
    report['copy_made'] = wait_mod_log(client, r'summon: creature net \S+ from player 1: \S+ copy under its GUID', 20, 'the client makes the late copy', since)
    report['client_stats_copy'] = report_line(client.bot, 'mirrorstats', 'MIRRORSTATS ')
    report['found_later'] = wait_mod_log(client, r'npcs: mirror \S+ found here, \d+ ms after its first snapshot', 10, 'the client finds the late creature', since)
    report['mirrored'] = wait_for(lambda: (npcs(client.bot).get(guid) or {}).get('mirrored') and ['yes'], 10, 'the creature is mirrored on the client')

    def stat(line, key):
        return int(re.search(key + r'=(\d+)', line).group(1))

    report['host_stats_before'] = before = report_line(host.bot, 'mirrorstats', 'MIRRORSTATS ')
    report['badrecord'] = report_line(client.bot, 'badrecord', 'BADRECORD ')
    assert 'sent=1' in report['badrecord'], report['badrecord']
    time.sleep(1.5)
    report['host_stats_sent'] = sent = report_line(host.bot, 'mirrorstats', 'MIRRORSTATS ')
    assert stat(sent, 'missing') > stat(before, 'missing'), f'the record made no missing mirror on the host: {before} -> {sent}'
    time.sleep(6)
    report['host_stats_later'] = later = report_line(host.bot, 'mirrorstats', 'MIRRORSTATS ')
    # Other missing mirrors come and go meanwhile (run 20261023-193747: one more expired with it), so only: it went.
    assert stat(later, 'missing') < stat(sent, 'missing') and stat(later, 'expired') > stat(sent, 'expired'), \
        f'the missing mirror did not go once stale: {sent} -> {later}'


def stageW9(copies, report, negative=False):
    """Stage W9, a hero record carries which skills the hero knows, and loading it makes them so (Codex review 22.10.: the
    restore only "taught" with the perk item's template, which did nothing, so a player loaded into the host's save kept
    the skills of the host's hero there). A skill stack is the perk item; whether it is learned is its skill's state
    (skilldiag 23.10.). On the client: the record is written (its skill lines carry the learned state); in the file one
    learned skill is set to unlearned and one unlearned skill to learned; loading it unlearns the first and teaches the
    second; the whole record, loaded again, puts both back. Negative control: skillreplaceoff on the client."""
    host, client = join_both(copies)
    clear_raid(host, report)
    record = client.root/'isolated-profile/Documents/gothic3'/'g3t-hero-G3TW9.txt'
    record.unlink(missing_ok=True)
    if negative:
        assert 'ok=1' in report_line(client.bot, 'skillreplaceoff', 'SKILLREPLACEOFF ')
    report['save'] = report_line(client.bot, 'herosave G3TW9', 'HEROSAVE ')
    assert 'ok=1' in report['save'], report['save']
    whole = record.read_text()
    lines = whole.splitlines()
    skill_lines = [l.split() for l in lines if l.startswith('stack ') and l.split()[5] == '10']
    assert skill_lines and all(len(p) == 7 for p in skill_lines), 'skill lines without the learned field'
    ids = [p[1] for p in skill_lines]
    known = [p[1] for p in skill_lines if p[6] == '1' and ids.count(p[1]) == 1]
    unknown = [p[1] for p in skill_lines if p[6] == '0' and ids.count(p[1]) == 1]
    report['record_skills'] = {'lines': len(skill_lines), 'learned': len(known), 'not_learned': len(unknown)}
    assert known and unknown, report['record_skills']
    learned_one, other = known[0], unknown[0]
    report['flipped'] = {'unlearn': learned_one, 'teach': other}

    def flip(line):
        p = line.split()
        if line.startswith('stack ') and len(p) == 7 and p[1] in (learned_one, other):
            p[6] = '0' if p[1] == learned_one else '1'
            return ' '.join(p)
        return line

    def skill(id_):
        line = report_line(client.bot, f'hasskill {id_}', 'HEROSKILL ')
        return int(re.search(r'learned=(\d)', line).group(1)), int(re.search(r'known=(\d+)', line).group(1)), line

    report['before'] = before = (skill(learned_one), skill(other))
    assert before[0][0] == 1 and before[1][0] == 0, before
    since = len(client.mod_log())
    record.write_text('\n'.join(flip(l) for l in lines) + '\n')
    report['load_flipped'] = load = report_line(client.bot, 'heroload G3TW9', 'HEROLOAD ')
    assert 'ok=1' in load, load
    report['applied'] = wait_mod_log(client, r'hero: record \S+G3TW9\.txt applied: \d+ skills in it, \d+ taught, \d+ unlearned', 5, 'the record is applied', since)
    report['after'] = after = (skill(learned_one), skill(other))
    assert after[0][0] == 0, f'the hero still knows a skill its record says it does not: {after[0][2]}'
    assert after[1][0] == 1, f'the hero did not learn a skill its record says it knows: {after[1][2]}'
    assert after[0][1] == before[0][1], f'more skills changed than the two: {before[0][2]} -> {after[0][2]}'
    record.write_text(whole)
    report['load_whole'] = load = report_line(client.bot, 'heroload G3TW9', 'HEROLOAD ')
    assert 'ok=1' in load, load
    report['restored'] = restored = (skill(learned_one), skill(other))
    assert restored[0][:2] == before[0][:2] and restored[1][:2] == before[1][:2], f'the whole record did not put them back: {before} -> {restored}'


def skilldiag(copies, report, negative=False):
    """Diagnostic for stage W9 (23.10.): what a skill stack of the hero is (SKILLDIAG), and which way puts a removed skill
    back (SKILLTRY: TeachSkill with the item's skill, else CreateItems with the stack's template). Client only."""
    host, client = join_both(copies)
    start = client.bot.send('skilldiag', 20)
    client.bot.line('SKILLDIAG end', 10, 'skilldiag', start)
    report['skilldiag'] = [l for l in client.bot.log()[start:].splitlines() if l.startswith('SKILLDIAG stack=')]
    start = client.bot.send('skilltry', 20)
    client.bot.line('SKILLTRY case=unlearned', 10, 'skilltry', start)
    report['skilltry'] = [l for l in client.bot.log()[start:].splitlines() if l.startswith('SKILLTRY ')]
    start = client.bot.send('skilldiag', 20)
    client.bot.line('SKILLDIAG end', 10, 'skilldiag', start)
    report['skilldiag_after'] = [l for l in client.bot.log()[start:].splitlines() if l.startswith('SKILLDIAG stack=')]


def stageW10(copies, report, negative=False):
    """Stage W10, a trade between players goes to the seller as an offer and is applied there whole or not at all (owner
    23.10., point 2; offer.cpp). The client offers the host (offersend, what B's trade window sends): the host's message
    waits for its answer; accepted (offeraccept, what the accept key does), the potion leaves the host and the gold
    arrives, and the client hears "accepted". Declined, nothing changes; a second offer meanwhile is answered "busy";
    unanswered, it runs out after 30 s and nothing changes. An offer of more than the host has is answered "not
    possible" and changes nothing, not even the part the host has. The other way round, the host buys from the client.
    Negative control: offerpartial on the host (no check before, no undo on a failure): the offer it cannot fill
    takes the potion all the same."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'offerpartial', 'OFFERPARTIAL ')
    report['host_add'] = report_line(host.bot, 'heroadd It_Potion_Health_3', 'HEROADD ')
    report['client_add'] = report_line(client.bot, 'heroadd It_Potion_Health_2', 'HEROADD ')
    assert 'stack=' in report['host_add'] and 'stack=-1' not in report['host_add'], report['host_add']

    def count(copy, name):
        line = report_line(copy.bot, f'herocount {name}', 'HEROCOUNT ')
        return int(re.search(r'tradable=(-?\d+)', line).group(1))

    def state(copy):
        return report_line(copy.bot, 'offerstate', 'OFFERSTATE ')

    def holdings(copy):
        return {name: count(copy, name) for name in ('It_Potion_Health', 'It_Gold', 'It_Bread')}

    def offer(buyer, seller, argument, label):
        """Sends an offer; returns its serial once the seller has it (its mod log)."""
        since = len(seller.mod_log())
        sent = report_line(buyer.bot, f'offersend {argument}', 'OFFERSENT ')
        serial = int(re.search(r'serial=(\d+)', sent).group(1))
        assert serial > 0, f'{label}: not sent: {sent}'
        report[label+'_sent'] = sent
        report[label+'_arrived'] = wait_mod_log(seller, rf'offer: {serial} from player \d+: \d+ entries', 10, label+': the seller has the offer', since)
        return serial

    def answer(buyer, serial, result, label, since, seconds=10):
        report[label+'_answer'] = wait_mod_log(buyer, rf'offer: {serial} answered by player \d+: result {result} ', seconds, label+f': the buyer hears result {result}', since)

    before = holdings(host)
    report['host_before'] = before
    assert before['It_Potion_Health'] >= 3, before

    # 1. Accepted: the host gives a potion for 50 gold.
    since = len(client.mod_log())
    host_since = len(host.mod_log())
    serial = offer(client, host, '1_-50_It_Potion_Health-1', 'accept')
    assert 'pending=1' in state(host), 'the host has no open offer'
    # The message names the item as the game does (its focus name, as B's trade window sends it), not by its template.
    report['accept_message'] = shown = wait_mod_log(host, r'hud: \[\d+\] .*(offers a trade|bietet einen Handel an)', 5, 'the host is shown the offer', host_since)
    assert 'It_Potion_Health' not in shown and '?' not in shown.split(':', 3)[-1], f'the offer is not shown by the game name: {shown}'
    report['accept_decide'] = report_line(host.bot, 'offeraccept', 'OFFERDECIDE ')
    answer(client, serial, 1, 'accept', since)
    report['host_after_accept'] = after = holdings(host)
    assert after['It_Potion_Health'] == before['It_Potion_Health'] - 1, f'the potion did not go: {before} -> {after}'
    assert after['It_Gold'] == before['It_Gold'] + 50, f'the gold did not arrive: {before} -> {after}'

    # 2. Declined, and a second offer meanwhile is busy.
    since = len(client.mod_log())
    first = offer(client, host, '1_-50_It_Potion_Health-1', 'decline')
    second = report_line(client.bot, 'offersend 1_-50_It_Potion_Health-1', 'OFFERSENT ')
    report['busy_sent'] = second
    answer(client, int(re.search(r'serial=(\d+)', second).group(1)), 5, 'busy', since)
    report['decline_decide'] = report_line(host.bot, 'offerreject', 'OFFERDECIDE ')
    answer(client, first, 2, 'decline', since)
    report['host_after_decline'] = now = holdings(host)
    assert now == after, f'a declined offer changed the host: {after} -> {now}'

    # 3. Unanswered: it runs out after 30 s.
    since = len(client.mod_log())
    host_since = len(host.mod_log())
    serial = offer(client, host, '1_-50_It_Potion_Health-1', 'expire')
    report['expired_seller'] = wait_mod_log(host, rf'offer: {serial} from player \d+ ran out after 30 s', 40, 'the offer runs out on the host', host_since)
    answer(client, serial, 3, 'expire', since)
    assert 'pending=0' in state(host), 'the offer stays open after it ran out'
    report['host_after_expire'] = now = holdings(host)
    assert now == after, f'an offer that ran out changed the host: {after} -> {now}'

    # 4. Not possible: more bread than the host has, with a potion it has. Nothing changes, not even the potion.
    bread = after['It_Bread'] + 5
    since = len(client.mod_log())
    serial = offer(client, host, f'1_0_It_Potion_Health-1-It_Bread-{bread}', 'unavailable')
    report['unavailable_decide'] = report_line(host.bot, 'offeraccept', 'OFFERDECIDE ')
    answer(client, serial, 4, 'unavailable', since)
    report['host_after_unavailable'] = now = holdings(host)
    assert now == after, f'an offer the host could not fill changed it: {after} -> {now}'

    # 5. The other way round: the host buys a potion from the client for 30 gold.
    client_before = holdings(client)
    report['client_before'] = client_before
    since = len(host.mod_log())
    serial = offer(host, client, '2_-30_It_Potion_Health-1', 'reverse')
    report['reverse_decide'] = report_line(client.bot, 'offeraccept', 'OFFERDECIDE ')
    answer(host, serial, 1, 'reverse', since)
    report['client_after'] = client_after = holdings(client)
    assert client_after['It_Potion_Health'] == client_before['It_Potion_Health'] - 1, f'{client_before} -> {client_after}'
    assert client_after['It_Gold'] == client_before['It_Gold'] + 30, f'{client_before} -> {client_after}'
    report['host_state'] = state(host)
    report['client_state'] = state(client)


def stageX1(copies, report, negative=False):
    """Stage X1, malformed messages from the other machine change nothing and crash nothing (Fable's code review 22.10.,
    section 1; hypotheses until this test). Host to client: a file chunk whose offset + length wraps past 2^32 (1.1) and a
    campaign named ..\\..\\G3T_evil (1.4): the client stays up, logs the name as refused and writes no file outside its save
    folder. Client to host: an NPC record with the hero's GUID and a NaN position (1.6) and player states with a NaN
    position (1.2): the host's hero keeps its own task and place, is not mirrored, and the client's puppet keeps a finite
    position; both games keep running. No negative control: without the checks a run may crash a game."""
    host, client = join_both(copies)
    clear_raid(host, report)  # no raid needed: starts from the post-raid save (25.10.)
    frames = lambda copy: int(re.findall(r'frame=(\d+)', copy.mod_log())[-1])
    before = {c.tag: frames(c) for c in (host, client)}
    since_client, since_host = len(client.mod_log()), len(host.mod_log())
    report['badtransfer'] = report_line(host.bot, 'badtransfer', 'BADTRANSFER ')
    assert 'sent=1' in report['badtransfer'], report['badtransfer']
    report['client_refused_name'] = wait_mod_log(client, r'not a plain save name', 20, 'the client refuses the save name', since_client)
    evil = [p for p in (client.root/'isolated-profile').rglob('G3T_evil*')]
    report['evil_files'] = [str(p) for p in evil]
    assert not evil, f'the client wrote {evil}'
    host_before = herostate(host.bot)
    host_pos = hero_pos(host.bot)
    report['badrecord'] = report_line(client.bot, 'badrecord', 'BADRECORD ')
    report['badstates_on'] = report_line(client.bot, 'badstates on', 'BADSTATES ')
    time.sleep(6)
    report['badstates_off'] = report_line(client.bot, 'badstates off', 'BADSTATES ')
    time.sleep(4)
    report['host_after'] = host_after = herostate(host.bot)
    report['host_moved_cm'] = round(distance(hero_pos(host.bot), host_pos))
    assert ' task=PS_' in host_after, f"the host's hero lost its own task: {host_before} -> {host_after}"
    assert report['host_moved_cm'] < 100, f"the host's hero was moved {report['host_moved_cm']} cm"
    report['host_mirrors_hero'] = [l for l in host.mod_log()[since_host:].splitlines() if 'PC_Hero' in l and 'mirror' in l][-3:]
    puppet = remotes(host.bot).get(2, {})
    report['puppet_pos'] = puppet.get('pos')
    assert puppet.get('pos') and all(math.isfinite(v) for v in puppet['pos']), f"the client's puppet has a broken position: {puppet}"
    time.sleep(5)
    report['frames'] = {c.tag: frames(c) - before[c.tag] for c in (host, client)}
    assert all(n > 300 for n in report['frames'].values()), f"a game stopped: {report['frames']}"


def stageM2(copies, report, negative=False):
    """Stage M2, after a join the host keeps the NPCs around its hero (npcs.cpp join grace, 21.10.): the client's first
    snapshots used to reach the host before the host's reached the client, and the host took the NPCs near its own hero
    over as mirrors (37 of 40 runs on 20.10.: raiders the host fought ran on the client). 12 s after the client joined
    no NPC within 20 m of the host's hero may be simulated by the client. Negative control: joingraceoff on the host
    before the client starts (the race may still leave none in a single run)."""
    host = copies['host'].launch('host', 'Host')
    enter_world(host)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'joingraceoff', 'JOINGRACEOFF ')
    client = copies['client'].launch('client', 'Client')
    enter_world(client, startup=120)
    wait_for(lambda: remotes(host.bot).get(2, {}).get('puppet'), 30, 'host shows a puppet for the client')
    time.sleep(12)
    me = hero_pos(host.bot)
    rows = npcs(host.bot)
    near = {r['name']: r['owner'] for r in rows.values() if not r['dead'] and distance(r['pos'], me) <= 2000}
    report['near_owners'] = near
    report['sync'] = report_line(host.bot, 'npcs', 'NPCSYNC ')
    clients = sorted(n for n, o in near.items() if o == 2)
    report['client_simulated'] = clients
    if negative:
        assert clients, 'no NPC near the host ran on the client although the join grace is off (the race may not happen every run)'
    else:
        assert near, 'no NPC near the host'
        assert not clients, f'NPCs near the host run on the client after the join: {clients}'


def stageEdgeClaim(copies, report, negative=False):
    """Stage EdgeClaim, an NPC the host's game still processes just outside the host's range stays the host's when the
    client's copy of it puts it inside (stageM2 24.10.: Orc_GameStartRaider_Scout_07, 47 m from the host's hero there and
    37.7 m in the client's copy, went to the client, so two machines simulated it; from the client it fought the host and
    killed Slave_03 next to it; one run in three). An NPC the host processes 41-58 m from both players (edgenpcs: left out
    for range just now) gets one snapshot from the client as the client's own, next to the players (claimrecord): the host
    must refuse it, keep simulating the NPC and send it itself (within 60 m of a player). NPCs the host does not process
    (a client's region far from the host, stage E) are not touched. Moving an NPC there with farmove did not do: the host's
    game stopped processing a teleported NPC for seconds. Negative control: contestoff on the host."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'contestoff', 'CONTESTOFF ')
    # An NPC by the players with a name of its own, the host's and mirrored by the client. Both heroes step 44 m away from it:
    # it stays where it is, processed by the host's game, but outside the host's 40 m (at 46 m, run 20261024-140544, the host
    # processed nothing in its band; Hamlar was processed at 44.6-44.7 m).
    me = hero_pos(host.bot)
    rows = npcs(host.bot)
    there = npcs(client.bot)
    names = [r['name'] for r in rows.values()]
    companions = ('Diego', 'Gorn', 'Milten', 'Lester')
    pick = sorted((g for g, r in rows.items() if not r['dead'] and not r['mirrored'] and r['owner'] == 0 and distance(r['pos'], me) <= 2500
                   and names.count(r['name']) == 1 and not r['name'].startswith(companions) and (there.get(g) or {}).get('mirrored')),
                  key=lambda g: distance(rows[g]['pos'], me))
    assert pick, 'no NPC by the players that the host simulates and the client mirrors'
    guid = pick[0]
    name = report['npc'] = rows[guid]['name']
    at = rows[guid]['pos']
    dx, dz = me[0] - at[0], me[2] - at[2]
    length = (dx * dx + dz * dz) ** 0.5
    dx, dz = (dx / length, dz / length) if length > 100 else (1.0, 0.0)
    away = (at[0] + dx * 4400, me[1] + 50, at[2] + dz * 4400)
    report['host_teleport'] = move_hero(host.bot, away)
    report['client_teleport'] = move_hero(client.bot, (away[0] + 150, away[1], away[2]))
    time.sleep(12)  # past the join grace, which refuses a client's snapshots near the host's hero for 10 s
    e = None
    for attempt in range(3):
        start = host.bot.send('edgenpcs', 20)
        host.bot.line('EDGENPC end', 10, 'edgenpcs', start)
        edge = [dict(re.findall(r'(\w+)=(\S+)', l)) for l in host.bot.log()[start:].splitlines() if l.startswith('EDGENPC guid=')]
        report['edge'] = [f"{x['name']} {x['nearest_m']} m {x['reason']} {x['age_ms']} ms mirror={x['mirror']}" for x in edge][:12]
        usable = [x for x in edge if x['reason'] == 'range' and 0 <= int(x['age_ms']) < 500 and x['mirror'] == '0' and float(x['nearest_m']) <= 58]
        # The one moved away from, or any other the host processes in its band now (Hamlar may walk out of it meanwhile).
        e = next((x for x in usable if x['guid'] == guid), usable[0] if usable else None)
        if e:
            break
        time.sleep(2)
    if not e:
        raise RuntimeError(f'inconclusive: no NPC processed by the host in its band: {report["edge"]}')
    guid, name = e['guid'], e['name']
    report['npc'] = f"{name} {e['nearest_m']} m"
    since = len(host.mod_log())
    report['claim'] = report_line(client.bot, f'claimrecord {guid}', ('CLAIMRECORD ', 'REFUSED claimrecord'))
    assert report['claim'].startswith('CLAIMRECORD sent=1'), report['claim']
    time.sleep(1.5)
    lines = host.mod_log()[since:].splitlines()
    taken = [l for l in lines if f'host mirrors {name} of player' in l]
    kept = [l for l in lines if f'npcs: {name} of player' in l and 'refused' in l]
    report['taken'], report['kept'] = taken[:3], kept[:3]
    report['npcsend_after'] = after = report_line(host.bot, 'npcsend ' + guid, ('NPCSEND ', 'REFUSED npcsend'))
    report['sync'] = report_line(host.bot, 'npcs', 'NPCSYNC ')
    assert not taken, f"the host took the client's copy of {name}: {taken[0]}"
    assert kept, f"the host neither took nor refused the client's snapshot of {name}: {report['sync']}"
    assert ' mirror=0 ' in after and 'reason=sent' in after, f"{name} is not the host's, sent from there: {after}"


def stageM1(copies, report, negative=False):
    """Stage M1, the host's NPCs stay mirrored on the client (npcs.cpp, 20.10.): when the client's mirrors went stale
    for a moment (3 s without a snapshot: own AI back, and the client starts sending them as its own), the host's
    snapshots win again and the client shows them as mirrors. Before, both kept simulating them for good (Slave_04
    following the host in stageControl, Lester in stageP3a). The stale moment is made with npcrecvoff/npcrecvon on the
    client. Negative control: hostwinsoff on the client (it keeps its own simulation)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)

    def view():
        hosts = {g: r for g, r in npcs(host.bot).items() if not r['dead'] and not r['mirrored']}
        seen = npcs(client.bot)
        rows = {r['name']: {'mirrored': seen[g]['mirrored'], 'owner': seen[g]['owner']} for g, r in hosts.items() if g in seen}
        return rows
    report['before'] = before = view()
    assert before and all(v['mirrored'] and v['owner'] == 1 for v in before.values()), f'not mirrored at the start: {before}'
    if negative:
        assert 'ok=1' in report_line(client.bot, 'hostwinsoff', 'HOSTWINSOFF ')
    report_line(client.bot, 'npcrecvoff', 'NPCRECVOFF ')
    time.sleep(7)
    report['stale'] = view()
    report_line(client.bot, 'npcrecvon', 'NPCRECVON ')
    time.sleep(8)
    report['after'] = after = view()
    report['sync'] = report_line(client.bot, 'npcs', 'NPCSYNC ')
    lost = sorted(n for n, v in after.items() if not (v['mirrored'] and v['owner'] == 1))
    report['lost'] = lost
    if negative:
        assert lost, 'every NPC is mirrored again although the client keeps its own simulation'
    else:
        assert not lost, f'the client still simulates the host\'s NPCs itself: {lost}'



def stageR2(copies, report, negative=False):
    """Stage R2, a deed of the client is not lost when the host's counters arrive first (enclaves.cpp, C's
    release-review P1, 21.10.): the client counts a murder and a theft at Ardea while its scan is paused (the 500 ms
    window made wide), the host counts a murder meanwhile and sends its counters. Afterwards both machines show
    Ardea with both murders and the theft. Negative control: keeplocaloff on the client (the host's counters
    overwrite its unreported deeds)."""
    host, client = join_both(copies)
    clear_raid(host, report)  # no raid needed: starts from the post-raid save (25.10.)
    time.sleep(5)
    before = enclave_counts(host.bot, 'Ardea')
    report['before'] = {'host': before, 'client': enclave_counts(client.bot, 'Ardea')}
    if negative:
        assert 'ok=1' in report_line(client.bot, 'keeplocaloff', 'KEEPLOCALOFF ')
    report_line(client.bot, 'enclavescanpause 6000', 'ENCLAVESCANPAUSE ')
    report['client_add'] = [report_line(client.bot, 'crimeadd Ardea-4-1', ('CRIMEADD ', 'REFUSED crimeadd')),
                            report_line(client.bot, 'crimeadd Ardea-3-1', ('CRIMEADD ', 'REFUSED crimeadd'))]
    report['host_add'] = report_line(host.bot, 'crimeadd Ardea-4-1', ('CRIMEADD ', 'REFUSED crimeadd'))
    time.sleep(12)  # the pause ends after 6 s; a theft is held back 3 s more
    after = {c.tag: enclave_counts(c.bot, 'Ardea') for c in (host, client)}
    report['after'] = after
    report['enclavesync'] = {c.tag: report_line(c.bot, 'enclavesync', 'ENCLAVESYNC ') for c in (host, client)}
    murders, thefts = after['host'][4] - before[4], after['host'][3] - before[3]
    report['added'] = {'murder': murders, 'theft': thefts}
    if negative:
        assert murders < 2 or thefts < 1, f'both deeds counted although local deeds are not kept: {report["added"]}'
        return
    assert after['host'] == after['client'], f'Ardea differs: {after}'
    assert murders == 2 and thefts == 1, f'the host counted {report["added"]} (want 2 murders, 1 theft)'


def stageR1(copies, report, negative=False):
    """Stage R1, town reputation (enclaves.cpp; owner 20.10., INI reputation = shared): a player takes bread that belongs
    to Ardea; Ardea's crime counters are the same on both machines afterwards and the theft counts once on the host.
    G3_WHO=host|client|both. Negative control: enclavesyncoff on both (the counters differ: the client never got the
    host's murders of the raid)."""
    theftdiag(copies, report, negative, check=True)


def potiondiag(copies, report, negative=False):
    """Which healing potion templates exist (helping a partner up costs one, owner 19.10.)?"""
    host = copies['host'].launch()
    enter_world(host)
    report['before'] = report_line(host.bot, 'potions', 'POTIONS end')
    report['give'] = [report_line(host.bot, 'givepotion '+name, 'GIVEPOTION ') for name in
                      os.environ.get('G3_POTIONS', ','.join(POTION_CANDIDATES)).split(',')]
    start = host.bot.send('potions', 20)
    host.bot.line('POTIONS end', 10, 'potions', start)
    report['carried'] = [l for l in host.bot.log()[start:].splitlines() if l.startswith('POTION')]
    report['status_note'] = 'diagnostic only'


CREATURES = ['Wolf']  # templates found at runtime: Wolf, Bloodfly (G3_CREATURES)


def watch_puppet(watcher, watched_id, report, tag, samples=12):
    """What the watcher's puppet of a player shows: the motion name it gets, what slot 0 plays, whether it runs."""
    rows = []
    for _ in range(samples):
        pose = puppet_pose(watcher.bot, watched_id)
        r = remotes(watcher.bot).get(watched_id, {})
        if pose:
            rows.append({'remote': pose['remote'], 'shows': pose['shows'], 'run0': pose['run0'], 'body': pose.get('body'),
                         'gap_cm': round(distance(r['pos'], r['puppet_pos'])) if r.get('puppet') else None})
        time.sleep(0.5)
    report[tag] = {'samples': len(rows), 'frozen': sum(1 for x in rows if x['run0'] == 0),
                   'bodies': sorted({str(x['body']) for x in rows}), 'moving_seen': sum(1 for x in rows if '_Move_' in x['shows'] and 'Move_Stand' not in x['shows']),
                   'remote_motions': sorted({x['remote'][:60] for x in rows}), 'shown': sorted({x['shows'][:60] for x in rows}),
                   'gap_cm_max': max((x['gap_cm'] for x in rows if x['gap_cm'] is not None), default=None)}


def transform_phase(transformer, watcher, watched_id, report, tag):
    name = report.get('creature')
    report[tag+'_transform'] = report_line(transformer.bot, 'transform '+name, 'TRANSFORM')
    time.sleep(3)
    snap(watcher, tag+'-transformed', report)
    snap(transformer, tag+'-self', report)
    watch_puppet(watcher, watched_id, report, tag+'_standing')
    # The creature runs 6 m with the game's AI goto (a movement goal does nothing on the body the player steers).
    report[tag+'_walk'] = report_line(transformer.bot, 'walkby 0', 'WALKBY')
    watch_puppet(watcher, watched_id, report, tag+'_walking', 6)
    report_line(transformer.bot, 'walkby 4', 'WALKBY')
    watch_puppet(watcher, watched_id, report, tag+'_walking_back', 6)
    watch_puppet(watcher, watched_id, report, tag+'_after_walk', 6)
    report[tag+'_state'] = report_line(transformer.bot, 'transformstate', 'TRANSFORMSTATE')


def stageT(copies, report, negative=False):
    """Stage T (owner 19.10.: spells and potions turn a player into a wolf or a bloodfly): while a player is transformed,
    the other machine shows its creature (not the hidden human body left where the spell was cast), with the creature's
    motions running, walking where it walks; turned back, the human again. Host transforms, client transforms, both.
    G3_CREATURES picks the creature (default Wolf). Negative control: shapeoff."""
    if negative:
        report['negative'] = True
    transformdiag(copies, report)
    report.pop('status_note', None)
    creature = report['creature']
    phases = ['host_standing', 'host_walking', 'host_walking_back', 'host_after_walk', 'client_standing', 'client_walking',
              'client_walking_back', 'client_after_walk', 'both_host_sees', 'both_client_sees']
    wrong = {p: report[p]['bodies'] for p in phases if report[p]['bodies'] != [creature]}
    assert not wrong, f'the other machine did not show the {creature}: {wrong}'
    back = {p: report[p]['bodies'] for p in ('host_back_seen', 'client_back_seen') if report[p]['bodies'] != ['PC_Hero']}
    assert not back, f'turned back, the other machine did not show the hero: {back}'
    samples = sum(report[p]['samples'] for p in phases)
    frozen = sum(report[p]['frozen'] for p in phases)
    report['frozen_total'] = f'{frozen}/{samples}'
    assert frozen <= 0.15 * samples, f'creature puppets stood frozen in {frozen} of {samples} looks'
    moving = sum(report[p]['moving_seen'] for p in ('host_walking', 'host_walking_back', 'client_walking', 'client_walking_back'))
    report['moving_seen_total'] = moving
    assert moving >= 2, f'a walking creature was seen walking in only {moving} looks'


def transformdiag(copies, report, negative=False):
    """Owner (19.10.): spells and potions turn a player into a wolf or a bloodfly. Host transforms while the client
    watches, then the other way round, then both; what each machine's puppet of the other shows."""
    host, client = join_both(copies)
    if report.get('negative'):
        for copy in (host, client):
            report_line(copy.bot, 'shapeoff', 'SHAPEOFF')
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):  # the raid may have knocked someone out (first run: the client)
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    for name in os.environ.get('G3_CREATURES', ','.join(CREATURES)).split(','):
        line = report_line(host.bot, 'transform '+name, 'TRANSFORM')
        if 'refused' not in line:
            report['creature'], report['probe'] = name, line
            time.sleep(2)
            report['probe_back'] = report_line(host.bot, 'transformback', 'TRANSFORMBACK')
            break
    assert report.get('creature'), 'no creature template found'
    time.sleep(4)
    # 1. The host transforms, the client watches.
    transform_phase(host, client, 1, report, 'host')
    report['host_back'] = report_line(host.bot, 'transformback', 'TRANSFORMBACK')
    time.sleep(4)
    watch_puppet(client, 1, report, 'host_back_seen', 6)
    # 2. The client transforms, the host watches.
    transform_phase(client, host, 2, report, 'client')
    report['client_back'] = report_line(client.bot, 'transformback', 'TRANSFORMBACK')
    time.sleep(4)
    watch_puppet(host, 2, report, 'client_back_seen', 6)
    # 3. Both at once.
    report['both_host'] = report_line(host.bot, 'transform '+report['creature'], 'TRANSFORM')
    report['both_client'] = report_line(client.bot, 'transform '+report['creature'], 'TRANSFORM')
    time.sleep(3)
    snap(host, 'both', report)
    snap(client, 'both', report)
    watch_puppet(host, 2, report, 'both_host_sees')
    watch_puppet(client, 1, report, 'both_client_sees')
    report['both_back'] = [report_line(c.bot, 'transformback', 'TRANSFORMBACK') for c in (host, client)]
    time.sleep(3)
    report['states_end'] = [herostate(c.bot) for c in (host, client)]
    report['remotes_end'] = [str(remotes(c.bot)) for c in (host, client)]
    report.setdefault('status_note', 'diagnostic only')


def npc_hp(bot, guid):
    v = npcs(bot).get(guid)
    return (v['hp'], v['dead']) if v else (None, None)


def flying(bot):
    line = report_line(bot, 'flying', 'FLYING ')
    return {k: int(v) for k, v in re.findall(r'(projectiles|meteors)=(\d+)', line)}


def shot_watch(shooter, watcher, shooter_id, seconds=3.0):
    """While a shot or spell goes off: what flies on both machines and what the watcher's puppet of the shooter plays."""
    seen = {'shooter_projectiles': 0, 'watcher_projectiles': 0, 'shooter_meteors': 0, 'watcher_meteors': 0, 'puppet': []}
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        s, w = flying(shooter.bot), flying(watcher.bot)
        seen['shooter_projectiles'] = max(seen['shooter_projectiles'], s.get('projectiles', 0))
        seen['watcher_projectiles'] = max(seen['watcher_projectiles'], w.get('projectiles', 0))
        seen['shooter_meteors'] = max(seen['shooter_meteors'], s.get('meteors', 0))
        seen['watcher_meteors'] = max(seen['watcher_meteors'], w.get('meteors', 0))
        pose = puppet_pose(watcher.bot, shooter_id)
        if pose:
            seen['puppet'].append(f"{pose['remote'][:48]} | {pose['shows'][:48]} run0={pose['run0']}")
    return seen


def calm_target(host):
    """A living villager the host simulates, not one of the hero's friends (the raid is over)."""
    keep = ('Diego', 'Gorn', 'Milten', 'Lester')
    living = {g: v for g, v in npcs(host.bot).items() if not v['dead'] and not v['mirrored'] and 'Orc' not in v['name']
              and v['name'] not in keep}
    assert living, 'no calm target near the host'
    return next((g for g, v in living.items() if v['name'].startswith('Slave')), next(iter(living)))


def near_target(shooter, watcher, shooter_id, host, target, report, tag, gap=1200):
    """Put the shooter `gap` cm from the target, on the line from the target to where the shooter stands."""
    t = npcs(host.bot)[target]['pos']
    me = remotes(watcher.bot)[shooter_id]['pos']
    d = distance(t, me) or 1.0
    spot = tuple(a + (b - a) * gap / d for a, b in zip(t, me))
    report[tag+'_moved'] = move_hero(shooter.bot, spot)


def xp_of(bot):
    return int(re.search(r' xp=(-?\d+)', report_line(bot, 'hp', 'HP '))[1])


def ranged_phase(shooter, watcher, shooter_id, host, report, tag):
    start = shooter.bot.send('inventory', 20)
    shooter.bot.line('INV end', 10, 'inventory', start)
    lines = [l for l in shooter.bot.log()[start:].splitlines() if l.startswith('INV name=')]
    bow = next((re.search(r'name=(\S+)', l)[1] for l in lines if ' use=5 ' in l), None)
    assert bow, f'{tag} carries no bow'
    target = calm_target(host)
    report[tag+'_target'] = npcs(host.bot)[target]['name']
    near_target(shooter, watcher, shooter_id, host, target, report, tag)
    report[tag+'_flying_before'] = {'shooter': flying(shooter.bot), 'watcher': flying(watcher.bot)}
    # 1. Bow: three shots, each drawn anew.
    hp0, xp0 = npc_hp(host.bot, target), xp_of(shooter.bot)
    shots = []
    for i in range(3):
        report_line(shooter.bot, 'ready '+bow, 'READY ')
        report_line(shooter.bot, 'aimat '+target, 'TARGET ')
        aim = report_line(shooter.bot, 'act PS_Ranged-PS_Ranged_Aim', 'ACT ')
        aiming = shot_watch(shooter, watcher, shooter_id, 1.2)['puppet']
        line = report_line(shooter.bot, 'act PS_Ranged-PS_Ranged_Shoot', 'ACT ')
        shots.append({'aim': aim[28:140], 'aiming_seen': aiming[-2:], 'shoot': line[28:140], **shot_watch(shooter, watcher, shooter_id, 2.0)})
    report[tag+'_shots'] = shots
    report[tag+'_bow'] = {'host_before': hp0, 'host_after': npc_hp(host.bot, target), 'shooter_sees': npc_hp(shooter.bot, target),
                          'watcher_sees': npc_hp(watcher.bot, target), 'shooter_xp': (xp0, xp_of(shooter.bot))}
    # 2. Meteor at the same target (the next one if it fell).
    if npc_hp(host.bot, target)[1] in (True, None):
        target = calm_target(host)
    near_target(shooter, watcher, shooter_id, host, target, report, tag+'_spell')
    report[tag+'_mana'] = report_line(shooter.bot, 'mana', 'MANA ')
    report[tag+'_ready_spell'] = report_line(shooter.bot, 'ready It_Spell_Meteor', 'READY ')
    hp1, xp1 = npc_hp(host.bot, target), xp_of(shooter.bot)
    report[tag+'_aimat_spell'] = report_line(shooter.bot, 'aimat '+target, 'TARGET ')
    report[tag+'_magic'] = report_line(shooter.bot, 'act PS_Magic-PS_Magic_Loop', 'ACT ')[28:160]
    time.sleep(0.8)
    report[tag+'_cast'] = report_line(shooter.bot, 'act PS_Magic-PS_Magic_Cast', 'ACT ')[28:160]
    report[tag+'_cast_seen'] = shot_watch(shooter, watcher, shooter_id, 4.0)
    snap(watcher, tag+'-cast-seen', report)
    snap(shooter, tag+'-cast-self', report)
    time.sleep(3)
    report[tag+'_meteor'] = {'target': npcs(host.bot).get(target, {}).get('name'), 'host_before': hp1, 'host_after': npc_hp(host.bot, target),
                             'shooter_sees': npc_hp(shooter.bot, target), 'watcher_sees': npc_hp(watcher.bot, target),
                             'shooter_xp': (xp1, xp_of(shooter.bot)), 'mana_after': report_line(shooter.bot, 'mana', 'MANA ')}


def clear_spot(shooter, host, target, report, tag, gap=600):
    """Put the shooter `gap` cm from the target where no figure on either machine stands within 120 cm of the
    line between them (an arrow stops in the first body it touches). Tries eight directions; takes the best."""
    t = npcs(host.bot)[target]['pos']
    # Dead ones too: the host's dead orcs were seen standing on the client (19.10.), and they stop arrows.
    others = [v['pos'] for view in (npcs(host.bot), npcs(shooter.bot)) for g, v in view.items() if g != target]
    def clearance(spot):
        best = 1e9
        for p in others:
            ab = [b - a for a, b in zip(t, spot)]
            ap = [b - a for a, b in zip(t, p)]
            k = max(0.0, min(1.0, (ab[0]*ap[0] + ab[2]*ap[2]) / ((ab[0]**2 + ab[2]**2) or 1.0)))
            best = min(best, math.hypot(ap[0] - k*ab[0], ap[2] - k*ab[2]))
        return best
    spots = [(t[0] + gap*math.sin(a), t[1], t[2] + gap*math.cos(a)) for a in (i*math.pi/4 for i in range(8))]
    spot = max(spots, key=clearance)
    report[tag+'_clearance_cm'] = round(clearance(spot))
    report[tag+'_moved'] = move_hero(shooter.bot, spot)
    report[tag+'_orcs'] = {c.tag: sorted({(v['name'][:12], v['dead'], v['task'][:16]) for v in npcs(c.bot).values() if 'Orc' in v['name'] or 'Ork' in v['name']})
                           for c in (host, shooter)}


def summons(bot):
    start = bot.send('summons', 20)
    bot.line('SUMMONS end', 10, 'summons', start)
    return [l[7:] for l in bot.log()[start:].splitlines() if l.startswith('SUMMON ')]


def ranged_bot_phase(shooter, watcher, shooter_id, host, report, tag, spell='It_Spell_Meteor'):
    """Shoot and cast the way the game's NPCs do (shootat, castat; re/findings/ranged-ai-paths-b.md)."""
    start = shooter.bot.send('inventory', 20)
    shooter.bot.line('INV end', 10, 'inventory', start)
    lines = [l for l in shooter.bot.log()[start:].splitlines() if l.startswith('INV name=')]
    bow = next((re.search(r'name=(\S+)', l)[1] for l in lines if ' use=5 ' in l), None)
    assert bow, f'{tag} carries no bow'
    target = calm_target(host)
    report[tag+'_target'] = npcs(host.bot)[target]['name']
    clear_spot(shooter, host, target, report, tag, int(os.environ.get('G3_BOW_GAP', '600')))
    report_line(shooter.bot, 'aimat '+target, 'TARGET ')
    report[tag+'_shooter_at'] = remotes(watcher.bot)[shooter_id]['pos']
    hp0, xp0 = npc_hp(host.bot, target), xp_of(shooter.bot)
    shots = []
    if os.environ.get('G3_HOMING_OFF'):
        report_line(shooter.bot, 'homingoff', 'HOMINGOFF')
    for i in range(3):
        ready = report_line(shooter.bot, 'ready '+bow, 'READY ')
        report_line(shooter.bot, 'aimat '+target, 'TARGET ')
        modes = os.environ.get('G3_SHOOT_MODES', '0,0,0').split(',')
        line = report_line(shooter.bot, f'shootat {target}-{modes[i % len(modes)]}', 'SHOOTAT ')
        track = []
        for _ in range(6):
            track.append(report_line(shooter.bot, 'lastarrow', 'LASTARROW ')[10:])
            time.sleep(0.15)
        shot_hits = {c.tag: summon_state(c.bot)[1][-4:] for c in (shooter, watcher)}
        if i == 0:
            snap(shooter, tag+'-shot-self', report)
        shots.append({'ready': ready[6:], 'shoot': line[8:360], 'track': track, 'hits': shot_hits, **shot_watch(shooter, watcher, shooter_id, 1.0),
                      'hp': npc_hp(host.bot, target), 'target_at': npcs(host.bot)[target]['pos']})
    report[tag+'_shots'] = shots
    time.sleep(1)
    report[tag+'_bow'] = {'host_before': hp0, 'host_after': npc_hp(host.bot, target), 'shooter_sees': npc_hp(shooter.bot, target),
                          'watcher_sees': npc_hp(watcher.bot, target), 'shooter_xp': (xp0, xp_of(shooter.bot))}
    if npc_hp(host.bot, target)[1] in (True, None):
        target = calm_target(host)
    near_target(shooter, watcher, shooter_id, host, target, report, tag+'_spell')
    report[tag+'_mana'] = report_line(shooter.bot, 'mana', 'MANA ')
    report[tag+'_ready_spell'] = report_line(shooter.bot, 'ready '+spell, 'READY ')
    hp1, xp1 = npc_hp(host.bot, target), xp_of(shooter.bot)
    report_line(shooter.bot, 'aimat '+target, 'TARGET ')
    report[tag+'_cast'] = report_line(shooter.bot, 'castat '+target, 'CASTAT ')[7:300]
    timeline = []
    until = time.monotonic() + 6.0
    while time.monotonic() < until:
        timeline.append({'t': round(6.0 - (until - time.monotonic()), 1), 'shooter': summons(shooter.bot), 'watcher': summons(watcher.bot)})
        time.sleep(0.2)
    report[tag+'_summons'] = timeline
    snap(watcher, tag+'-cast-seen', report)
    snap(shooter, tag+'-cast-self', report)
    time.sleep(3)
    report[tag+'_spell_result'] = {'target': npcs(host.bot).get(target, {}).get('name'), 'host_before': hp1, 'host_after': npc_hp(host.bot, target),
                                   'shooter_sees': npc_hp(shooter.bot, target), 'watcher_sees': npc_hp(watcher.bot, target),
                                   'shooter_xp': (xp1, xp_of(shooter.bot)), 'mana_after': report_line(shooter.bot, 'mana', 'MANA ')}


def summon_state(bot):
    start = bot.send('summonstate', 20)
    bot.line('SUMMONSTATE end', 10, 'summonstate', start)
    lines = bot.log()[start:].splitlines()
    head = next(l for l in lines if l.startswith('SUMMONSTATE ') and not l.startswith('SUMMONSTATE end'))
    return {k: int(v) for k, v in re.findall(r'(\w+)=(\d+)', head)}, [l[7:] for l in lines if l.startswith('SMNHIT ')]


def meteor_phase(caster, watcher, caster_id, host, report, tag):
    """One meteor of `caster` at a villager the host simulates, with `watcher` looking on."""
    keep = ('Diego', 'Gorn', 'Milten', 'Lester')
    living = {g: v for g, v in npcs(host.bot).items() if not v['dead'] and not v['mirrored'] and v['hp'] >= 100
              and 'Orc' not in v['name'] and 'Ork' not in v['name'] and v['name'] not in keep}
    assert living, 'no villager with 100 hit points near the host'
    target = next((g for g, v in living.items() if v['name'].startswith('Slave')), next(iter(living)))
    report[tag+'_target'] = (npcs(host.bot)[target]['name'], npcs(host.bot)[target]['hp'])
    clear_spot(caster, host, target, report, tag, 900)
    before = {c.tag: summon_state(c.bot)[0] for c in (caster, watcher)}
    xp0 = xp_of(caster.bot)
    report_line(caster.bot, 'mana', 'MANA ')
    spell = os.environ.get('G3_SPELL', 'It_Spell_Meteor')  # other area spells (Smn_ objects): It_Spell_Firerain ...
    report[tag+'_ready'] = report_line(caster.bot, 'ready '+spell, 'READY ')
    report_line(caster.bot, 'aimat '+target, 'TARGET ')
    report[tag+'_cast'] = report_line(caster.bot, 'castat '+target, 'CASTAT ')[7:300]
    copies_seen, own_seen = [], []
    until = time.monotonic() + 4.0
    while time.monotonic() < until:
        own_seen += [l for l in summons(caster.bot) if 'Smn_' in l]
        copies_seen += [l for l in summons(watcher.bot) if 'Smn_' in l]
        time.sleep(0.15)
    snap(watcher, tag+'-meteor-seen', report)
    time.sleep(3)
    after = {c.tag: summon_state(c.bot) for c in (caster, watcher)}
    report[tag+'_own_seen'] = sorted(set(own_seen))[:4]
    report[tag+'_copies_seen'] = sorted(set(copies_seen))[:4]
    report[tag+'_state'] = {t: {k: v - before[t].get(k, 0) for k, v in s[0].items()} for t, s in after.items()}
    # The copy's hits name the caster's puppet as owner; the watcher's own meteor of the other phase (owner_puppet_of=0)
    # may have hit one of the watcher's own NPCs, rightly applied (20.10., agent A).
    copy_hits = [h for h in after[watcher.tag][1] if ' applied=1' in h and 'source=Smn_' in h and 'owner_puppet_of=0 ' not in h]
    report[tag+'_watcher_hits'] = after[watcher.tag][1][-8:]
    report[tag+'_caster_hits'] = after[caster.tag][1][-8:]
    report[tag+'_result'] = {'host': npc_hp(host.bot, target), 'xp': (xp0, xp_of(caster.bot))}
    problems = []
    if report[tag+'_state'][watcher.tag].get('copies_bound', 0) < 1:
        problems.append('the watcher never had a copy of the meteor')
    elif not all(f'puppet_owner={caster_id} ' in l for l in report[tag+'_copies_seen']):
        problems.append('a copy did not belong to the caster\'s puppet')
    if copy_hits:
        problems.append(f'{len(copy_hits)} hits of the copy were applied on the watcher')
    hp_after, dead = npc_hp(host.bot, target)
    if hp_after is None or hp_after >= report[tag+'_target'][1]:
        problems.append('the target lost no hit points on the host')
    if dead and xp_of(caster.bot) <= xp0:
        problems.append('the target died, but the caster earned no experience')
    report[tag+'_problems'] = problems


def stageP1(copies, report, negative=False):
    """P1, summons, the meteor first (agent B): a player's meteor gets a net ID and the other machine replays it on that
    player's puppet. The copy shows (a Smn_Meteor owned by the puppet), its hits count nowhere (only the caster's
    meteor acts: the target dies on its owner's machine once, the caster earns the experience). Client casts, then
    host. Negative control: summoncopyhero (the copy belongs to the watcher's own hero, as a naive copy would, and
    its hits count there too)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    if negative:
        report['negative'] = True
        for c in (host, client):
            report_line(c.bot, 'summoncopyhero', 'COPYHEROOFF')
    meteor_phase(client, host, 2, host, report, 'client')
    meteor_phase(host, client, 1, host, report, 'host')
    problems = report['client_problems'] + report['host_problems']
    assert not problems, '; '.join(problems)


def shot_state(bot):
    start = bot.send('shotstate', 20)
    bot.line('SHOTSTATE end', 10, 'shotstate', start)
    lines = bot.log()[start:].splitlines()
    head = next(l for l in lines if l.startswith('SHOTSTATE ') and not l.startswith('SHOTSTATE end'))
    return {k: int(v) for k, v in re.findall(r'(\w+)=(\d+)', head)}, [l[9:] for l in lines if l.startswith('SHOTCOPY ')]


def trace_points(text):
    """'t:x_y_z,...' (LASTARROW trace=, SHOTCOPY path=) as (t, x, y, z), without 'gone'."""
    points = []
    for part in (text or '').strip(',').split(','):
        if ':' in part and 'gone' not in part:
            t, xyz = part.split(':')
            points.append((int(t), *map(float, xyz.split('_'))))
    return points


def flight(points, until=None):
    """Distance and unit direction from the first to the last sample (up to `until` ms)."""
    if until is not None:
        points = [p for p in points if p[0] <= until]
    if len(points) < 2:
        return 0.0, None
    d = [points[-1][i] - points[0][i] for i in (1, 2, 3)]
    length = math.sqrt(sum(v * v for v in d))
    return length, ([v / length for v in d] if length > 1 else None)


OpenGround = (90078, 5080, -12374)  # start village: open ground with a free line of fire towards the huts (stageP2)


def arrow_phase(shooter, watcher, shooter_id, host, report, tag, shots=3):
    """Arrows of `shooter` at a villager the host simulates, with `watcher` looking on (P2)."""
    start = shooter.bot.send('inventory', 20)
    shooter.bot.line('INV end', 10, 'inventory', start)
    lines = [l for l in shooter.bot.log()[start:].splitlines() if l.startswith('INV name=')]
    bow = next((re.search(r'name=(\S+)', l)[1] for l in lines if ' use=5 ' in l), None)
    assert bow, f'{tag} carries no bow'
    target = calm_target(host)
    report[tag+'_target'] = npcs(host.bot)[target]['name']
    clear_spot(shooter, host, target, report, tag, 600)
    # The watcher 6 m behind the shooter: its puppet stood at the shooter's elbow and stopped the arrows.
    at, goal = remotes(watcher.bot)[shooter_id]['pos'], npcs(host.bot)[target]['pos']
    away = [at[i] - goal[i] for i in (0, 2)]
    scale = 600 / max(1.0, math.hypot(*away))
    report[tag+'_watcher_moved'] = move_hero(watcher.bot, (at[0] + away[0] * scale, at[1], at[2] + away[1] * scale))
    time.sleep(2)
    before = {c.tag: shot_state(c.bot)[0] for c in (shooter, watcher)}
    hp0 = npc_hp(host.bot, target)
    results, good, tries = [], 0, 0
    mode = os.environ.get('G3_SHOOT_MODE', '7')
    # Up to 8 tries for `shots` arrows that fly free. In the cluttered start village an arrow often stops at the hand (a
    # stool, a palisade: seen around 91000,-8400) or, at some spots, vanishes the moment it is spawned; after such a try
    # the shooter moves 2 m (back, left or right of the line, in turn) before the next.
    while good < shots and tries < 8:
        tries += 1
        report_line(shooter.bot, 'ready '+bow, 'READY ')
        report_line(shooter.bot, 'aimat '+target, 'TARGET ')
        # From the idle stance the new arrow starts inside the hero's own body parts (Body_Player, Head_Player), which
        # Shoot does not exclude; modes 5 and 7 switch their collision off for the shot. Drawing first (G3_AIM_FIRST)
        # also avoids them, but the aiming state then shoots and reloads by itself now and then, and the hand is empty.
        aim = ''
        if os.environ.get('G3_AIM_FIRST'):
            aim = report_line(shooter.bot, 'act PS_Ranged-PS_Ranged_Aim', 'ACT ')
            time.sleep(2.0)
        # Mode 7: the script's own shot, aimed (instead of at the camera's screen centre: a bot steers no camera) over the
        # target and on; no arrow has hurt an NPC yet (see NOTES), so hitting is not the point.
        # Where the shooter and the target are now (the villager walks about: run 20261020-203211, 36 degrees off).
        from_pos, aim_pos = remotes(watcher.bot)[shooter_id]['pos'], npcs(host.bot)[target]['pos']
        shot = report_line(shooter.bot, f"shootat {target}-{mode}-{os.environ.get('G3_DRAW', '0')}", 'SHOOTAT ')
        time.sleep(1.8)
        own = report_line(shooter.bot, 'lastarrow', 'LASTARROW ')
        copies = shot_state(watcher.bot)[1]
        # Free: 3 m in the first 250 ms and, seen from above, towards the target (free arrows: 0-3 degrees off). An arrow
        # that glances off something at the hand also covers 3 m, but sideways (runs 20261020-192644 and -201623: 31
        # degrees off, its copy hit the same thing and stuck).
        own_len, own_dir = flight(trace_points((re.search(r'trace=(\S*)', own) or [None, ''])[1]), 250)
        towards = [aim_pos[i] - from_pos[i] for i in (0, 2)]
        flat = math.hypot(own_dir[0], own_dir[2]) if own_dir else 0.0
        bearing = (math.degrees(math.acos(max(-1.0, min(1.0, (own_dir[0] * towards[0] + own_dir[2] * towards[1]) /
                                                         (flat * max(1.0, math.hypot(*towards)))))))
                   if flat > 0.01 else 180.0)
        # Or, if not at the target, straight on at the same speed (run 20261020-203211: 17 m straight, 36 degrees off): the
        # first and the second ~110 ms point the same way and the second is not much shorter. In past runs clean flights
        # turned 1-3 degrees, those whose copy went elsewhere 7-9, ones that brushed something 42 and 50.
        points = [p for p in trace_points((re.search(r'trace=(\S*)', own) or [None, ''])[1]) if p[0] <= 250]
        bend, steady = 180.0, False
        if len(points) >= 3:
            legs = [[points[1][i] - points[0][i] for i in (1, 2, 3)], [points[2][i] - points[1][i] for i in (1, 2, 3)]]
            sizes = [math.sqrt(sum(v * v for v in leg)) for leg in legs]
            if min(sizes) > 1:
                bend = math.degrees(math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(*legs)) / (sizes[0] * sizes[1])))))
                steady = sizes[1] >= sizes[0] / 2
        free = ' spawned=1 ' in shot and own_len >= 300 and (bearing < 15 or (bend < 4 and steady))
        results.append({'aim': aim[4:200], 'shoot': shot[8:420], 'own': own[10:], 'copy': copies[-1] if copies else None, 'free': free,
                        'bearing': round(bearing), 'bend': round(bend)})
        if free:
            good += 1
            if good == 1:
                snap(watcher, tag+'-arrow-seen', report)
            continue
        at = remotes(watcher.bot)[shooter_id]['pos']
        back = [at[i] - goal[i] for i in (0, 2)]
        length = max(1.0, math.hypot(*back))
        back = [v / length for v in back]
        step = [(back[0], back[1]), (-back[1], back[0]), (back[1], -back[0])][tries % 3]
        # 2 m steps for the first three misses, then the open ground south-west of the huts (every stageP2 run that shot
        # from there passed; near Slave_04, around 91000,-8400 and 92000,-8900, almost every run had 8 misses, and at
        # 92086,-11632 arrows vanished as they were spawned), 2 m further each time; the aim stays the villager.
        if tries < 3:
            spot = (at[0] + step[0] * 200, at[1], at[2] + step[1] * 200)
        else:
            spot = (OpenGround[0] + step[0] * 200 * (tries - 3), OpenGround[1], OpenGround[2] + step[1] * 200 * (tries - 3))
        report.setdefault(tag+'_moved_for_shot', []).append(move_hero(shooter.bot, spot))
        time.sleep(1)
    time.sleep(1)
    after = {c.tag: shot_state(c.bot)[0] for c in (shooter, watcher)}
    report[tag+'_shots'] = results
    report[tag+'_state'] = {t: {k: v - before[t].get(k, 0) for k, v in s.items()} for t, s in after.items()}
    hits = summon_state(watcher.bot)[1]
    copy_hits = [h for h in hits if f'owner_puppet_of={shooter_id} ' in h and ' applied=1' in h and 'Smn_' not in h]
    report[tag+'_watcher_hits'] = hits[-8:]
    report[tag+'_shooter_hits'] = summon_state(shooter.bot)[1][-8:]
    report[tag+'_target_hp'] = {'before': hp0, 'after': npc_hp(host.bot, target)}
    problems = []
    own_state, watch_state = report[tag+'_state'][shooter.tag], report[tag+'_state'][watcher.tag]
    spawned = sum(' spawned=1 ' in r['shoot'] for r in results)
    # Every shot the hook saw is announced or given up (its arrow never showed: it vanished or stuck in its first frame),
    # every announcement reaches the watcher and is flown there.
    if own_state.get('shots', 0) != own_state.get('announced', 0) + own_state.get('unresolved', 0):
        problems.append(f'{tag}: shots not accounted for: {own_state}')
    if own_state.get('announced', 0) < spawned - own_state.get('unresolved', 0) or own_state.get('announced', 0) < good:
        problems.append(f"{tag}: {own_state.get('announced', 0)} announced of {spawned} arrows spawned")
    if watch_state.get('copies', 0) < own_state.get('announced', 0) or not watch_state.get('copies', 0):
        problems.append(f"{tag}: the watcher flew {watch_state.get('copies', 0)} copies of {own_state.get('announced', 0)} announced")
    compared = []
    for r in results:
        if not r['copy'] or not r['free']:
            continue
        if ' alive=1 ' in r['copy'] and f' owner_puppet_of={shooter_id} ' not in r['copy']:
            problems.append(f"{tag}: a copy did not belong to the shooter's puppet")
        # The free flight only: after the first touch the arrow and the copy bounce off slightly different spots.
        own_len, own_dir = flight(trace_points((re.search(r'trace=(\S*)', r['own']) or [None, ''])[1]), 250)
        copy_len, copy_dir = flight(trace_points((re.search(r'path=(\S*)', r['copy']) or [None, ''])[1]), 250)
        angle = None
        if own_dir and copy_dir:
            angle = math.degrees(math.acos(max(-1.0, min(1.0, sum(a * b for a, b in zip(own_dir, copy_dir))))))
        compared.append({'own_cm': round(own_len), 'copy_cm': round(copy_len), 'angle': None if angle is None else round(angle, 1)})
        if copy_len < own_len / 2 or angle is None or angle > 20:
            problems.append(f"{tag}: the copy did not fly like the arrow (own {own_len:.0f} cm, copy {copy_len:.0f} cm, angle {angle})")
    report[tag+'_compared'] = compared
    report[tag+'_tries'] = tries
    if good < shots:
        problems.append(f'{tag}: {good} of {shots} arrows flew free in {tries} tries (nothing more to compare)')
    if copy_hits:
        problems.append(f'{tag}: {len(copy_hits)} hits of the copies were applied on the watcher')
    report[tag+'_problems'] = problems


def touch_lines(bot):
    start = bot.send('touchlog', 20)
    bot.line('TOUCHLOG end', 10, 'touchlog', start)
    return [l[6:] for l in bot.log()[start:].splitlines() if l.startswith('TOUCH ')]


def arrowtouchdiag(copies, report, negative=False):
    """Experiment 0 (fable/20261020-112808-q-arrow-hits-question): which entities do arrows touch? Host, then client, shoot
    at a villager's chest (shootat mode 5) with the other player 6 m behind; after each shot, the gCProjectile_PS::OnTouch
    calls on both machines. A shot whose first touch is not the target (a chest, a house) is shot again from a spot 2 m
    aside or back, up to 6 times, until 3 arrows reached the target."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for c in (host, client):
        report[c.tag+'_touchlog'] = report_line(c.bot, 'touchlog', 'TOUCHLOG ')
    for shooter, watcher, shooter_id in ((host, client, 1), (client, host, 2)):
        tag = shooter.tag
        start = shooter.bot.send('inventory', 20)
        shooter.bot.line('INV end', 10, 'inventory', start)
        lines = [l for l in shooter.bot.log()[start:].splitlines() if l.startswith('INV name=')]
        bow = next((re.search(r'name=(\S+)', l)[1] for l in lines if ' use=5 ' in l), None)
        target = calm_target(host)
        name = npcs(host.bot)[target]['name']
        report[tag+'_target'] = name
        clear_spot(shooter, host, target, report, tag, 500)
        goal = npcs(host.bot)[target]['pos']

        def step(distance, sideways):
            at = remotes(watcher.bot)[shooter_id]['pos']
            back = [at[0] - goal[0], at[2] - goal[2]]
            length = max(1.0, math.hypot(*back))
            back = [v / length for v in back]
            d = (-back[1], back[0]) if sideways else back
            return (at[0] + d[0] * distance, at[1], at[2] + d[1] * distance)

        report[tag+'_watcher_moved'] = move_hero(watcher.bot, step(600, False))
        time.sleep(2)
        shots, reached, tries = [], 0, 0
        while reached < 3 and tries < 6:
            tries += 1
            since = max([int(re.search(r't=(\d+)', l)[1]) for l in touch_lines(shooter.bot)] or [0])
            report_line(shooter.bot, 'ready '+bow, 'READY ')
            report_line(shooter.bot, 'aimat '+target, 'TARGET ')
            shot = report_line(shooter.bot, f'shootat {target}-5', 'SHOOTAT ')
            time.sleep(2)
            touches = [l for l in touch_lines(shooter.bot) if int(re.search(r't=(\d+)', l)[1]) > since]
            first = next((l for l in touches if ' armed=1 ' in l), '')
            hit = f' touched={name} ' in first
            reached += hit
            shots.append({'shoot': shot[8:120], 'hp': npc_hp(host.bot, target), 'first': first[first.find('arrow='):], 'reached': hit,
                          'touches': [l[l.find('arrow='):] for l in touches[:6]],
                          'watcher_touches': [l[l.find('arrow='):] for l in touch_lines(watcher.bot)[-4:]]})
            if not hit:
                report.setdefault(tag+'_stepped', []).append(move_hero(shooter.bot, step(200, tries % 2 == 1)))
                time.sleep(1)
        report[tag+'_shots'] = shots
        report[tag+'_reached'] = (reached, tries)
        report[tag+'_hits'] = {c.tag: [h for h in summon_state(c.bot)[1] if 'Smn_' not in h][-6:] for c in (shooter, watcher)}
    report['status_note'] = 'diagnostic only'


def arrow_hit_phase(shooter, watcher, shooter_id, host, report, tag, wanted=1, spell=None, target=None, pin=None):
    """Arrows (or, with `spell`, spell projectiles cast with castat) of `shooter` at a sturdy NPC the host simulates
    until `wanted` of them touched it (QA touchlog), the other player 6 m behind. Each projectile that reached the
    target must be applied exactly once on the host."""
    weapon = spell or os.environ.get('G3_BOW')  # G3_BOW: another ranged weapon (It_Crossbow_01), created by ready
    if not weapon:
        start = shooter.bot.send('inventory', 20)
        shooter.bot.line('INV end', 10, 'inventory', start)
        lines = [l for l in shooter.bot.log()[start:].splitlines() if l.startswith('INV name=')]
        weapon = next((re.search(r'name=(\S+)', l)[1] for l in lines if ' use=5 ' in l), None)
        assert weapon, f'{tag} carries no bow'
    if target is None:
        keep = ('Diego', 'Gorn', 'Milten', 'Lester')
        sturdy = {g: v for g, v in npcs(host.bot).items() if not v['dead'] and not v['mirrored'] and v['hp'] >= 300
                  and 'Orc' not in v['name'] and v['name'] not in keep}
        assert sturdy, 'no villager with 300 hit points near the host'
        target = next(iter(sturdy))
    if pin:
        report[tag+'_pin_setup'] = pin()
    name = npcs(host.bot)[target]['name']
    report[tag+'_target'] = (name, npcs(host.bot)[target]['hp'])
    gap = 300 if pin else 250
    clear_spot(shooter, host, target, report, tag, gap)
    goal = npcs(host.bot)[target]['pos']
    at = remotes(watcher.bot)[shooter_id]['pos']
    bearing = math.atan2(at[2] - goal[2], at[0] - goal[0])

    def around(angle, distance):
        return (goal[0] + math.cos(angle) * distance, at[1], goal[2] + math.sin(angle) * distance)

    # The other player stays behind the shooter (its puppet would stop the arrows). P2hit starts on the +45 degree
    # lane that reached Hamlar in the diagnostic run, outside his automatic-dialog range.
    if pin:
        bearing += math.pi / 4
        report[tag+'_pinned_lane'] = move_hero(shooter.bot, around(bearing, gap))
    report[tag+'_watcher_moved'] = move_hero(watcher.bot, around(bearing, 900 if pin else 850))
    time.sleep(2)

    def end_dialogs():
        # A villager the shooter comes close to may start a dialog of his own (Hamlar's greeting FILLER65 at the
        # game's start); an NPC in a dialog takes no damage, so the shot would count as a miss of the mirroring.
        dialogs = {}
        for copy in (shooter, watcher):
            state = talkstate(copy.bot)
            if state.get('running') == '1':
                dialogs[copy.tag+'_before'] = state
                start = copy.bot.send('endtalk', 20)
                dialogs[copy.tag+'_end'] = copy.bot.line('TALKSTATE ', 10, 'endtalk', start)
                wait_for(lambda c=copy: talkstate(c.bot).get('running') == '0', 10, copy.tag+' accidental dialog ends')
        wait_for(lambda: (row := npcs(host.bot).get(target)) and row['task'] != 'ZS_Talk' and [row], 10,
                 tag+' target leaves accidental dialog')
        if dialogs:
            report.setdefault(tag+'_dialogs', []).append(dialogs)

    end_dialogs()
    if pin:
        if os.environ.get('G3_P2_FIXTURE_ONLY') == '1':
            report[tag+'_fixture_only_pin'] = pin()
            time.sleep(3)
            report[tag+'_fixture_only_stable'] = pin()
            return
    # Any source: a client's arrow reaches the host as a claim, applied there by the combat filter.
    applied_before = [h for h in summon_state(host.bot)[1] if f'victim={name} ' in h and ' applied=1' in h]
    shots, reached, tries = [], 0, 0
    while reached < wanted and tries < 8:
        tries += 1
        if pin:
            report.setdefault(tag+'_pins', []).append(pin())
        end_dialogs()
        since = max([int(re.search(r't=(\d+)', l)[1]) for l in touch_lines(shooter.bot)] or [0])
        report_line(shooter.bot, 'ready '+weapon, 'READY ')
        report_line(shooter.bot, 'aimat '+target, 'TARGET ')
        if spell:
            shot = report_line(shooter.bot, 'castat '+target, 'CASTAT ')
        else:
            # P2hit aims at the head (mode 8): aimed at the chest, its arrows fell short into the ground or Slave_02 lying
            # in front of Hamlar (25.10.).
            shot = report_line(shooter.bot, f"shootat {target}-{os.environ.get('G3_SHOT_MODE', '8' if pin else '5')}", 'SHOOTAT ')
        time.sleep(2.5)
        touches = [l for l in touch_lines(shooter.bot) if int(re.search(r't=(\d+)', l)[1]) > since]
        # Reached: an armed touch of the target, also after a bounce (the first touch can be the ground, the arrow armed on).
        first = next((l for l in touches if ' armed=1 ' in l and f' touched={name} ' in l), next((l for l in touches if ' armed=1 ' in l), ''))
        hit = f' touched={name} ' in first
        reached += hit
        shots.append({'shoot': shot[8:120], 'first': first[first.find('arrow='):], 'reached': hit,
                      'watcher_touches': [l[l.find('arrow='):] for l in touch_lines(watcher.bot)[-2:]]})
        if not hit:
            report.setdefault(tag+'_stepped', []).append(move_hero(shooter.bot, around(bearing + tries * math.pi / 4, gap)))
            move_hero(watcher.bot, around(bearing + tries * math.pi / 4, 900 if pin else 850))
            time.sleep(1.5)
    time.sleep(2)
    host_hits = [h for h in summon_state(host.bot)[1] if f'victim={name} ' in h and ' applied=1' in h]
    new_hits = [h for h in host_hits if h not in applied_before]
    # The fixed P2hit fixture counts the hits of players' arrows: the host's own arrow, and a client's claim (applied on
    # the host with that player's puppet as the source; a copy that hits a second time shows up the same way). Other
    # callers retain the established all-applied-hits behaviour (spell projectiles have their own source entities).
    correlated = [h for h in new_hits if not pin or re.search(r' source=(Arrow|PC_Hero|G3T_Player\d+) ', h)]
    report[tag+'_shots'] = shots
    report[tag+'_reached'] = (reached, tries)
    report[tag+'_host_hits'] = correlated
    if pin:
        report[tag+'_other_hits'] = [h for h in new_hits if h not in correlated]
    report[tag+'_watcher_hits'] = [h for h in summon_state(watcher.bot)[1] if 'source=Arrow' in h][-4:]
    report[tag+'_target_after'] = npc_hp(host.bot, target)
    problems = []
    if reached < wanted:
        problems.append(f'{tag}: only {reached} of {wanted} arrows reached {name} in {tries} tries')
    if len(correlated) != reached:
        qualifier = ' (not evaluable: correlated SMNHIT evidence missing)' if reached and len(correlated) < reached else ''
        problems.append(f'{tag}: {len(correlated)} arrow hits applied on the host for {reached} arrows that reached {name}{qualifier}')
    report[tag+'_problems'] = problems


def stageP2fire(copies, report, negative=False):
    """P2, spell projectiles (agent B), the fireball first: MagicProjectile is wrapped in the script table, the projectile
    announced like an arrow with the item cast from; the copy on the other machine belongs to the caster's puppet and
    carries the spell. Each fireball that reaches a sturdy NPC is applied once on the host. Host casts, then client.
    Negative control: shotcopyhero. G3_SPELL picks the spell (default It_Spell_Fireball)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    for c in (host, client):
        report[c.tag+'_touchlog'] = report_line(c.bot, 'touchlog', 'TOUCHLOG ')
        if negative:
            report_line(c.bot, 'shotcopyhero', 'SHOTCOPYHERO')
    report['negative'] = negative
    spell = os.environ.get('G3_SPELL', 'It_Spell_Fireball')
    before = {c.tag: shot_state(c.bot)[0] for c in (host, client)}
    arrow_hit_phase(host, client, 1, host, report, 'host', spell=spell)
    arrow_hit_phase(client, host, 2, host, report, 'client', spell=spell)
    after = {c.tag: shot_state(c.bot) for c in (host, client)}
    report['shotstate'] = {t: {k: v - before[t].get(k, 0) for k, v in s[0].items()} for t, s in after.items()}
    report['copies'] = {t: s[1][-2:] for t, s in after.items()}
    problems = report['host_problems'] + report['client_problems']
    for t in ('host', 'client'):
        if report['shotstate'][t].get('no_spell', 0):
            problems.append(f'{t}: a copy got no spell')
        if not report['shotstate'][t].get('copies', 0):
            problems.append(f'{t}: no copy flew on this machine')
    assert not problems, '; '.join(problems)


def raiddiag(copies, report, negative=False):
    """The Entity::AttachTo crash (Script.dll+0x13ea3) seen while killraiders ended the raid (17.10., 18.10., 20.10.):
    join, end the raid, nothing else; with QA crashlog any access violation is in g3together.log (CRASH lines)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    report['crash_lines'] = {c.tag: [l for l in c.mod_log().splitlines() if ' CRASH ' in l][:6] for c in (host, client)}
    report['status_note'] = 'diagnostic only'


def stageRaid(copies, report, negative=False):
    """The AttachTo crash in the raid (Script.dll+0x13ea3 from GetCombatMoveLength, ~1 s after an orc died; 20.10.
    22:56): ai.cpp hands the script an empty wrapper for the fight target, which it never reads
    (re/findings/attachto-guard-c.md). Join, fight the raid to its end; no CRASH line on either side, and the wrapper
    took every call. Negative control: movelengthoff after the join (the later calls reach the original, which binds
    the target again: sanitized < calls; the crash is rare, so its absence is not the control)."""
    host, client = join_both(copies)
    if negative:
        report['negative'] = True
        for c in (host, client):
            report_line(c.bot, 'movelengthoff', 'MOVELENGTHOFF')
    clear_raid(host, report)
    time.sleep(5)
    lengths = {c.tag: dict(re.findall(r'(\w+)=(\d+)', report_line(c.bot, 'movelength', 'MOVELENGTH '))) for c in (host, client)}
    report['movelength'] = lengths
    report['crash_lines'] = crashes = {c.tag: [l for l in c.mod_log().splitlines() if ' CRASH ' in l][:6] for c in (host, client)}
    problems = [f'{tag} logged a crash' for tag, lines in crashes.items() if lines]
    if not any(int(v.get('calls', 0)) for v in lengths.values()):
        problems.append('GetCombatMoveLength was never called in the raid (no fight to check)')
    for tag, v in lengths.items():
        if v.get('wrapped') != '1':
            problems.append(f'{tag}: GetCombatMoveLength not wrapped')
        elif v.get('sanitized') != v.get('calls'):  # negative control: the calls after movelengthoff reached the original
            problems.append(f"{tag}: sanitized={v.get('sanitized')} of calls={v.get('calls')}")
    assert not problems, '; '.join(problems)


def stageNetOutage(copies, report, negative=False):
    """A bad internet link and outages (Hamachi, first human test 21.10.), through the chaos box (G3_CHAOS, e.g.
    bad_wifi). Join and fight the raid on that link; then 5 s with nothing through, 20 s (longer than the 15 s after
    which a puppet is taken away; the connection itself lasts 60 s: hamachi-net-c.md) and 8 s one way (the client hears
    nothing). After each, both sides must show the other's puppet with fresh state again, and the client's teleport must
    reach the host's puppet within 3 m. Negative control: the link stays cut."""
    assert NETLINK or CHAOS, 'run with G3_NETSIM=delay,jitter,loss (or G3_CHAOS=<profile> and G3_CHAOS_DIR)'

    def cut(direction, seconds):
        if NETLINK:
            NETLINK.cut({'s2c': 'down', 'c2s': 'up'}.get(direction, direction), seconds)
        elif seconds == float('inf'):
            CHAOS.partition(direction, True)
        elif direction == 'both':
            CHAOS.disconnect(seconds * 1000)
        else:
            CHAOS.partition(direction, True, seconds * 1000)
    host, client = join_both(copies)
    clear_raid(host, report)
    spot = [90078.0, 5080.0, -12374.0]  # open ground in the start village (stageP2)

    def recovered(tag, seconds):
        def fresh():
            on_host, on_client = remotes(host.bot).get(2, {}), remotes(client.bot).get(1, {})
            return on_host.get('puppet') and on_client.get('puppet') and on_host['age_ms'] < 1500 \
                and on_client['age_ms'] < 1500 and distance(on_host['puppet_pos'], on_host['pos']) < 300 and [on_host]
        start = time.monotonic()
        try:
            state = wait_for(fresh, seconds, f'{tag}: both puppets back with fresh state')
        finally:
            report[tag+'_seconds'] = round(time.monotonic() - start, 1)
        report[tag+'_host_view'] = {k: state[0][k] for k in ('age_ms', 'pos', 'puppet_pos')}

    for tag, outage in (('down5', 'both 5000'), ('down20', 'both 20000'), ('oneway8', 's2c 8000')):
        direction, ms = outage.split()
        if negative and tag == 'down5':
            report['negative'] = True
            cut('both', float('inf'))
        else:
            cut(direction, float(ms) / 1000)
        spot[0] += 500
        report[tag+'_teleport'] = move_hero(client.bot, spot)
        time.sleep(float(ms) / 1000)
        recovered(tag, 40)
    report['wifi_after'] = swings_seen(client, host, 2, report, 'wifi', count=6)


def selfspelldiag(copies, report, negative=False):
    """Spells on oneself (agent B, inventory 21.10.: MagicHeal/MagicCure, MagicRefresh, MagicTeleport were open): each
    player casts Healing at 40 hit points, Refresh after spending mana, and the teleport stone to Lago. What does the
    caster get, and what does the other machine show (its own hero untouched; the puppet at the new place)?"""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    for caster, other, other_id, caster_id in ((client, host, 1, 2), (host, client, 2, 1)):
        tag = caster.tag
        guid = re.search(r'guid=(\S+)', report_line(caster.bot, f'puppetguid {other_id}', 'PUPPET '))[1]
        other_before = combat_state(other.bot)
        report_line(caster.bot, 'sethp 40', 'SETHP ')
        report_line(caster.bot, 'mana', 'MANA ')
        for spell in ('It_Spell_Healing', 'It_Spell_Heal', 'It_Spell_Refresh'):
            report_line(caster.bot, 'sethp 40', 'SETHP ')
            report[f'{tag}_{spell}_ready'] = report_line(caster.bot, 'ready '+spell, 'READY ')[6:200]
            report[f'{tag}_{spell}_cast'] = report_line(caster.bot, 'castat '+guid, 'CASTAT ')[7:300]
            time.sleep(2)
            report[f'{tag}_{spell}_hp'] = combat_state(caster.bot)
        report[tag+'_other_hp'] = (other_before, combat_state(other.bot))
        seen_before = remotes(other.bot).get(caster_id, {})
        report[tag+'_teleport_ready'] = report_line(caster.bot, 'ready It_Teleport_Lago', 'READY ')[6:200]
        report[tag+'_teleport_cast'] = report_line(caster.bot, 'castat '+guid, 'CASTAT ')[7:300]
        time.sleep(10)
        seen_after = remotes(other.bot).get(caster_id, {})
        report[tag+'_teleport_seen'] = {when: {k: seen.get(k) for k in ('pos', 'puppet', 'puppet_pos', 'same_world', 'age_ms')}
                                       for when, seen in (('before', seen_before), ('after', seen_after))}
        if seen_before.get('pos'):
            move_hero(caster.bot, seen_before['pos'])  # back to the other player for the next caster
            time.sleep(5)
    report['status_note'] = 'diagnostic only'


def animalscandiag(copies, report, negative=False):
    """Are the world's animals the same on both machines (agent B, 21.10.)? farnpcs lists only people, and an animal the
    host spawns does not exist on the client (callanimalsdiag), so animals may come from spawn points on each machine.
    Both players go to a few places away from the village (next to people farnpcs lists); at each, every NPC within
    reach on both machines is listed. Compared: names, GUIDs, who owns or mirrors them. G3_SPOTS: how many places."""
    host, client = join_both(copies)
    clear_raid(host, report)
    rows = far_rows(host.bot)
    spots = []
    for r in sorted(rows, key=lambda r: r['distance']):
        if r['distance'] > 300 and all(distance(r['pos'], s['pos']) > 20000 for s in spots):
            spots.append(r)
        if len(spots) >= int(os.environ.get('G3_SPOTS', '5')):
            break
    people = ('Slave', 'Orc', 'Rebel', 'Hunter', 'Lumberjack', 'Guard', 'Mercenary', 'Nomad', 'Assassin', 'Paladin',
              'Citizen', 'Farmer', 'Bandit', 'Pirate', 'Fire', 'Water', 'Mage', 'Wolfclan', 'Hammerclan', 'Boss')
    places = []
    for spot in spots:
        at = spot['pos']
        jumps = {c.tag: teleport_to(c.bot, spot['name'])[:120] for c in (host, client)}  # as stageOpenLocks (Lago)
        time.sleep(int(os.environ.get('G3_SCAN_WAIT', '20')))
        lists = {c.tag: npcs(c.bot) for c in (host, client)}
        named = jumps
        animals = {tag: {g: v for g, v in found.items() if not any(p in v['name'] for p in people)} for tag, found in lists.items()}
        both = set(animals['host']) & set(animals['client'])
        place = {'near': spot['name'], 'pos': [round(v) for v in at], 'teleportto': named,
                 'all_npcs': {tag: len(found) for tag, found in lists.items()},
                 'animals_host': sorted(f"{v['name']}{'(m)' if v['mirrored'] else ''}" for v in animals['host'].values())[:15],
                 'animals_client': sorted(f"{v['name']}{'(m)' if v['mirrored'] else ''}" for v in animals['client'].values())[:15],
                 'same_guid': len(both), 'host_only': len(set(animals['host']) - both), 'client_only': len(set(animals['client']) - both)}
        places.append(place)
    report['places'] = places
    report['status_note'] = 'diagnostic only'


def callanimals2diag(copies, report, negative=False):
    """Call Animals on world animals (animalscandiag 21.10.: Goose and Lizard at Montera, Pig at Gamal have the same GUID
    on both machines). Both players go to G3_SPOT (Montera_Slave_01); each casts in turn at G3_TARGET (Merc_03). After
    each cast, every animal's attack reason (16 = called) and task is read on both machines, with who owns it."""
    host, client = join_both(copies)
    clear_raid(host, report)
    spot = os.environ.get('G3_SPOT', 'Montera_Slave_01')
    for c in (host, client):
        report[c.tag+'_jump'] = teleport_to(c.bot, spot)[:120]
    time.sleep(20)
    kinds = os.environ.get('G3_ANIMAL_NAMES', 'Goose,Lizard,Pig,Chicken,Scavenger,Wolf,Boar,Sheep,Rabbit,Deer,Bison').split(',')
    found = npcs(host.bot)
    animals = {g: v['name'] for g, v in found.items() if any(v['name'].startswith(k) for k in kinds) and not v['dead']}
    target = next((g for g, v in found.items() if v['name'] == os.environ.get('G3_TARGET', 'Merc_03')), None)
    report['animals'] = {g: n for g, n in animals.items()}
    assert animals and target, f'animals {list(animals.values())}, target {target}'

    def state():
        rows = {}
        for g, name in animals.items():
            row = {}
            for c in (host, client):
                memory = report_line(c.bot, 'npcmemory '+g, 'NPCMEMORY ')
                n = npcs(c.bot).get(g, {})
                row[c.tag] = f"reason={re.search(r'attack_reason=(-?\d+)', memory)[1] if 'attack_reason=' in memory else '?'} " \
                             f"task={n.get('task')} mirrored={n.get('mirrored')}"
            rows[name+'_'+g[:8]] = row
        return rows
    report['before'] = state()
    for caster in (client, host):
        tag = caster.tag
        report_line(caster.bot, 'mana', 'MANA ')
        report[tag+'_ready'] = report_line(caster.bot, 'ready It_Spell_CallAnimals', 'READY ')[6:120]
        report[tag+'_cast'] = report_line(caster.bot, 'castat '+target, 'CASTAT ')[7:220]
        time.sleep(4)
        report[tag+'_after'] = state()
    report['status_note'] = 'diagnostic only'


def stageCallAnimals(copies, report, negative=False):
    """Call Animals by the client (callanimals2diag 21.10.: the client's cast found only mirrors and called nothing; the
    host's called a ripper). spells.cpp sends the client's cast with its target to the host, which runs it round the
    client's puppet. At Kent (G3_SPOT; scavenger, deer, rippers the host simulates), the client casts at Kent: on the host
    an animal must get attack reason 16 (called) within 6 s. Negative control: spellmirroroff (the cast stays on the
    client)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    spot = os.environ.get('G3_SPOT', 'Kent')
    for c in (host, client):
        report[c.tag+'_jump'] = teleport_to(c.bot, spot)[:120]
    time.sleep(20)
    if negative:
        report['negative'] = True
        for c in (host, client):
            report_line(c.bot, 'spellmirroroff', 'SPELLMIRROROFF')
    kinds = ('Scavenger', 'Deer', 'Okara_Ripper', 'Ripper', 'Wolf', 'Boar')
    found = npcs(host.bot)
    animals = [g for g, v in found.items() if v['name'].startswith(kinds) and not v['dead']]
    target = next((g for g, v in found.items() if v['name'] == os.environ.get('G3_TARGET', spot)), None)
    report['animals'] = [found[g]['name'] for g in animals]
    assert animals and target, f"animals {report['animals']}, target {target}"
    report_line(client.bot, 'mana', 'MANA ')
    report['ready'] = report_line(client.bot, 'ready It_Spell_CallAnimals', 'READY ')[6:120]
    report['cast'] = report_line(client.bot, 'castat '+target, 'CASTAT ')[7:220]

    def called():
        hits = [found[g]['name'] for g in animals
                if 'attack_reason=16 ' in report_line(host.bot, 'npcmemory '+g, 'NPCMEMORY ')]
        return hits
    hits = []
    until = time.monotonic() + 6
    while time.monotonic() < until and not hits:
        time.sleep(1)
        hits = called()
    report['called_on_host'] = hits
    report['spellstate'] = {c.tag: report_line(c.bot, 'spellstate', 'SPELLSTATE ')[11:300] for c in (host, client)}
    if negative:
        assert not hits, f'animals called on the host although the cast stayed on the client: {hits}'
    else:
        assert hits, "the client's Call Animals called no animal on the host"


def callanimalsdiag(copies, report, negative=False):
    """Call Animals (MagicCallAnimals, re/findings/summon-calls-b.md: no spawn; up to 3 animals within reach get attack
    reason 0x10 and go for the spell's target). The host spawns an animal next to itself (so the host owns it), the
    caster stands by and casts at a villager the host simulates; the host's view of the animal (attack reason, task) is
    sampled. Host casts first (the game's own path), then the client with a fresh animal: does the client's cast reach
    the host's animal? G3_ANIMALS: templates to try (first valid)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    target = calm_target(host)
    report['target'] = npcs(host.bot)[target]['name']
    for caster in (host, client):
        tag = caster.tag
        at = remotes(client.bot)[1]['pos']
        move_hero(client.bot, (at[0] + 150, at[1], at[2]))
        time.sleep(2)
        animal = None
        for name in os.environ.get('G3_ANIMALS', 'Scavenger,Boar,Wolf,Sheep').split(','):
            line = report_line(host.bot, 'spawnnpc '+name, 'SPAWNNPC ')
            if 'guid=' in line:
                animal = re.search(r'guid=(\S+)', line)[1]
                report[tag+'_animal'] = line[9:120]
                break
        assert animal, 'no animal template could be spawned'
        time.sleep(3)
        report[tag+'_before'] = report_line(host.bot, 'npcmemory '+animal, 'NPCMEMORY ')[10:160]
        report_line(caster.bot, 'mana', 'MANA ')
        report[tag+'_ready'] = report_line(caster.bot, 'ready It_Spell_CallAnimals', 'READY ')[6:120]
        report[tag+'_cast'] = report_line(caster.bot, 'castat '+target, 'CASTAT ')[7:220]
        samples = []
        for _ in range(6):
            time.sleep(1)
            memory = report_line(host.bot, 'npcmemory '+animal, 'NPCMEMORY ')
            on_host = npcs(host.bot).get(animal, {})
            on_client = npcs(client.bot).get(animal, {})
            samples.append(f"reason={re.search(r'attack_reason=(-?\d+)', memory)[1] if 'attack_reason=' in memory else '?'} "
                           f"host_task={on_host.get('task')} client_task={on_client.get('task')} client_mirrored={on_client.get('mirrored')}")
        report[tag+'_samples'] = samples
        report[tag+'_called'] = any(s.startswith('reason=16 ') for s in samples)
    report['status_note'] = 'diagnostic only'


def stageOpenLocks(copies, report, negative=False):
    """The spell Open Locks (MagicOpenLocks: a locked lock with difficulty up to 4 becomes unlocked, the door stays shut;
    re/findings/npc-spells-c.md) on the prison door in Lago, cast by G3_WHO (client by default). doors.cpp watches every
    lock (doors and chests) and sends a change, so the other machine must see it unlocked too. Negative control:
    doorsyncoff on both (the lock stays shut over there)."""
    host, client = join_both(copies)
    clear_raid(host, report)  # no raid needed: starts from the post-raid save (25.10.)
    if negative:
        report['negative'] = True
        for c in (host, client):
            assert 'ok=1' in report_line(c.bot, 'doorsyncoff', 'DOORSYNCOFF ')
    spot = os.environ.get('G3_DOOR_AT', 'Lago_Prison_Guard')
    for c in (host, client):
        report[c.tag+'_teleport'] = report_line(c.bot, 'teleportto '+spot, ('TELEPORTTO ', 'REFUSED teleportto'))
    time.sleep(8)
    caster, other = (host, client) if os.environ.get('G3_WHO') == 'host' else (client, host)
    # The prison door (difficulty 0 and a key) is refused by the spell (21.10.); take the nearest lock it may open: locked,
    # difficulty 1 to 4 (a key does not matter then: npc-spells-c.md).
    start = caster.bot.send('locks', 20)
    caster.bot.line('LOCKS end', 10, 'locks', start)
    locks = [dict(re.findall(r'(\w+)=(\S+)', l)) for l in caster.bot.log()[start:].splitlines() if l.startswith('LOCK ')]
    report['locks'] = [f"{l['name']} d={l['distance']} locked={l['locked']} difficulty={l['difficulty']} key={l['key']}" for l in locks][:20]
    fit = sorted((l for l in locks if l['locked'] == '1' and 1 <= int(l['difficulty']) <= 4),
                 key=lambda l: float(l['distance']))
    door = os.environ.get('G3_DOOR') or (fit[0]['name'] if fit else None)
    assert door, 'no lock here the spell may open'
    report['target'] = door
    report['before'] = {c.tag: use_info(c.bot, door) for c in (host, client)}
    assert ' locked=1 ' in report['before'][caster.tag], 'the door is not locked to begin with: ' + report['before'][caster.tag]
    report_line(caster.bot, 'mana', 'MANA ')
    report['ready'] = report_line(caster.bot, 'ready It_Spell_OpenLocks', 'READY ')[6:160]
    report['cast'] = report_line(caster.bot, 'castat '+door, ('CASTAT ', 'CASTAT refused'))[7:260]
    time.sleep(4)
    report['after'] = after = {c.tag: use_info(c.bot, door) for c in (host, client)}
    report['doors'] = {c.tag: report_line(c.bot, 'doors', 'DOORS ') for c in (host, client)}
    assert ' locked=0 ' in after[caster.tag], 'the spell did not unlock it here: ' + report['cast'] + ' / ' + after[caster.tag][:200]
    seen = ' locked=0 ' in after[other.tag]
    if negative:
        assert not seen, 'unlocked on the other machine although door sync is off'
    else:
        assert seen, 'still locked on the other machine: ' + after[other.tag][:200]


def stageSelfSpell(copies, report, negative=False):
    """Spells on oneself, both players in turn (selfspelldiag 21.10.: no mod code involved, the caster's machine runs
    them on its own hero): Healing (Spl_Cure) fills the caster from 40 to its maximum and leaves the other player, also
    at 40, alone; the teleport stone to Lago moves the caster there and the other machine shows its puppet in Lago
    within 15 s. Refresh (MagicRefresh) cannot be tested: It_Spell_Refresh, It_Scroll_Refresh and It_Spell_Heal are in
    Templates.pak but the game loads no such template (READY valid=0). No negative control: nothing of the mod to switch off."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    problems = []
    for caster, other, other_id, caster_id in ((client, host, 1, 2), (host, client, 2, 1)):
        tag = caster.tag
        guid = re.search(r'guid=(\S+)', report_line(caster.bot, f'puppetguid {other_id}', 'PUPPET '))[1]
        report_line(caster.bot, 'mana', 'MANA ')
        for copy in (caster, other):
            report_line(copy.bot, 'sethp 40', 'SETHP ')
        report[tag+'_heal'] = report_line(caster.bot, 'ready It_Spell_Healing', 'READY ')[6:120], \
            report_line(caster.bot, 'castat '+guid, 'CASTAT ')[7:200]
        time.sleep(2)
        mine, theirs = combat_state(caster.bot), combat_state(other.bot)
        report[tag+'_heal_hp'] = {'caster': (mine.get('hero'), mine.get('max')), 'other': theirs.get('hero')}
        if mine.get('hero') != mine.get('max'):
            problems.append(f"{tag} healed itself to {mine.get('hero')} of {mine.get('max')}")
        if theirs.get('hero') != 40:
            problems.append(f"{tag}'s healing changed the other player: {theirs.get('hero')} instead of 40")
        report_line(other.bot, 'sethp 200', 'SETHP ')
        seen_before = remotes(other.bot).get(caster_id, {})
        report[tag+'_teleport'] = report_line(caster.bot, 'ready It_Teleport_Lago', 'READY ')[6:120], \
            report_line(caster.bot, 'castat '+guid, 'CASTAT ')[7:200]
        def arrived():
            seen = remotes(other.bot).get(caster_id, {})
            return seen.get('puppet') and seen_before.get('pos') and distance(seen['pos'], seen_before['pos']) > 10000 \
                and distance(seen['puppet_pos'], seen['pos']) < 500 and [seen]
        try:
            seen = wait_for(arrived, 15, f"{other.tag} shows {tag}'s puppet in Lago")[0]
            report[tag+'_teleport_seen'] = {k: seen[k] for k in ('pos', 'puppet_pos', 'age_ms')}
        except AssertionError as error:
            problems.append(str(error))
        if seen_before.get('pos'):
            move_hero(caster.bot, seen_before['pos'])
            time.sleep(5)
    assert not problems, '; '.join(problems)


def stageHealOther(copies, report, negative=False):
    """Heal other (agent B, inventory list): on the caster's machine the target is the other player's puppet, and
    MagicHealOther sets only the puppet's hit points (re/findings/spells-b.md). spells.cpp sends the cast to the puppet's
    player, whose machine replays the script on its own hero: that hero is fully healed. Client heals the host, then the
    host heals the client. Negative control: spellmirroroff (the cast is not sent, the hero stays hurt)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    if negative:
        report['negative'] = True
        for c in (host, client):
            report_line(c.bot, 'spellmirroroff', 'SPELLMIRROROFF')
    problems = []
    for caster, target, target_id in ((client, host, 1), (host, client, 2)):
        tag = caster.tag
        report[tag+'_hurt'] = report_line(target.bot, 'sethp 40', 'SETHP ')
        guid = report_line(caster.bot, f'puppetguid {target_id}', 'PUPPET ')
        report[tag+'_puppet'] = guid
        guid = re.search(r'guid=(\S+)', guid)[1]
        report[tag+'_ready'] = report_line(caster.bot, 'ready It_Spell_HealOther', 'READY ')
        report_line(caster.bot, 'aimat '+guid, 'TARGET ')
        report[tag+'_cast'] = report_line(caster.bot, 'castat '+guid, 'CASTAT ')[7:300]
        time.sleep(2)
        hp = combat_state(target.bot)
        report[tag+'_target_hp'] = (hp.get('hero'), hp.get('max'))
        report[tag+'_spellstate'] = {c.tag: report_line(c.bot, 'spellstate', 'SPELLSTATE ') for c in (caster, target)}
        if hp.get('hero') != hp.get('max'):
            problems.append(f"{tag} healed the other player: hero at {hp.get('hero')} of {hp.get('max')}")
    assert not problems, '; '.join(problems)


def stageNpcSpell(copies, report, negative=False):
    """Spells on NPCs (agent B, after re/findings/npc-spells-c.md): the client casts at a villager the host simulates;
    spells.cpp sends the cast to the host, which runs the game's script there, cast by the client's puppet, so the
    real NPC takes the spell's task (sleep: ZS_MagicSleep, fear: ZS_Flee; charm wipes its memory). Then the host casts at
    another villager (its own NPC, the game's own path). G3_SPELL picks the spell (default It_Spell_Sleep). Negative
    control: spellmirroroff (the client's cast stays on its mirror; the host's NPC goes on as before)."""
    # No spell item runs MagicStun or MagicHypnosis (spellfuncdiag 22.10., all 42 It_Spell_/It_Scroll_ templates): there is
    # no It_Spell_Stun or It_Spell_Hypnosis, and runs with them never cast (plan 3.6).
    expected = {'It_Spell_Sleep': 'ZS_MagicSleep', 'It_Spell_Fear': 'ZS_Flee', 'It_Spell_Charm': 'memory', 'It_Spell_Bloodlust': 'ZS_Attack'}
    spell = os.environ.get('G3_SPELL', 'It_Spell_Sleep')
    task = expected.get(spell, os.environ.get('G3_TASK', ''))
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(5)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    if negative:
        report['negative'] = True
        for c in (host, client):
            report_line(c.bot, 'spellmirroroff', 'SPELLMIRROROFF')
    problems, used = [], set()
    for caster in (client, host):
        tag = caster.tag
        keep = ('Diego', 'Gorn', 'Milten', 'Lester')
        calm = [g for g, v in npcs(host.bot).items() if not v['dead'] and not v['mirrored'] and 'Orc' not in v['name']
                and v['name'] not in keep and g not in used and v['task'] != task]
        assert calm, 'no calm villager near the host'
        target = next((g for g in calm if npcs(host.bot)[g]['name'].startswith('Slave')), calm[0])
        used.add(target)
        name = npcs(host.bot)[target]['name']
        goal = npcs(host.bot)[target]['pos']
        report[tag+'_moved'] = move_hero(caster.bot, (goal[0] + 300, goal[1], goal[2]))
        time.sleep(2)
        report[tag+'_target'] = (name, npcs(host.bot)[target]['task'])
        if task == 'memory':  # charm: a grudge on the host first, which the spell must wipe there
            report_line(host.bot, 'npcgrudge '+target, 'NPCGRUDGE ')
            report[tag+'_memory_before'] = report_line(host.bot, 'npcmemory '+target, 'NPCMEMORY ')
        report[tag+'_ready'] = report_line(caster.bot, 'ready '+spell, 'READY ')
        assert 'valid=0' not in report[tag+'_ready'], f'no spell item {spell}'
        report_line(caster.bot, 'aimat '+target, 'TARGET ')
        report[tag+'_cast'] = report_line(caster.bot, 'castat '+target, 'CASTAT ')[7:300]
        seen = []
        until = time.monotonic() + 4
        while time.monotonic() < until:
            seen.append(npcs(host.bot).get(target, {}).get('task'))
            time.sleep(0.4)
        report[tag+'_host_tasks'] = seen
        if task == 'memory':
            memory = report_line(host.bot, 'npcmemory '+target, 'NPCMEMORY ')
            report[tag+'_memory_after'] = memory
            # The game recomputes the attitude itself (1, friendly, after the charm); the remembered fight, crime and
            # attack reason are what MagicCharm wipes.
            values = {k: int(v) for k, v in re.findall(r'(attitude|last_player_ar|last_crime|last_fight)=(\d+)', memory)}
            wiped = values and not any(values.get(k) for k in ('last_player_ar', 'last_crime', 'last_fight')) and values.get('attitude') != 3
            seen = ['memory'] if wiped else seen
        report[tag+'_spellstate'] = {c.tag: report_line(c.bot, 'spellstate', 'SPELLSTATE ') for c in (host, client)}
        if task not in seen:
            problems.append(f'{tag} cast {spell} at {name}: on the host it never took {task} (saw {sorted(set(map(str, seen)))})')
    assert not problems, '; '.join(problems)


def status_of(bot, guid):
    line = report_line(bot, 'statusof '+guid, 'STATUSOF ')
    m = re.search(r'bits=(\d+) hp=(-?\d+) hp_max=(-?\d+) poisoned_at=(\d+) mirrored=(\d) owner=(\d+) dead=(\d)', line)
    if not m:
        return None
    return {'bits': int(m[1]), 'hp': int(m[2]), 'mirrored': m[5] == '1', 'dead': m[7] == '1'}


def stagePoison(copies, report, negative=False):
    """Poison and disease, step 1 (agent B, after re/findings/poison-plan-c.md): a cast on an NPC another machine
    simulates goes to that machine and runs there, cast by the caster's puppet; the game's OnTick (which takes the
    poison's hit points past the combat filter and decides KO or death) is skipped on mirrors and puppets. Checked, for
    a client's cast at a villager the host simulates and then the host's at another: on the host the NPC gets the
    status bit (4 poisoned, 8 diseased) and, for poison, loses hit points; the client's mirror never runs OnTick for a
    poisoned NPC and its hit points follow the host's. G3_SPELL picks the spell (default It_Spell_Poison). Negative
    control: spellmirroroff and tickguardoff (the client's cast stays on its mirror, mirrors tick again)."""
    spell = os.environ.get('G3_SPELL', 'It_Spell_Poison')
    bit = 8 if 'Disease' in spell else 4
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(5)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    if negative:
        report['negative'] = True
        for c in (host, client):
            report_line(c.bot, 'spellmirroroff', 'SPELLMIRROROFF')
            report_line(c.bot, 'tickguardoff', 'TICKGUARDOFF')
    problems, used = [], set()
    for caster in (client, host):
        tag = caster.tag
        keep = ('Diego', 'Gorn', 'Milten', 'Lester')
        calm = [g for g, v in npcs(host.bot).items() if not v['dead'] and not v['mirrored'] and 'Orc' not in v['name']
                and v['name'] not in keep and g not in used]
        assert calm, 'no calm villager near the host'
        target = next((g for g in calm if npcs(host.bot)[g]['name'].startswith('Slave')), calm[0])
        used.add(target)
        name = npcs(host.bot)[target]['name']
        goal = npcs(host.bot)[target]['pos']
        report[tag+'_moved'] = move_hero(caster.bot, (goal[0] + 300, goal[1], goal[2]))
        time.sleep(2)
        report[tag+'_target'] = name
        before = {c.tag: report_line(c.bot, 'spellstate', 'SPELLSTATE ') for c in (host, client)}
        report[tag+'_before'] = {'host': status_of(host.bot, target), 'client': status_of(client.bot, target)}
        report[tag+'_ready'] = report_line(caster.bot, 'ready '+spell, 'READY ')
        report_line(caster.bot, 'aimat '+target, 'TARGET ')
        report[tag+'_cast'] = report_line(caster.bot, 'castat '+target, 'CASTAT ')[7:300]
        samples = []
        until = time.monotonic() + 10
        while time.monotonic() < until:
            samples.append((round(time.monotonic() - until + 10, 1), status_of(host.bot, target), status_of(client.bot, target)))
            time.sleep(0.5)
        report[tag+'_samples'] = [(t, h and (h['bits'], h['hp']), c and (c['bits'], c['hp'], c['mirrored'])) for t, h, c in samples]
        after = {c.tag: report_line(c.bot, 'spellstate', 'SPELLSTATE ') for c in (host, client)}
        report[tag+'_spellstate'] = after
        on_host = [h for _, h, _ in samples if h and h['bits'] & bit]
        if not on_host:
            problems.append(f'{tag} cast {spell} at {name}: the host\'s NPC never got status {bit}')
            continue
        hps = [h['hp'] for _, h, _ in samples if h]
        if bit == 4 and min(hps) >= hps[0]:
            problems.append(f'{tag}: the poisoned {name} lost no hit points on the host ({hps[0]}..{min(hps)})')
        def count(text, key):
            m = re.search(key + r'=(\d+)', text)
            return int(m[1]) if m else 0
        ran = count(after['client'], 'ticks_run') - count(before['client'], 'ticks_run')
        skipped = count(after['client'], 'poisoned_skipped') - count(before['client'], 'poisoned_skipped')
        report[tag+'_client_ticks'] = {'run': ran, 'poisoned_skipped': skipped}
        # The client's mirror: never ticks the poison itself (bit set there or not), and its hit points follow the host's.
        if count(after['client'], 'tick_guard') != 1 or not count(after['client'], 'tick_hooked'):
            problems.append(f'{tag}: the client does not guard OnTick ({after["client"][11:]})')
        lows = [(t, h['hp'], c['hp']) for t, h, c in samples if h and c and c['hp'] < h['hp'] - 10]
        if lows:
            problems.append(f'{tag}: the client\'s mirror of {name} fell below the host\'s hit points {lows[:3]}')
    assert not problems, '; '.join(problems)


def creatures(bot):
    start = bot.send('creatures', 20)
    bot.line('CREATURES end', 10, 'creatures', start)
    out = []
    for l in bot.log()[start:].splitlines():
        m = re.match(r'CREATURE own name=(\S+) guid=(\S+) at=(-?\d+)_(-?\d+)_(-?\d+) dead=(\d)', l)
        if m:
            out.append({'name': m[1], 'guid': m[2], 'pos': (float(m[3]), float(m[4]), float(m[5])), 'dead': m[6] == '1'})
    return out


def stageCreature(copies, report, negative=False):
    """Summoned creatures (agent B, P1): a player's creature (party member type 5) is announced with its GUID; the
    other machine spawns a copy and gives it that GUID (eCNode::SetID), so the NPC mirroring drives it from the
    machine that simulates it. Checked: the watcher has an NPC with the creature's GUID, mirrored, near the original;
    then the caster walks off 8 m, the creature follows, and its mirror is still near it. Host casts, then client. G3_SPELL picks the spell (default
    It_Spell_SummonSkeleton). Negative control: summonmirroroff (nothing announced, no copy)."""
    spell = os.environ.get('G3_SPELL', 'It_Spell_SummonSkeleton')
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    if negative:
        report['negative'] = True
        for c in (host, client):
            report_line(c.bot, 'summonmirroroff', 'SUMMONMIRROROFF')
    problems = []
    for caster, watcher in ((host, client), (client, host)):
        tag = caster.tag
        target = calm_target(host)
        report[tag+'_ready'] = report_line(caster.bot, 'ready '+spell, 'READY ')
        report_line(caster.bot, 'aimat '+target, 'TARGET ')
        report[tag+'_cast'] = report_line(caster.bot, 'castat '+target, 'CASTAT ')[7:300]
        time.sleep(3)
        own = creatures(caster.bot)
        report[tag+'_own'] = own
        if not own:
            problems.append(f'{tag}: no summoned creature after the cast')
            continue
        checks = []
        for when in ('at once', 'later'):
            seen = npcs(watcher.bot)
            row = []
            for c in creatures(caster.bot):
                mirror = seen.get(c['guid'])
                distance = math.dist(mirror['pos'], c['pos']) if mirror else None
                row.append({'name': c['name'], 'found': bool(mirror), 'mirrored': bool(mirror and mirror['mirrored']),
                            'distance_cm': None if distance is None else round(distance)})
            checks.append(row)
            if when == 'at once':
                # The caster walks off 8 m: the creature follows it there, and its mirror with it.
                at = remotes(watcher.bot)[1 if caster is host else 2]['pos']
                report[tag+'_walked'] = move_hero(caster.bot, (at[0] + 800, at[1], at[2]))
                time.sleep(8)
        report[tag+'_watcher'] = checks
        later = creatures(caster.bot)
        moved = [math.dist(c['pos'], o['pos']) for c in later for o in own if c['guid'] == o['guid']]
        report[tag+'_creature_moved_cm'] = [round(m) for m in moved]
        if moved and max(moved) < 200:
            problems.append(f'{tag}: the creature did not follow its caster (moved {max(moved):.0f} cm)')
        report[tag+'_state'] = {c.tag: summon_state(c.bot)[0] for c in (caster, watcher)}
        for when, row in zip(('at once', 'later'), checks):
            for r in row:
                if not r['found'] or not r['mirrored']:
                    problems.append(f"{tag}: {when}, the watcher has no mirror of {r['name']}")
                elif r['distance_cm'] > 400:
                    problems.append(f"{tag}: {when}, the mirror of {r['name']} is {r['distance_cm']} cm from it")
    assert not problems, '; '.join(problems)


def creature_cast(caster, host, spell, report, key):
    target = calm_target(host)
    report[key+'_ready'] = report_line(caster.bot, 'ready '+spell, 'READY ')
    report_line(caster.bot, 'aimat '+target, 'TARGET ')
    report[key+'_cast'] = report_line(caster.bot, 'castat '+target, 'CASTAT ')[7:300]
    time.sleep(3)
    return creatures(caster.bot)


def watcher_view(watcher, guids):
    """For each GUID: 'mirrored', 'dead' (a corpse), 'free' (alive, not mirrored: a copy left to its own AI) or None."""
    seen = npcs(watcher.bot)
    out = {}
    for g in guids:
        v = seen.get(g)
        out[g] = None if not v else 'dead' if v['dead'] else 'mirrored' if v['mirrored'] else 'free'
    return out


def stageCreatureLife(copies, report, negative=False):
    """B4, summoned creatures over their life (agent B; re/findings/summon-lifecycle-c.md): a new summon replaces the
    old one (it dies on the caster's machine, and its copy dies or goes on the other); the Army of Darkness brings
    several creatures, all mirrored; when the caster walks off 50 m its creatures end there, and no copy lives on as a
    free creature. Host casts, then client. Negative control: summonmirroroff (no copies at all)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    if negative:
        report['negative'] = True
        for c in (host, client):
            report_line(c.bot, 'summonmirroroff', 'SUMMONMIRROROFF')
    problems = []
    for caster, watcher, caster_id in ((host, client, 1), (client, host, 2)):
        tag = caster.tag
        first = creature_cast(caster, host, 'It_Spell_SummonSkeleton', report, tag+'_first')
        second = creature_cast(caster, host, 'It_Spell_SummonSkeleton', report, tag+'_second')
        old = [c['guid'] for c in first if c['guid'] not in {x['guid'] for x in second}]
        time.sleep(2)
        replaced = watcher_view(watcher, old + [c['guid'] for c in second])
        report[tag+'_replaced'] = {'first': [c['guid'] for c in first], 'second': [c['guid'] for c in second], 'watcher': replaced}
        if not second:
            problems.append(f'{tag}: no creature after the second cast')
        for g in old:
            if replaced[g] in ('mirrored', 'free'):
                problems.append(f'{tag}: the replaced creature still lives on the watcher ({replaced[g]})')
        for c in second:
            if replaced[c['guid']] != 'mirrored':
                problems.append(f"{tag}: the new {c['name']} is {replaced[c['guid']]} on the watcher")
        army = creature_cast(caster, host, 'It_Spell_ArmyOfDarkness', report, tag+'_army')
        time.sleep(2)
        army_view = watcher_view(watcher, [c['guid'] for c in army] + [c['guid'] for c in second])
        report[tag+'_army'] = {'own': [(c['name'], c['guid']) for c in army], 'watcher': army_view}
        if len(army) < 2:
            problems.append(f'{tag}: the army brought {len(army)} creatures')
        missing = [c['name'] for c in army if army_view[c['guid']] != 'mirrored']
        if missing:
            problems.append(f'{tag}: {len(missing)} of {len(army)} army creatures not mirrored on the watcher')
        for c in second:
            if c['guid'] not in {a['guid'] for a in army} and army_view[c['guid']] in ('mirrored', 'free'):
                problems.append(f'{tag}: the skeleton the army replaced still lives on the watcher')
        # The caster walks off 50 m: its creatures end there (farther than 40 m from it), their copies must not stay.
        at = remotes(watcher.bot)[caster_id]['pos']
        report[tag+'_walked'] = move_hero(caster.bot, (at[0] + 5000, at[1], at[2]))
        time.sleep(10)
        left = creatures(caster.bot)
        far = watcher_view(watcher, [c['guid'] for c in army])
        report[tag+'_far'] = {'own_left': [(c['name'], c['dead']) for c in left], 'watcher': far}
        free = [g for g, v in far.items() if v == 'free']
        if free:
            problems.append(f'{tag}: {len(free)} copies live on as free creatures after the caster left')
        move_hero(caster.bot, at)
        time.sleep(3)
        report[tag+'_state'] = {c.tag: summon_state(c.bot)[0] for c in (caster, watcher)}
    assert not problems, '; '.join(problems)


def party_members(bot):
    start = bot.send('party', 20)
    bot.line('PARTY end', 10, 'party', start)
    out = []
    for l in bot.log()[start:].splitlines():
        m = re.match(r'MEMBER name=(\S+) guid=(\S+) type=(\d+) task=(\S*) at=(-?\d+)_(-?\d+)_(-?\d+)', l)
        if m:
            out.append({'name': m[1], 'guid': m[2], 'type': int(m[3]), 'task': m[4], 'pos': (float(m[5]), float(m[6]), float(m[7]))})
    return out


def stageControl(copies, report, negative=False):
    """B4, word of control (agent B; re/findings/npc-spells-c.md): the scripts insist on the local player as caster,
    so a client's cast on a mirrored NPC first takes it over as a companion (infocmd::TakeOver), then the game's
    script makes it a Controlled (4) party member that follows. Checked: the NPC is in the caster's party as type 4,
    follows when the caster walks off 8 m, and the other machine shows it mirrored next to the caster's. Client casts,
    then host. G3_SPELL picks the spell (default It_Spell_WordOfControl); with G3_SPAWN (an animal template such as
    Wolf, for It_Spell_TameAnimal) the host spawns the target next to it first; with G3_ANIMAL (Scavenger) both go to
    a world animal of that kind north of Ardea (a spawned one exists only on the host), or at the NPC G3_ANIMAL_AT (Kent; G3_ANIMAL
    may list several kinds, Deer,Okara_Ripper). G3_SAYFOLLOW=1 (P9 StartSayFollow, called by MagicWordOfControl with the
    caster as self; the caster says "follow me" only if it is Entity::GetPlayer): counted at its code (QA nativewatch), it
    must say yes for the caster's own hero on the caster's machine and never on the other one. Negative control: spellmirroroff (the
    client does not take it over; the host keeps simulating it, and it does not follow there)."""
    spell = os.environ.get('G3_SPELL', 'It_Spell_WordOfControl')
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(5)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    if negative:
        report['negative'] = True
        for c in (host, client):
            report_line(c.bot, 'spellmirroroff', 'SPELLMIRROROFF')
    sayfollow = os.environ.get('G3_SAYFOLLOW') == '1'
    if sayfollow:
        for c in (host, client):
            report[c.tag + '_watch'] = native_watch(c.bot, ['StartSayFollow'])
    problems, used = [], set()
    # G3_CASTER=client: only the client casts (Kent has one deer; the rippers are too strong to tame, as in single-player).
    casters = ((client, host, 2),) if os.environ.get('G3_CASTER') == 'client' else ((client, host, 2), (host, client, 1))
    for caster, watcher, caster_id in casters:
        tag = caster.tag
        keep = ('Diego', 'Gorn', 'Milten', 'Lester')
        calm = [g for g, v in npcs(host.bot).items() if not v['dead'] and not v['mirrored'] and 'Orc' not in v['name']
                and v['name'] not in keep and g not in used]
        assert calm, 'no calm villager near the host'
        target = next((g for g in calm if npcs(host.bot)[g]['name'].startswith('Slave')), calm[0])
        if os.environ.get('G3_ANIMAL'):
            # A world animal both machines have (scavengers graze north of Ardea, about 90500,-6100; seen in old runs).
            # G3_ANIMAL_AT: an NPC to meet at instead (Kent: deer and rippers the host simulates, stageCallAnimals).
            for c in (host, client):
                if os.environ.get('G3_ANIMAL_AT'):
                    teleport_to(c.bot, os.environ['G3_ANIMAL_AT'])
                else:
                    move_hero(c.bot, (90500.0, 5300.0, -6600.0))
            herd = []
            until = time.monotonic() + 20  # the area's animals come in once it is loaded
            while not herd and time.monotonic() < until:
                time.sleep(2)
                herd = [g for g, v in npcs(host.bot).items() if v['name'].startswith(tuple(os.environ['G3_ANIMAL'].split(','))) and not v['dead']
                        and not v['mirrored'] and g not in used]
            report[tag+'_near_host'] = sorted({v['name'] for v in npcs(host.bot).values()})
            assert herd, f"no {os.environ['G3_ANIMAL']} near the host"
            target = herd[0]
        if os.environ.get('G3_SPAWN'):
            spawned = report_line(host.bot, 'spawnnpc '+os.environ['G3_SPAWN'], 'SPAWNNPC ')
            report[tag+'_spawned'] = spawned
            target = re.search(r'guid=(\S+)', spawned)[1]
            time.sleep(3)
            wait_for(lambda: target in npcs(host.bot) and [target], 10, "the spawned animal in the host's NPC list")
        used.add(target)
        name = npcs(host.bot)[target]['name']
        goal = npcs(host.bot)[target]['pos']
        report[tag+'_moved'] = move_hero(caster.bot, (goal[0] + 300, goal[1], goal[2]))
        # The watcher close by: its QA NPC list covers 40 m round its hero only.
        report[tag+'_watcher_moved'] = move_hero(watcher.bot, (goal[0] + 300, goal[1], goal[2] + 600))
        time.sleep(2)
        report[tag+'_target'] = name
        report[tag+'_ready'] = report_line(caster.bot, 'ready '+spell, 'READY ')
        report_line(caster.bot, 'aimat '+target, 'TARGET ')
        if sayfollow:
            for c in (host, client):
                report_line(c.bot, 'nativewatch reset', 'NATIVEWATCH ')
        report[tag+'_cast'] = report_line(caster.bot, 'castat '+target, 'CASTAT ')[7:300]
        time.sleep(3)
        if sayfollow:
            said = report[tag+'_sayfollow'] = {c.tag: native_counts(c.bot).get('StartSayFollow', {}) for c in (caster, watcher)}
            if not (said[caster.tag].get('hero_self', 0) >= 1 and said[caster.tag].get('nonzero', 0) >= 1):
                problems.append(f'{tag}: StartSayFollow did not say yes for the caster on its machine: {said}')
            if said[watcher.tag].get('nonzero', 0):
                problems.append(f'{tag}: the other machine said "follow me" too: {said}')
        members = party_members(caster.bot)
        report[tag+'_party'] = members
        # What the caster's NPC list says of it (a client sends it only while it is not a mirror there) and the host's.
        report[tag+'_caster_view'] = npcs(caster.bot).get(target)
        report[tag+'_host_view'] = npcs(host.bot).get(target)
        joined = next((m for m in members if m['guid'] == target), None)
        if not joined or joined['type'] != 4:
            problems.append(f'{tag}: {name} is not a controlled member of the caster\'s party ({joined})')
            continue
        # 8 m towards the open ground: eastwards between the huts the follower stuck after 2 m (runs 20261020-223005,
        # -225045: ZS_FollowPlayer, 2 m).
        at = remotes(watcher.bot)[caster_id]['pos']
        way = [OpenGround[i] - at[i] for i in (0, 2)]
        length = max(1.0, math.hypot(*way))
        report[tag+'_walked'] = move_hero(caster.bot, (at[0] + way[0] / length * 800, at[1], at[2] + way[1] / length * 800))
        # Why the caster's SendOwned leaves the NPC out, once a second (A's QA npcsend; a client that took the NPC over
        # sometimes sent it for 15 s not at all and the host ended the claim: AGENTEN.md 20.10. 23:05).
        sends = []
        for _ in range(10):
            line = report_line(caster.bot, 'npcsend '+name, 'NPCSEND ')
            sends.append(' '.join(re.findall(r'(?:reason|age_ms|myclaim|mirror|givenback_ms)=\S+', line)))
            time.sleep(1)
        report[tag+'_npcsend'] = sends
        after = next((m for m in party_members(caster.bot) if m['guid'] == target), None)
        mirror = npcs(watcher.bot).get(target)
        report[tag+'_after'] = {'own': after, 'watcher': mirror}
        if not after or math.dist(after['pos'], joined['pos']) < 200:
            problems.append(f'{tag}: {name} did not follow its caster')
        elif not mirror or not mirror['mirrored']:
            problems.append(f'{tag}: the other machine does not mirror {name} ({mirror})')
        elif math.dist(mirror['pos'], after['pos']) > 400:
            problems.append(f"{tag}: on the other machine {name} is {math.dist(mirror['pos'], after['pos']):.0f} cm from the caster's")
        report[tag+'_spellstate'] = {c.tag: report_line(c.bot, 'spellstate', 'SPELLSTATE ') for c in (host, client)}
    assert not problems, '; '.join(problems)


def stageP2hit(copies, report, negative=False):
    """P2, arrows act once (agent B): pin an existing host-owned NPC in an open firing lane before each shot, then
    require every arrow that touches it to have exactly one correlated application on the host. Host shoots, then
    client. Negative control: shotcopyhero (the copies belong to the watcher's own hero and hit a second time)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    report['fixture_moved'] = {
        'host': move_hero(host.bot, OpenGround),
        'client': move_hero(client.bot, (OpenGround[0] + 100, OpenGround[1], OpenGround[2])),
    }
    time.sleep(5)
    matches = [(guid, npc) for guid, npc in npcs(host.bot).items() if npc['name'] == 'Hamlar']
    assert len(matches) == 1, f'Hamlar is not unique on the host: {len(matches)} matches'
    target, original = matches[0]
    assert not original['dead'] and not original['mirrored'], 'Hamlar is not an alive host-owned NPC'
    original_pos, original_hp, name = original['pos'], original['hp'], original['name']
    fixture_pos = original_pos

    def pin_target():
        moved = report_line(host.bot, 'farmove ' + '_'.join(f'{v:.0f}' for v in fixture_pos) + '_' + name, 'FARMOVE ')

        def fixed():
            host_npc, client_npc = npcs(host.bot).get(target), npcs(client.bot).get(target)
            if (host_npc and client_npc and not host_npc['dead'] and not client_npc['dead']
                    and not host_npc['mirrored'] and client_npc['mirrored']
                    and host_npc['hp'] > 0 and host_npc['hp'] == client_npc['hp']
                    and host_npc['task'] not in ('ZS_Fall', 'ZS_RagDollDead', 'ZS_Dead', 'ZS_Talk')
                    and distance(host_npc['pos'], fixture_pos) < 100 and distance(client_npc['pos'], fixture_pos) < 150):
                return [(host_npc, client_npc)]

        host_npc, client_npc = wait_for(fixed, 10, 'Hamlar pinned standing with the same GUID on both machines')[0]
        return {'move': moved, 'guid': target, 'host_pos': host_npc['pos'], 'client_pos': client_npc['pos'],
                'host_mirrored': host_npc['mirrored'], 'client_mirrored': client_npc['mirrored']}

    problems = []
    try:
        report['fixture_pin'] = pin_target()
        report['fixture_hp'] = report_line(host.bot, f'npchp {name}_500', 'NPCHP ')

        def ready_fixture():
            host_npc, client_npc = npcs(host.bot).get(target), npcs(client.bot).get(target)
            if (host_npc and client_npc and not host_npc['dead'] and not client_npc['dead']
                    and host_npc['hp'] == 500 and client_npc['hp'] == 500 and client_npc['mirrored']):
                return [(host_npc, client_npc)]

        fixture_host, fixture_client = wait_for(ready_fixture, 10, 'Hamlar hit points mirrored at 500')[0]
        report['fixture_ready'] = {'guid': target, 'host': fixture_host, 'client': fixture_client}
        for c in (host, client):
            report[c.tag+'_touchlog'] = report_line(c.bot, 'touchlog', 'TOUCHLOG ')
            if negative:
                report_line(c.bot, 'shotcopyhero', 'SHOTCOPYHERO')
        report['negative'] = negative
        arrow_hit_phase(host, client, 1, host, report, 'host', target=target, pin=pin_target)
        arrow_hit_phase(client, host, 2, host, report, 'client', target=target, pin=pin_target)
        if os.environ.get('G3_P2_FIXTURE_ONLY') == '1':
            return
        problems = report['host_problems'] + report['client_problems']
    finally:
        report['fixture_restore_move'] = report_line(
            host.bot, 'farmove ' + '_'.join(f'{v:.0f}' for v in original_pos) + '_' + name, 'FARMOVE ')
        report['fixture_restore_hp'] = report_line(host.bot, f'npchp {name}_{original_hp}', 'NPCHP ')

        def restored_fixture():
            host_npc, client_npc = npcs(host.bot).get(target), npcs(client.bot).get(target)
            if (host_npc and client_npc and not host_npc['dead'] and not client_npc['dead']
                    and not host_npc['mirrored'] and client_npc['mirrored']
                    and host_npc['hp'] == original_hp and client_npc['hp'] == original_hp
                    and distance(host_npc['pos'], original_pos) < 150 and distance(client_npc['pos'], original_pos) < 200):
                return [(host_npc, client_npc)]

        restored_host, restored_client = wait_for(restored_fixture, 10, 'Hamlar position and hit points restored')[0]
        report['fixture_restored'] = {'guid': target, 'host': restored_host, 'client': restored_client}
    assert not problems, '; '.join(problems)


def stageP2(copies, report, negative=False):
    """P2, arrows (agent B): a player's arrow is announced as it leaves the bow (template, pose, direction, speed,
    path); the other machine flies a copy that belongs to that player's puppet, so its hits count nowhere. Checked: every
    shot announced and copied, the copy owned by the puppet and flying like the arrow (distance, direction), none of its
    hits applied on the watcher. Client shoots, then host. Negative control: shotmirroroff (shots are not announced, the
    watcher sees nothing)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    if negative:
        report['negative'] = True
        for c in (host, client):
            report_line(c.bot, 'shotmirroroff', 'SHOTMIRROROFF')
    arrow_phase(client, host, 2, host, report, 'client')
    arrow_phase(host, client, 1, host, report, 'host')
    problems = report['client_problems'] + report['host_problems']
    assert not problems, '; '.join(problems)


def rangedbot(copies, report, negative=False):
    """Step 2 of bows and spells (agent B): the bot shoots and casts by the NPC path; does it hit, on whose machine does
    the target take damage, what does the watcher see. G3_SPELL picks the spell (default It_Spell_Meteor)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    spell = os.environ.get('G3_SPELL', 'It_Spell_Meteor')
    ranged_bot_phase(client, host, 2, host, report, 'client', spell)
    ranged_bot_phase(host, client, 1, host, report, 'host', spell)
    report['combat_end'] = [report_line(c.bot, 'hp', 'HP ') for c in (host, client)]
    report['status_note'] = 'diagnostic only'


def rangeddiag(copies, report, negative=False):
    """Owner (19.10.): bows and spells, the meteor in particular. After the raid, the client shoots and casts at a
    villager the host simulates while the host watches, then the other way round: what flies on each machine, what
    the watcher's puppet plays, whether the target is hurt on the machine that owns it, and who earns experience."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(3)
    for copy, helper in ((client, host), (host, client)):
        if ' down=1 ' in herostate(copy.bot):
            help_up(helper, copy, report, copy.tag+'_raid')
            wait_for(lambda: ' down=0 ' in herostate(copy.bot) and ['up'], 30, copy.tag+' helped up after the raid')
    ranged_phase(client, host, 2, host, report, 'client')
    ranged_phase(host, client, 1, host, report, 'host')
    report['combat_end'] = [report_line(c.bot, 'hp', 'HP ') for c in (host, client)]
    report['status_note'] = 'diagnostic only'


def invariantself(copies, report, negative=False):
    """Self-test of the P5 invariant monitors: the client stops taking the host's NPC snapshots (npcrecvoff), its
    mirrors go stale and run their own AI, so both machines simulate the same NPCs; the monitors must report it
    (double_sim, usually drift too). The mod repairs smaller divergences by itself (a QA kill on a mirror was undone by
    the next snapshot), which is why the self-test cuts the stream. Negative control: nothing is cut, nothing reported."""
    host, client = join_both(copies)
    time.sleep(5)
    if not negative:
        report['recvoff'] = report_line(client.bot, 'npcrecvoff', 'NPCRECVOFF')
    time.sleep(12)
    invariants(copies, report)
    found = report['invariants'].get('violations', {})
    assert found.get('double_sim'), f'the monitors missed both machines simulating: {report["invariants"]}'


def stageP3a(copies, report, negative=False):
    """Stage P3a (DESIGN-2026-10-19.md P3): a kill by a player's companion across machines pays that player. The client
    recruits Lester (his machine simulates him), Lester kills one of the host's NPCs; the host applies the kill and
    tells the client, whose hero earns the experience, as for its own kills. Before, nobody earned anything."""
    host, client = join_both(copies)
    if negative:
        report_line(client.bot, 'partyxpoff', 'PARTYXPOFF')
    clear_raid(host, report)
    teleport_to(host.bot, 'Lester')
    time.sleep(6)
    teleport_to(client.bot, 'Lester')
    time.sleep(8)
    wait_for(lambda: npc_named(client.bot, 'Lester')[1].get('mirrored') and ['yes'], 20, 'Lester mirrored on the client')
    report_line(client.bot, 'setevent GetLester', 'EVENT ')
    talk_to(client, 'Lester', report, 'partners')
    report_line(client.bot, 'talkinfo NEWM1055', ('TALKINFO ', 'REFUSED talkinfo'))
    wait_for(lambda: talkstate(client.bot).get('running') == '0', 90, 'the recruiting dialog ends')
    wait_for(lambda: 'owner=2' in find_npc(host.bot, 'Lester').split() and ['yes'], 15, 'the client has Lester')
    # A victim: one of the host's NPCs near Lester with a name of its own (npcfight addresses it by name).
    rows = npcs(client.bot)
    names = [v['name'] for v in rows.values()]
    lester = next(v['pos'] for v in rows.values() if v['name'] == 'Lester')
    candidates = sorted(((distance(v['pos'], lester), g, v['name']) for g, v in rows.items()
                         if v['mirrored'] and not v['dead'] and v['name'] != 'Lester'),
                        key=lambda c: c[0])
    assert candidates, 'no host NPC with a unique name near Lester'
    _, victim, name = candidates[0]
    report['victim'] = name
    before = combat_state(client.bot)
    report['client_before'] = {k: before[k] for k in ('xp', 'xp_awarded')}
    report['fight'] = report_line(client.bot, f'npcfight Lester_{victim}', ('NPCFIGHT ', 'REFUSED npcfight'))
    assert report['fight'].startswith('NPCFIGHT '), report['fight']

    def killed():
        h = combat_state(host.bot)
        return h['npc_kills'] >= 1 and h
    report['host_combat'] = wait_for(killed, 120, 'Lester kills the host NPC')
    time.sleep(3)
    after = combat_state(client.bot)
    report['client_after'] = {k: after[k] for k in ('xp', 'xp_awarded')}
    assert report['host_combat']['party_kills'] >= 1, 'the host did not credit the kill to the client'
    assert after['xp_awarded'] > before['xp_awarded'] and after['xp'] > before['xp'], 'the client earned nothing for its companion\'s kill'


def stageL(copies, report, negative=False):
    """Stage L, the soft leash (owner 2026-10-19): instead of a hard distance limit a key (INI partner_key, default Page
    Down) puts the local hero next to the other player. The host walks 60 m away; the client jumps (QA: partnerjump,
    the same function as the key) and stands within 3 m of the host on both machines. Negative control: no jump."""
    host, client = join_both(copies)
    clear_raid(host, report)  # no raid needed: starts from the post-raid save (25.10.)
    start_pos = remotes(client.bot)[1]['pos']
    move_hero(host.bot, (start_pos[0] + 6000, start_pos[1], start_pos[2]))
    time.sleep(4)

    def gap():
        return distance(remotes(host.bot)[2]['pos'], remotes(client.bot)[1]['pos'])
    report['gap_before_m'] = round(gap() / 100, 1)
    assert report['gap_before_m'] > 40, f"the players are only {report['gap_before_m']} m apart"
    if not negative:
        report['jump'] = report_line(client.bot, 'partnerjump', 'PARTNERJUMP ')
        assert 'ok=1' in report['jump'], report['jump']
        report['too_soon'] = report_line(client.bot, 'partnerjump', 'PARTNERJUMP ')
        assert 'reason=too' in report['too_soon'], 'no cooldown: ' + report['too_soon']
    report['gap_after_m'] = round(wait_for(lambda: gap() < 300 and [gap()], 10, 'the client stands next to the host')[0] / 100, 1)


def stageEmote(copies, report, negative=False):
    """Emotes (owner 27.10.: "Kleine Spaß-Idee"): a key (INI emote_key, default Home) plays one of the game's own NPC
    motions on the local hero; nothing new travels, the motion goes to the puppet with the PlayerState. The client plays
    each of G3_EMOTE (comma list, default cheer; an emote's key as QA emote, the same function as the key, or
    raw:<motion>[-method] for any motion with emoteraw, _x_ for _%_; "all" in the list: every emote) and stands still;
    the client's hero shows the emote's motion and the host's puppet of the client shows the same motion, running. The
    host looks at the puppet from 4 m, so that the screenshots at 1, 3 and 5 s show it. Negative control: no emote, the
    puppet shows none of these motions."""
    actions = {'cheer': 'SuperCheer', 'dance': 'Dance', 'arms': 'ArmsCrossed', 'hips': 'HandsOnHips',
               'think': 'Think', 'approve': 'Say_Approve', 'point': 'Say_Point', 'threaten': 'Say_Threaten', 'pee': 'Pee'}
    keys = os.environ.get('G3_EMOTE', 'cheer')
    keys = [k for key in keys.split(',') for k in (list(actions) if key == 'all' else [key])]
    host, client = join_both(copies)
    clear_raid(host, report)
    for copy in (host, client):
        report_line(copy.bot, 'unarm', 'UNARM')
    time.sleep(3)
    report['lookat'] = report_line(host.bot, 'lookat puppet2', ('LOOKAT', 'REFUSED lookat'))
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 150, y, z))  # a step aside: right behind the host's hero the camera did not see the puppet
    time.sleep(2)
    failed = []
    for key in keys:
        word = key[4:].rsplit('-', 1)[0] if key.startswith('raw:') else key.split('-')[0]
        action = actions.get(word) or re.split(r'_(?:Begin|Loop|End|N|O)_', word.replace('Hero_Stand_None_None_P0_', ''))[0]
        entry = report.setdefault('emotes', {}).setdefault(key, {'action': action})
        if not negative:
            command = 'emoteraw ' + key[4:] if key.startswith('raw:') else 'emote ' + key
            entry['emote'] = line = report_line(client.bot, command, 'EMOTE ')
            if 'ok=1' not in line:
                failed.append(f'{key}: {line}')
                continue
        motion = re.search(r' motion=(\S+)', entry.get('emote', ''))
        motion = motion[1] if motion else None
        samples = []
        for i in range(12):
            time.sleep(0.5)
            samples.append((report_line(client.bot, 'emotestate', 'EMOTESTATE '), puppet_pose(host.bot, 2)))
            if i in (1, 5, 9):
                snap(host, f'emote-host-{word[:20]}-{(i + 1) // 2}s', report)
                snap(client, f'emote-client-{word[:20]}-{(i + 1) // 2}s', report)
        entry['samples'] = samples
        seen = [p for _, p in samples if p and '_' + action + '_' in p['shows'] and p['run0'] == 1]
        hero = [h for h, _ in samples if motion and '_' + action + '_' in h.split(' shows=')[-1].split(' ')[0]]
        entry['puppet_seen'], entry['hero_seen'] = len(seen), len(hero)
        entry['loop_seen'] = sum(' loop=1 ' in h for h, _ in samples)
        if negative:
            if seen:
                failed.append(f'{key}: the puppet showed {action} without an emote')
        elif not hero or not seen:
            failed.append(f'{key}: hero {len(hero)}/12, puppet {len(seen)}/12')
        time.sleep(2)  # a loop goes on until the hero walks off; the next emote replaces it
        if negative:
            break
    report['failed'] = failed
    assert not failed, failed


TRACE_LINE = re.compile(r'^\s*(\d+) (anim|task|aitask) (\S+?)(\(puppet\)|\(mirror\))? (.*) by (\S+)$')


def read_trace(copy):
    path = copy.root/'g3trace.log'
    if not path.exists():
        return None
    return [m.groups() for m in map(TRACE_LINE.match, path.read_text(errors='replace').splitlines()) if m]


def tracediag(copies, report, negative=False):
    """The runtime trace (owner 2026-10-19: trace instead of more static reverse engineering). With "trace on" each
    machine logs to g3trace.log which code starts an animation (Engine PlayMotion) or sets a task (Game SetCurrentTask,
    AISetTask) on a body the mod drives, with the caller as module+RVA. Checks: all three hooks installed, lines for
    mirrors on the client and for the puppet on the host, callers resolved to modules. Negative control: no trace
    command, no file."""
    from collections import Counter
    host, client = join_both(copies)
    via_ini = 'trace = on' in os.environ.get('G3_INI_EXTRA', '')  # the players' way: INI line, no QA command
    report['via_ini'] = via_ini
    if not negative:
        for copy in (host, client):
            line = report_line(copy.bot, 'trace' if via_ini else 'trace on', 'TRACE ')
            report[copy.tag+'_trace_on'] = line
            assert 'mode=driven' in line and 'hooks=111' in line, copy.tag+': '+line
    time.sleep(int(os.environ.get('G3_TRACE_SECONDS', '45')))
    for copy, tag in ((host, '(puppet)'), (client, '(mirror)')):
        if not negative:
            report[copy.tag+'_trace_off'] = report_line(copy.bot, 'trace off', 'TRACE ')
        lines = read_trace(copy)
        if negative:
            assert lines is None, copy.tag+': g3trace.log written without a trace command'
            continue
        assert lines, copy.tag+': no trace lines'
        report[copy.tag+'_lines'] = len(lines)
        report[copy.tag+'_by_kind'] = dict(Counter(f"{k}{m or ''}" for _, k, _, m, _, _ in lines))
        report[copy.tag+'_top_callers'] = Counter(f'{k} by {c}' for _, k, _, _, _, c in lines).most_common(12)
        report[copy.tag+'_top_details'] = Counter(f'{k} {d}' for _, k, _, _, d, _ in lines).most_common(15)
        assert any(m == tag for _, _, _, m, _, _ in lines), f'{copy.tag}: no line for a {tag} body'
        resolved = sum(1 for *_, c in lines if re.match(r'[\w.]+\.(dll|exe)\+0x[0-9a-f]+$', c, re.I))
        report[copy.tag+'_callers_resolved'] = resolved
        assert resolved == len(lines), f'{copy.tag}: {len(lines) - resolved} callers not resolved to a module'


DEATH_WORDS = ('dead', 'die', 'death', 'ragdoll', 'down', 'lie')


def deaddiag(copies, report, negative=False):
    """Diagnostic for agent B's side finding (19.10.): orcs the host kills sometimes stand upright on the client, although
    its NPC list says dead. The client traces (g3trace.log) while the host kills mirrored orcs near it, half by the
    cheat kill (farkill), half by weapon hits (hitnpc, the game's damage path). Per victim: the task the client set,
    the motions it played after that, the last motion per slot and who started it. Screenshots of the client."""
    host, client = join_both(copies)
    for copy in (host, client):
        armed = report_line(copy.bot, 'arm '+WEAPON, 'ARM ')
    time.sleep(8)
    if negative:
        assert 'ok=1' in report_line(client.bot, 'deadposeoff', 'DEADPOSEOFF ')
    mode = os.environ.get('G3_TRACE_MODE', 'on')
    report['trace_on'] = report_line(client.bot, 'trace '+mode, 'TRACE ')
    report['host_trace_on'] = report_line(host.bot, 'trace all', 'TRACE ')  # the host's own orcs die natively there
    host_npcs, client_npcs = npcs(host.bot), npcs(client.bot)
    client_at = remotes(host.bot)[2]['pos']
    alive = [g for g in set(host_npcs) & set(client_npcs)
             if client_npcs[g]['mirrored'] and not host_npcs[g]['dead'] and host_npcs[g]['hp'] > 0
             and host_npcs[g]['name'].startswith('Orc_')]
    alive.sort(key=lambda g: distance(client_npcs[g]['pos'], client_at))
    victims = alive[:int(os.environ.get('G3_VICTIMS', '4'))]
    assert victims, 'no living mirrored orc'
    report['victims'] = []
    for i, g in enumerate(victims):
        name = host_npcs[g]['name']
        entry = {'guid': g, 'name': name, 'distance_m': round(distance(client_npcs[g]['pos'], client_at) / 100, 1)}
        if i % 2 == 0:
            entry['how'] = 'farkill'
            entry['kill'] = report_line(host.bot, 'farkill '+name, 'FARKILL ')
        else:
            entry['how'] = 'hits'
            hits = []
            for _ in range(25):
                hit = hit_npc(host.bot, g)
                hits.append(hit['line'][-60:])
                if hit.get('dead'):
                    break
                time.sleep(0.4)
            entry['hits'] = len(hits)
            entry['last_hit'] = hits[-1]
        report['victims'].append(entry)
        time.sleep(1)
    time.sleep(10)
    snap(client, 'after-deaths', report)
    after = npcs(client.bot)
    pattern = r'DEADPOSE mirror=(\S+) killed=(\d) running=(\d) height=(-?[\d.]+) ani=(-?\d+) motion=(\S+) slots=(\S*)'

    def pose_lines():
        start = client.bot.send('deadpose', 20)
        summary = client.bot.line('DEADPOSE enabled', 10, 'deadpose', start)
        return summary, {m[0]: {'killed': m[1] == '1', 'running': m[2] == '1', 'height': float(m[3]), 'ani': int(m[4]),
                                'motion': m[5], 'slots': m[6]} for m in re.findall(pattern, client.bot.log()[start:])}
    report['deadpose'], poses = pose_lines()
    # The engine animates only bodies on screen; bones of a corpse behind the camera keep an old pose. Look at each
    # victim (hero 4 m away, facing it), then measure it and take a picture.
    for entry in report['victims']:
        report_line(client.bot, 'lookat '+entry['name'], 'LOOKAT ')
        time.sleep(2)
        _, seen = pose_lines()
        if entry['name'] in seen:
            poses[entry['name']] = seen[entry['name']]
        snap(client, 'corpse-'+entry['name'], report)
    report['alive_heights'] = {k: v['height'] for k, v in poses.items() if not v['killed']}
    start = client.bot.send('nearnpcs', 20)
    client.bot.line('NEARX end', 10, 'nearnpcs', start)
    shows = dict(re.findall(r'NEARX guid=\S+ name=(\S+) .*? motion=(\S*)', client.bot.log()[start:]))
    host_after = npcs(host.bot)
    report['trace_off'] = report_line(client.bot, 'trace off', 'TRACE ')
    report['host_trace_off'] = report_line(host.bot, 'trace off', 'TRACE ')
    lines = read_trace(client) or []
    host_lines = read_trace(host) or []
    report['trace_lines'] = len(lines)
    for entry in report['victims']:
        theirs = [l for l in host_lines if l[2] == entry['name']]
        host_death = next((int(l[0]) for l in theirs if l[1] != 'anim' and ('Dead' in l[4] or 'Die' in l[4])), None)
        entry['host_tasks'] = [f'{l[0]} {l[4]} by {l[5]}' for l in theirs if l[1] != 'anim'][-6:]
        entry['host_anims_after_death'] = [f'{l[0]} {l[4]} by {l[5]}' for l in theirs
                                           if l[1] == 'anim' and host_death is not None and int(l[0]) >= host_death][:12]
        g, name = entry['guid'], entry['name']
        entry['host_dead'] = host_after.get(g, {}).get('dead')
        entry['client'] = {k: after.get(g, {}).get(k) for k in ('dead', 'mirrored', 'task', 'hp')}
        mine = [l for l in lines if l[2] == name]
        tasks = [(int(l[0]), l[4], l[5]) for l in mine if l[1] != 'anim']
        death_at = next((ms for ms, d, _ in tasks if 'Dead' in d or 'Die' in d), None)
        entry['tasks'] = [f'{ms} {d} by {c}' for ms, d, c in tasks][-8:]
        entry['death_task_ms'] = death_at
        later = [l for l in mine if l[1] == 'anim' and death_at is not None and int(l[0]) >= death_at]
        entry['anims_after_death'] = len(later)
        last = {}
        for l in later:
            slot = re.match(r'type=(\d+)', l[4])
            last[slot[1] if slot else '?'] = f'{l[0]} {l[4]} by {l[5]}'
        entry['last_anim_per_slot'] = last
        entry['non_death_after_death'] = [f'{l[0]} {l[4]} by {l[5]}' for l in later
                                          if not any(w in l[4].lower() for w in DEATH_WORDS)][-10:]
        entry['looks_standing'] = bool(later) and not any(any(w in l[4].lower() for w in DEATH_WORDS) for l in later[-3:])
        entry['client_shows'] = shows.get(name)
        entry['client_pose'] = poses.get(name)
    # Lying: the lying-dead motion runs on slot 0 and nothing runs on the locomotion slots 1-3 (without the fix they
    # play walking and fighting motions on the corpse). The height of the posed body (highest bone, lying 54 cm,
    # standing 170-205 cm) is reported too, but it is no proof: the engine animates only bodies on screen and the
    # camera does not turn with "lookat", so a corpse behind the camera keeps an old pose (19.10.: 205 = never shown).
    def lies(entry):
        pose = entry.get('client_pose') or {}
        return ('LieDead' in pose.get('motion', '') and pose.get('running') and ':run:' not in pose.get('slots', ':run:'))
    lying = [e['name'] for e in report['victims'] if lies(e)]
    report['flat_by_height'] = f"{sum(1 for e in report['victims'] if (e.get('client_pose') or {}).get('height', 999) < 100)}/{len(report['victims'])}"
    report['lying_on_client'] = f'{len(lying)}/{len(report["victims"])}'
    # Stage "dead lie down": every orc the host killed lies on the client (motion LieDead_*); negative control: none.
    if negative:  # the bug: without laying down most corpses stand (some lie by themselves)
        assert len(lying) < len(report['victims']), f'all dead mirrors lie although laying down is off: {lying}'
    else:
        assert len(lying) == len(report['victims']), 'dead mirrors not lying on the client: ' + str(
            {e['name']: e.get('client_shows') for e in report['victims'] if e['name'] not in lying})


def scriptfinddiag(copies, report, negative=False):
    """Which registered script, AI callback, routine, state or function of Script_Game.dll sits at the RVAs in G3_RVAS
    (default: 0x100a5ea0, the fight-end script that sent puppets to their routine, and 0x100a9440, the routine
    starter)."""
    host = copies['host'].launch()
    enter_world(host)
    for rva in [r for r in os.environ.get('G3_RVAS', 'a5ea0,a9440').split(',') if r]:
        start = host.bot.send('scriptfind '+rva, 20)
        host.bot.line('SCRIPTFIND end', 10, 'scriptfind', start)
        report[rva] = [l for l in host.bot.log()[start:].splitlines() if l.startswith('SCRIPTFIND')]
    report['status_note'] = 'diagnostic only'


def pick_friend(host, client):
    """The nearest idle NPC of the host, mirrored on the client, friendly to the host's hero (friendlydiag)."""
    victim = None
    host_npcs, client_npcs = npcs(host.bot), npcs(client.bot)
    client_at = remotes(host.bot)[2]['pos']
    candidates = []
    for g in sorted(set(host_npcs) & set(client_npcs), key=lambda g: distance(client_npcs[g]['pos'], client_at)):
        v = host_npcs[g]
        if v['dead'] or v['hp'] <= 0 or not client_npcs[g]['mirrored'] or v['name'].startswith(('Orc_', 'Lester')):
            continue
        if distance(client_npcs[g]['pos'], client_at) > 2500:
            break
        line = report_line(host.bot, 'attitude '+v['name'], ('ATTITUDE ', 'REFUSED attitude'))
        candidates.append(line)
        if ' hero=1 ' in line and 'task=ZS_Attack' not in line:  # a friend not busy fighting
            victim = (g, v['name'])
            break
    return victim, candidates


def friendlydiag(copies, report, negative=False):
    """The friend case (re/findings/attitude-callers.md: unknown at run time): the client strikes an NPC of the host
    that is friendly to the host's hero. A puppet is judged like the host's hero (GetAttitude wrapper), so
    AssessAgressor and CallHelp see a friend: does the NPC still fight back, flee, or call help? Sampled on the host
    for 15 s after three hits: the victim's task and target, and bystanders targeting the puppet. Negative control:
    attitudeoff on the host (the puppet judged as an ordinary NPC)."""
    host, client = join_both(copies)
    if os.environ.get('G3_FRIEND_ATTITUDEOFF'):
        report_line(host.bot, 'attitudeoff', 'ATTITUDEOFF')
    if negative:
        assert 'ok=1' in report_line(host.bot, 'reacthitsoff', 'REACTHITSOFF ')
    report_line(client.bot, 'arm '+WEAPON, 'ARM ')
    # After the opening raid: a friend busy fighting raiders (Gorn, first try) ignores everything else.
    report['killraiders'] = report_line(host.bot, 'killraiders', ('KILLRAIDERS', 'REFUSED killraiders'))
    report['killboss'] = report_line(host.bot, 'farkill Ardea_Orkboss', ('FARKILL', 'REFUSED farkill'))  # killraiders spares him
    time.sleep(10)
    victim, candidates = None, []
    until = time.monotonic() + 30  # after the raid the friends calm down one by one
    while victim is None and time.monotonic() < until:
        victim, candidates = pick_friend(host, client)
        if victim is None:
            time.sleep(4)
    report['candidates'] = candidates
    assert victim, "no idle NPC friendly to the host's hero near the client"
    g, name = victim
    report['victim'] = name
    report['attitude_before'] = candidates[-1]
    report['hits'] = []
    for _ in range(3):
        report['hits'].append(hit_npc(client.bot, g)['line'][-90:])
        time.sleep(1.5)
    samples = []
    until = time.monotonic() + 15
    while time.monotonic() < until:
        rows = near_rows(host.bot)
        me = rows.get(next((k for k in rows if rows[k]['name'] == name), ''), {})
        helpers = sorted({r['name'] for r in rows.values() if r['target_puppet'] and r['name'] != name and not r['dead'] and not r['name'].startswith('Orc_')})
        samples.append({'task': me.get('task'), 'target': me.get('target'), 'target_puppet': me.get('target_puppet'),
                        'hp': npcs(host.bot).get(g, {}).get('hp'), 'helpers': helpers})
        time.sleep(1)
    report['samples'] = samples
    report['attitude_after'] = report_line(host.bot, 'attitude '+name, ('ATTITUDE ', 'REFUSED attitude'))
    report['victim_targeted_puppet'] = sum(1 for s in samples if s['target_puppet'])
    report['victim_tasks'] = sorted({s['task'] for s in samples if s['task']})
    report['helpers'] = sorted({h for s in samples for h in s['helpers']})
    report['hp_change'] = [samples[0]['hp'], samples[-1]['hp']] if samples else None
    report['host_reactions'] = combat_state(host.bot).get('reactions')  # client hits that made the NPC judge its attacker
    # Stage F1: the NPC the client struck reacts as in the single-player game: it turns on the client's puppet or flees.
    reacted = report['victim_targeted_puppet'] > 0 or any('Flee' in t_ for t_ in report['victim_tasks'])
    report['reacted'] = reacted
    if negative:
        assert not reacted, 'the NPC reacted although reactions are off'
    else:
        assert reacted, 'the NPC did not react to the client\'s hits: ' + str(report['victim_tasks'])


def state_hp(bot):
    start = bot.send('hp', 20)
    line = bot.line('HP hero=', 10, 'hp', start)
    m = re.search(r'HP hero=(-?\d+) max=(-?\d+)', line)
    return f'{m[1]}/{m[2]}' if m else line[:40]


USABLE_LINE = re.compile(r'USABLE name=(\S+) pos=(\S+) usefunc=(\S*) usetype=(\d+) item=(\d) door=(\d) open=(\d) lock=(\d) locked=(\d) user=(\S+) distance=(\d+)')


def usables(bot, radius=3000):
    start = bot.send(f'usables {radius}', 20)
    bot.line('USABLE end', 10, 'usables', start)
    return [dict(zip(('name', 'pos', 'usefunc', 'usetype', 'item', 'door', 'open', 'lock', 'locked', 'user', 'distance'), m.groups()))
            for m in USABLE_LINE.finditer(bot.log()[start:])]


def use_info(bot, name):
    return report_line(bot, 'useinfo '+name, ('USEINFO ', 'REFUSED useinfo'))


def usediag(copies, report, negative=False):
    """World interactions of the inventory (INVENTAR-SKRIPTE.md, agent A): what can be used around the start, and what
    happens on both machines when the host, the client or both use an object (G3_USE: entity name, G3_WHO:
    host|client|both, G3_USE_WAIT: seconds). Without G3_USE: the list only. Diagnostic."""
    host, client = join_both(copies)
    name = os.environ.get('G3_USE', '')
    if not name:
        for copy in (host, client):
            found = usables(copy.bot, os.environ.get('G3_USE_RADIUS', '4000'))
            from collections import Counter
            report[copy.tag+'_usables'] = len(found)
            report[copy.tag+'_end'] = next((l for l in copy.bot.log().splitlines()[::-1] if l.startswith('USABLE end')), '')
            report[copy.tag+'_kinds'] = Counter(f"{u['usefunc']}/{u['usetype']}/item{u['item']}/door{u['door']}" for u in found).most_common(40)
            report[copy.tag+'_nonitems'] = sorted({u['name'] for u in found if u['item'] == '0'})[:80]
            report[copy.tag+'_examples'] = {}
            for u in found:
                report[copy.tag+'_examples'].setdefault(f"{u['usefunc']}/{u['usetype']}", f"{u['name']} {u['distance']} cm item={u['item']} door={u['door']} open={u['open']} locked={u['locked']}")
        report['status_note'] = 'diagnostic only'
        return
    who = os.environ.get('G3_WHO', 'host')
    wait = float(os.environ.get('G3_USE_WAIT', '6'))
    if os.environ.get('G3_USE_CALM'):  # after the opening raid (beds refuse during a fight)
        report['killraiders'] = report_line(host.bot, 'killraiders', ('KILLRAIDERS', 'REFUSED killraiders'))
        report_line(host.bot, 'farkill Ardea_Orkboss', ('FARKILL', 'REFUSED farkill'))
        time.sleep(8)

    def vitals(copy):
        clock = report_line(copy.bot, 'clock', ('CLOCK ',))
        m = re.search(r'day=(\d+) hour=(\d+) minute=(\d+)', clock)
        return {'hp': state_hp(copy.bot), 'time': f'{m[1]}/{m[2]}:{int(m[3]):02d}' if m else clock}

    def puppet_motion(copy, other_id):
        lines = [l for l in copy.mod_log().splitlines()[-400:] if f'TRACE puppet{other_id} ' in l]
        m = re.search(r' motion=(\S+)', lines[-1]) if lines else None
        return m[1] if m else '-'

    report['vitals_before'] = {c.tag: vitals(c) for c in (host, client)}
    report['before'] = {c.tag: use_info(c.bot, name) for c in (host, client)}
    users = [host] if who == 'host' else [client] if who == 'client' else [host, client]
    report['use'] = {c.tag: report_line(c.bot, 'use '+name, ('USE ', 'REFUSED use')) for c in users}
    samples = []
    began = time.monotonic()
    until = began + wait
    choose = os.environ.get('G3_USE_CHOOSE', '')  # a dialog the use opens (the bed asks how long): option index
    chosen = False
    while time.monotonic() < until:
        time.sleep(1)
        if choose and not chosen and time.monotonic() - began > 5:
            chosen = True
            report['dialog'] = {}
            for c in users:
                state_ = report_line(c.bot, 'talkstate', ('TALKSTATE ', 'REFUSED talkstate'))
                pick = report_line(c.bot, 'talkchoose '+choose, ('TALKCHOOSE ', 'REFUSED talkchoose')) if 'running=1' in state_ else '-'
                report['dialog'][c.tag] = [state_, pick]
        sample = {c.tag: re.sub(r'name=\S+ pos=\S+ usefunc=\S+ usetype=\d+ item=\d ', '', use_info(c.bot, name)[8:]) for c in (host, client)}
        sample['host_sees_client'] = puppet_motion(host, 2)
        sample['client_sees_host'] = puppet_motion(client, 1)
        samples.append(sample)
    report['samples'] = samples
    report['after'] = {c.tag: use_info(c.bot, name) for c in (host, client)}
    report['vitals_after'] = {c.tag: vitals(c) for c in (host, client)}
    report['status_note'] = 'diagnostic only'


def talk_infos(copy, npc):
    """Talk to an NPC (QA talk): its offered infos, without the ':xp' marks."""
    line = report_line(copy.bot, 'talk ' + npc, ('TALK ', 'REFUSED talk'))
    m = re.search(r' infos=(\S+)', line)
    return line, ([] if not m or m[1] == '-' else [i.split(':')[0] for i in m[1].split(',')])


def start_quest(copy, npc, quest_id, report, tag, chain):
    """The player at `copy` takes `quest_id`: through `chain`, the infos of `npc` chosen one after the other in one talk
    (an info's child is offered only after its parent; infoscan type_5 names a quest's Open info and its parent), or with
    chain 'runquest' through the quest manager (a quest the game starts outside a dialog, Hamlar_gotoReddock)."""
    if chain == ['runquest']:
        report[tag + '_start'] = quest(copy.bot, 'runquest', quest_id)
    else:
        teleport_to(copy.bot, npc)
        time.sleep(2)
        report[tag + '_talk'], infos = talk_infos(copy, npc)
        report[tag + '_chain'] = []
        for info in chain:
            line = report_line(copy.bot, 'talkinfo ' + info, 'TALKINFO ')
            if ' ok=0' in line:  # not on offer in this talk (a sibling ended it): talk again and choose it then
                report[tag + '_talk_again'], _ = talk_infos(copy, npc)
                line = report_line(copy.bot, 'talkinfo ' + info, 'TALKINFO ')
            report[tag + '_chain'].append(line[:120])
            time.sleep(4)
        report_line(copy.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))
    status = quest(copy.bot, 'quest', quest_id)
    assert status == RUNNING, f'{quest_id} does not run on the {copy.tag} ({status}): ' + str(report.get(tag + '_chain'))


def deliver_by_dialog(copy, npc, info, report, tag, until=None):
    """The player at `copy` talks to `npc` until it offers `info` (it walks on after a dialog) and chooses it."""
    infos = []
    for _ in range(6):
        teleport_to(copy.bot, npc)
        time.sleep(2)
        report[tag + '_talk'], infos = talk_infos(copy, npc)
        if info in infos:
            break
        report_line(copy.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))
        time.sleep(3)
    assert info in infos, f'{npc} does not offer {info}: ' + report[tag + '_talk']
    report[tag] = report_line(copy.bot, 'talkinfo ' + info, 'TALKINFO ')
    # The info's lines play out before the talk ends: its later lines pay (Give It_Gold), and ending the talk 4 s in cut the
    # reward (runs 15:29, 15:34). The talk itself stays open on the NPC's choices, so `until` says when the info is done.
    try:
        wait_for(lambda: (until() if until else False) and ['done'], 40, f'{info} plays out')
    except AssertionError:
        report[tag + '_until'] = 'timed out'
    report[tag + '_end'] = report_line(copy.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))


def stageBringItems(copies, report, negative=False):
    """P4 quest kind "bring items" (157 quests; Kapitel-Checkliste): one player takes the quest, the other brings the items
    and hands them in; the quest succeeds for both, the experience goes to both, the reward gold only to the one who hands
    in, the items leave his inventory. Default Norris_BringWeapons in Reddock (Open info BPANKRATZ31240 after Norris's
    infos 31237 to 31239, It_WeaponPack, hand-in BPANKRATZ31243 with 300 gold): the host takes it and leaves, the client brings.
    G3_QUEST, G3_NPC, G3_START (infos, comma separated, or runquest), G3_ITEM, G3_COUNT, G3_HANDIN, G3_TAKER=client.
    Negative control: storyoff on the client (quest states not shared)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    quest_id = os.environ.get('G3_QUEST', 'Norris_BringWeapons')
    npc = os.environ.get('G3_NPC', 'Norris')
    chain = os.environ.get('G3_START', 'runquest').split(',')
    item, count = os.environ.get('G3_ITEM', 'It_WeaponPack'), int(os.environ.get('G3_COUNT', '5'))
    handin = os.environ.get('G3_HANDIN', 'BPANKRATZ31243')
    taker = client if os.environ.get('G3_TAKER') == 'client' else host
    bringer = host if os.environ.get('G3_BRINGER') == 'host' else client  # host: the single-player control
    other = client if bringer is host else host
    if negative:
        story_off(client)
    for copy in (host, client):
        report_line(copy.bot, 'unarm', 'UNARM')
    start_quest(taker, npc, quest_id, report, 'taker', chain)
    if taker is not bringer:
        teleport_to(taker.bot, os.environ.get('G3_HOME_PLACE', 'Diego'))  # the taker leaves; the other brings the items alone
        time.sleep(4)
    report['bringer_knows'] = wait_for(lambda: quest(bringer.bot, 'quest', quest_id) == RUNNING and ['yes'], 20,
                                       f'the quest runs on the {bringer.tag} too')[0]
    for _ in range(count):
        report_line(bringer.bot, 'receiveitem ' + item, ('RECEIVEITEM ', 'REFUSED receiveitem'))
    report['items_before'] = carried(bringer.bot, item)
    time.sleep(3)
    report['xp_before'] = {c.tag: xp_of(c.bot) for c in (host, client)}
    report['gold_before'] = {c.tag: carried(c.bot, 'It_Gold') for c in (host, client)}
    deliver_by_dialog(bringer, npc, handin, report, 'handin',
                      until=lambda: carried(bringer.bot, 'It_Gold') > report['gold_before'][bringer.tag])
    for copy in (host, client):
        wait_for(lambda: quest(copy.bot, 'quest', quest_id) == SUCCESS and ['yes'], 40, f'the quest succeeds on the {copy.tag}')
    time.sleep(4)
    report['xp_after'] = {c.tag: xp_of(c.bot) for c in (host, client)}
    report['gold_after'] = {c.tag: carried(c.bot, 'It_Gold') for c in (host, client)}
    report['items_after'] = carried(bringer.bot, item)
    report['bringer_log'] = [l for l in bringer.mod_log().splitlines() if 'infocmd:' in l and npc in l][-8:]
    gained = {t: report['xp_after'][t] - report['xp_before'][t] for t in ('host', 'client')}
    report['xp_gained'] = gained
    assert report['items_after'] < report['items_before'], 'the items stayed with the bringer'
    assert gained['host'] == gained['client'] and gained['client'] > 0, f'experience: {gained}'
    assert report['gold_after'][bringer.tag] > report['gold_before'][bringer.tag], f'the {bringer.tag} got no reward gold'
    assert report['gold_after'][other.tag] == report['gold_before'][other.tag], f'the {other.tag} got gold too'


def stageReportQuest(copies, report, negative=False):
    """P4 quest kind "report" (130 quests; Kapitel-Checkliste): one player has the quest, the other reports to the
    addressee; the quest succeeds for both, with the same experience. Default Hamlar_gotoReddock (Hamlar in Ardea sends
    the hero to Javier in Reddock; the game starts it outside a dialog, so the host runs it through the quest manager;
    Delivery info FILLER938 at Javier): the client reports. G3_QUEST, G3_NPC (giver), G3_START, G3_TO (addressee),
    G3_DELIVERY, G3_TAKER=client. Negative control: storyoff on the client."""
    host, client = join_both(copies)
    clear_raid(host, report)
    quest_id = os.environ.get('G3_QUEST', 'Hamlar_gotoReddock')
    giver, to = os.environ.get('G3_NPC', 'Hamlar'), os.environ.get('G3_TO', 'Javier')
    chain = os.environ.get('G3_START', 'runquest').split(',')
    delivery = os.environ.get('G3_DELIVERY', 'FILLER938')
    taker = client if os.environ.get('G3_TAKER') == 'client' else host
    reporter = host if taker is client else client
    if negative:
        story_off(client)
    for copy in (host, client):
        report_line(copy.bot, 'unarm', 'UNARM')
    start_quest(taker, giver, quest_id, report, 'taker', chain)
    report['reporter_knows'] = wait_for(lambda: quest(reporter.bot, 'quest', quest_id) == RUNNING and ['yes'], 20,
                                        f'the quest runs on the {reporter.tag} too')[0]
    report['xp_before'] = {c.tag: xp_of(c.bot) for c in (host, client)}
    deliver_by_dialog(reporter, to, delivery, report, 'report', until=lambda: quest(reporter.bot, 'quest', quest_id) == SUCCESS)
    for copy in (host, client):
        wait_for(lambda: quest(copy.bot, 'quest', quest_id) == SUCCESS and ['yes'], 40, f'the quest succeeds on the {copy.tag}')
    time.sleep(6)
    report['xp_after'] = {c.tag: xp_of(c.bot) for c in (host, client)}
    gained = {t: report['xp_after'][t] - report['xp_before'][t] for t in ('host', 'client')}
    report['xp_gained'] = gained
    assert gained['host'] == gained['client'], f'experience differs: {gained}'


def stageArenaQuest(copies, report, negative=False):
    """P4 quest kind "arena" (35 quests; Kapitel-Checkliste): the client takes an arena quest in the dialog, steps into the
    arena, the opponent attacks (the game's OnArena: AssessTarget with reason Arena, 18), the client knocks it out: the quest
    succeeds for both, it is no murder, and the reward talk pays the client. Default Joey_Arena in Reddock (Joey: 31249 tells
    his duties, 31250 opens the quest, 31253 pays). G3_HOST_NEAR=1: the host stands at the arena too, so Joey is the host's
    NPC (OnArena looks at Entity::GetPlayer, the host's hero). Negative control: storyoff on the client."""
    quest_id = os.environ.get('G3_QUEST', 'Joey_Arena')
    name = os.environ.get('G3_TARGET', 'Joey')
    chain = os.environ.get('G3_START', 'BPANKRATZ31249,BPANKRATZ31250').split(',')
    payer = os.environ.get('G3_PAY', 'BPANKRATZ31253')
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        story_off(client)
    for copy in (host, client):
        report_line(copy.bot, 'unarm', 'UNARM')
    near = os.environ.get('G3_HOST_NEAR') == '1'
    if near:
        report['host_at'] = teleport_to(host.bot, name)
    report['client_at'] = teleport_to(client.bot, name)
    time.sleep(6)
    report['talks'] = []
    tried = []
    if os.environ.get('G3_START', 'runquest') == 'runquest':
        # 31250 was not offered after 31249 (conditions the info data do not show; runs 15:5x): open the quest as 31250
        # does, the quest manager's RunQuest and its event Joey_IsHot, on the client.
        chain = []
        report['talks'].append(report_line(client.bot, 'runquest ' + quest_id, 'QUEST '))
        for event in os.environ.get('G3_START_EVENTS', 'Joey_HisDuties,Joey_IsHot').split(','):
            report['talks'].append(report_line(client.bot, 'setevent ' + event, 'EVENT '))
    for _ in range(6 if chain else 0):  # one info per talk, the wanted ones first, then whatever Joey offers, until the quest runs
        teleport_to(client.bot, name)
        time.sleep(2)
        line, infos = talk_infos(client, name)
        report['talks'].append(line[:200])
        todo = [i for i in chain if i in infos and i not in tried] + [i for i in infos if i not in tried and i != 'BPANKRATZ30']
        if not todo:
            break
        tried.append(todo[0])
        report['talks'].append(report_line(client.bot, 'talkinfo ' + todo[0], 'TALKINFO ')[:120])
        time.sleep(8)
        report_line(client.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))
        time.sleep(2)
        if quest(client.bot, 'quest', quest_id) == RUNNING:
            break
    report['status_started'] = {c.tag: quest(c.bot, 'quest', quest_id) for c in (host, client)}
    assert report['status_started']['client'] == RUNNING, f'{quest_id} does not run on the client: {report["talks"]}'
    owner = host if near else client
    guid = next((g for g, v in npcs(owner.bot).items() if v['name'] == name and not v['dead']), None)
    assert guid, f'{name} is not among the {owner.tag} NPCs'
    report['owner'] = owner.tag
    striker = host if os.environ.get('G3_STRIKER') == 'host' else client  # host: the single-player control (its own Joey)
    report['striker'] = striker.tag
    report_line(striker.bot, 'arm ' + WEAPON, 'ARM ')
    if striker is host:
        teleport_to(host.bot, name)
        time.sleep(2)
    tasks = []
    for _ in range(20):  # the opponent turns on the client in the arena
        time.sleep(1.5)
        v = npcs(owner.bot).get(guid, {})
        tasks.append(v.get('task'))
        if v.get('task') and v['task'] not in ('ZS_HangAround', 'ZS_Ambient', 'ZS_Arena', 'ZS_Arena_Loop', 'ZS_Talk', 'ZS_Talk_Loop'):
            break
    report['opponent_tasks'] = tasks
    report_line(owner.bot, f'npchp {name}_30', ('NPCHP ', 'STATUSOF ', 'REFUSED'))
    report['hits'] = []
    state = {}
    for _ in range(25):
        hit = hit_npc(striker.bot, guid)
        report['hits'].append(hit['line'][-90:])
        # Stop at 0 hit points: one more blow on the knocked-out Joey is the finishing blow (P7), a murder (runs 16:00).
        if hit.get('after', 1) <= 0 or hit.get('dead'):
            break
        time.sleep(0.8)
        state = npcs(owner.bot).get(guid, {})
        if state.get('dead') or state.get('task') in KO_TASKS:
            break
        time.sleep(0.6)
    try:
        state = wait_for(lambda: (lambda v: (v.get('dead') or v.get('task') in KO_TASKS) and [v])(npcs(owner.bot).get(guid, {})),
                         10, f'{name} goes down on the {owner.tag}')[0]
    except AssertionError:
        state = npcs(owner.bot).get(guid, {})
    report['state'] = state
    report['murders'] = {c.tag: [l for l in c.mod_log().splitlines() if 'murder' in l.lower()][-4:] for c in (host, client)}
    assert state.get('task') in KO_TASKS and not state.get('dead'), f'{name} was not knocked out: {state}'
    try:
        wait_for(lambda: quest(client.bot, 'quest', quest_id) == SUCCESS and ['yes'], 30, f'{quest_id} is a success for the client')
        wait_for(lambda: quest(host.bot, 'quest', quest_id) == SUCCESS and ['yes'], 20, f'{quest_id} is a success for the host too')
    finally:
        report['after'] = {c.tag: quest(c.bot, 'quest', quest_id) for c in (host, client)}


def find_all(bot, name):
    """QA findall: [(pos, dead)] of every entity of that name in the world, processed or not."""
    start = bot.send('findall ' + name, 20)
    bot.line('FINDALL end', 10, 'findall', start)
    return [(tuple(float(v) for v in m[1].split('_')), m[2] == '1')
            for m in re.finditer(r'FINDALL name=\S+ pos=(\S+) dead=(\d)', bot.log()[start:])]


def stageKillQuest(copies, report, negative=False):
    """Quest type 2, kill (114 quests, Kapitel-Checkliste): the host takes the quest, the client alone kills every target
    with the game's damage path (hitnpc, sword); the quest is a success on both machines, the host earns its experience
    once and as much as the client (whose kills pay only him), and the host's counter stands at the number of targets (no
    kill counted twice). Default Zakosh_KillTrolls (three Faring_Troll_01 in the hills before Faring; the quest pays 2500,
    the table's 500 five times) far from the host: the trolls are the client's own NPCs and the death script runs only there
    (stageQ checked one count). G3_HOST_NEAR=1: the host stands by each target first, so they are the host's NPCs and the
    client's hits are claims the host applies. G3_QUEST, G3_TARGET. Negative control: questcountoff on the host (it does not
    count the client's kills; storyoff on the client is no control: its own quest manager counts the kills even for a quest it
    never took and pays the reward, as in single-player when the targets die before the quest is given, run 16:40)."""
    quest_id = os.environ.get('G3_QUEST', 'Zakosh_KillTrolls')
    target = os.environ.get('G3_TARGET', 'Faring_Troll_01')
    near = os.environ.get('G3_HOST_NEAR') == '1'
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report_line(host.bot, 'questcountoff', 'QUESTCOUNTOFF ')
    for copy in (host, client):
        report_line(copy.bot, 'unarm', 'UNARM')
    report['run'] = quest(host.bot, 'runquest', quest_id)
    assert report['run'] == RUNNING, f'the host could not start {quest_id}'
    wait_for(lambda: quest(client.bot, 'quest', quest_id) == RUNNING and ['yes'], 20, f'{quest_id} runs on the client too')
    spots = [pos for pos, dead in find_all(client.bot, target) if not dead]
    report['spots'] = [tuple(round(v) for v in s) for s in spots]
    assert spots, f'no living {target} in the world'
    report['xp_before'] = {c.tag: xp_of(c.bot) for c in (host, client)}
    log_from = {c.tag: len(c.mod_log().splitlines()) for c in (host, client)}
    owner = host if near else client
    report['owner'] = owner.tag
    report['kills'] = []
    for spot in spots:
        at = (spot[0] + 300, spot[1] + 50, spot[2])
        for copy in ((host, client) if near else (client,)):  # the host first: an NPC stays with who processed it first (E1)
            move_hero(copy.bot, at)
            report_line(copy.bot, 'sethp 1000', 'SETHP ')
            time.sleep(4)
        time.sleep(3)
        rows = npcs(owner.bot)
        guid = min((g for g, v in rows.items() if v['name'] == target and not v['dead']),
                   key=lambda g: distance(rows[g]['pos'], spot), default=None)
        kill = {'spot': tuple(round(v) for v in spot), 'guid': guid}
        report['kills'].append(kill)
        assert guid, f'no living {target} at {kill["spot"]} on the {owner.tag}'
        kill['mirrored_on_client'] = npcs(client.bot).get(guid, {}).get('mirrored')
        report_line(owner.bot, f'npchp {guid}_30', ('NPCHP ', 'STATUSOF ', 'REFUSED'))
        report_line(client.bot, 'arm ' + WEAPON, 'ARM ')
        kill['xp'] = [xp_of(client.bot)]
        hits = []
        for _ in range(25):
            hit = hit_npc(client.bot, guid)
            hits.append(hit['line'][-80:])
            if hit.get('dead') or npcs(owner.bot).get(guid, {}).get('dead'):
                break
            time.sleep(0.8)
        kill['hits'] = len(hits)
        kill['last_hit'] = hits[-1]
        wait_for(lambda: npcs(owner.bot).get(guid, {}).get('dead') and ['yes'], 10, f'{target} dies on the {owner.tag}')
        time.sleep(1)
        kill['xp'].append(xp_of(client.bot))
        report_line(client.bot, 'unarm', 'UNARM')
    try:
        wait_for(lambda: quest(host.bot, 'quest', quest_id) == SUCCESS and ['yes'], 30, f'{quest_id} is a success for the host')
        wait_for(lambda: quest(client.bot, 'quest', quest_id) == SUCCESS and ['yes'], 20, f'{quest_id} is a success for the client too')
    finally:
        report['after'] = {c.tag: quest(c.bot, 'quest', quest_id) for c in (host, client)}
        time.sleep(4)
        report['xp_after'] = {c.tag: xp_of(c.bot) for c in (host, client)}
        report['xp_log'] = {c.tag: [l[-110:] for l in c.mod_log().splitlines()[log_from[c.tag]:] if 'xp: GiveXP' in l] for c in (host, client)}
        start = host.bot.send('questcounters ' + quest_id, 20)
        report['host_counter'] = host.bot.line('QUESTCOUNTER name=', 15, 'questcounters', start)
    gained = {t: report['xp_after'][t] - report['xp_before'][t] for t in ('host', 'client')}
    report['xp_gained'] = gained
    rewards = {t: [l for l in report['xp_log'][t] if 'self=World_MCP' in l] for t in ('host', 'client')}
    assert all(len(r) == 1 for r in rewards.values()), f'quest rewards paid: {rewards}'
    assert gained['host'] > 0, f'the host earned nothing: {gained}'
    if len(spots) > 1:  # the client's kills pay him alone; the last kill and the reward land together, so it counts as the others
        each = [k['xp'][1] - k['xp'][0] for k in report['kills'][:-1]]
        report['client_kill_xp'] = each
        reward = gained['client'] - sum(each) - sum(each) // len(each)
        assert reward == gained['host'], f'the client earned {reward} for the quest, the host {gained["host"]}'
    counted = re.search(re.escape(target) + r':(-?\d+)/', report['host_counter'])
    assert counted and int(counted[1]) == len(spots), f'host counter: {report["host_counter"]}'


def stageArenaStart(copies, report, negative=False):
    """P4 arena (35 quests; Kapitel-Checkliste: the fight's start by entering the arena): the client takes an arena fight in
    the dialog while the host stands by and simulates the opponent. As in the single-player game the opponent walks into
    the arena (ZS_Arena), attacks once the client steps in, the client knocks it out: the quest succeeds on both, no
    murder, and afterwards the opponent is the host's again. Before (arenastartdiag 25.10.) the client's dialog sent only
    its mirror, Copper never went into the arena. Default Copper_Arena in Reddock (31259, its child 31260, then the
    Activator 31261 in a new talk). G3_QUEST, G3_TARGET, G3_CHAIN (| between talks), G3_ARENA. Negative control:
    arenasyncoff on the client."""
    quest_id, name = os.environ.get('G3_QUEST', 'Copper_Arena'), os.environ.get('G3_TARGET', 'Copper')
    chain = os.environ.get('G3_CHAIN', 'BPANKRATZ31259,BPANKRATZ31260|BPANKRATZ31261')
    arena = os.environ.get('G3_ARENA', 'Reddock_Arena')
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report['negative'] = report_line(client.bot, 'arenasyncoff', 'ARENASYNCOFF ')
    for copy in (host, client):
        report_line(copy.bot, 'unarm', 'UNARM')
    report['host_at'] = teleport_to(host.bot, name)
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 1200, y, z))  # near enough to simulate the opponent, out of the arena
    time.sleep(4)
    assert not next((v for v in npcs(host.bot).values() if v['name'] == name), {}).get('mirrored', True), f'the host does not simulate {name}'
    for talk in chain.split('|'):
        take_quest(client, name, talk.split(','), report)
        time.sleep(2)
    report['quest_started'] = {c.tag: quest_status(c.bot, quest_id) for c in (host, client)}
    assert report['quest_started']['client'] == RUNNING, f'{quest_id} does not run on the client: {report.get("talks")}'
    state = lambda bot: dict(re.findall(r'(\w+)=(\S+)', find_npc(bot, name)))
    report['to_arena'] = wait_for(lambda: (lambda s: s.get('task', '').startswith('ZS_Arena') and [s])(state(client.bot)), 30,
                                  f'{name} goes into the arena on the client')[0]
    report_line(client.bot, 'sethp 1000', 'SETHP ')
    report['in_arena'] = teleport_to(client.bot, arena)
    ax, ay, az = hero_pos(client.bot)
    for dx in (150, -150, 0):  # steps inside: walkto's 15 m left the arena and lost the quest (status 6, run 17:26)
        move_hero(client.bot, (ax + dx, ay + 20, az))
        time.sleep(1)
    report['attacks'] = wait_for(lambda: (lambda s: s.get('target_hero') == '1' and [s])(state(client.bot)), 40,
                                 f'{name} attacks the client in the arena')[0]
    report['host_sees'] = state(host.bot)
    guid = next((g for g, v in npcs(client.bot).items() if v['name'] == name and not v['dead']), None)
    assert guid, f'{name} is not among the client NPCs'
    report_line(client.bot, 'arm ' + WEAPON, 'ARM ')
    # One blow while the bout is on: a second one sometimes killed (IsDeadlyDamage judges by the attitude at that moment,
    # re/findings/attitude-callers.md; Copper died at the second blow in run 174123, as Joey did for the host itself).
    report_line(client.bot, f'npchp {guid}_5', ('NPCHP ', 'STATUSOF ', 'REFUSED'))
    report['hits'] = []
    for _ in range(25):
        report_line(client.bot, 'arm ' + WEAPON, 'ARM ')  # his blows knock the sword out of the hand (stageA4)
        hit = hit_npc(client.bot, guid)
        report['hits'].append(hit['line'][-90:])
        if hit.get('after', 1) <= 0 or hit.get('dead'):  # no blow on the knocked-out opponent: that one kills (P7)
            break
        time.sleep(1.2)
    report_line(client.bot, 'unarm', 'UNARM')
    report['down'] = wait_for(lambda: (lambda s: (s.get('dead') == '1' or s.get('task') in KO_TASKS) and [s])(state(client.bot)), 15,
                              f'{name} goes down')[0]
    if report['down'].get('dead') == '1':
        # The game's own roll: even one blow at 5 hit points killed (run 185221, bare hands against a power attack); the fight
        # started and ran on the client, so the outcome has to be the same on both machines.
        report['outcome'] = 'died'
        final = wait_for(lambda: (lambda q: q != RUNNING and [q])(quest_status(client.bot, quest_id)), 30, f'{quest_id} ends on the client')[0]
        report['after'] = wait_for(lambda: (lambda q: q == final and {'client': final, 'host': q})(quest_status(host.bot, quest_id)), 20,
                                   f'{quest_id} ends the same on the host')[0]
        return
    report['outcome'] = 'knocked out'
    try:
        wait_for(lambda: quest_status(client.bot, quest_id) == SUCCESS and ['yes'], 30, f'{quest_id} is a success for the client')
        wait_for(lambda: quest_status(host.bot, quest_id) == SUCCESS and ['yes'], 20, f'{quest_id} is a success for the host too')
    finally:
        report['after'] = {c.tag: quest_status(c.bot, quest_id) for c in (host, client)}
        report['murders'] = {c.tag: [l[-120:] for l in c.mod_log().splitlines() if 'murder +' in l and 'murder +0' not in l][-4:] for c in (host, client)}
    report['given_back'] = wait_for(lambda: (lambda s: s.get('mirrored') == '1' and [s])(state(client.bot)), 60,
                                    f'{name} is the host\'s again after the fight')[0]
    report['arenasync'] = report_line(client.bot, 'arenasync', 'ARENASYNC ')
    assert not any(report['murders'].values()), f'the fight counted as a murder: {report["murders"]}'


def watch_scripts(c, names):
    """QA scriptwatch: count calls of these scripts through the script table on this machine."""
    return [report_line(c.bot, 'scriptwatch ' + n, 'SCRIPTWATCH ')[12:90] for n in names]


def watched_scripts(c):
    """{script: calls} of every script this machine counts (QA scriptwatch)."""
    start = c.bot.send('scriptwatch', 20)
    c.bot.line('SCRIPTWATCH end', 10, 'scriptwatch', start)
    return {m[1]: int(m[2]) for m in re.finditer(r'SCRIPTWATCH name=(\S+) calls=(\d+)', c.bot.log()[start:])}


def stageRevolution(copies, report, negative=False):
    """P4/P7 (OnRevolutionAttack, GetRevolutionAttitude, ZS_RevolutionSuccess; the coop-evening list): a town's uprising
    mid-game with the story set by QA instead of played. From the post-raid save both stand at G3_GOTO (default Phil in
    Kap Dun; the host simulates the town). The starter (G3_WHO, default client; host: the single-player reference) raises the
    town as the rebel leader's dialog does (QA revolution <town> = SetRevolutionEnabled <town> true) and starts the fight as
    the dialog command StartRevolution does (QA startrevolution <town>: raid, status Attack 1; Kap Dun's own dialog has only
    the first, a kill of a defender did not turn the town 26.10.). The defenders then take ZS_RevolutionAttack (task
    callback OnRevolutionAttack; re/out/hero5-scriptgame.c, hero2-scriptgame.c). Then the host's far members fall (QA
    townfarkill, the shortcut for the rest of the fight) and the starter kills the near defenders left (the client: lethal
    claims, hitnpcburst; the host: farkill): the town flees (2), the rest cheer (ZS_RevolutionSuccess). Counted on both
    machines: the ZS_Revolution states (QA aicount) and GetRevolutionAttitude at its code (QA nativewatch). Checked: Attack
    and then Flee on both PCs, ZS_RevolutionAttack ran and GetRevolutionAttitude was asked on the host (nobody cheers on this
    road, the host alone neither: ZS_RevolutionSuccess only reported). Negative control: townstatusoff
    on both and infocmdoff on the client (the host never hears of the uprising)."""
    town, goto = os.environ.get('G3_TOWN', 'KapDun'), os.environ.get('G3_GOTO', 'Phil')
    host, client = join_both(copies)
    clear_raid(host, report)
    starter = host if os.environ.get('G3_WHO') == 'host' else client
    report['starter'] = starter.tag
    if negative:
        report['negative'] = [report_line(c.bot, 'townstatusoff', 'TOWNSTATUSOFF ') for c in (host, client)]
        report['negative'].append(report_line(client.bot, 'infocmdoff', 'INFOCMDOFF '))
    for c in (host, client):
        report[c.tag + '_watch'] = native_watch(c.bot, ['GetRevolutionAttitude'])
        report_line(c.bot, 'aicount ZS_Revolution', ('AICOUNT end',))
        teleport_to(c.bot, goto)
    report_line(starter.bot, 'arm ' + WEAPON, 'ARM ')
    time.sleep(8)
    defenders = {m['guid']: m['member'] for m in town_members(host.bot, town) if m['alive'] == '1' and m.get('counts') == '1'}
    report['defenders'] = len(defenders)
    report['before'] = {c.tag: town_status(c.bot, town) for c in (host, client)}
    report['revolution'] = report_line(starter.bot, 'revolution ' + town, ('REVOLUTION ', 'REFUSED revolution'))
    time.sleep(2)
    report['start'] = report_line(starter.bot, 'startrevolution ' + town, ('STARTREVOLUTION ', 'REFUSED startrevolution'))

    def kill(guid):
        if starter is client:
            return report_line(client.bot, f'hitnpcburst {guid}_1', ('HITNPCBURST ', 'REFUSED hitnpcburst'))[:80]
        return report_line(host.bot, 'farkill ' + guid, ('FARKILL ', 'REFUSED farkill'))[:80]

    try:
        wait_for(lambda: town_status(host.bot, town) == 1 and ['yes'], 20, f'{town} under attack (1) on the host')
        report['attack_host'] = 1
        wait_for(lambda: town_status(client.bot, town) == 1 and ['yes'], 20, f'{town} under attack on the client too')
    finally:
        time.sleep(15)
        report['attack'] = {c.tag: {'status': town_status(c.bot, town), 'states': ai_counts(c.bot),
                                    'native': native_counts(c.bot).get('GetRevolutionAttitude')} for c in (host, client)}
        fighting = {v['name'] for v in npcs(host.bot).values() if 'Revolution' in v['task']}
        report['attack_tasks'] = sorted({v['task'] for v in npcs(host.bot).values() if 'Revolution' in v['task'] or 'Attack' in v['task']})
        report['attack_targets'] = {n: re.sub(r'FIND name=\S+ ', '', find_npc(host.bot, n))[:220] for n in sorted(fighting)[:4]}
    report['farkill'] = report_line(host.bot, 'townfarkill ' + town, ('TOWNFARKILL ', 'REFUSED townfarkill'))
    for k in range(6):  # every defender still standing, by GUID (the client: claims, also on far ones)
        left = [m['guid'] for m in town_members(host.bot, town) if m['alive'] == '1' and m.get('counts') == '1']
        report.setdefault('left', []).append(len(left))
        if not left:
            break
        report.setdefault('kills', []).extend(kill(g) for g in left)
        if starter is client:
            report_line(client.bot, 'sethp 1000', 'SETHP ')
        time.sleep(4)
        if k == 0:
            fighting = {v['name'] for v in npcs(host.bot).values() if 'Revolution' in v['task']}
            report['fight_tasks'] = sorted({v['task'] for v in npcs(host.bot).values() if 'Revolution' in v['task'] or 'Attack' in v['task']})
            report['fight_targets'] = {n: re.sub(r'FIND name=\S+ ', '', find_npc(host.bot, n))[:220] for n in sorted(fighting)[:4]}
    try:
        wait_for(lambda: town_status(host.bot, town) == 2 and ['yes'], 40, f'{town} flees (2) on the host')
        wait_for(lambda: town_status(client.bot, town) == 2 and ['yes'], 20, f'{town} flees on the client too')
    finally:
        time.sleep(8)
        report['flee'] = {c.tag: {'status': town_status(c.bot, town), 'states': ai_counts(c.bot),
                                  'native': native_counts(c.bot).get('GetRevolutionAttitude')} for c in (host, client)}
        cheering = {v['name'] for v in npcs(host.bot).values() if 'RevolutionSuccess' in v['task']}
        report['flee_tasks'] = sorted({v['task'] for v in npcs(host.bot).values() if 'Revolution' in v['task'] or 'Flee' in v['task']})
        report['cheer'] = {n: {c.tag: re.sub(r'FIND name=\S+ ', '', find_npc(c.bot, n))[:220] for c in (host, client)} for n in sorted(cheering)[:3]}
    # ZS_RevolutionAttack counted over the whole fight (the host alone took it only after the first kills, 26.10. 13:37);
    # nobody cheered on this road even for the host alone (ZS_RevolutionSuccess 0): reported, not required.
    done = report['flee']['host']['states']
    report['cheered'] = done.get('ZS_RevolutionSuccess', 0)
    assert done.get('ZS_RevolutionAttack', 0) > 0, f'no defender took ZS_RevolutionAttack on the host: {done}'
    asked = int((report['flee']['host']['native'] or {}).get('calls', 0))
    assert asked > 0, f"GetRevolutionAttitude never asked on the host: {report['flee']['host']['native']}"


def stageSectorRefresh(copies, report, negative=False):
    """Basis (OnEnclaveSectorRefresh): the story moves after a town changes hands. For Ardea (freed, not Switched) the
    script moves Diego to Braga, Gorn to GothaPrison, Milten to the monastery and Lester away once the game's player is more
    than 420 m from the town (Script_Game 0x100b16f0, threshold 42000 at 0x101076c0; re/out/basis-rest-20261026-b.c;
    sectorrefreshdiag went only 400 m). From the post-raid save (Ardea freed): G3_WHO (default client) goes 700 m off while
    the other stays at Ardea, then the other follows. The friends on both machines after each step (QA findnpc), the calls
    of OnEnclaveSectorRefresh (QA scriptwatch). Checked: Diego, Gorn, Milten have moved (> 50 m from where they stood) on
    both machines, and the two agree (< 10 m); before 26.10. (coop-evening) the client kept its copies at Ardea, 1748 m from
    the host's (the script moves them with SetRoutine and MoveTo on the machine whose player left; infocmd.cpp now passes
    the host's moves on). Negative control: storymoveoff on the host."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report['negative'] = report_line(host.bot, 'storymoveoff', 'STORYMOVEOFF ')
    friends = ('Diego', 'Gorn', 'Milten', 'Lester')

    def look(c):
        out = {}
        for n in friends:
            line = find_npc(c.bot, n)
            m = re.search(r'pos=(-?\d+)_(-?\d+)_(-?\d+)', line)
            out[n] = {'pos': tuple(map(float, m.groups())) if m else None, 'task': (re.search(r'task=(\S+)', line) or [0, '-'])[1],
                      'mirrored': (re.search(r'mirrored=(\d)', line) or [0, '-'])[1]}
        return out
    for c in (host, client):
        report[c.tag + '_watch'] = watch_scripts(c, ['OnEnclaveSectorRefresh'])
    start = {c.tag: look(c) for c in (host, client)}
    report['start'] = start
    first = host if os.environ.get('G3_WHO') == 'host' else client
    second = client if first is host else host
    for tag, goer in (('first', first), ('second', second)):
        x, y, z = hero_pos(goer.bot)
        move_hero(goer.bot, (x + 70000, y + 300, z))
        time.sleep(25)
        report[tag] = {c.tag: look(c) for c in (host, client)}
        report[tag + '_who'] = goer.tag
        report[tag + '_calls'] = {c.tag: watched_scripts(c) for c in (host, client)}
    # both back at Ardea: whoever simulates the friends now shows them to the other
    for c in (host, client):
        x, y, z = hero_pos(c.bot)
        move_hero(c.bot, (x - 70000, y - 300, z))
    time.sleep(20)
    end = {c.tag: look(c) for c in (host, client)}
    report['back'] = end
    report['back_calls'] = {c.tag: watched_scripts(c) for c in (host, client)}
    report['story_moves'] = {c.tag: report_line(c.bot, 'storymoves', 'STORYMOVES ')[11:] for c in (host, client)}
    moved = {}
    for n in ('Diego', 'Gorn', 'Milten'):
        h0, h1, c1 = start['host'][n]['pos'], end['host'][n]['pos'], end['client'][n]['pos']
        moved[n] = {'host_moved_m': round(distance(h0, h1) / 100) if h0 and h1 else None,
                    'client_vs_host_m': round(distance(h1, c1) / 100) if h1 and c1 else None}
    report['moved'] = moved
    assert all(m['host_moved_m'] is not None and m['host_moved_m'] > 50 for m in moved.values()), f'the friends stayed on the host: {moved}'
    assert all(m['client_vs_host_m'] is not None and m['client_vs_host_m'] < 10 for m in moved.values()), f'the client shows them elsewhere: {moved}'


def stageStoryCompanion(copies, report, negative=False):
    """Basis (OnEnclaveSectorRefresh), review 26.10. (point 8): a story move of the other player's companion. G3_PARTY
    (default Diego) joins the client (party-enabled by QA partyenable on the host, the client chooses SKALVERAM336, as in
    stagePartyJoin); the client takes him over and the host mirrors him. Then the host goes 700 m from freed Ardea: its
    script moves Diego to Braga and the host passes the move on (stageSectorRefresh). Before 26.10. the client moved its own
    companion too: he stood in Braga, still in the client's party. Now the client keeps him: STORYMOVES companions_kept >= 1,
    he is still near the client's hero (< 25 m), still claimed, and the host shows him there too (< 25 m from the client's).
    The host keeps its copy with the client too (its script moved it; infocmd.cpp puts it back and does not pass the move on;
    the first run 26.10. had the host's copy in Braga, 1796 m from the client's). Negative control: storycompanionoff on both
    (the move is taken as before)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    npc = os.environ.get('G3_PARTY', 'Diego')
    if negative:
        report['negative'] = [report_line(c.bot, 'storycompanionoff', 'STORYCOMPANIONOFF ') for c in (host, client)]
    teleport_to(host.bot, npc)
    teleport_to(client.bot, npc)
    time.sleep(8)
    wait_for(lambda: (npc_named(client.bot, npc)[1] or {}).get('mirrored') and ['yes'], 20, f'{npc} mirrored on the client')
    report['enable'] = report_line(host.bot, 'partyenable ' + npc, ('PARTYENABLE ', 'REFUSED partyenable'))
    time.sleep(3)
    joined = False
    for _ in range(4):
        teleport_to(client.bot, npc)
        time.sleep(2)
        line, infos = talk_infos(client, npc)
        report.setdefault('join_offered', []).append(line[:200])
        if 'SKALVERAM336' in infos:
            report['join'] = report_line(client.bot, 'talkinfo SKALVERAM336', ('TALKINFO ', 'REFUSED talkinfo'))
            wait_for(lambda: talkstate(client.bot).get('running') == '0', 60, 'the join dialog ends')
            joined = True
            break
        report_line(client.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))
        time.sleep(3)
    assert joined, f"{npc} never offered the join: {report.get('join_offered')}"
    time.sleep(4)
    report['joined'] = {c.tag: find_npc(c.bot, npc)[:300] for c in (host, client)}
    pos_of = lambda line: (lambda m: tuple(float(v) for v in m[1].split('_')) if m else None)(re.search(r'pos=(\S+)', line))
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 70000, y + 300, z))
    time.sleep(25)
    report['after'] = {c.tag: find_npc(c.bot, npc)[:300] for c in (host, client)}
    report['story_moves'] = {c.tag: report_line(c.bot, 'storymoves', 'STORYMOVES ')[11:] for c in (host, client)}
    hero = hero_pos(client.bot)
    on_client, on_host = pos_of(report['after']['client']), pos_of(report['after']['host'])
    report['client_companion_m'] = round(distance(on_client, hero) / 100) if on_client else None
    report['host_vs_client_m'] = round(distance(on_host, on_client) / 100) if on_host and on_client else None
    kept = sum(int((re.search(r'companions_kept=(\d+)', report['story_moves'][t]) or [0, '0'])[1]) for t in ('host', 'client'))
    calls = int((re.search(r'calls=(\d+)', report['story_moves']['host']) or [0, '0'])[1])
    assert calls >= 1, f"the host's script never ran: {report['story_moves']}"
    assert report['client_companion_m'] is not None and report['client_companion_m'] < 25, \
        f"the client's companion was moved away ({report['client_companion_m']} m): {report['after']['client']}"
    assert kept >= 1, f"nobody kept the companion: {report['story_moves']}"
    assert report['host_vs_client_m'] is not None and report['host_vs_client_m'] < 25, \
        f"the host shows {npc} elsewhere ({report['host_vs_client_m']} m): {report['after']['host']}"


def stageSwim(copies, report, negative=False):
    """P2 (PS_Swim; coop-evening list): the joining player swims forward and the host sees its puppet swim along. The client
    goes into the sea 160 m off the start beach (G3_WATER_SPOT, swimdiag 26.10.), the host stands on the shore
    (G3_WATER_SHORE); the client swims 15 m along and back by its own movement goal (QA swimgoal, no key input). Every
    0.5 s: the client's hero (position, movement mode) and the host's puppet (position, movement mode, motion from TRACE).
    Checked: the client moved at least 8 m swimming (mode 7), the puppet followed (ends < 3 m from the client's hero),
    playing a swim stroke (Hero_Swim_..._Move_*), not only the idle loop. Before 26.10. the puppet glided with
    Hero_Swim_..._Ambient_Loop in movement mode 1 while the client made strokes (steering it with movement mode 7 left it
    in mode 1); body.cpp now plays the player's stroke on the swimming puppet. Negative control: G3_NEGATIVE (default
    swimstrokeoff on the host)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    xyz = lambda v: tuple(float(c) for c in v.split('_'))
    spot = xyz(os.environ.get('G3_WATER_SPOT', '102767_-608_-4075'))
    shore = xyz(os.environ.get('G3_WATER_SHORE', '103985_-200_-10198'))
    if negative:
        report['negative'] = report_line(host.bot, os.environ.get('G3_NEGATIVE', 'swimstrokeoff'), ('SWIMSTROKEOFF', 'SCRIPTANI', 'REFUSED', 'GLIDE'))
    move_hero(host.bot, shore)
    move_hero(client.bot, spot)
    time.sleep(6)
    mode = lambda c: dict(re.findall(r'(\w+)=(-?\d+)', report_line(c.bot, 'movemode', 'MOVEMODE ')))
    report['start_mode'] = {'client': mode(client).get('hero'), 'host_puppet': mode(host).get('puppet2')}
    assert report['start_mode']['client'] == '7', f'the client does not swim at the spot: {report["start_mode"]}'

    def puppet_pos():
        lines = [l for l in host.mod_log().splitlines()[-300:] if 'TRACE puppet2 ' in l]
        m = re.search(r'pos=([-\d.]+),([-\d.]+),([-\d.]+)', lines[-1]) if lines else None
        return tuple(map(float, m.groups())) if m else None
    samples = []
    start = hero_pos(client.bot)
    for leg, (dx, dz) in enumerate(((1500, 0), (-1500, 0))):
        report[f'goal_{leg}'] = report_line(client.bot, f'swimgoal {dx}_{dz}_7000', ('SWIMGOAL ', 'REFUSED swimgoal'))[:160]
        for _ in range(14):
            time.sleep(0.5)
            lines = [l for l in host.mod_log().splitlines()[-300:] if 'TRACE puppet2 ' in l]
            motion = re.search(r' motion=(\S+)', lines[-1])[1] if lines else '-'
            here = hero_pos(client.bot)
            samples.append({'leg': leg, 'client_m': round(distance(here, start) / 100, 1), 'client_mode': mode(client).get('hero'),
                            'host_mode': mode(host).get('puppet2'), 'host_motion': motion[:60], 'pos': here})
    report['samples'] = [{k: v for k, v in s.items() if k != 'pos'} for s in samples]
    swam = max(s['client_m'] for s in samples)
    report['client_swam_m'] = swam
    end = hero_pos(client.bot)
    time.sleep(2)
    pp = puppet_pos()
    report['puppet_vs_client_m'] = round(distance(pp, end) / 100, 1) if pp else None
    strokes = [s['host_motion'] for s in samples if 'Swim' in s['host_motion'] and 'Ambient' not in s['host_motion']]
    report['host_strokes'] = len(strokes)
    report['swimstrokes'] = report_line(host.bot, 'swimstrokes', ('SWIMSTROKES', 'UNKNOWN', 'REFUSED'))
    report['host_motions'] = sorted({s['host_motion'] for s in samples})
    assert swam >= 8, f'the client did not swim forward ({swam} m)'
    assert all(s['client_mode'] == '7' for s in samples), 'the client left the water'
    assert report['puppet_vs_client_m'] is not None and report['puppet_vs_client_m'] < 3, f'the puppet did not follow: {report["puppet_vs_client_m"]}'
    assert strokes, f'the puppet played no swim stroke: {report["host_motions"]}'


def stageArenaLeave(copies, report, negative=False):
    """Basis (OnLeaveArea) and P4 arena: who leaves the arena during the fight loses it (gCQuest_PS::OnLeave, quest status
    Lost 6; re/findings/world-scripts-c.md), as in single-player. The client takes Copper's arena fight as stageArenaStart
    does; once Copper attacks it in the arena, the client runs 25 m out (QA runto: the area's trigger fires on movement).
    Checked: the quest is lost on the client and on the host. Negative control: storyoff on the host (it never hears)."""
    quest_id, name = os.environ.get('G3_QUEST', 'Copper_Arena'), os.environ.get('G3_TARGET', 'Copper')
    chain = os.environ.get('G3_CHAIN', 'BPANKRATZ31259,BPANKRATZ31260|BPANKRATZ31261')
    arena = os.environ.get('G3_ARENA', 'Reddock_Arena')
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report['negative'] = report_line(host.bot, 'storyoff', 'STORY')
    for copy in (host, client):
        report_line(copy.bot, 'unarm', 'UNARM')
    report['host_at'] = teleport_to(host.bot, name)
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 1200, y, z))
    time.sleep(4)
    for talk in chain.split('|'):
        take_quest(client, name, talk.split(','), report)
        time.sleep(2)
    report['quest_started'] = {c.tag: quest_status(c.bot, quest_id) for c in (host, client)}
    assert report['quest_started']['client'] == RUNNING, f'{quest_id} does not run on the client: {report.get("talks")}'
    state = lambda bot: dict(re.findall(r'(\w+)=(\S+)', find_npc(bot, name)))
    report['to_arena'] = wait_for(lambda: (lambda s: s.get('task', '').startswith('ZS_Arena') and [s])(state(client.bot)), 30,
                                  f'{name} goes into the arena on the client')[0]
    report_line(client.bot, 'sethp 1000', 'SETHP ')
    report['in_arena'] = teleport_to(client.bot, arena)
    ax, ay, az = hero_pos(client.bot)
    for dx in (150, -150, 0):
        move_hero(client.bot, (ax + dx, ay + 20, az))
        time.sleep(1)
    report['attacks'] = wait_for(lambda: (lambda s: s.get('target_hero') == '1' and [s])(state(client.bot)), 40,
                                 f'{name} attacks the client in the arena')[0]
    report['running'] = {c.tag: quest_status(c.bot, quest_id) for c in (host, client)}
    report['leave'] = report_line(client.bot, f'runto {ax + 2500:.0f}_{ay:.0f}_{az:.0f}', ('RUNTO ', 'REFUSED runto'))
    try:
        wait_for(lambda: quest_status(client.bot, quest_id) == 6 and ['lost'], 30, f'{quest_id} is lost for the client')
        wait_for(lambda: quest_status(host.bot, quest_id) == 6 and ['lost'], 20, f'{quest_id} is lost for the host too')
    finally:
        report['after'] = {c.tag: quest_status(c.bot, quest_id) for c in (host, client)}
        report['client_pos'] = tuple(round(v) for v in hero_pos(client.bot))
        report['opponent'] = {c.tag: {k: state(c.bot).get(k) for k in ('task', 'target_hero', 'mirrored')} for c in (host, client)}


def arenastartdiag(copies, report, negative=False):
    """P4 measurement (Kapitel-Checkliste: Arena, 35 quests): the fight start of an arena quest as the game runs it. The quest's
    Activator info (Joey: 31256) ends with SendToArena for the opponent (Game gCInfo_PS::OnEndInfo, quest types 5 and 12), its
    routine then picks ZS_Arena, and OnArena attacks with reason Arena (18) once Entity::GetPlayer stands in the destination
    zone (Script_Game 0x11e60). The taker (client; G3_WHO=host: the host, single-player control) opens Joey_Arena as
    stageArenaQuest does, chooses the Activator, then steps into Reddock_Arena; the host stands at Joey too (it simulates him;
    G3_HOST_FAR=1: it does not). Joey on both machines over time. Diagnostic."""
    quest_id, name = os.environ.get('G3_QUEST', 'Joey_Arena'), os.environ.get('G3_TARGET', 'Joey')
    activator = os.environ.get('G3_ACTIVATOR', 'BPANKRATZ31256')
    arena = os.environ.get('G3_ARENA', 'Reddock_Arena')
    host, client = join_both(copies)
    clear_raid(host, report)
    for copy in (host, client):
        report_line(copy.bot, 'unarm', 'UNARM')
    taker = host if os.environ.get('G3_WHO') == 'host' else client
    other = client if taker is host else host
    report['taker'] = taker.tag
    if os.environ.get('G3_HOST_FAR') != '1' or taker is host:
        report['host_at'] = teleport_to(host.bot, name)
        if taker is client:
            x, y, z = hero_pos(host.bot)
            move_hero(host.bot, (x + 1200, y, z))  # near enough to simulate Joey, out of the arena
    time.sleep(4)
    report['taker_at'] = teleport_to(taker.bot, name)
    time.sleep(6)
    if os.environ.get('G3_CHAIN'):  # the quest taken in dialogs only, | between talks (Copper: 31259, its child 31260 | Activator 31261)
        for talk in os.environ['G3_CHAIN'].split('|'):
            take_quest(taker, name, talk.split(','), report)
            time.sleep(2)
    else:  # runquest hides the Activator (run 16:46: Joey offered 31245, 31249, 31246 only)
        report['start'] = [report_line(taker.bot, 'runquest ' + quest_id, 'QUEST ')]
        for event in os.environ.get('G3_START_EVENTS', 'Joey_HisDuties,Joey_IsHot').split(','):
            report['start'].append(report_line(taker.bot, 'setevent ' + event, 'EVENT '))
        report['talk'], infos = talk_infos(taker, name)
        report['activator'] = report_line(taker.bot, 'talkinfo ' + activator, 'TALKINFO ') if activator in infos else f'not offered: {infos}'
        time.sleep(10)
        report_line(taker.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))
    report['quest_started'] = {c.tag: quest_status(c.bot, quest_id) for c in (host, client)}

    def sample(tag):
        rows = []
        for _ in range(8):
            time.sleep(2.5)
            rows.append({c.tag: re.sub(r'FIND name=\S+ found=1 ', '', find_npc(c.bot, name))[:260] for c in (host, client)})
        report[tag] = rows
    sample('after_activator')
    report['in_arena'] = teleport_to(taker.bot, arena)
    time.sleep(2)
    report['walks'] = [report_line(taker.bot, f'walkto {i}', ('WALKTO ', 'REFUSED walkto')) for i in (0, 4)]
    sample('in_arena_samples')
    report['quest'] = {c.tag: quest_status(c.bot, quest_id) for c in (host, client)}
    report['status_note'] = 'diagnostic only'


def movers(bot):
    start = bot.send('movers', 20)
    bot.line('MOVER end', 10, 'movers', start)
    return [dict(re.findall(r'(\w+)=(\S+)', l)) for l in bot.log()[start:].splitlines() if l.startswith('MOVER name=')]


def moverscan(copies, report, negative=False):
    """P6: where are mechanisms (PSMover: gates, lifts) a player could move with telekinesis? The host alone goes to the
    first member of every town (QA enclaves; G3_AT: NPC names instead) and lists the movers processed there. Diagnostic."""
    host = copies['host'].launch()
    enter_world(host)
    start = host.bot.send('enclaves', 20)
    host.bot.line('ENCLAVES end', 15, 'enclaves', start)
    towns = [m.groups() for m in re.finditer(r'ENCLAVE members=\d+ first=(\S+) status=\d+ enclave=(\S+)', host.bot.log()[start:])]
    if os.environ.get('G3_AT'):
        towns = [(name, name) for name in os.environ['G3_AT'].split(',')]
    report['found'] = {}
    for first, town in towns:
        if first == '-':
            continue
        line = report_line(host.bot, 'teleportto ' + first, ('TELEPORTTO ', 'REFUSED teleportto'))
        if 'REFUSED' in line:
            continue
        time.sleep(6)
        found = [(m['name'], m['state'], m['loop'], m['distance']) for m in movers(host.bot)]
        if found:
            report['found'][town + '@' + first] = found[:12]
    report['status_note'] = 'diagnostic only'


def spellnamediag(copies, report, negative=False):
    """Which item templates exist (QA ready: valid=0 when the game loads no such template)? G3_NAMES, comma separated.
    P6: the spells behind MagicDispel ("Bannen"). Diagnostic."""
    host = copies['host'].launch()
    enter_world(host)
    names = os.environ.get('G3_NAMES', 'It_Spell_Dispel,It_Spell_DispelMagic,It_Spell_DispelFire,It_Spell_DispelIce,It_Spell_Extinguish,'
                           'It_Spell_Defrost,It_Scroll_Dispel,It_Spell_Banish,It_Spell_Unfreeze,It_Spell_RemoveIce,It_Spell_RemoveFire')
    report['ready'] = {n: report_line(host.bot, 'ready ' + n, 'READY ')[:120] for n in names.split(',')}
    report['status_note'] = 'diagnostic only'


def near_view(bot, names):
    start = bot.send('nearnpcs', 20)
    bot.line('NEARX end', 10, 'nearnpcs', start)
    out = {}
    for l in bot.log()[start:].splitlines():
        m = re.match(r'NEARX guid=\S+ name=(\S+) .*?task=(\S*) .*?motion=(\S+) state=(\S*)', l)
        if m and m[1] in names:
            out[m[1]] = (m[2], m[4], m[3][:60])
    return out


def dialogviewdiag(copies, report, negative=False):
    """Phase 5 exploration: what does the other player see and hear of a dialog today? Both stand by Hamlar (the host's NPC).
    1. The client talks to Hamlar and picks an option: the host samples Hamlar and the client's puppet (task, state,
    motion), its dialog channel and its own dialog state; a screenshot of each. 2. While the client is in the dialog, the
    host addresses Hamlar too. 3. The other way round: the host talks, the client watches. Diagnostic."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(5)
    name = os.environ.get('G3_TARGET', 'Hamlar')
    for copy in (host, client):
        report_line(copy.bot, 'unarm', 'UNARM')
    for speaker, watcher, tag in ((client, host, 'client_talks'), (host, client, 'host_talks')):
        teleport_to(watcher.bot, name)
        teleport_to(speaker.bot, name)
        time.sleep(4)
        puppet = remotes(watcher.bot)
        report[tag + '_audio_before'] = dialogaudio(watcher.bot)
        talk_to(speaker, name, report, tag + '_partner')
        report[tag + '_offer'] = talkstate(speaker.bot)
        options = re.search(r' infos=(\S+)', report_line(speaker.bot, 'talk ' + name, ('TALK ', 'REFUSED talk')))
        first = options[1].split(',')[0].split(':')[0] if options and options[1] != '-' else None
        if first:
            report[tag + '_choose'] = report_line(speaker.bot, 'talkinfo ' + first, 'TALKINFO ')
        samples = []
        for i in range(10):
            time.sleep(0.8)
            samples.append(near_view(watcher.bot, (name, 'PC_Hero')))
            if i == 3:
                snap(watcher, tag + '-watcher', report)
                snap(speaker, tag + '-speaker', report)
        report[tag + '_watcher_samples'] = samples
        report[tag + '_watcher_talkstate'] = talkstate(watcher.bot)
        report[tag + '_audio_after'] = dialogaudio(watcher.bot)
        report[tag + '_second_talker'] = report_line(watcher.bot, 'assesstalk ' + name, ('ASSESSTALK ', 'REFUSED'))
        time.sleep(3)
        report[tag + '_second_state'] = talkstate(watcher.bot)
        for copy in (speaker, watcher):
            report_line(copy.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))
        time.sleep(4)
    report['status_note'] = 'diagnostic only'


def stageBusyNpc(copies, report, negative=False):
    """Phase 5, one dialog per NPC as in single player: while the client talks to the host's Hamlar, the host's hero cannot
    start a dialog with him (AssessTalk refused, "Hamlar spricht gerade mit ..."); once the client's dialog ends, it can.
    Before 25.10. both talked to him at once (dialogviewdiag). Negative control: talkholdoff on the host."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(5)
    name = os.environ.get('G3_TARGET', 'Hamlar')
    if negative:
        report['negative'] = report_line(host.bot, 'talkholdoff', 'TALKHOLDOFF ')
    for copy in (host, client):
        report_line(copy.bot, 'unarm', 'UNARM')
    teleport_to(host.bot, name)
    teleport_to(client.bot, name)
    time.sleep(4)
    talk_to(client, name, report, 'client_partner')
    time.sleep(2)
    report['host_try'] = report_line(host.bot, 'assesstalk ' + name, ('ASSESSTALK ', 'REFUSED'))
    time.sleep(3)
    report['host_state'] = talkstate(host.bot)
    report['talksync'] = report_line(host.bot, 'talksync', 'TALKSYNC ')
    report['host_log'] = [l for l in host.mod_log().splitlines() if 'talk:' in l][-4:]
    assert report['host_state'].get('running') == '0', f'the host talks to {name} while the client does: {report["host_state"]}'
    report_line(client.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))
    wait_for(lambda: ' held=0 ' in report_line(host.bot, 'talksync', 'TALKSYNC ') and ['free'], 15, f'{name} is free again on the host')
    time.sleep(2)
    talk_to(host, name, report, 'host_partner_after')
    report['host_after'] = talkstate(host.bot)


def stageDialogOptions(copies, report, negative=False):
    """Phase 5 level 2 (owner 25.10.: the one who talks chooses, the other sees the choice): both stand by Hamlar; the client
    talks to him; while its menu is open the host shows the options in its subtitle box, under the client's name; the client
    chooses one; the host then hears and reads that line. Then the other way round. Negative control: optionsoff on both."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(5)
    name = os.environ.get('G3_TARGET', 'Hamlar')
    if negative:
        report['negative'] = [report_line(c.bot, 'optionsoff', 'OPTIONSOFF ') for c in (host, client)]
    for copy in (host, client):
        report_line(copy.bot, 'unarm', 'UNARM')
    count = lambda line, key: int(re.search(key + r'=(\d+)', line)[1])
    for speaker, listener, tag in ((client, host, 'client_talks'), (host, client, 'host_talks')):
        teleport_to(listener.bot, name)
        teleport_to(speaker.bot, name)
        time.sleep(4)
        before = report_line(listener.bot, 'dialogaudio', 'DIALOGAUDIO ')
        talk_to(speaker, name, report, tag + '_partner')
        try:
            report[tag + '_menu'] = wait_for(lambda: (lambda l: count(l, 'options_shown') > count(before, 'options_shown') and [l])(
                report_line(listener.bot, 'dialogaudio', 'DIALOGAUDIO ')), 10, f'the {listener.tag} shows the menu')[0]
        except AssertionError:
            report[tag + '_menu'] = report_line(listener.bot, 'dialogaudio', 'DIALOGAUDIO ')
        snap(listener, tag + '-menu', report)
        options = re.search(r' infos=(\S+)', report_line(speaker.bot, 'talk ' + name, ('TALK ', 'REFUSED talk')))
        first = options[1].split(',')[0].split(':')[0] if options and options[1] != '-' else None
        report[tag + '_choose'] = report_line(speaker.bot, 'talkinfo ' + first, 'TALKINFO ') if first else None
        time.sleep(3)
        snap(listener, tag + '-chosen', report)
        report[tag + '_after'] = report_line(listener.bot, 'dialogaudio', 'DIALOGAUDIO ')
        report[tag + '_speaker'] = report_line(speaker.bot, 'dialogaudio', 'DIALOGAUDIO ')
        for copy in (speaker, listener):
            report_line(copy.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))
        time.sleep(4)
        assert count(report[tag + '_menu'], 'options_shown') > count(before, 'options_shown'), \
            f'the {listener.tag} showed no menu: ' + report[tag + '_menu'][-200:]
        assert count(report[tag + '_after'], 'subtitles') > count(before, 'subtitles'), f'the {listener.tag} showed no line after the choice'


def stageNpcGestures(copies, report, negative=False):
    """Phase 5 (owner 25.10. "NPC-Gesten"): while the client talks to the host's Hamlar, the host's Hamlar gestures as the
    client's copy does (talk and listen motions, facing the client) instead of standing still (dialogviewdiag: only
    Move_Stand on the host). Negative control: gesturesoff on the client."""
    host, client = join_both(copies)
    clear_raid(host, report)
    time.sleep(5)
    name = os.environ.get('G3_TARGET', 'Hamlar')
    if negative:
        report['negative'] = report_line(client.bot, 'gesturesoff', 'GESTURESOFF ')
    for copy in (host, client):
        report_line(copy.bot, 'unarm', 'UNARM')
    teleport_to(host.bot, name)
    teleport_to(client.bot, name)
    x, y, z = hero_pos(host.bot)
    move_hero(host.bot, (x + 600, y, z))  # a few steps off: Hamlar greets a hero next to him by himself (run 17:19)
    time.sleep(4)
    report_line(host.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))
    wait_for(lambda: talkstate(host.bot).get('running') == '0' and ['free'], 20, 'the host is in no dialog')
    talk_to(client, name, report, 'partner')
    options = re.search(r' infos=(\S+)', report_line(client.bot, 'talk ' + name, ('TALK ', 'REFUSED talk')))
    first = options[1].split(',')[0].split(':')[0] if options and options[1] != '-' else None
    report['choose'] = report_line(client.bot, 'talkinfo ' + first, 'TALKINFO ') if first else None
    samples = []
    for i in range(14):
        time.sleep(0.7)
        samples.append(near_view(host.bot, (name,)).get(name))
        if i % 3 == 0:
            report.setdefault('client_samples', []).append(near_view(client.bot, (name,)).get(name))
        if i == 5:
            snap(host, 'gesture-host', report)
            snap(client, 'gesture-client', report)
    report['host_samples'] = samples
    report['host_dialog'] = talkstate(host.bot)
    assert report['host_dialog'].get('running') == '0', f'the host had a dialog of its own: {report["host_dialog"]}'
    report['talksync'] = {c.tag: report_line(c.bot, 'talksync', 'TALKSYNC ') for c in (host, client)}
    report_line(client.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))
    talking = [s_ for s_ in samples if s_ and ('_Say_' in s_[2] or '_Listen' in s_[2] or 'Talk' in s_[2] or 'Gesture' in s_[2])]
    report['talking_samples'] = len(talking)
    assert talking, f'{name} showed no talk motion on the host: {[s_ and s_[2] for s_ in samples]}'


def stageUseStation(copies, report, negative=False):
    """P1 stations, one test run each (Kapitel 90 % board): the client uses a station of use type G3_USETYPE near G3_USE_AT
    (67 ore vein, 86 water barrel, 88 campfire, 89 sit at a campfire; G3_PERK: a skill the station needs, Perk_Mining for
    ore) with the host next to it. Checked: the client's hero goes through the game's interaction states (PS_Interact, the
    station's own state); the host shows the client's puppet as the station's user; what the station gives lands in the
    client's inventory only (the host's stays as it was); after the client lets go its hero is back in PS_Normal and the host
    sees the station free. Negative control: usesyncoff on both (the host does not see the user)."""
    host, client = join_both(copies)
    clear_raid(host, report)
    kind = os.environ.get('G3_USETYPE', '88')
    place = os.environ.get('G3_USE_AT', 'Hamlar')
    if negative:
        report['negative'] = [report_line(c.bot, 'usesyncoff', 'USESYNCOFF ') for c in (host, client)]
    for c in (host, client):
        report_line(c.bot, 'unarm', 'UNARM')
    if os.environ.get('G3_PERK'):
        report['perk'] = report_line(client.bot, 'learnperk ' + os.environ['G3_PERK'], ('LEARNPERK ', 'REFUSED learnperk'))
    for item in filter(None, os.environ.get('G3_GIVE', '').split(',')):  # a tool the station wants (ore: It_Pickaxe)
        report.setdefault('give', []).append(report_line(client.bot, 'receiveitem ' + item, ('RECEIVEITEM ', 'REFUSED receiveitem')))
    report['teleport'] = {c.tag: teleport_to(c.bot, place)[:80] for c in (host, client)}
    time.sleep(8)
    around = usables(client.bot, os.environ.get('G3_USE_RADIUS', 'all6000'))
    report['candidates'] = [(u['name'], u['user'], u['distance']) for u in around if u['usetype'] == kind][:8]
    free = sorted((u for u in around if u['usetype'] == kind and u['user'] == '-'), key=lambda u: float(u['distance']))
    assert free, f'no free station of use type {kind} near {place}: {report["candidates"]}'
    station = report['station'] = free[0]['name']
    goal = tuple(float(v) for v in free[0]['pos'].replace(',', '_').split('_')[:3]) if free[0]['pos'] else None
    if goal:
        move_hero(host.bot, (goal[0] + 400, goal[1], goal[2]))
    time.sleep(2)
    report['inv_before'] = {c.tag: inventory_of(c.bot) for c in (host, client)}
    counting = os.environ.get('G3_AICOUNT')  # AI states/functions to count on the client while it uses the station
    if counting:
        report_line(client.bot, 'aicount ' + counting, ('AICOUNT end',))
    report['use'] = report_line(client.bot, 'use ' + station, ('USE ', 'REFUSED use'))
    states, host_seen = [], []
    for _ in range(16):
        time.sleep(1)
        info = use_info(client.bot, station)
        states.append(use_field(info, 'hero_state'))
        host_seen.append(use_field(use_info(host.bot, station), 'user'))
    report['client_states'] = states
    report['host_user'] = host_seen
    snap(host, 'station-host', report)
    report['unuse'] = report_line(client.bot, 'unuse', ('UNUSE ', 'REFUSED unuse'))
    try:
        report['client_back'] = wait_for(lambda: (lambda st: st and st.startswith('PS_Normal') and [st])(
            use_field(use_info(client.bot, station), 'hero_state')), 20, 'the client is back in PS_Normal')[0]
    except AssertionError:
        report['client_back'] = use_field(use_info(client.bot, station), 'hero_state')
    time.sleep(3)
    report['host_free'] = use_field(use_info(host.bot, station), 'user')
    report['inv_after'] = {c.tag: inventory_of(c.bot) for c in (host, client)}
    if counting:
        start = client.bot.send('aicount', 20)
        client.bot.line('AICOUNT end', 10, 'aicount', start)
        report['aicount'] = [l for l in client.bot.log()[start:].splitlines() if l.startswith('AICOUNT name=')]
    diff = lambda tag: {k: report['inv_after'][tag].get(k, 0) - report['inv_before'][tag].get(k, 0)
                        for k in set(report['inv_after'][tag]) | set(report['inv_before'][tag])
                        if report['inv_after'][tag].get(k, 0) != report['inv_before'][tag].get(k, 0)}
    report['inv_change'] = {'host': diff('host'), 'client': diff('client')}
    interacted = [st for st in states if st and st.startswith('PS_Interact')]
    assert interacted, f'the client never got into an interaction state: {states}'
    assert any(u not in (None, '-') for u in host_seen), f'the host never showed a user of the station: {host_seen}'
    assert not report['inv_change']['host'], f"the host's inventory changed: {report['inv_change']['host']}"
    assert (report['client_back'] or '').startswith('PS_Normal'), f'the client hangs: {report["client_back"]}'


def enclave_lines(bot):
    start = bot.send('enclaves', 20)
    bot.line('ENCLAVES end', 15, 'enclaves', start)
    return [l for l in bot.log()[start:].splitlines() if l.startswith('ENCLAVE members=')]


def stageDialogCommands(copies, report, negative=False):
    """P4, dialog commands the client's dialog runs (IS_Running_ProcessInfo; the game's infos use 60 commands, infoscan
    commands): for each command of G3_COMMANDS the client runs the nearest info that uses it (QA talkinfo, its conditions
    not checked) at its owner, with the host next to it; afterwards both machines must agree on what it changed: the NPC
    (trade, teach, guard, routine: SetTeachEnabled, SetRoutine ...), the clock (Sleep) or the towns (ModReputEnc,
    ResetCrime). Negative control: infocmdoff and storyoff on the client."""
    host, client = join_both(copies)
    clear_raid(host, report)
    if negative:
        report['negative'] = [report_line(client.bot, 'infocmdoff', 'INFOCMDOFF '), story_off(client)]
    for c in (host, client):
        report_line(c.bot, 'unarm', 'UNARM')
    problems = []
    for command in os.environ.get('G3_COMMANDS', 'SetTeachEnabled,SetRoutine,Sleep,ModReputEnc').split(','):
        start = client.bot.send('infoscan ' + command, 20)
        client.bot.line('INFOSCAN end', 15, 'infoscan', start)
        rows = [dict(re.findall(r'(\w+)=(\S*)', l)) for l in client.bot.log()[start:].splitlines() if l.startswith('INFOSCAN info=')]
        rows = [r for r in rows if r.get('owner') and r['owner'] not in ('PC_Hero', 'Diego', 'Gorn', 'Milten', 'Lester')
                and float(r.get('distance_m', '1e9')) < 1e8]
        if not rows:
            problems.append(f'{command}: no info uses it')
            continue
        tag = command
        report[tag + '_tried'] = []
        for row in rows[:4]:  # the first info the game lets run (ExecuteDialogInfo refused some: run 17:10)
            owner, info = row['owner'], row['info']
            teleport_to(host.bot, owner)
            teleport_to(client.bot, owner)
            time.sleep(5)
            before = {'npc': {c.tag: npc_state(c.bot, owner)[0] for c in (host, client)}, 'clock': clock_of(host.bot)[1],
                      'towns': enclave_lines(host.bot)}
            report_line(client.bot, 'talk ' + owner, ('TALK ', 'REFUSED talk'))
            time.sleep(3)
            run = report_line(client.bot, 'talkinfo ' + info, ('TALKINFO ', 'REFUSED talkinfo'))
            report[tag + '_tried'].append(run)
            if ' ok=1' in run:
                break
            report_line(client.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))
            time.sleep(2)
        report[tag + '_info'] = row
        report[tag + '_run'] = run
        if ' ok=1' not in run:
            problems.append(f'{command}: no info of it would run ({report[tag + "_tried"]})')
            continue
        time.sleep(10)
        report_line(client.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))
        time.sleep(6)
        after = {'npc': {c.tag: npc_state(c.bot, owner)[0] for c in (host, client)},
                 'clock': {c.tag: clock_of(c.bot)[1] for c in (host, client)},
                 'towns': {c.tag: enclave_lines(c.bot) for c in (host, client)}}
        strip = lambda line: re.sub(r'^NPCSTATE name=\S+ ', '', line)
        if command in ('Sleep',):
            jump = after['clock']['host']['timestamp'] - before['clock']['timestamp']
            gap = abs(after['clock']['host']['timestamp'] - after['clock']['client']['timestamp'])
            report[tag + '_result'] = {'host_jump_s': jump, 'gap_s': gap}
            if jump < 1800 or gap > 180:
                problems.append(f'{command}: host clock moved {jump} s, clocks {gap} s apart')
        elif command in ('ModReputEnc', 'ModReputPol', 'ResetCrime', 'Bribe', 'Excuse'):
            changed = [l for l in after['towns']['client'] if l not in before['towns']]
            differ = [(a, b) for a, b in zip(after['towns']['host'], after['towns']['client']) if a != b]
            report[tag + '_result'] = {'client_changed': changed[:3], 'differ': differ[:3]}
            if differ:
                problems.append(f'{command}: towns differ between host and client: {differ[:2]}')
        else:
            report[tag + '_result'] = {'before': before['npc'], 'after': after['npc']}
            if strip(after['npc']['host']) != strip(after['npc']['client']):
                problems.append(f"{command} at {owner}: host {strip(after['npc']['host'])} / client {strip(after['npc']['client'])}")
    report['problems'] = problems
    assert not problems, problems


def scan_type(copy, kind, limit=12):
    """QA infoscan type_<kind>: the dialog infos of that condition type, nearest first (dicts of the INFOSCAN fields)."""
    start = copy.bot.send('infoscan type_' + str(kind), 30)
    copy.bot.line('INFOSCAN end', 30, 'infoscan', start)
    rows = [dict(re.findall(r'(\w+)=(\S*)', l)) for l in copy.bot.log()[start:].splitlines() if l.startswith('INFOSCAN info=')]
    return sorted(rows, key=lambda r: float(r.get('distance_m', '1e9')))[:limit]


def dialogcondiag(copies, report, negative=False):
    """P4 dialog conditions (26.10. pair 2): the host alone lists the infos of condition types 24 (EnclaveFriend:
    IS_Running_NoEnclaveReputation below fame 75), 25 (PoliticalFriend), 28 (MobJoin: JoinPlayerMob), 32 (PoliticalCrime:
    HasBossNews) and the ones with a gold cost (16 teach, 3 general), its hero's political memory and the towns.
    G3_INFO_TYPES: other types. Diagnostic."""
    host = copies['host'].launch()
    enter_world(host)
    for kind in os.environ.get('G3_INFO_TYPES', '24,25,28,32,29,33').split(','):
        report['type_' + kind] = scan_type(host, kind, 14)
    for kind in ('16', '3', '17'):
        report['gold_' + kind] = [r for r in scan_type(host, kind, 400) if r.get('gold', '0') not in ('0', '')][:10]
    report['polmem'] = report_line(host.bot, 'polmem', ('POLMEM', 'REFUSED polmem'))
    report['towns'] = [l[:200] for l in enclave_lines(host.bot)]
    report['status_note'] = 'diagnostic only'


def ai_counts(bot):
    """QA aicount: {name: calls} of the AI scripts counted since the last 'aicount PREFIX' (hero and others together)."""
    start = bot.send('aicount', 20)
    bot.line('AICOUNT end', 10, 'aicount', start)
    return {m[1]: int(m[2]) + int(m[3]) for m in re.finditer(r'AICOUNT name=(\S+) hero=(\d+) others=(\d+)', bot.log()[start:])}


def end_dialog(copy):
    for _ in range(3):
        report_line(copy.bot, 'endtalk', ('TALKSTATE', 'REFUSED endtalk'))
        time.sleep(2)
        if talkstate(copy.bot).get('running') == '0':
            break


def pick_offered(speaker, owner, info, report, tag, count='IS_Running_'):
    """The speaker talks to `owner` as the game starts a dialog (AssessTalk) and picks `info` where the game offers it
    (QA talkchoose with its index in the offered list); then the AI scripts with prefix `count` it ran (QA aicount). If the
    NPC does not take up the address (busy), the dialog is started by QA talk (the info manager between hero and NPC)."""
    try:
        talk_to(speaker, owner, report, tag + '_partner')
    except AssertionError as error:
        report[tag + '_address'] = str(error)[:120]
    line, offered = talk_infos(speaker, owner)
    report[tag + '_offered'] = offered
    report_line(speaker.bot, 'aicount ' + count, ('AICOUNT end',))
    report[tag + '_choose'] = None
    if info in offered:
        report[tag + '_choose'] = report_line(speaker.bot, 'talkchoose %d' % offered.index(info), ('TALKCHOOSE', 'REFUSED talkchoose'))
    time.sleep(6)
    report[tag + '_counts'] = ai_counts(speaker.bot)
    report[tag + '_state'] = report_line(speaker.bot, 'talkstate', 'TALKSTATE')[:160]
    end_dialog(speaker)
    return info in offered


def stageEnclaveFriend(copies, report, negative=False):
    """P4 IS_Running_NoEnclaveReputation / IS_Running_ProcessInfo (26.10. pair 2): an info of condition 24 (EnclaveFriend;
    gCInfo_PS::Execute runs IS_Running_NoEnclaveReputation instead of the info while the PlayerFame of the owner's town is
    below 75). The host sets that fame (QA enclavefame; enclaves.cpp shares it); the client talks to the owner and picks the
    info where the game offers it: at fame 50 the game refuses (NoEnclaveReputation, no ProcessInfo), at fame 80 it runs
    the info (ProcessInfo). G3_TARGET owner (default Orc_KapDun_Guard_01), G3_INFO the info (BPANKRATZ31797), G3_TOWN the
    town (default: the owner's). G3_WHO=host: the host talks (single player control). Negative control: enclavesyncoff on
    both."""
    host, client = join_both(copies)
    clear_raid(host, report)
    owner = os.environ.get('G3_TARGET', 'Orc_KapDun_Guard_01')
    info = os.environ.get('G3_INFO', 'BPANKRATZ31797')
    town = os.environ.get('G3_TOWN', owner)
    speaker = host if os.environ.get('G3_WHO') == 'host' else client
    if negative:
        report['negative'] = [report_line(c.bot, 'enclavesyncoff', 'ENCLAVESYNCOFF') for c in (host, client)]
    for c in (host, client):
        report_line(c.bot, 'unarm', 'UNARM')
    report['teleport'] = [teleport_to(c.bot, owner)[:80] for c in (host, client)]
    time.sleep(6)

    def fame(c):
        m = re.search(r' fame=(-?\d+)', report_line(c.bot, 'enclavefame ' + town, ('ENCLAVEFAME', 'REFUSED enclavefame')))
        return int(m[1]) if m else -1
    report['fame_start'] = {c.tag: fame(c) for c in (host, client)}
    for value, tag in ((50, 'low'), (80, 'high')):
        report[tag + '_set'] = report_line(host.bot, 'enclavefame %s-%d' % (town, value), ('ENCLAVEFAME', 'REFUSED enclavefame'))
        try:
            wait_for(lambda: fame(speaker) == value and ['ok'], 12, f'the speaker sees fame {value}')
        except AssertionError:
            pass
        report[tag + '_fame'] = {c.tag: fame(c) for c in (host, client)}
        pick_offered(speaker, owner, info, report, tag)
        time.sleep(3)
    low, high = report['low_counts'], report['high_counts']
    assert report['low_choose'] and ' ok=' in report['low_choose'], f'{info} not offered at {owner}: {report["low_offered"]}'
    assert low.get('IS_Running_NoEnclaveReputation', 0) >= 1 and not low.get('IS_Running_ProcessInfo'), f'fame 50: {low}'
    assert report['high_choose'], f'{info} not offered again at {owner}: {report["high_offered"]}'
    assert high.get('IS_Running_ProcessInfo', 0) >= 1 and not high.get('IS_Running_NoEnclaveReputation'), f'fame 80: {high}'


def stageBossNews(copies, report, negative=False):
    """P4 HasBossNews (26.10. pair 2): the game raises GetPlayer's PoliticalCrimeCount of a town's alignment when the town is
    freed (ChangeEnclaveStatus to 2, not Ardea), only on the machine where it turns; the generic info of condition 32
    (FILLERX2144, PoliticalCrime: HasBossNews, the hero's count above its PoliticalSuspectComment) brings a boss's news. The
    host frees G3_TOWN (default Montera; QA enclavestatus: the game's ChangeEnclaveStatus; the quest's success turns the town
    back to 0 within 0.5 s, so the status 2 never reaches the client: run 131615); the speaker talks to the first of
    G3_TARGETS (default Kan, Gnar: orc bosses) whose HasBossNews answers 1. G3_WHO=host: the host talks (single player
    control). G3_SETPOLMEM=1: the speaker's count is set by QA polmem as the deed would have (forced entry: the counter is
    all that is missing). Since protocol 56 (owner 26.10.: as the reputation setting) a freed town's political crime goes to
    the other heroes in shared and together (enclaves.cpp EnclavePolitical): the client gets the count and Kan's news. With
    G3_REPUTATION=personal only the liberator keeps it: the host's hero counts, the client's does not and hears no news.
    Negative control: politicaloff on both (the client gets nothing, also in shared)."""
    os.environ.setdefault('G3_NATIVEWATCH', 'HasBossNews')
    reputation = os.environ.get('G3_REPUTATION', 'shared')
    report['reputation'] = reputation
    if reputation != 'shared':
        copies['host'].ini_extra = f'reputation = {reputation}\n'
    host, client = join_both(copies)
    clear_raid(host, report)
    town = os.environ.get('G3_TOWN', 'Montera')
    targets = os.environ.get('G3_TARGETS', 'Kan,Gnar').split(',')
    info = os.environ.get('G3_INFO', 'FILLERX2144')
    speaker = host if os.environ.get('G3_WHO') == 'host' else client
    personal = os.environ.get('G3_REPUTATION') == 'personal'
    if negative:
        report['negative'] = [report_line(c.bot, 'politicaloff', ('POLITICALOFF', 'UNKNOWN', 'REFUSED')) for c in (host, client)]
    for c in (host, client):
        report_line(c.bot, 'unarm', 'UNARM')
    report['town'] = report_line(host.bot, 'enclavefame ' + town, ('ENCLAVEFAME', 'REFUSED enclavefame'))
    pal = int(re.search(r' alignment=(\d+)', report['town'])[1])
    crime = lambda c: int(report_line(c.bot, 'polmem', 'POLMEM').split(' crime=')[1].split(' ')[0].split(',')[pal])
    report['crime_before'] = {c.tag: crime(c) for c in (host, client)}
    report['free'] = report_line(host.bot, 'enclavestatus %s-2' % town, ('ENCLAVESTATUS', 'REFUSED enclavestatus'))
    try:
        wait_for(lambda: crime(client) > report['crime_before']['client'] and ['ok'], 10 if personal else 20, 'the client counts the freed town')
    except AssertionError:
        pass
    time.sleep(3)
    report['town_client'] = report_line(client.bot, 'enclavefame ' + town, ('ENCLAVEFAME', 'REFUSED enclavefame'))
    report['crime_after'] = {c.tag: crime(c) for c in (host, client)}
    report['polmem'] = {c.tag: report_line(c.bot, 'polmem', 'POLMEM') for c in (host, client)}
    if os.environ.get('G3_SETPOLMEM') == '1' and report['crime_after'][speaker.tag] <= report['crime_before'][speaker.tag]:
        report['forced'] = report_line(speaker.bot, 'polmem %d_%d_-1_-1' % (pal, report['crime_before'][speaker.tag] + 1), 'POLMEM')
        report['crime_after'][speaker.tag] = crime(speaker)
    report['tries'] = []
    news = None
    for owner in targets:
        report_line(speaker.bot, 'nativewatch reset', 'NATIVEWATCH')
        teleport_to(speaker.bot, owner)
        time.sleep(8)
        try_ = {'owner': owner}
        try:
            talk_to(speaker, owner, try_, 'partner')
        except AssertionError as error:
            try_['error'] = str(error)[:120]
        line, try_['offered'] = talk_infos(speaker, owner)
        try_['native'] = native_counts(speaker.bot).get('HasBossNews')
        try_['type32'] = [(r.get('info'), r.get('given')) for r in scan_type(speaker, 32, 3)]
        if info in try_['offered']:
            try_['choose'] = report_line(speaker.bot, 'talkchoose %d' % try_['offered'].index(info), ('TALKCHOOSE', 'REFUSED talkchoose'))
            time.sleep(8)
            try_['type32_after'] = [(r.get('info'), r.get('given')) for r in scan_type(speaker, 32, 3)]
        end_dialog(speaker)
        report['tries'].append(try_)
        if try_['native'] and try_['native']['selves'].get(owner, {}).get('nonzero'):
            news = try_
            break
    report['polmem_end'] = report_line(speaker.bot, 'polmem', 'POLMEM')
    report['enclavesync'] = {c.tag: report_line(c.bot, 'enclavesync', 'ENCLAVESYNC ') for c in (host, client)}
    tag = speaker.tag
    if personal and speaker is client:  # only the liberator (the host) keeps it
        assert report['crime_after']['host'] > report['crime_before']['host'], f'the liberating host got no political crime: {report["crime_after"]}'
        assert report['crime_after'][tag] == report['crime_before'][tag], f'personal: the client got the host\'s political crime: {report["crime_after"]}'
        assert not news, 'personal: HasBossNews answered 1 for the client'
        return
    assert report['crime_after'][tag] > report['crime_before'][tag], f'the {tag} hero got no political crime: {report["crime_after"]}'
    assert news, f'HasBossNews never 1 for the {tag}: {[(t["owner"], t.get("native")) for t in report["tries"]]}'
    assert info in news['offered'] or ('1' in [g for _, g in news.get('type32', [])]), f'{info} neither offered nor given: {news}'


def stageMobJoin(copies, report, negative=False):
    """P9 JoinPlayerMob (26.10. pair 2): the generic info of condition 28 (KALVERAMX228, CmdMobJoin; gCInfo_PS::OnEndInfo
    case 0x1c calls JoinPlayerMob for the dialog's NPC). Reto_FreeMine's infos set MobEnabled (SetMobEnabled, Nordmar); the
    host does the same for G3_TARGET (default: the nearest NPC those infos enable; QA partyenable NAME_mob, shared). The
    client talks to it and picks the info where offered (else QA talkinfo, noted as forced): JoinPlayerMob runs on the
    client; the NPC follows the client on both PCs. G3_WHO=host: the host (single player control). Negative control:
    guidesyncoff on the client (it does not take its new follower over)."""
    os.environ.setdefault('G3_NATIVEWATCH', 'JoinPlayerMob')
    host, client = join_both(copies)
    clear_raid(host, report)
    info = os.environ.get('G3_INFO', 'KALVERAMX228')
    owner = os.environ.get('G3_TARGET', '')
    if not owner:
        start = host.bot.send('infoscan SetMobEnabled', 30)
        host.bot.line('INFOSCAN end', 30, 'infoscan', start)
        rows = [dict(re.findall(r'(\w+)=(\S*)', l)) for l in host.bot.log()[start:].splitlines() if l.startswith('INFOSCAN info=')]
        report['mob_infos'] = [(r.get('info'), r.get('owner'), r.get('e1'), r.get('id1')) for r in rows]
        names = [r.get('owner') if r.get('e1') in ('self', 'npc', 'owner') else r.get('e1') for r in rows if r.get('e1') not in ('player', 'PC_Hero')]
        owner = names[0] if names else rows[0]['owner']
    report['owner'] = owner
    speaker = host if os.environ.get('G3_WHO') == 'host' else client
    other = client if speaker is host else host
    if negative:
        report['negative'] = report_line(client.bot, 'guidesyncoff', 'GUIDESYNCOFF')
    for c in (host, client):
        report_line(c.bot, 'unarm', 'UNARM')
    report['teleport'] = [teleport_to(c.bot, owner)[:80] for c in (host, client)]
    time.sleep(8)
    # Condition 28 also wants Reto_FreeMine running (gCInfo_PS::AreConditionsFulfilled 0x1c, re/out/mobjoin-cond-p2x-b.c).
    report['quest'] = quest(host.bot, 'runquest', os.environ.get('G3_QUEST', 'Reto_FreeMine'))
    time.sleep(3)
    report['quest_speaker'] = quest(speaker.bot, 'quest', os.environ.get('G3_QUEST', 'Reto_FreeMine'))
    report['mobenable'] = report_line(host.bot, 'partyenable %s_mob' % owner, ('PARTYENABLE', 'REFUSED partyenable'))
    time.sleep(3)
    talk_to(speaker, owner, report, 'partner')
    line, offered = talk_infos(speaker, owner)
    report['offered'] = offered
    if info in offered:
        report['choose'] = report_line(speaker.bot, 'talkchoose %d' % offered.index(info), ('TALKCHOOSE', 'REFUSED talkchoose'))
    else:
        report['forced'] = report_line(speaker.bot, 'talkinfo ' + info, ('TALKINFO', 'REFUSED talkinfo'))
    for _ in range(12):
        time.sleep(2)
        if 'current=' + info + ' ' not in report_line(speaker.bot, 'talkstate', 'TALKSTATE'):
            break
    end_dialog(speaker)
    time.sleep(4)
    report['native'] = native_counts(speaker.bot).get('JoinPlayerMob')
    report['npc'] = {c.tag: npc_state(c.bot, owner)[0][:300] for c in (host, client)}
    where = lambda c: (npc_named(c.bot, owner)[1] or {}).get('pos')
    report['npc_before_walk'] = {c.tag: where(c) for c in (host, client)}
    x, y, z = hero_pos(speaker.bot)
    move_hero(speaker.bot, (x + 1500, y, z))
    time.sleep(14)
    goal = hero_pos(speaker.bot)
    report['npc_after_walk'] = {c.tag: report_line(c.bot, 'crime ' + owner, ('CRIME', 'REFUSED crime'))[:300] for c in (host, client)}
    report['npc_pos_after'] = {c.tag: where(c) for c in (host, client)}
    report['dist_to_speaker_m'] = {c.tag: (distance(report['npc_pos_after'][c.tag], goal) / 100 if report['npc_pos_after'][c.tag] else None)
                                   for c in (host, client)}
    assert report['native'] and report['native']['calls'] >= 1, f'JoinPlayerMob did not run on the {speaker.tag}: {report["native"]}'
    assert any('Follow' in v for v in report['npc_after_walk'].values()), f'{owner} does not follow: {report["npc_after_walk"]}'
    assert all(d is not None and d < 9 for d in report['dist_to_speaker_m'].values()),         f'{owner} not with the {speaker.tag} on both PCs (m): {report["dist_to_speaker_m"]}'


def questinfodiag(copies, report, negative=False):
    """P4 quest kinds: which dialog infos start, take items for and close the quests nearest the start (bring items, report)?
    The host alone lists the infos with the commands RunQuest, Take, Give, SuccessQuest, CloseQuest (QA infoscan; the 25
    nearest each). G3_INFO_COMMANDS: other commands. Diagnostic."""
    host = copies['host'].launch()
    enter_world(host)
    if os.environ.get('G3_AT'):
        report['at'] = teleport_to(host.bot, os.environ['G3_AT'])
        time.sleep(6)
    report['infos'] = {}
    for command in os.environ.get('G3_INFO_COMMANDS', 'RunQuest,Take,Give,SuccessQuest,CloseQuest').split(','):
        start = host.bot.send('infoscan ' + command, 20)
        host.bot.line('INFOSCAN end', 15, 'infoscan', start)
        report['infos'][command] = [l[9:260] for l in host.bot.log()[start:].splitlines() if l.startswith('INFOSCAN ') and not l.startswith('INFOSCAN end')]
    report['status_note'] = 'diagnostic only'


def objectscan(copies, report, negative=False):
    """P1: where are the orc boulders, the Innos and Beliar statues (shrines), anvils and pirate treasures? Their templates
    (Templates.pak: G3_Object_Interact_Animated_OrcBoulder_*, *_Innos_Statue_01, *_Beliar_Statue_01, *_Anvil_01,
    *_PirateTreasure_01) say what to look for; the world data does not say where. The host alone teleports to the first
    member of every town (QA enclaves) and lists those objects within 80 m, by name or use script (OB_Faring_Boulder: a
    boulder in Faring). G3_AT: NPC names (comma separated) to look around instead of the towns. Diagnostic."""
    host = copies['host'].launch()
    enter_world(host)
    start = host.bot.send('enclaves', 20)
    host.bot.line('ENCLAVES end', 15, 'enclaves', start)
    towns = [m.groups() for m in re.finditer(r'ENCLAVE members=\d+ first=(\S+) status=\d+ enclave=(\S+)', host.bot.log()[start:])]
    if os.environ.get('G3_AT'):
        towns = [(name, name) for name in os.environ['G3_AT'].split(',')]
    wanted = tuple(os.environ['G3_WANTED'].split(',')) if os.environ.get('G3_WANTED') else (
        'OrcBoulder', 'Boulder', 'Statue', 'Anvil', 'PirateTreasure', 'Shrine', 'Altar')
    kinds = tuple(os.environ.get('G3_WANTED_TYPES', '').split(',')) if os.environ.get('G3_WANTED_TYPES') else ()
    report['found'] = {}
    report['spots'] = {}
    for first, town in towns:
        if first == '-':
            continue
        line = report_line(host.bot, 'teleportto ' + first, ('TELEPORTTO ', 'REFUSED teleportto'))
        report['spots'][town] = line[:60]
        if 'REFUSED' in line:
            continue
        time.sleep(6)
        for u in usables(host.bot, 'all8000'):
            if any(w in u['name'] or w in u['usefunc'] for w in wanted) or u['usetype'] in kinds:
                report['found'].setdefault(u['name'], []).append({'town': town, 'at': first, 'pos': u['pos'], 'usetype': u['usetype'],
                                                                   'usefunc': u['usefunc'], 'distance': u['distance']})
    report['status_note'] = 'diagnostic only'


def worldscan(copies, report, negative=False):
    """Where in the world are doors, ore veins, shrines, anvils, boulders, water barrels? The host alone teleports to
    up to G3_SPOTS dialog owners spread over the map (farnpcs) and lists the interact objects within 40 m there
    (usables all). Diagnostic, for the world-interaction tests of INVENTAR-SKRIPTE.md."""
    from collections import Counter
    host = copies['host'].launch()
    enter_world(host)
    rows = far_rows(host.bot)
    spots = []
    for r in sorted(rows, key=lambda r: r['distance']):
        if all(distance(r['pos'], s['pos']) > 15000 for s in spots):
            spots.append(r)
        if len(spots) >= int(os.environ.get('G3_SPOTS', '14')):
            break
    report['spots'] = {}
    for spot in spots:
        line = report_line(host.bot, 'teleportto '+spot['name'], ('TELEPORTTO ', 'REFUSED teleportto'))
        time.sleep(6)
        found = [u for u in usables(host.bot, 'all4000') if u['item'] == '0']
        kinds = Counter(f"{u['usetype']}{'/door' if u['door'] == '1' else ''}{'/lock' if u['lock'] == '1' else ''}" for u in found)
        names = sorted({u['name'] for u in found if u['name'].startswith('G3_') or u['door'] == '1' or u['lock'] == '1'})
        report['spots'][spot['name']] = {'teleport': line[:80], 'kinds': dict(kinds.most_common(30)), 'objects': names[:40]}
    report['status_note'] = 'diagnostic only'


def doordiag(copies, report, negative=False):
    """Stage W1, doors and locks (INVENTAR-SKRIPTE.md, agent A; doors.cpp): both players go to the prison door in Lago (Animated_Door_Fabio,
    locked; the only door the world scan found). One player unlocks it (QA stand-in for lock picking or a key) and opens
    it the game's way (use: PS_Interact_Door); the door and lock state on both machines is sampled. G3_WHO=host|client
    (who acts), G3_DOOR (entity name), G3_DOOR_AT (an NPC next to it). Passes when the other machine has it open and
    unlocked too; negative control: doorsyncoff on both."""
    host, client = join_both(copies)
    if negative:
        for c in (host, client):
            assert 'ok=1' in report_line(c.bot, 'doorsyncoff', 'DOORSYNCOFF ')
    door = os.environ.get('G3_DOOR', 'G3_Object_Interact_Animated_Door_Fabio')
    spot = os.environ.get('G3_DOOR_AT', 'Lago_Prison_Guard')
    for c in (host, client):
        report[c.tag+'_teleport'] = report_line(c.bot, 'teleportto '+spot, ('TELEPORTTO ', 'REFUSED teleportto'))
    time.sleep(8)
    report['before'] = {c.tag: use_info(c.bot, door) for c in (host, client)}
    who = os.environ.get('G3_WHO', 'host')
    actors = [host] if who == 'host' else [client] if who == 'client' else [host, client]
    actor = actors[0]
    report['unlock'] = {c.tag: report_line(c.bot, 'unlock '+door, ('UNLOCK ', 'REFUSED unlock')) for c in actors}
    time.sleep(2)
    report['after_unlock'] = {c.tag: use_info(c.bot, door) for c in (host, client)}
    report['use'] = {c.tag: report_line(c.bot, 'use '+door, ('USE ', 'REFUSED use')) for c in actors}
    samples = []
    for _ in range(8):
        time.sleep(1)
        samples.append({c.tag: re.sub(r'name=\S+ pos=\S+ usefunc=\S+ usetype=\d+ item=\d ', '', use_info(c.bot, door)[8:])[:150] for c in (host, client)})
    report['samples'] = samples
    report['after'] = {c.tag: use_info(c.bot, door) for c in (host, client)}
    other = client if actor is host else host  # with both acting: both must have it open
    report['doors'] = {c.tag: report_line(c.bot, 'doors', 'DOORS ') for c in (host, client)}
    # Stage W1 (doors.cpp): the door the actor opened is open and unlocked on the other machine too.
    assert ' open=1 ' in report['after'][actor.tag] and ' locked=0 ' in report['after'][actor.tag], 'the actor did not open it: ' + report['after'][actor.tag]
    seen_open = ' open=1 ' in report['after'][other.tag] and ' locked=0 ' in report['after'][other.tag]
    if negative:
        assert not seen_open, 'open on the other machine although door sync is off'
    else:
        assert seen_open, 'still shut or locked on the other machine: ' + report['after'][other.tag]


def doorlatediag(copies, report, negative=False):
    """Stage W1b, a door opened before a player joins: the host unlocks and opens the prison door in Lago while alone;
    the client starts, joins, goes there, and finds it open and unlocked (the host sends this session's door changes to
    a player who joins). Negative control: doorsyncoff on the host before it opens the door (nothing recorded)."""
    door = os.environ.get('G3_DOOR', 'G3_Object_Interact_Animated_Door_Fabio')
    spot = os.environ.get('G3_DOOR_AT', 'Lago_Prison_Guard')
    host = copies['host'].launch('host', 'Host')
    enter_world(host)
    start = host.bot.send('releaselock')
    host.bot.line('LOCK scan complete', 10, 'lock scan', start)
    if negative:
        assert 'ok=1' in report_line(host.bot, 'doorsyncoff', 'DOORSYNCOFF ')
    report['host_teleport'] = report_line(host.bot, 'teleportto '+spot, ('TELEPORTTO ', 'REFUSED teleportto'))
    time.sleep(6)
    report['unlock'] = report_line(host.bot, 'unlock '+door, ('UNLOCK ', 'REFUSED unlock'))
    time.sleep(1)
    report['use'] = report_line(host.bot, 'use '+door, ('USE ', 'REFUSED use'))
    time.sleep(6)
    report['host_after'] = use_info(host.bot, door)
    assert ' open=1 ' in report['host_after'], 'the host did not open it: ' + report['host_after']
    client = copies['client'].launch('client', 'Client')
    enter_world(client, startup=120)
    wait_for(lambda: remotes(host.bot).get(2, {}).get('puppet'), 30, 'host shows a puppet for the client')
    report['client_teleport'] = report_line(client.bot, 'teleportto '+spot, ('TELEPORTTO ', 'REFUSED teleportto'))
    time.sleep(8)
    report['client_after'] = use_info(client.bot, door)
    report['doors'] = {c.tag: report_line(c.bot, 'doors', 'DOORS ') for c in (host, client)}
    seen_open = ' open=1 ' in report['client_after'] and ' locked=0 ' in report['client_after']
    if negative:
        assert not seen_open, 'open for the late player although the host did not record it'
    else:
        assert seen_open, 'shut or locked for the late player: ' + report['client_after']


def infodescdiag(copies, report, negative=False):
    """Why can a dialog option not run? Conditions and script lines of the options named in G3_INFOS."""
    host = copies['host'].launch()
    enter_world(host)
    for name in [t for t in os.environ.get('G3_INFOS', 'NEWM1055').split(',') if t]:
        start = host.bot.send('infodesc '+name, 20)
        host.bot.line('INFODESC end', 10, 'infodesc', start)
        report[name] = [l for l in host.bot.log()[start:].splitlines() if l.startswith(('INFODESC', 'INFOLINE'))]
    report['status_note'] = 'diagnostic only'


def snap(copy, name, report):
    """A screenshot of the copy's game window into the run folder (for a human or agent to look at)."""
    try:
        path = screenshot(copy.process.pid, copy.run/f'{copy.tag}-{name}.png')
    except Exception as error:
        path = f'failed: {error}'
    report.setdefault('screenshots', []).append(str(path))


CLEANUPS = []  # run by main after both games are closed (files the games might still rewrite)


def parity_items(copy):
    return {m.group(1): m.group(2) for m in re.finditer(r'PARITY item=\d+ name=([^=\n]+) hash=([0-9a-f]+)', copy.mod_log())}


def log_line(copy, needle, seconds, what):
    wait_for(lambda: needle in copy.mod_log(), seconds, what)
    return next(l for l in copy.mod_log().splitlines() if needle in l)


def parity(copies, report, negative=False):
    """Install parity: identical installs join; a client whose balancing option differs is refused, with the reason."""
    host, client = join_both(copies)
    report['parity_host'], report['parity_client'] = parity_items(host), parity_items(client)
    assert len(report['parity_host']) == 7 and report['parity_host'] == report['parity_client'], 'fingerprints differ'
    report['options_host'] = log_line(host, 'PARITY options', 5, 'host options')
    # The engine's values must match the copy's ge3.ini ([Game] XPModifier=100, MinHitDuration=6).
    assert 'xp_modifier=100 ' in report['options_host'] and 'min_hit_duration=6 ' in report['options_host'], report['options_host']
    report['computed_ms'] = [log_line(c, 'PARITY computed', 5, 'compute time') for c in (host, client)]
    assert close(client), 'first client did not close'
    client.collect()
    (client.run/'client').rename(client.run/'client-first')
    wait_for(lambda: 2 not in remotes(host.bot), 30, 'host drops the client')
    # The same client with one balancing option changed in its (test) copy's ge3.ini.
    ini = client.root/'Ini/ge3.INI'
    original = ini.read_bytes()
    CLEANUPS.append(lambda: ini.write_bytes(original))
    if not negative:
        assert original.count(b'XPModifier=100') == 1
        ini.write_bytes(original.replace(b'XPModifier=100', b'XPModifier=150'))
    client.launch('client', 'Client')
    report['options_client_changed'] = log_line(client, 'PARITY options', 120, 'changed client options')
    report['client_refused'] = log_line(client, 'REFUSED by host', 120, 'client refused')
    report['host_refused'] = log_line(host, 'net: refused Client', 10, 'host refusal')
    assert 'install differs from the host: options' in report['client_refused'], report['client_refused']
    time.sleep(10)
    report['host_remotes_after'] = remotes(host.bot)
    assert not report['host_remotes_after'].get(2, {}).get('puppet'), 'refused client still has a puppet'
    report['client_connect_attempts'] = client.mod_log().count('net: connecting to')


def stageLanguage(copies, report, negative=False):
    """Mod messages in the game's language (26.10.): the client's copy shows Gothic 3's texts in Polish (ge3.ini [Language]
    Text=Polish and the options' SubtitleLanguage=Polish in UserOptions.ini, which the game takes; both put back after
    the run) and has language = auto; 60 m from the host it is told "Daleko od hosta" and back "Znów w pobliżu hosta.",
    its log names the language it took; the host (no language line) stays English. Screenshots show the characters.
    Negative control: the client has language = en (English messages, the Polish check fails)."""
    client_copy = copies['client']
    for path, german, polish in ((client_copy.root/'Ini/ge3.INI', b'Text=German', b'Text=Polish'),
                                 (client_copy.root/'isolated-profile/Documents/gothic3/UserOptions.ini',
                                  b'SubtitleLanguage=German', b'SubtitleLanguage=Polish')):
        original = path.read_bytes()
        assert original.count(german) == 1, f'{path.name}: no {german}'
        CLEANUPS.append(lambda path=path, original=original: path.write_bytes(original))
        path.write_bytes(original.replace(german, polish))
    client_copy.ini_extra = 'language = en\n' if negative else 'language = auto\n'
    host, client = join_both(copies)
    clear_raid(host, report)
    start_pos = remotes(host.bot)[2]['pos']
    report['client_away'] = move_hero(client.bot, (start_pos[0] + 6000, start_pos[1], start_pos[2]))
    time.sleep(4)
    snap(client, 'far', report)
    report['client_hud_far'] = report_line(client.bot, 'hud', 'HUD ')
    report['host_hud_far'] = report_line(host.bot, 'hud', 'HUD ')
    report['client_language'] = [l for l in client.mod_log().splitlines() if 'i18n:' in l or 'config: role' in l]
    assert 'is far away' in report['host_hud_far'], report['host_hud_far']
    assert 'Daleko od hosta' in report['client_hud_far'], report['client_hud_far']
    assert any('game text language Polish -> pl' in l for l in report['client_language']), report['client_language']
    report['client_back'] = move_hero(client.bot, start_pos)
    time.sleep(4)
    snap(client, 'near', report)
    report['client_hud_near'] = report_line(client.bot, 'hud', 'HUD ')
    assert 'pobli' in report['client_hud_near'] and 'hosta' in report['client_hud_near'], report['client_hud_near']


SUITES = {'stageSwim': stageSwim, 'stageRevolution': stageRevolution, 'stageSectorRefresh': stageSectorRefresh, 'dialogcondiag': dialogcondiag, 'stageEnclaveFriend': stageEnclaveFriend, 'stageBossNews': stageBossNews, 'stageMobJoin': stageMobJoin, 'stageSpeciesAttitude': stageSpeciesAttitude, 'stagePoliticalChapter': stagePoliticalChapter, 'stageCrimeMemory': stageCrimeMemory, 'stageEnterHome': stageEnterHome, 'stageTheftSeen': stageTheftSeen, 'stageZoneAttitude': stageZoneAttitude, 'p7attscandiag': p7attscandiag, 'stageFriendBlow': stageFriendBlow, 'p9smalldiag': p9smalldiag, 'followtimediag': followtimediag, 'stageAnimalWake': stageAnimalWake, 'stageFreeDestination': stageFreeDestination, 'stageGuideRoutine': stageGuideRoutine, 'observesuspectdiag': observesuspectdiag, 'stageAttitudeShared': stageAttitudeShared, 'attitudeleakdiag': attitudeleakdiag, 'stageGuardNews': stageGuardNews, 'commentdiag': commentdiag, 'aggressordiag': aggressordiag, 'stageUpset': stageUpset, 'stageGrudgeLoad': stageGrudgeLoad, 'guardcommentdiag': guardcommentdiag, 'upsetdiag': upsetdiag, 'stageDefeatSeen': stageDefeatSeen, 'crimenewsdiag': crimenewsdiag, 'defeatwitnessdiag': defeatwitnessdiag, 'stageCompanionWait': stageCompanionWait, 'stagePartyJoin': stagePartyJoin, 'zonescandiag': zonescandiag, 'zoneattitudediag': zoneattitudediag, 'sectorrefreshdiag': sectorrefreshdiag, 'animalsleepdiag': animalsleepdiag, 'stageArenaLeave': stageArenaLeave, 'stageCounterHit': stageCounterHit, 'counterhitdiag': counterhitdiag, 'guardnewsdiag': guardnewsdiag, 'newsscandiag': newsscandiag, 'boulderseqdiag': boulderseqdiag, 'stageBoulderHold': stageBoulderHold, 'stageMeleeSwing': stageMeleeSwing, 'stageNpcBlock': stageNpcBlock, 'stageShieldBlock': stageShieldBlock, 'stageFlameSword': stageFlameSword, 'flamesworddiag': flamesworddiag, 'stageAreaSpells': stageAreaSpells, 'areaspelldiag': areaspelldiag, 'stageFightMemory': stageFightMemory, 'stageWarn': stageWarn, 'stageObserveSuspect': stageObserveSuspect, 'stageEnterRange': stageEnterRange, 'swimdiag': swimdiag, 'stageQuickUse': stageQuickUse, 'stageSleepBed': stageSleepBed, 'chestscandiag': chestscandiag, 'chestwitnessdiag': chestwitnessdiag, 'stageIntruderFight': stageIntruderFight, 'intruderkodiag': intruderkodiag, 'useownediag': useownediag, 'fightmemorydiag': fightmemorydiag, 'p7opendiag': p7opendiag, 'stageEnding': stageEnding, 'storyepilogdiag': storyepilogdiag, 'stageStoryNordmar': stageStoryNordmar, 'stageStoryInnos': stageStoryInnos, 'homescandiag': homescandiag, 'boulderdiag': boulderdiag, 'stageBoulder': stageBoulder, 'importanttalkdiag': importanttalkdiag, 'stageAddressed': stageAddressed, 'stageSneak': stageSneak, 'stageForeignHome': stageForeignHome, 'stageDialogCommands': stageDialogCommands, 'stageUseStation': stageUseStation, 'stageNpcGestures': stageNpcGestures, 'stageDialogOptions': stageDialogOptions, 'stageBusyNpc': stageBusyNpc, 'dialogviewdiag': dialogviewdiag, 'spellnamediag': spellnamediag, 'stageEscortFar': stageEscortFar, 'stageArenaStart': stageArenaStart, 'stageTownFree': stageTownFree, 'sneakhomediag': sneakhomediag, 'summonmurderdiag': summonmurderdiag, 'arenastartdiag': arenastartdiag, 'stageKillQuest': stageKillQuest, 'moverscan': moverscan, 'stageArenaQuest': stageArenaQuest, 'stageBringItems': stageBringItems, 'stageReportQuest': stageReportQuest, 'questinfodiag': questinfodiag, 'stageRespawn': stageRespawn, 'respawndiag': respawndiag, 'stageWait': stageWait, 'stageGuideSave': stageGuideSave, 'stageGuide': stageGuide, 'stageEscort': stageEscort, 'stageTreasure': stageTreasure, 'treasurediag': treasurediag, 'soultraveldiag': soultraveldiag, 'followdiag': followdiag, 'stageHailCopy': stageHailCopy, 'stageDefeatQuest': stageDefeatQuest, 'stageLiberate': stageLiberate, 'stageArtifactFound': stageArtifactFound, 'stageEnterPlace': stageEnterPlace, 'stageShapeFight': stageShapeFight, 'objectscan': objectscan, 'stageEdgeClaim': stageEdgeClaim, 'stageSharedUse': stageSharedUse, 'stagePickPocket': stagePickPocket, 'stageMurder': stageMurder, 'murderwitnessdiag': murderwitnessdiag, 'stageMurderWitness': stageMurderWitness, 'trespassdiag': trespassdiag, 'stageTrespass': stageTrespass, 'stageCompanionMurder': stageCompanionMurder, 'questdumpdiag': questdumpdiag, 'stageHostUnreachable': stageHostUnreachable, 'menutextdiag': menutextdiag, 'stageSubtitles': stageSubtitles, 'stageWitness': stageWitness, 'stageTogether': stageTogether, 'stageRansack': stageRansack, 'stageMapMarks': stageMapMarks, 'stageTradeQuality': stageTradeQuality, 'stageTradeKeys': stageTradeKeys, 'fakebreaddiag': fakebreaddiag, 'stageCorpseProp': stageCorpseProp, 'fakebreadlivediag': fakebreadlivediag, 'stagePlayerTrade': stagePlayerTrade, 'stageTradePrice': stageTradePrice, 'skilldiag': skilldiag, 'stageW10': stageW10, 'stageW8': stageW8, 'stageW9': stageW9, 'stageTradeGoods': stageTradeGoods, 'postraidsave': postraidsave, 'puppettalkdiag': puppettalkdiag, 'stageGlide': stageGlide, 'stageWear': stageWear, 'stageW1': stageW1, 'stageFormatTrunc': stageFormatTrunc, 'stageW2': stageW2, 'stageW3': stageW3, 'stageW4': stageW4, 'stageStoryReload': stageStoryReload, 'stageW5': stageW5, 'stageW6': stageW6, 'stageW7': stageW7, 'stageX1': stageX1, 'stageF1': stageF1, 'brentondiag': brentondiag, 'xpinfodiag': xpinfodiag, 'stageA4': stageA4, 'stageA3': stageA3, 'stageQ2': stageQ2, 'stageK1': stageK1, 'stageS1': stageS1, 'stageS2': stageS2, 'stageS3': stageS3, 'stageCallAnimals': stageCallAnimals, 'callanimals2diag': callanimals2diag, 'animalscandiag': animalscandiag, 'callanimalsdiag': callanimalsdiag, 'stageOpenLocks': stageOpenLocks, 'stageSelfSpell': stageSelfSpell, 'selfspelldiag': selfspelldiag, 'stageNetOutage': stageNetOutage, 'stageR2': stageR2, 'stageM2': stageM2, 'stageRaid': stageRaid, 'stageM1': stageM1, 'stageR1': stageR1, 'theftdiag': theftdiag, 'stageCreature': stageCreature, 'stageCreatureLife': stageCreatureLife, 'stageControl': stageControl, 'stageNpcSpell': stageNpcSpell, 'stagePoison': stagePoison, 'theftscan': theftscan, 'doorlatediag': doorlatediag, 'doordiag': doordiag, 'worldscan': worldscan, 'usediag': usediag, 'friendlydiag': friendlydiag, 'parity': parity, 'scriptfinddiag': scriptfinddiag, 'tracediag': tracediag, 'deaddiag': deaddiag, 'stageL': stageL, 'stageP3a': stageP3a, 'invariantself': invariantself, 'puppetposediag': puppetposediag, 'syncdiag': syncdiag, 'ownerdiag': ownerdiag, 'potiondiag': potiondiag, 'transformdiag': transformdiag, 'rangeddiag': rangeddiag, 'rangedbot': rangedbot, 'stageP1': stageP1, 'stageP2': stageP2, 'stageP2hit': stageP2hit, 'stageP2fire': stageP2fire, 'stageHealOther': stageHealOther, 'raiddiag': raiddiag, 'arrowtouchdiag': arrowtouchdiag, 'scriptdumpdiag': scriptdumpdiag, 'stageT': stageT, 'stageV5': stageV5, 'stageV4': stageV4, 'posediag': posediag, 'stageV3': stageV3, 'stageV1': stageV1, 'stageV2': stageV2, 'viewdiag': viewdiag, 'stageE5': stageE5, 'stageQ': stageQ, 'questdiag': questdiag, 'stageE4': stageE4, 'infodescdiag': infodescdiag, 'stageE3': stageE3, 'stageE2': stageE2, 'stageE1': stageE1, 'aggrodiag': aggrodiag, 'stageH3b': stageH3b, 'infoscandiag': infoscandiag, 'gotodiag2': gotodiag2, 'walkdiag': walkdiag, 'gotodiag': gotodiag, 'navdiag': navdiag, 'soak': soak, 'stageH2': stageH2, 'stageH1': stageH1, 'infodiag': infodiag, 'stageG2': stageG2, 'stageG': stageG, 'stageD3b': stageD3b, 'stageD3a': stageD3a, 'stageD2': stageD2, 'stageD1': stageD1, 'stageC': stageC, 'stageB': stageB, 'stageA2': stageA2, 'stageA': stageA, 'herodeathdiag': herodeathdiag, 'herodiag': herodiag, 'givexpdiag': givexpdiag, 'stage3c': stage3c, 'farwritediag': farwritediag, 'stage1': stage1, 'stage2': stage2, 'animdiag': animdiag, 'stage3a': stage3a, 'scriptdiag': scriptdiag,
          'stage3b': stage3b, 'combatdiag': combatdiag, 'stackdiag': stackdiag, 'stage4': stage4, 'stage4b': stage4b, 'talkdiag': talkdiag, 'namesdiag': namesdiag, 'talkdiag2': talkdiag2, 'stage4c': stage4c, 'itemdiag': itemdiag, 'stage5': stage5, 'lootdiag': lootdiag, 'stage5b': stage5b, 'stage5c': stage5c, 'chestdiag': chestdiag, 'stageChest': stageChest, 'quicklootdiag': quicklootdiag, 'radiusdiag': radiusdiag, 'pausediag': pausediag, 'savediag': savediag, 'stageQuickLoot': stageQuickLoot, 'tradediag': tradediag, 'stageTrade': stageTrade, 'spellfuncdiag': spellfuncdiag, 'stageDialogAudio': stageDialogAudio, 'listendiag': listendiag, 'stageU1': stageU1, 'stageBigTrade': stageBigTrade, 'stageReloadLoot': stageReloadLoot, 'stageLanguage': stageLanguage, 'stageEndingWait': stageEndingWait, 'stageStoryCompanion': stageStoryCompanion}
SUITES['stageEmote'] = stageEmote


IDLE_TASKS = ('ZS_HangAround_DoNothing', '')


def invariants(copies, report, samples=3, pause=3.0, radius=3000.0):
    """P5 invariant monitors (DESIGN-2026-10-19.md): with both games still running at the end of a suite, compare the
    NPCs near both heroes three times, 3 s apart; a finding counts only if it holds in every sample (no transients).
    Report only (report['invariants']); a suite does not fail on them yet. Rules:
      dead_mismatch   dead on one machine, alive on the other
      double_sim      seen on both, mirrored on neither: both machines simulate it
      foreign_ai      a mirror runs a task of its own (its owner decides what it does)
      drift           alive on both, more than 5 m apart
      hp_mismatch     alive on both, different hit points
      no_motion       alive, no motion at all (T-pose)
    """
    host, client = copies.get('host'), copies.get('client')
    if not host or not client or not host.process or not client.process or host.process.poll() is not None or client.process.poll() is not None:
        report['invariants'] = {'skipped': 'not both games running'}
        return
    try:
        if not remotes(host.bot).get(2, {}).get('puppet'):
            report['invariants'] = {'skipped': 'not connected in one world'}
            return
        rounds = []
        for i in range(samples):
            h, c = npcs(host.bot), npcs(client.bot)
            hn, cn = near_rows(host.bot), near_rows(client.bot)
            rounds.append((h, c, hn, cn))
            if i + 1 < samples:
                time.sleep(pause)
    except Exception as error:
        report['invariants'] = {'skipped': f'{type(error).__name__}: {error}'[:200]}
        return

    def short(guid):
        return guid.replace('-', '')[:12]
    found = {}
    for h, c, hn, cn in rounds:
        this = {}
        for guid, a in h.items():
            b = c.get(guid)
            if not b:
                continue
            name = a['name']
            rules = []
            if a['dead'] != b['dead']:
                rules.append('dead_mismatch')
            if not a['mirrored'] and not b['mirrored'] and not a['dead'] and not b['dead']:
                rules.append('double_sim')
            for side, row in (('host', a), ('client', b)):
                if row['mirrored'] and not row['dead'] and row['task'] not in IDLE_TASKS and not row['task'].startswith('ZS_Dead') \
                        and not row['task'].startswith('ZS_Talk'):
                    rules.append(f'foreign_ai_{side}')
            if not a['dead'] and not b['dead']:
                if distance(a['pos'], b['pos']) > 500.0:
                    rules.append('drift')
                if a['hp'] != b['hp']:
                    rules.append('hp_mismatch')
            for side, rows in (('host', hn), ('client', cn)):
                row = rows.get(short(guid))
                if row and not row['dead'] and row['motion'] in ('-', '', '<fault>'):
                    rules.append(f'no_motion_{side}')
            for rule in rules:
                this[(guid, rule)] = name
        found = this if not found and rounds.index((h, c, hn, cn)) == 0 else {k: v for k, v in found.items() if k in this}
    summary = {}
    for (guid, rule), name in found.items():
        summary.setdefault(rule, []).append(name)
    report['invariants'] = {'checked_npcs': len(set(rounds[-1][0]) & set(rounds[-1][1])), 'violations': summary,
                            'count': sum(len(v) for v in summary.values())}


def hang_stacks(copy, report):
    """Before a game that does not close is killed: where its threads are (qa/hangstack.py), into <tag>-hang-stacks.txt and
    the first lines of its main thread into the report (22.10.: hangs without a crash line or a GPU reset)."""
    try:
        text = hangstack.sample(copy.process.pid)
        (copy.run/f'{copy.tag}-hang-stacks.txt').write_text(text, encoding='utf-8')
        report.setdefault('processes', {})[copy.tag+'_hang_main_thread'] = text.splitlines()[1:12]
    except Exception as error:  # noqa: BLE001 - the kill goes on either way
        report.setdefault('processes', {})[copy.tag+'_hang_stacks_error'] = f'{type(error).__name__}: {error}'


def close(copy):
    """Ask the game to close like the title-bar button; returns True if the process ended."""
    if copy.process is None or copy.process.poll() is not None:
        return copy.process is not None
    try:
        copy.bot.send('closewindow')
    except RuntimeError:
        pass
    try:
        copy.process.wait(timeout=30)
        return True
    except subprocess.TimeoutExpired:
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', choices=sorted(SUITES), required=True)
    parser.add_argument('--seconds', type=int, default=420)
    parser.add_argument('--negative-control', action='store_true')
    parser.add_argument('--puppet-template', default='PC_Hero')
    args = parser.parse_args()
    global PUPPET_TEMPLATE, POSTRAID
    PUPPET_TEMPLATE = args.puppet_template
    POSTRAID = postraid_for(args.suite)
    if PAIR == '1':
        receipt = json.loads(RECEIPT.read_text())
        assert any(e['file'] == OURS for e in receipt['files']) and 'scripts/Script_G3Coop.dll' in receipt.get('disabled', []), \
            'Run profile.py together first'
        mod_sha = next(e['sha256'] for e in receipt['files'] if e['file'] == OURS)
        checks = (('verify.py',), ('verify_client.py',))
    else:
        deployed = WORKSPACE/f'runtime/pair{PAIR}-deployment.json'
        assert deployed.is_file(), f'Run runtime/install_pair2.py first (pair {PAIR}: --pair {PAIR})'
        mod_sha = json.loads(deployed.read_text())['sha256']
        checks = (('verify_pair2.py', PAIR),)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    lock = 'Local\\CoopAnything_Gothic3QA_'+hashlib.sha256(str(COPIES['host']).casefold().encode()).hexdigest()[:16]
    assert kernel.CreateMutexW(None, True, lock) and ctypes.get_last_error() != 183, 'Another QA runner is active'
    (WORKSPACE/f'runtime/pair{PAIR}-runner.pid').write_text(str(os.getpid()))  # runtime/stop_pair.py stops only this pair
    for name, *check_args in checks:
        subprocess.run([sys.executable, str(WORKSPACE/'isolation'/name), *check_args], cwd=WORKSPACE.parent, capture_output=True, check=True)
    protected = Path(json.loads((WORKSPACE/'project.json').read_text(encoding='utf-8-sig'))['protected_save_dir'])
    before = snapshot(protected)
    slot = {}
    if not POSTRAID:
        RAID.take(slot)  # before the run's clock starts: waiting for another pair's raid is not the suite's time
    run = RUNS/(time.strftime('%Y%m%d-%H%M%S')+'-together'+('' if PAIR == '1' else f'-p{PAIR}'))
    run.mkdir(parents=True)
    report = {'suite': args.suite, 'negative_control': args.negative_control, 'puppet_template': args.puppet_template, 'processes': {},
              'mod_sha256': mod_sha, 'pair': PAIR, 'start': 'post-raid save' if POSTRAID else 'new game', **slot}
    began = time.monotonic()
    deadline = began+args.seconds
    copies = {tag: Copy(tag, run, deadline, report) for tag in COPIES}
    global NETLINK
    netsim = NETLINK = NetSim(NETSIM) if NETSIM else None
    if netsim:
        report['netsim'] = {'spec': NETSIM}
    global CHAOS
    if CHAOS_PROFILE:
        assert not NETSIM, 'G3_NETSIM or G3_CHAOS, not both'
        sys.path.insert(0, os.environ['G3_CHAOS_DIR'])
        from relay import Relay
        CHAOS = Relay(('127.0.0.1', PORT), RELAY_PORT, CHAOS_PROFILE, log_path=run/'chaos.jsonl').start()
        report['chaos'] = {'profile': CHAOS_PROFILE}
    try:
        SUITES[args.suite](copies, report, args.negative_control)
        report['status'] = 'passed'
    except Exception as error:
        report['status'] = 'failed'
        report['error'] = f'{type(error).__name__}: {error}'
    finally:
        for c in NATIVE_WATCHED:
            try:
                if os.environ.get('G3_NATIVEWATCH'):
                    report.setdefault('native', {})[c.tag] = native_counts(c.bot)
                if os.environ.get('G3_AICOUNT'):
                    start = c.bot.send('aicount', 20)
                    c.bot.line('AICOUNT end', 10, 'aicount', start)
                    report.setdefault('aicount', {})[c.tag] = [l[8:] for l in c.bot.log()[start:].splitlines() if l.startswith('AICOUNT name=')]
            except Exception as error:
                report.setdefault('native', {})[c.tag] = f'{type(error).__name__}: {error}'[:200]
        if os.environ.get('G3_INVARIANTS', '1') != '0':
            invariants(copies, report)
        for tag, copy in copies.items():
            if copy.process is None:
                continue
            report[tag+'_closed_normally'] = close(copy)
            if copy.process.poll() is None:
                report['processes'][tag+'_forced_cleanup'] = True
                hang_stacks(copy, report)
                copy.process.kill()
                copy.process.wait(timeout=10)
            report['processes'][tag+'_exit_code'] = copy.process.returncode
            copy.collect()
            # A crash at exit is still a crash: require the engine's own shutdown line and no exception.
            engine = (copy.root/'isolated-profile/Documents/gothic3/paru/g3debug.log').read_text(errors='replace')
            clean = report[tag+'_closed_normally'] and 'DestroyEngine - all engine modules destroyed' in engine \
                and 'EXCEPTION_' not in engine
            report[tag+'_clean_shutdown'] = clean
            if not clean and report['status'] == 'passed':
                report['status'] = 'failed'
                report['error'] = f'{tag}: no clean engine shutdown'
        for cleanup in CLEANUPS:
            cleanup()
        RAID.release()
        if netsim:
            report['netsim'].update(netsim.stop())
        if CHAOS:
            report['chaos']['stats'] = CHAOS.get_stats()
            CHAOS.close()
        report['protected_saves_unchanged'] = snapshot(protected) == before
        resets = driver_resets(time.monotonic() - began + 10)
        if resets:
            report['driver_resets'] = resets
            if report['status'] == 'failed':
                report['error'] = f'GPU driver reset during the run {resets} (not a finding about the mod; run again): ' + report['error']
        (run/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        (WORKSPACE/'runtime/latest-run.txt').write_text(str(run))
        emit(report)
        failed = report['status'] == 'failed' or not report['protected_saves_unchanged']
        print('RESULT: '+('FAIL' if failed else 'PASS'), flush=True)
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
