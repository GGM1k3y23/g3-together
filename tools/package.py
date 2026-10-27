"""Build a player package: the mod DLL, the setup (Setup.cmd, Einrichten.cmd, Instalacja.cmd, tools/setup.ps1), example configuration,
the player guides, the test plan, the log collector and the licences.

    python tools/package.py <built Script_G3Together.dll> <output folder> [<steam_api.dll>]

Writes <output folder>/G3Together-<git hash>.zip. Contains no game files. With a steam_api.dll (Valve's 32-bit one from the
Steamworks.NET Standalone release 2025.164.1, checked by its SHA-256) it goes in as G3Together/steam_api.dll for
transport = steam; without it the setup offers to download that file.
"""
import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STEAM_DLL_SHA256 = 'fdafb7c65ec7d4182b42bf92cc8671c68e4dc547cc6e613899d2618a47eb65da'

HOST_INI = """# G3 Together: this player hosts the campaign.
role = host
port = 34567
name = Host
# Connection: udp (by address: IP, Hamachi, ZeroTier, Tailscale) or steam (Steam friends find this game over Steam; needs
# G3Together/steam_api.dll, see PLAYING.md). Over Steam the UDP port stays open for players with an address.
transport = udp
# Mod messages only: auto (the language of your Gothic 3 texts), en, de or pl; without this line: en.
# Each player can choose independently.
language = auto
# Key that jumps to the other player (default PAGEDOWN): F1-F24, PAGEUP, HOME, END, INSERT, DELETE or a letter
partner_key = PAGEDOWN
# Keys that accept / decline the other player's trade offer (same names; the mod warns if two of these keys are the same)
offer_accept_key = INSERT
offer_decline_key = DELETE
# Your own look for the other players (a character template such as Diego; default: their puppet_template)
# look = Diego
# For a bug report about NPCs: remove the # below, restart, reproduce (writes g3trace.log, see PLAYING.md)
# trace = on
# Town reputation: shared (one record per town for both; default), together (shared guilt), personal (each their own)
# reputation = shared
"""
CLIENT_INI = """# G3 Together: this player joins the host.
role = client
# Connection: udp (by address) or steam (finds the host among your Steam friends; then the address is only the fallback
# for a PC without Steam). With several friends hosting: steam_host = <their Steam name>.
transport = udp
address = 127.0.0.1
port = 34567
name = Player
# Mod messages only: auto (the language of your Gothic 3 texts), en, de or pl; without this line: en.
# Each player can choose independently.
language = auto
partner_key = PAGEDOWN
offer_accept_key = INSERT
offer_decline_key = DELETE
# Your own look for the other players (a character template such as Diego; default: their puppet_template)
# look = Diego
# For a bug report about NPCs: remove the # below, restart, reproduce (writes g3trace.log, see PLAYING.md)
# trace = on
"""


def main():
    dll, out = Path(sys.argv[1]), Path(sys.argv[2])
    steam_dll = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    if steam_dll and hashlib.sha256(steam_dll.read_bytes()).hexdigest() != STEAM_DLL_SHA256:
        sys.exit(f'{steam_dll} is not the tested steam_api.dll (SHA-256 {STEAM_DLL_SHA256})')
    out.mkdir(parents=True, exist_ok=True)
    version = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True).stdout.strip() or 'dev'
    target = out / f'G3Together-{version}.zip'
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as z:
        z.write(dll, 'scripts/Script_G3Together.dll')
        z.writestr('g3together.ini.host-example', HOST_INI)
        z.writestr('g3together.ini.client-example', CLIENT_INI)
        # Explicit public guides only. Keep their relative language links valid in the ZIP.
        for guide in ('PLAYING.md', 'JOINING.md', 'PLAYTEST.md',
                      'SPIELEN.md', 'MITSPIELER.md', 'TESTPLAN.md',
                      'GRANIE.md', 'DOLACZANIE.md'):
            z.write(ROOT / 'docs' / guide, guide)
        z.write(ROOT / 'tools/collect_logs.py', 'tools/collect_logs.py')
        z.write(ROOT / 'tools/setup.ps1', 'tools/setup.ps1')
        for launcher in ('Setup.cmd', 'Einrichten.cmd', 'Instalacja.cmd'):
            z.write(ROOT / launcher, launcher)
        z.write(ROOT / 'COPYING', 'COPYING')
        z.write(ROOT / 'COPYING.STEAMWORKS', 'COPYING.STEAMWORKS')  # GPLv3 section 7: the Steamworks API library
        enet = ROOT / 'thirdparty/enet/LICENSE'
        if enet.exists():
            z.write(enet, 'licenses/ENet-LICENSE')
        z.write(ROOT / 'thirdparty/licenses/jericho-coop-LICENSE', 'licenses/JerichoCoop-LICENSE')  # steam.cpp is adapted from it
        if steam_dll:
            z.write(steam_dll, 'G3Together/steam_api.dll')
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f'{target} sha256={digest} dll_sha256={hashlib.sha256(dll.read_bytes()).hexdigest()}')


if __name__ == '__main__':
    main()
