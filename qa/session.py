"""Run several stages in one game session (owner 22.10.: faster tests, same quality).

    session.py stage5b stage5c stageChest ...      (env as for together.py: G3_PAIR, G3_REPO, G3_ORDER, ...)

Starting both games, joining, the opening raid and the shutdown take most of a stage's time; here they happen once per
session. A stage shares the session only if its code joins with join_both() and clears the raid with clear_raid(), and
does nothing that changes the world or ends a game (its own launch, saves and loads, closing a window, killing the hero,
a network simulation, a negative control set before the join); every other stage runs on its own (together.py).

Quality as before:
- a stage that fails in the shared session is run again on its own, and only that result counts;
- after a failure the session ends and the next stages get a fresh one;
- if a session does not end with a clean engine shutdown, every stage of it runs again on its own;
- negative controls always run on their own.
One line per stage, as series.sh prints them; a report per stage in the session's run folder.
"""
import inspect
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import together as T  # noqa: E402

# Code that keeps a stage out of a shared session.
NOT_SHARED = ('.launch(', 'nativesave', 'nativeload', 'closewindow', 'killhero', 'NETSIM', 'NETLINK', 'host_before', 'CHAOS')


class Fresh(Exception):
    """This stage needs a session of its own."""


def shareable(name):
    source = inspect.getsource(T.SUITES[name])
    if 'join_both(' not in source or 'clear_raid(' not in source:
        return False
    return not any(token in source for token in NOT_SHARED)


def hero_position(copy):
    """The hero's position from the mod's per-second report (x, y, z), or None."""
    reports = copy.hero_reports()
    match = re.search(r'pos=(-?[\d.]+),(-?[\d.]+),(-?[\d.]+)', reports[-1]) if reports else None
    return tuple(float(v) for v in match.groups()) if match else None


class Session:
    def __init__(self, seconds):
        self.run = T.RUNS/(time.strftime('%Y%m%d-%H%M%S')+'-session'+('' if T.PAIR == '1' else f'-p{T.PAIR}'))
        self.run.mkdir(parents=True)
        self.report = {'processes': {}, 'pair': T.PAIR, 'puppet_template': T.PUPPET_TEMPLATE}
        self.copies = {tag: T.Copy(tag, self.run, time.monotonic()+seconds, self.report) for tag in T.COPIES}
        self.joined = None
        self.raid_cleared = False
        self.start = {}
        self.stages = []

    def join_both(self, copies, host_before=None):
        if host_before:
            raise Fresh('needs a command before the client joins')
        if self.joined is None:
            self.joined = ORIGINAL_JOIN(copies)
        return self.joined

    def clear_raid(self, host, report):
        if self.raid_cleared:
            report['raid_kills'] = []  # a list, as clear_raid gives it (stage3c sums it)
            report['raid_note'] = 'cleared earlier in this session'
            return
        ORIGINAL_RAID(host, report)
        self.raid_cleared = True
        for tag, copy in self.copies.items():
            self.start[tag] = hero_position(copy)

    def between(self):
        """Back to where the session began: no dialog, both heroes at their start positions."""
        for tag, copy in self.copies.items():
            if copy.bot is None:
                continue
            try:
                if T.talkstate(copy.bot).get('running') == '1':
                    start = copy.bot.send('endtalk', 20)
                    copy.bot.line('TALKSTATE ', 10, 'endtalk', start)
                if self.start.get(tag):
                    T.move_hero(copy.bot, self.start[tag])
            except Exception as error:  # noqa: BLE001 - the next stage will notice a broken session
                self.report.setdefault('between_errors', []).append(f'{tag}: {error}')
        time.sleep(3)

    def close(self):
        """End both games; True if both shut down cleanly (the engine's own line, no exception)."""
        clean = True
        for tag, copy in self.copies.items():
            if copy.process is None:
                continue
            closed = T.close(copy)
            if copy.process.poll() is None:
                T.hang_stacks(copy, self.report)
                copy.process.kill()
                copy.process.wait(timeout=10)
            copy.collect()
            engine = (copy.root/'isolated-profile/Documents/gothic3/paru/g3debug.log').read_text(errors='replace')
            ok = closed and 'DestroyEngine - all engine modules destroyed' in engine and 'EXCEPTION_' not in engine
            self.report[tag+'_clean_shutdown'] = ok
            clean = clean and ok
        for cleanup in T.CLEANUPS:
            cleanup()
        (self.run/'session.json').write_text(json.dumps({**self.report, 'stages': self.stages}, indent=2), encoding='utf-8')
        return clean


ORIGINAL_JOIN, ORIGINAL_RAID = T.join_both, T.clear_raid


