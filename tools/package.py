"""Build a player package: the mod DLL, example configuration, the player guides, the test plan, the log
collector and the licences.

    python tools/package.py <built Script_G3Together.dll> <output folder>

Writes <output folder>/G3Together-<git hash>.zip. Contains no game files.
"""
import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

HOST_INI = """# G3 Together: this player hosts the campaign.
role = host
port = 34567
name = Host
# Key that jumps to the other player (default PAGEDOWN): F1-F24, PAGEUP, HOME, END, INSERT, DELETE or a letter
partner_key = PAGEDOWN
"""
CLIENT_INI = """# G3 Together: this player joins the host.
role = client
address = 127.0.0.1
port = 34567
name = Player
partner_key = PAGEDOWN
"""


def main():
    dll, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    version = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True).stdout.strip() or 'dev'
    target = out / f'G3Together-{version}.zip'
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as z:
        z.write(dll, 'scripts/Script_G3Together.dll')
        z.writestr('g3together.ini.host-example', HOST_INI)
        z.writestr('g3together.ini.client-example', CLIENT_INI)
        z.write(ROOT / 'docs/SPIELEN.md', 'SPIELEN.md')
        z.write(ROOT / 'docs/MITSPIELER.md', 'LIESMICH-MITSPIELER.md')
        z.write(ROOT / 'docs/TESTPLAN.md', 'TESTPLAN.md')
        z.write(ROOT / 'tools/collect_logs.py', 'Werkzeuge/collect_logs.py')
        z.write(ROOT / 'COPYING', 'COPYING')
        enet = ROOT / 'thirdparty/enet/LICENSE'
        if enet.exists():
            z.write(enet, 'licenses/ENet-LICENSE')
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f'{target} sha256={digest} dll_sha256={hashlib.sha256(dll.read_bytes()).hexdigest()}')


if __name__ == '__main__':
    main()
