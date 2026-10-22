# G3 Together

Play Gothic 3 together: one player hosts the campaign, the other joins over the network, and you play the story side by side.
Tested with Steam, Community Patch 1.75.14 and Parallel Universe Patch 1.1.1, built on the unofficial
[Gothic 3 SDK](https://github.com/georgeto/gothic3sdk). GPL-3.0-or-later (see COPYING).
Bundles [ENet](https://github.com/lsalzman/enet) 1.3.18 (MIT, thirdparty/enet/LICENSE).

**Download:** the newest package is under [Releases](https://github.com/GGM1k3y23/g3-together/releases). Both players need
exactly the same version.

> **Alpha.** Tested with bots that drive two real game clients on one PC and with one evening of human play. Nobody has
> finished the story in co-op yet and play over the internet is barely tested. Expect bugs and back up your saves.

## What works

- Playing the campaign together: you see each other move, fight, cast spells and use the world.
- Fighting together: hits, kills and deaths count on both PCs; experience goes to whoever earned it, quest rewards to everyone.
- Quests and dialogs are shared; a player standing nearby hears the other player's conversation.
- Looting corpses and chests, Quick Loot and trading: both players see the same contents, nothing is duplicated.
- Instead of game over, a knocked-out player waits for the partner to help them up with a healing potion.
- Duels and the arena work as in single-player.
- Saving and loading: the host saves; when the host loads a save, the other player is loaded into it automatically.
- The host checks that both installations match (patches, difficulty, balancing) and says what differs.

## Known limitations

- A guard of the host does not yet rob a knocked-out partner.
- Only two players have been tested; more may work but are untested.

Found a bug? Note the time and what happened, then send both players' logs (`tools/collect_logs.py` in the package).

## Player guides

- **English:** [Install and play](docs/PLAYING.md) · [Join a host](docs/JOINING.md) · [Playtest checklist](docs/PLAYTEST.md)
- **Deutsch:** [Installieren und spielen](docs/SPIELEN.md) · [Beim Host beitreten](docs/MITSPIELER.md) · [Spieltest-Checkliste](docs/TESTPLAN.md)

Use the same tested setup on both PCs: Gothic 3 from Steam, Community Patch **1.75.14**, then
[Parallel Universe Patch **1.1.1**](https://www.worldofgothic.de/dl/download_678.htm), then the same G3 Together build.
This is the tested baseline; compatibility without Parallel Universe Patch has not been established.
Mod messages use English by default. Set `language = de` in `g3together.ini` for German, or `language = en`
for English. Each player can choose independently; this does not change the game's own language.

## For developers

How the mod is built up and tested: [development stages](docs/STAGES.md). The test bot is compiled in and only active
in disposable copies that contain `qa-enable.txt`.

### Layout

```
dev/
  g3-together/     this repository: src/, thirdparty/enet/, qa/ (bot suites)
  g3-workspace/    private, never committed: test copies of the game, isolation receipts,
                   Gothic 3 SDK checkout, builds, run logs, reverse-engineering output
  coop-anything/   the playbook clone; findings go back there as pull requests
```

### Build

The SDK is not part of this repository. By default CMake looks for it in
`../g3-workspace/tools/gothic3sdk`; override with `-DG3SDK_DIR=<path>`. MSVC, 32-bit:

```
cmake -S . -B <build dir> -G "Visual Studio 18 2026" -A Win32
cmake --build <build dir> --config Release
```

In the workspace, `build-mod.ps1` does both.

### Bot suites

`qa/together.py --suite stage1|stage2|stage3a|stage3b [--negative-control]` launches the isolated test
copies from the workspace (`../g3-workspace`, or the `G3_WORKSPACE` environment variable), drives them
through the mod's QA command file and prints `RESULT: PASS/FAIL`. It refuses to run unless the workspace's
isolation checks pass, and it hashes the owner's real save folder before and after every run.