def prepare():
    """What together.py checks before every run: the installed mod, the isolation of the test copies, one runner per
    pair, and a fingerprint of the owner's real saves. Returns (mutex handle, protected folder, its fingerprint, mod hash)."""
    import ctypes
    import hashlib
    if T.PAIR == '1':
        receipt = json.loads(T.RECEIPT.read_text())
        assert any(e['file'] == T.OURS for e in receipt['files']), 'Run profile.py together first'
        mod_sha = next(e['sha256'] for e in receipt['files'] if e['file'] == T.OURS)
        checks = ('verify.py', 'verify_client.py')
    else:
        deployed = T.WORKSPACE/'runtime/pair2-deployment.json'
        assert deployed.is_file(), 'Run runtime/install_pair2.py first'
        mod_sha = json.loads(deployed.read_text())['sha256']
        checks = ('verify_pair2.py',)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    lock = 'Local\\CoopAnything_Gothic3QA_'+hashlib.sha256(str(T.COPIES['host']).casefold().encode()).hexdigest()[:16]
    handle = kernel.CreateMutexW(None, True, lock)
    assert handle and ctypes.get_last_error() != 183, 'Another QA runner is active'
    (T.WORKSPACE/f'runtime/pair{T.PAIR}-runner.pid').write_text(str(os.getpid()))
    for name in checks:
        subprocess.run([sys.executable, str(T.WORKSPACE/'isolation'/name)], cwd=T.WORKSPACE.parent, capture_output=True, check=True)
    protected = Path(json.loads((T.WORKSPACE/'project.json').read_text(encoding='utf-8-sig'))['protected_save_dir'])
    return kernel, handle, protected, T.snapshot(protected), mod_sha


def run_alone(name):
    """The stage on its own, the way series.sh runs it; returns its summary."""
    args = [sys.executable, str(Path(T.__file__)), '--suite', name, '--seconds', '2400' if name == 'soak' else '1200']
    out = subprocess.run(args, capture_output=True, text=True, errors='replace').stdout.strip().splitlines()
    try:
        report = json.loads(out[-2])
        return report['status'], (report.get('error') or '')[:150]
    except (IndexError, ValueError, KeyError):
        return 'failed', 'no report'


def main():
    names = [n for n in sys.argv[1:] if not n.startswith('--')]
    unknown = [n for n in names if n not in T.SUITES]
    assert not unknown, f'unknown stages: {unknown}'
    shared = [n for n in names if shareable(n)]
    alone = [n for n in names if n not in shared]
    rerun = []
    pending = list(shared)
    kernel, handle, protected, before, mod_sha = prepare()
    while pending:
        session = Session(seconds=1200*len(pending))
        T.join_both, T.clear_raid = session.join_both, session.clear_raid
        started = time.monotonic()
        passed_here = []
        failed = False
        try:
            while pending:
                name = pending.pop(0)
                report = {'suite': name, 'negative_control': False, 'session': session.run.name}
                began = time.monotonic()
                try:
                    T.SUITES[name](session.copies, report, False)
                    report['status'] = 'passed'
                except Fresh as why:
                    report['status'], report['error'] = 'alone', str(why)
                except Exception as error:  # noqa: BLE001 - recorded, then the stage runs on its own
                    report['status'], report['error'] = 'failed', f'{type(error).__name__}: {error}'
                if report['status'] == 'passed' and os.environ.get('G3_INVARIANTS', '1') != '0':
                    T.invariants(session.copies, report)
                report['seconds'] = round(time.monotonic()-began)
                (session.run/f'{name}.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
                session.stages.append({'suite': name, 'status': report['status'], 'seconds': report['seconds']})
                if report['status'] == 'passed':
                    passed_here.append(name)
                    print(f'--suite {name} => passed (session {session.run.name}, {report["seconds"]} s)', flush=True)
                    session.between()
                else:
                    rerun.append(name)
                    if report['status'] == 'failed':
                        failed = True
                        break  # the session may be broken: a fresh one for the rest
        finally:
            clean = session.close()
            T.join_both, T.clear_raid = ORIGINAL_JOIN, ORIGINAL_RAID
        if not clean:
            print(f'session {session.run.name}: no clean shutdown; its {len(passed_here)} stages run again on their own', flush=True)
            rerun += passed_here
        print(f'session {session.run.name}: {len(session.stages)} stages in {round(time.monotonic()-started)} s'
              + (' (ended after a failure)' if failed else ''), flush=True)
    saves_unchanged = T.snapshot(protected) == before
    print(f'sessions done: mod {mod_sha[:8]}, protected saves unchanged: {saves_unchanged}', flush=True)
    assert saves_unchanged, "the owner's real saves changed"
    kernel.ReleaseMutex(handle)
    kernel.CloseHandle(handle)  # together.py takes the pair's lock for each stage run on its own
    for name in alone + rerun:
        status, error = run_alone(name)
        print(f'--suite {name} => {status} (alone) | {error}', flush=True)


if __name__ == '__main__':
    main()
