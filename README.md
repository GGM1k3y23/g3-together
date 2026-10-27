# G3 Together

**Return to Gothic 3 with a friend.** Share the campaign, fight side by side, and help each other back up when a battle goes wrong. One player hosts the world; the other joins over the network.

[Download the beta](https://github.com/GGM1k3y23/g3-together/releases) · [Quickstart](#quickstart) · [Player guides](#player-guides) · [Build and test](#for-developers)

> **Beta — back up your saves.** Tested mainly with bots driving two real game clients on one PC, plus one evening of human play. Bots reached Innos' ending; no human co-op playthrough has been completed yet. Internet play remains unverified; only two players have been tested.

## What you can do together

- **Share the adventure.** Quest progress, dialogs and story milestones sync. Listen to nearby conversations and see your partner's dialog choices.
- **Fight as a team.** Shared enemies, visible attacks and spells, individual kill XP. Quest XP is shared; dialog gold goes to the player handing in.
- **Loot once, trade together.** Chests and corpses share loot; picked pockets stay picked. Player trades require your partner's confirmation.
- **Look out for each other.** Find your partner on the map and compass, jump to their side, and revive them with a healing potion.
- **Keep your character and campaign.** Show your armor and helmet. The host saves; each player's hero record returns when loading the shared campaign.

More: [combat, companions, guards and world interactions](docs/PLAYING.md#what-is-shared).

## Quickstart

### 1. Match your installations

On **both Windows PCs**, use the tested setup in this order:

1. Gothic 3 from Steam with **Community Patch 1.75.14**.
2. [**Parallel Universe Patch 1.1.1**](https://www.worldofgothic.de/dl/download_678.htm).
3. The **same G3 Together release package**.

Game files and settings must match; the mod checks compatibility on joining. See the [full requirements](docs/PLAYING.md#requirements-the-same-on-both-pcs). Compatibility without Parallel Universe Patch is unverified.

Prefer a separate game copy for co-op. Keep other co-op mods out of `scripts`.

### 2. Run setup on both PCs

Unpack the **whole mod package** from [Releases](https://github.com/GGM1k3y23/g3-together/releases) and run `Setup.cmd` (Deutsch: `Einrichten.cmd`; Polski: `Instalacja.cmd`). GitHub's source-code ZIP does not include the built mod.

Choose one **host** and one **joining player**, enter names and the host's address. Setup finds the game, checks patches and installs the mod with `g3together.ini`. Replaced files go into `G3Together-backup`. See also [manual installation](docs/PLAYING.md#installation).

### 3. Connect and play

The default is **UDP port 34567**. Allow Gothic 3 through the host's firewall and use a reachable host address. See the [network guide](docs/PLAYING.md#connecting-over-the-internet) for VPN and port-forwarding options.

Experimental: choose **Steam** in the setup (`transport = steam`) and the joining player's game finds the host among its Steam friends, with no address or port forwarding. See [over Steam](docs/PLAYING.md#over-steam-experimental).

| Host starts first… | Joining player… |
| --- | --- |
| Load a saved campaign. | **Wait in the main menu**; the campaign transfers and loads automatically. |
| Start a new, unsaved campaign. | **Start a new game too.** Once the host has saved, future joins can load from the main menu. |

The **host saves**. Keep player names unchanged to restore companions. Read about [first-join heroes and saves](docs/PLAYING.md#starting-and-saving).

## A few things to try

| Want to… | How |
| --- | --- |
| Regroup | **Page Down** jumps behind your partner in the same world; five-second cooldown. |
| Revive your partner | Talk to them: **“I will help you.”** Costs one healing potion. If nobody can help, normal death still applies. |
| Trade | Talk to your partner: **“Show me your wares!”** Close the trade window; they accept with **Insert** or decline with **Delete**. |

Edit your `g3together.ini` and restart to customize messages, appearance and the regroup key:

```ini
language = auto
look = Diego
partner_key = F9
```

`auto` follows the game's text language: German, Polish, otherwise English. Override with `en`, `de` or `pl`; omitting the setting gives English. Each player chooses independently, without changing the game's language.

The host chooses `reputation = shared` (default), `together` (shared guilt) or `personal`; see [reputation rules](docs/PLAYING.md#known-limitations).

## Know the limits

- **Crashes and sync issues remain possible.** Save before difficult fights; see [known issues and workarounds](docs/PLAYING.md#known-issues-21-october-2026).
- **Human testing is limited.** Setup was checked on test folders; trading through the game's functions was checked by bots. Mouse trading and forward swimming still need human testing.
- **Untested story paths:** Adanos and Beliar endings, Xardas' plan, the fifth temple, Cape Dun's uprising, midgame town liberation, drive-away quests and plundering.
- **Soul travel:** your partner sees your ghost, not the body left behind.
- **Fire/ice effects that extinguish burning or freezing** on NPCs simulated by your partner only take effect on your PC.

Read the [full limitations](docs/PLAYING.md#known-limitations), including saving while transformed, before a longer campaign.

## Player guides

| Language | Install and play | Join a host | Help test |
| --- | --- | --- | --- |
| English | [Player guide](docs/PLAYING.md) | [Joining guide](docs/JOINING.md) | [Playtest checklist](docs/PLAYTEST.md) |
| Deutsch | [Installieren und spielen](docs/SPIELEN.md) | [Beim Host beitreten](docs/MITSPIELER.md) | [Spieltest-Checkliste](docs/TESTPLAN.md) |
| Polski | [Instalacja i gra](docs/GRANIE.md) | [Dołączanie do hosta](docs/DOLACZANIE.md) | — |

### Found a bug?

[Report a bug](https://github.com/GGM1k3y23/g3-together/issues) with the time, host, actions and what **each player** saw. Preserve both players' logs before restarting. With Python installed, run from the extracted package folder using your game path:

```powershell
python "tools\collect_logs.py" "C:\Games\Gothic 3"
```

Share both players' ZIPs privately with the host first. User paths and IP addresses are redacted; review before sharing further. Do not attach saves or unredacted INIs. [Without Python](docs/JOINING.md#if-something-goes-wrong), collect logs manually.

## For developers

Built on the unofficial [Gothic 3 SDK](https://github.com/georgeto/gothic3sdk) for the Genome engine. [Development stages](docs/STAGES.md) covers implementation and testing, following the [coop-anything](https://github.com/cranebeaker/coop-anything) playbook.

### Build

Use **MSVC, 32-bit**. This example needs Visual Studio 2026 C++ tools and [CMake 4.2+](https://cmake.org/cmake/help/latest/generator/Visual%20Studio%2018%202026.html) for its generator (project minimum: 3.24).

The SDK is a separate checkout. Its default path is `../g3-workspace/tools/gothic3sdk`; add `-DG3SDK_DIR="C:/path/to/gothic3sdk"` to the first command to override it. From the repository root:

```powershell
cmake -S . -B build -G "Visual Studio 18 2026" -A Win32
cmake --build build --config Release
```

### Test workspace

```text
dev/
  g3-together/     Public source: src/, thirdparty/enet/, qa/, docs/, tools/
  g3-workspace/   Private SDK, builds, isolated game copies, receipts, logs and research
  coop-anything/  Development playbook; findings go back as pull requests
```

Keep the workspace private and uncommitted. Its `build-mod.ps1` helper is not included here.

Bot suites need a **configured private workspace** with `project.json`, deployment receipts and isolation checks; a fresh clone is insufficient. Default: `../g3-workspace`; override with `G3_WORKSPACE`.

```powershell
python qa/together.py --suite stage2
python qa/together.py --suite stage2 --negative-control
```

These `stage2` runs drive two isolated game copies; the negative control is expected to report `RESULT: FAIL`. The runner checks isolation and hashes the owner's real saves before and after each run. Other suites include `stage1`, `stage3a` and `stage3b`; see [development stages](docs/STAGES.md).

The compiled QA bot activates only with `qa-enable.txt`. Use it only in disposable test installations.

## License and credits

**GPL-3.0-or-later** — see [COPYING](COPYING). Built on the unofficial [Gothic 3 SDK](https://github.com/georgeto/gothic3sdk); bundles [ENet 1.3.18](https://github.com/lsalzman/enet) under its [MIT license](thirdparty/enet/LICENSE).
