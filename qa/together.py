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

# Two pairs of test copies so that two agents can test in parallel (G3_PAIR=1 or 2; owner's approval 2026-10-18). Each pair
# has its own copies, port and QA lock; pair 2 is created by runtime/make_pair2.py and checked by isolation/verify_pair2.py.
PAIR = os.environ.get('G3_PAIR', '1')
assert PAIR in ('1', '2'), 'G3_PAIR must be 1 or 2'
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


class Copy:
    def __init__(self, tag, run, deadline, report):
        self.tag, self.root, self.run, self.deadline, self.report = tag, COPIES[tag], run, deadline, report
        self.process = None
        self.bot = None

    def launch(self, role='host', name='Host'):
        (self.root/'g3together.ini').write_text(
            f'role = {role}\naddress = 127.0.0.1\nport = {RELAY_PORT if (NETSIM or CHAOS_PROFILE) and role == "client" else PORT}\nname = {name}\n'
            f'puppet_template = {PUPPET_TEMPLATE}\n'
            + ''.join(f'{line}\n' for line in os.environ.get('G3_INI_EXTRA', '').split(';') if line))
        (self.root/'qa-enable.txt').write_text('Enabled only for this bounded QA run\n')
        # Gothic 3 allows one running game per PC (named mutex GenomeMutex); a game that finds it exits at once with
        # code 0. With two pairs of test copies (two agents) any game may be the one holding it, so every game
        # releases it as soon as its QA hook answers, and a start that exits early is retried after a random pause.
        import random
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
        if ready:
            start = self.bot.send('releaselock')
            self.bot.line('LOCK scan complete', 10, 'lock scan', start)
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


def join_both(copies, host_before=None):
    """host_before: (command, reply prefix) the host runs before the client joins (a negative control that must be in
    place before the client is first close to anything)."""
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
    target = max(orcs, key=lambda g: (host_npcs[g]['hp'], g))
    report['target'] = {'guid': target, 'name': host_npcs[target]['name'], 'hp': host_npcs[target]['hp']}
    if negative:
        start = client.bot.send('combatoff', 20)
        assert 'ok=1' in client.bot.line('COMBATOFF ', 10, 'combatoff', start)
    # A: one client hit. Undone locally at once, applied on the host, mirrored back.
    host_before = host_npcs[target]['hp']
    first = hit_npc(client.bot, target)
    # A raider in a fight may parry (task ZS_Parade*): no damage, so nothing to claim. Game behaviour, swing again.
    for _ in range(3):
        if first['claim_sent'] or 'task=ZS_Parade' not in first['line']:
            break
        report.setdefault('parried', []).append(first['line'])
        time.sleep(2)
        first = hit_npc(client.bot, target)
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
    if negative:
        start = client.bot.send('xpguardoff', 20)
        assert 'ok=1' in client.bot.line('XPGUARDOFF ', 10, 'xpguardoff', start)
    report['client_xp_before'] = combat_state(client.bot)['xp']
    report['host_xp_before'] = combat_state(host.bot)['xp']
    clear_raid(host, report)
    time.sleep(10)
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
    for _ in range(80):
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


