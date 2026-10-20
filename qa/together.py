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
RECEIPT = WORKSPACE/'runtime/myrtana-deployment.json'
OURS = 'scripts/Script_G3Together.dll'
PUPPET_TEMPLATE = 'PC_Hero'
WEAPON = 'It_1H_Sword_Rust_01'


class Copy:
    def __init__(self, tag, run, deadline, report):
        self.tag, self.root, self.run, self.deadline, self.report = tag, COPIES[tag], run, deadline, report
        self.process = None
        self.bot = None

    def launch(self, role='host', name='Host'):
        (self.root/'g3together.ini').write_text(
            f'role = {role}\naddress = 127.0.0.1\nport = {PORT}\nname = {name}\npuppet_template = {PUPPET_TEMPLATE}\n'
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


def join_both(copies):
    host = copies['host'].launch('host', 'Host')
    enter_world(host)
    start = host.bot.send('releaselock')
    host.bot.line('LOCK scan complete', 10, 'lock scan', start)
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
    allowed = 500 if quest(client.bot, 'quest', 'Ardea_Revolution') == SUCCESS else 0
    report['client_quest_reward_allowed'] = allowed
    gained = report['client_end']['xp'] - report['client_xp_before']
    assert gained in (0, allowed), f"Client earned {gained} XP for kills it had no part in"


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
                                     and 'TRACE puppet2 ' in l and ' motion=Hero_LieKnockDown' in l)
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
    assert 'liegt am Boden' in report['host_told'], 'host was not told: '+report['host_told']
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
    assert 'aufgeholfen' in report['client_hud'], report['client_hud']
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
    assert 'brauchst einen Heiltrank' in report['host_hud_no_potion'], report['host_hud_no_potion']
    assert 'keinen Heiltrank' in report['client_hud_no_potion'], report['client_hud_no_potion']
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
    assert 'Weit weg vom Host' in report['client_hud_far'], report['client_hud_far']
    assert 'weit entfernt' in report['host_hud_far'], report['host_hud_far']
    report['client_back'] = move_hero(client.bot, start_pos)
    time.sleep(4)
    report['client_range_near'] = report_line(client.bot, 'range', 'RANGE ')
    report['client_hud_near'] = report_line(client.bot, 'hud', 'HUD ')
    report['host_hud_near'] = report_line(host.bot, 'hud', 'HUD ')
    assert ' far=0 ' in report['client_range_near'] and 'all_clears=1' in report['client_range_near'], report['client_range_near']
    assert 'Wieder beim Host' in report['client_hud_near'], report['client_hud_near']
    assert 'wieder in der N' in report['host_hud_near'], report['host_hud_near']


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

    def hurt():
        line = find_npc(client.bot, victim)
        hp = int(re.search(r' hp=(-?\d+)', line)[1])
        return combat_state(client.bot)['npc_hits_applied'] > 0 and hp < hp_before and [line]
    report['client_victim'] = wait_for(hurt, 45, "Lester's hits reach the client's NPC")[0]
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
    report_line(caster.bot, 'ready It_Spell_Meteor', 'READY ')
    report_line(caster.bot, 'aimat '+target, 'TARGET ')
    report[tag+'_cast'] = report_line(caster.bot, 'castat '+target, 'CASTAT ')[7:300]
    copies_seen, own_seen = [], []
    until = time.monotonic() + 4.0
    while time.monotonic() < until:
        own_seen += [l for l in summons(caster.bot) if 'Smn_Meteor' in l]
        copies_seen += [l for l in summons(watcher.bot) if 'Smn_Meteor' in l]
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
        shot = report_line(shooter.bot, f"shootat {target}-{mode}-{os.environ.get('G3_DRAW', '0')}", 'SHOOTAT ')
        time.sleep(1.8)
        own = report_line(shooter.bot, 'lastarrow', 'LASTARROW ')
        copies = shot_state(watcher.bot)[1]
        free = ' spawned=1 ' in shot and flight(trace_points((re.search(r'trace=(\S*)', own) or [None, ''])[1]), 250)[0] >= 300
        results.append({'aim': aim[4:200], 'shoot': shot[8:420], 'own': own[10:], 'copy': copies[-1] if copies else None, 'free': free})
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
        report.setdefault(tag+'_moved_for_shot', []).append(move_hero(shooter.bot, (at[0] + step[0] * 200, at[1], at[2] + step[1] * 200)))
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


SUITES = {'parity': parity, 'scriptfinddiag': scriptfinddiag, 'tracediag': tracediag, 'deaddiag': deaddiag, 'stageL': stageL, 'stageP3a': stageP3a, 'invariantself': invariantself, 'puppetposediag': puppetposediag, 'syncdiag': syncdiag, 'ownerdiag': ownerdiag, 'potiondiag': potiondiag, 'transformdiag': transformdiag, 'rangeddiag': rangeddiag, 'rangedbot': rangedbot, 'stageP1': stageP1, 'stageP2': stageP2, 'stageP2hit': stageP2hit, 'stageP2fire': stageP2fire, 'stageHealOther': stageHealOther, 'raiddiag': raiddiag, 'arrowtouchdiag': arrowtouchdiag, 'scriptdumpdiag': scriptdumpdiag, 'stageT': stageT, 'stageV5': stageV5, 'stageV4': stageV4, 'posediag': posediag, 'stageV3': stageV3, 'stageV1': stageV1, 'stageV2': stageV2, 'viewdiag': viewdiag, 'stageE5': stageE5, 'stageQ': stageQ, 'questdiag': questdiag, 'stageE4': stageE4, 'infodescdiag': infodescdiag, 'stageE3': stageE3, 'stageE2': stageE2, 'stageE1': stageE1, 'aggrodiag': aggrodiag, 'stageH3b': stageH3b, 'infoscandiag': infoscandiag, 'gotodiag2': gotodiag2, 'walkdiag': walkdiag, 'gotodiag': gotodiag, 'navdiag': navdiag, 'soak': soak, 'stageH2': stageH2, 'stageH1': stageH1, 'infodiag': infodiag, 'stageG2': stageG2, 'stageG': stageG, 'stageD3b': stageD3b, 'stageD3a': stageD3a, 'stageD2': stageD2, 'stageD1': stageD1, 'stageC': stageC, 'stageB': stageB, 'stageA2': stageA2, 'stageA': stageA, 'herodeathdiag': herodeathdiag, 'herodiag': herodiag, 'givexpdiag': givexpdiag, 'stage3c': stage3c, 'farwritediag': farwritediag, 'stage1': stage1, 'stage2': stage2, 'animdiag': animdiag, 'stage3a': stage3a, 'scriptdiag': scriptdiag,
          'stage3b': stage3b, 'combatdiag': combatdiag, 'stackdiag': stackdiag, 'stage4': stage4, 'stage4b': stage4b, 'talkdiag': talkdiag, 'namesdiag': namesdiag, 'talkdiag2': talkdiag2, 'stage4c': stage4c, 'itemdiag': itemdiag, 'stage5': stage5, 'lootdiag': lootdiag, 'stage5b': stage5b, 'stage5c': stage5c, 'radiusdiag': radiusdiag, 'pausediag': pausediag, 'savediag': savediag}


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