def help_up(helper, downed, report, tag):
    """Since stage A2 a knocked-out player waits for a partner: the helper steps next to it."""
    lying = remotes(helper.bot)[2 if downed.tag == 'client' else 1]['pos']
    report[tag+'_helper_moved'] = move_hero(helper.bot, (lying[0] + 100, lying[1], lying[2]))


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
    """Stage A2, a knocked-out player waits for help (owner's rules of 19.10.): it stays down far longer than the
    game's own 5 to 10 s while a partner could help; a partner standing next to it for 3 s pays one healing potion
    and stands it up; a partner without a potion is told so and the player stays down; when nobody can help
    (everybody down) both die as in the single-player game. Everyone is told on screen. Negative control:
    potionoff (helping up is free)."""
    host, client = join_both(copies)
    if negative:
        report_line(host.bot, 'potionoff', 'POTIONOFF')
    potions = lambda bot: int(re.search(r'health=(\d+)', report_line(bot, 'potions', 'POTIONS end'))[1])
    up = lambda bot: (lambda s: ' down=0 ' in s and ' dead=0 ' in s and [s])(herostate(bot))
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
    report['client_revived'] = wait_for(lambda: up(client.bot), 15, 'client stands up after help')[0]
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
    time.sleep(8)
    report['client_without_potion'] = herostate(client.bot)
    report['host_hud_no_potion'] = report_line(host.bot, 'hud', 'HUD ')
    report['client_hud_no_potion'] = report_line(client.bot, 'hud', 'HUD ')
    assert ' down=1 ' in report['client_without_potion'], 'helped up without a potion: ' + report['client_without_potion']
    assert any(text in report['host_hud_no_potion'] for text in ('need a healing potion', 'brauchst einen Heiltrank')), report['host_hud_no_potion']
    assert any(text in report['client_hud_no_potion'] for text in ('has no healing potion', 'keinen Heiltrank')), report['client_hud_no_potion']
    report['host_one_potion'] = report_line(host.bot, 'givepotion It_Potion_Health-1', 'GIVEPOTION ')
    report['client_revived_2'] = wait_for(lambda: up(client.bot), 15, 'client stands up once the host has a potion')[0]
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
            lying = remotes(host.bot)[2]['pos']
            move_hero(host.bot, (lying[0] + 100, lying[1], lying[2]))
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


def stageE4(copies, report, negative=False):
    """Stage E4, NPC against NPC where two regions meet: in the client's region (its NPCs mirrored on the host) one of
    the host's NPCs (Lester, moved there) attacks a client-owned NPC. Lester's hits go to the client, which applies
    them to its NPC; that NPC's hits on Lester come back to the host."""
    host, client = join_both(copies)
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
    me = tuple(map(float, report_line(host.bot, 'heropos', 'HEROPOS ').split('pos=')[1].split(',')))
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
    return tuple(map(float, report_line(bot, 'heropos', 'HEROPOS ').split('pos=')[1].split(',')))


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
        hit_npc(client.bot, guid)
        line, a = arena_of(host.bot, fighter)
        if a.get('down') == '1' or a.get('dead') == '1':
            break
        time.sleep(0.3)
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
    report['fighting_before_save'] = wait_for(fighting, 90, "host NPCs fight the client's puppet")
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


def stageX1(copies, report, negative=False):
    """Stage X1, malformed messages from the other machine change nothing and crash nothing (Fable's code review 22.10.,
    section 1; hypotheses until this test). Host to client: a file chunk whose offset + length wraps past 2^32 (1.1) and a
    campaign named ..\\..\\G3T_evil (1.4): the client stays up, logs the name as refused and writes no file outside its save
    folder. Client to host: an NPC record with the hero's GUID and a NaN position (1.6) and player states with a NaN
    position (1.2): the host's hero keeps its own task and place, is not mirrored, and the client's puppet keeps a finite
    position; both games keep running. No negative control: without the checks a run may crash a game."""
    host, client = join_both(copies)
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
    me = tuple(map(float, report_line(host.bot, 'heropos', 'HEROPOS ').split('pos=')[1].split(',')))
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


def arrow_hit_phase(shooter, watcher, shooter_id, host, report, tag, wanted=1, spell=None):
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
    keep = ('Diego', 'Gorn', 'Milten', 'Lester')
    sturdy = {g: v for g, v in npcs(host.bot).items() if not v['dead'] and not v['mirrored'] and v['hp'] >= 300
              and 'Orc' not in v['name'] and v['name'] not in keep}
    assert sturdy, 'no villager with 300 hit points near the host'
    target = next(iter(sturdy))
    name = npcs(host.bot)[target]['name']
    report[tag+'_target'] = (name, npcs(host.bot)[target]['hp'])
    clear_spot(shooter, host, target, report, tag, 250)
    goal = npcs(host.bot)[target]['pos']
    at = remotes(watcher.bot)[shooter_id]['pos']
    bearing = math.atan2(at[2] - goal[2], at[0] - goal[0])

    def around(angle, distance):
        return (goal[0] + math.cos(angle) * distance, at[1], goal[2] + math.sin(angle) * distance)

    # The other player 6 m behind the shooter (its puppet would stop the arrows); after a blocked shot the shooter
    # goes 45 degrees further round the target at 2.5 m (stepping back left it between Hamlar's houses).
    report[tag+'_watcher_moved'] = move_hero(watcher.bot, around(bearing, 850))
    time.sleep(2)
    # Any source: a client's arrow reaches the host as a claim, applied there by the combat filter.
    applied_before = [h for h in summon_state(host.bot)[1] if f'victim={name} ' in h and ' applied=1' in h]
    shots, reached, tries = [], 0, 0
    while reached < wanted and tries < 8:
        tries += 1
        since = max([int(re.search(r't=(\d+)', l)[1]) for l in touch_lines(shooter.bot)] or [0])
        report_line(shooter.bot, 'ready '+weapon, 'READY ')
        report_line(shooter.bot, 'aimat '+target, 'TARGET ')
        if spell:
            shot = report_line(shooter.bot, 'castat '+target, 'CASTAT ')
        else:
            shot = report_line(shooter.bot, f'shootat {target}-5', 'SHOOTAT ')
        time.sleep(2.5)
        touches = [l for l in touch_lines(shooter.bot) if int(re.search(r't=(\d+)', l)[1]) > since]
        # Reached: an armed touch of the target, also after a bounce (the first touch can be the ground, the arrow armed on).
        first = next((l for l in touches if ' armed=1 ' in l and f' touched={name} ' in l), next((l for l in touches if ' armed=1 ' in l), ''))
        hit = f' touched={name} ' in first
        reached += hit
        shots.append({'shoot': shot[8:120], 'first': first[first.find('arrow='):], 'reached': hit,
                      'watcher_touches': [l[l.find('arrow='):] for l in touch_lines(watcher.bot)[-2:]]})
        if not hit:
            report.setdefault(tag+'_stepped', []).append(move_hero(shooter.bot, around(bearing + tries * math.pi / 4, 250)))
            move_hero(watcher.bot, around(bearing + tries * math.pi / 4, 850))
            time.sleep(1.5)
    time.sleep(2)
    host_hits = [h for h in summon_state(host.bot)[1] if f'victim={name} ' in h and ' applied=1' in h]
    new_hits = [h for h in host_hits if h not in applied_before]
    report[tag+'_shots'] = shots
    report[tag+'_reached'] = (reached, tries)
    report[tag+'_host_hits'] = new_hits
    report[tag+'_watcher_hits'] = [h for h in summon_state(watcher.bot)[1] if 'source=Arrow' in h][-4:]
    report[tag+'_target_after'] = npc_hp(host.bot, target)
    problems = []
    if reached < wanted:
        problems.append(f'{tag}: only {reached} of {wanted} arrows reached {name} in {tries} tries')
    if len(new_hits) != reached:
        problems.append(f'{tag}: {len(new_hits)} arrow hits applied on the host for {reached} arrows that reached {name}')
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
    expected = {'It_Spell_Sleep': 'ZS_MagicSleep', 'It_Spell_Fear': 'ZS_Flee', 'It_Spell_Charm': 'memory'}
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
    a world animal of that kind north of Ardea (a spawned one exists only on the host). Negative control: spellmirroroff (the
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
    problems, used = [], set()
    for caster, watcher, caster_id in ((client, host, 2), (host, client, 1)):
        tag = caster.tag
        keep = ('Diego', 'Gorn', 'Milten', 'Lester')
        calm = [g for g, v in npcs(host.bot).items() if not v['dead'] and not v['mirrored'] and 'Orc' not in v['name']
                and v['name'] not in keep and g not in used]
        assert calm, 'no calm villager near the host'
        target = next((g for g in calm if npcs(host.bot)[g]['name'].startswith('Slave')), calm[0])
        if os.environ.get('G3_ANIMAL'):
            # A world animal both machines have (scavengers graze north of Ardea, about 90500,-6100; seen in old runs).
            for c in (host, client):
                move_hero(c.bot, (90500.0, 5300.0, -6600.0))
            herd = []
            until = time.monotonic() + 20  # the area's animals come in once it is loaded
            while not herd and time.monotonic() < until:
                time.sleep(2)
                herd = [g for g, v in npcs(host.bot).items() if v['name'].startswith(os.environ['G3_ANIMAL']) and not v['dead']
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
        report[tag+'_cast'] = report_line(caster.bot, 'castat '+target, 'CASTAT ')[7:300]
        time.sleep(3)
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
    """P2, arrows act once (agent B): the shooter's arrows that reach a sturdy NPC (first touch logged by QA touchlog)
    are applied exactly once on the host, the NPC's owner; the copies on the other machine touch it too but belong to
    the shooter's puppet, so the combat filter drops their hits. Host shoots, then client. Negative control:
    shotcopyhero (the copies belong to the watcher's own hero and hit a second time)."""
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
    arrow_hit_phase(host, client, 1, host, report, 'host')
    arrow_hit_phase(client, host, 2, host, report, 'client')
    problems = report['host_problems'] + report['client_problems']
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


SUITES = {'stageX1': stageX1, 'stageF1': stageF1, 'brentondiag': brentondiag, 'xpinfodiag': xpinfodiag, 'stageA4': stageA4, 'stageA3': stageA3, 'stageQ2': stageQ2, 'stageK1': stageK1, 'stageS1': stageS1, 'stageS2': stageS2, 'stageS3': stageS3, 'stageCallAnimals': stageCallAnimals, 'callanimals2diag': callanimals2diag, 'animalscandiag': animalscandiag, 'callanimalsdiag': callanimalsdiag, 'stageOpenLocks': stageOpenLocks, 'stageSelfSpell': stageSelfSpell, 'selfspelldiag': selfspelldiag, 'stageNetOutage': stageNetOutage, 'stageR2': stageR2, 'stageM2': stageM2, 'stageRaid': stageRaid, 'stageM1': stageM1, 'stageR1': stageR1, 'theftdiag': theftdiag, 'stageCreature': stageCreature, 'stageCreatureLife': stageCreatureLife, 'stageControl': stageControl, 'stageNpcSpell': stageNpcSpell, 'stagePoison': stagePoison, 'theftscan': theftscan, 'doorlatediag': doorlatediag, 'doordiag': doordiag, 'worldscan': worldscan, 'usediag': usediag, 'friendlydiag': friendlydiag, 'parity': parity, 'scriptfinddiag': scriptfinddiag, 'tracediag': tracediag, 'deaddiag': deaddiag, 'stageL': stageL, 'stageP3a': stageP3a, 'invariantself': invariantself, 'puppetposediag': puppetposediag, 'syncdiag': syncdiag, 'ownerdiag': ownerdiag, 'potiondiag': potiondiag, 'transformdiag': transformdiag, 'rangeddiag': rangeddiag, 'rangedbot': rangedbot, 'stageP1': stageP1, 'stageP2': stageP2, 'stageP2hit': stageP2hit, 'stageP2fire': stageP2fire, 'stageHealOther': stageHealOther, 'raiddiag': raiddiag, 'arrowtouchdiag': arrowtouchdiag, 'scriptdumpdiag': scriptdumpdiag, 'stageT': stageT, 'stageV5': stageV5, 'stageV4': stageV4, 'posediag': posediag, 'stageV3': stageV3, 'stageV1': stageV1, 'stageV2': stageV2, 'viewdiag': viewdiag, 'stageE5': stageE5, 'stageQ': stageQ, 'questdiag': questdiag, 'stageE4': stageE4, 'infodescdiag': infodescdiag, 'stageE3': stageE3, 'stageE2': stageE2, 'stageE1': stageE1, 'aggrodiag': aggrodiag, 'stageH3b': stageH3b, 'infoscandiag': infoscandiag, 'gotodiag2': gotodiag2, 'walkdiag': walkdiag, 'gotodiag': gotodiag, 'navdiag': navdiag, 'soak': soak, 'stageH2': stageH2, 'stageH1': stageH1, 'infodiag': infodiag, 'stageG2': stageG2, 'stageG': stageG, 'stageD3b': stageD3b, 'stageD3a': stageD3a, 'stageD2': stageD2, 'stageD1': stageD1, 'stageC': stageC, 'stageB': stageB, 'stageA2': stageA2, 'stageA': stageA, 'herodeathdiag': herodeathdiag, 'herodiag': herodiag, 'givexpdiag': givexpdiag, 'stage3c': stage3c, 'farwritediag': farwritediag, 'stage1': stage1, 'stage2': stage2, 'animdiag': animdiag, 'stage3a': stage3a, 'scriptdiag': scriptdiag,
          'stage3b': stage3b, 'combatdiag': combatdiag, 'stackdiag': stackdiag, 'stage4': stage4, 'stage4b': stage4b, 'talkdiag': talkdiag, 'namesdiag': namesdiag, 'talkdiag2': talkdiag2, 'stage4c': stage4c, 'itemdiag': itemdiag, 'stage5': stage5, 'lootdiag': lootdiag, 'stage5b': stage5b, 'stage5c': stage5c, 'chestdiag': chestdiag, 'stageChest': stageChest, 'quicklootdiag': quicklootdiag, 'radiusdiag': radiusdiag, 'pausediag': pausediag, 'savediag': savediag, 'stageQuickLoot': stageQuickLoot, 'tradediag': tradediag, 'stageTrade': stageTrade, 'spellfuncdiag': spellfuncdiag, 'stageDialogAudio': stageDialogAudio, 'listendiag': listendiag}


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
    global PUPPET_TEMPLATE
    PUPPET_TEMPLATE = args.puppet_template
    if PAIR == '1':
        receipt = json.loads(RECEIPT.read_text())
        assert any(e['file'] == OURS for e in receipt['files']) and 'scripts/Script_G3Coop.dll' in receipt.get('disabled', []), \
            'Run profile.py together first'
        mod_sha = next(e['sha256'] for e in receipt['files'] if e['file'] == OURS)
        checks = ('verify.py', 'verify_client.py')
    else:
        deployed = WORKSPACE/'runtime/pair2-deployment.json'
        assert deployed.is_file(), 'Run runtime/install_pair2.py first'
        mod_sha = json.loads(deployed.read_text())['sha256']
        checks = ('verify_pair2.py',)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    lock = 'Local\\CoopAnything_Gothic3QA_'+hashlib.sha256(str(COPIES['host']).casefold().encode()).hexdigest()[:16]
    assert kernel.CreateMutexW(None, True, lock) and ctypes.get_last_error() != 183, 'Another QA runner is active'
    (WORKSPACE/f'runtime/pair{PAIR}-runner.pid').write_text(str(os.getpid()))  # runtime/stop_pair.py stops only this pair
    for name in checks:
        subprocess.run([sys.executable, str(WORKSPACE/'isolation'/name)], cwd=WORKSPACE.parent, capture_output=True, check=True)
    protected = Path(json.loads((WORKSPACE/'project.json').read_text(encoding='utf-8-sig'))['protected_save_dir'])
    before = snapshot(protected)
    run = RUNS/(time.strftime('%Y%m%d-%H%M%S')+'-together'+('' if PAIR == '1' else f'-p{PAIR}'))
    run.mkdir(parents=True)
    report = {'suite': args.suite, 'negative_control': args.negative_control, 'puppet_template': args.puppet_template, 'processes': {},
              'mod_sha256': mod_sha, 'pair': PAIR}
    deadline = time.monotonic()+args.seconds
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
        if os.environ.get('G3_INVARIANTS', '1') != '0':
            invariants(copies, report)
        for tag, copy in copies.items():
            if copy.process is None:
                continue
            report[tag+'_closed_normally'] = close(copy)
            if copy.process.poll() is None:
                report['processes'][tag+'_forced_cleanup'] = True
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
        if netsim:
            report['netsim'].update(netsim.stop())
        if CHAOS:
            report['chaos']['stats'] = CHAOS.get_stats()
            CHAOS.close()
        report['protected_saves_unchanged'] = snapshot(protected) == before
        (run/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        (WORKSPACE/'runtime/latest-run.txt').write_text(str(run))
        emit(report)
        failed = report['status'] == 'failed' or not report['protected_saves_unchanged']
        print('RESULT: '+('FAIL' if failed else 'PASS'), flush=True)
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
