# G3 Together

Play Gothic 3 together: one player hosts the campaign, the other joins over the network, and you play the story side by side.
Tested with Steam, Community Patch 1.75.14 and Parallel Universe Patch 1.1.1, built on the unofficial
[Gothic 3 SDK](https://github.com/georgeto/gothic3sdk). GPL-3.0-or-later (see COPYING).
Bundles [ENet](https://github.com/lsalzman/enet) 1.3.18 (MIT, thirdparty/enet/LICENSE).

**Download:** the newest package is under [Releases](https://github.com/GGM1k3y23/g3-together/releases). Both players need
exactly the same version. Unpack it and run `Setup.cmd` (German: `Einrichten.cmd`): it finds Gothic 3, checks the patches,
copies the mod and writes `g3together.ini`.

> **Alpha.** Tested with bots that drive two real game clients on one PC and with one evening of human play. Nobody has
> finished the story in co-op yet and play over the internet is barely tested. Expect bugs and back up your saves.

## What works

- Playing the campaign together: you see each other move, fight, cast spells and use the world. Armor and helmet show on
  the other player, and each player can choose their own look (`look = Diego` in their INI).
- Fighting together: hits, kills and deaths count on both PCs; experience goes to whoever earned it, quest rewards to everyone.
- Beating people as in single-player: a human or orc you beat is knocked out, a blow on them lying there kills them, and
  a murder counts for their town whichever PC simulates them.
- Quests and dialogs are shared; a player standing nearby hears the other player's conversation.
- Looting corpses and chests, Quick Loot and trading with NPCs: both players see the same contents, nothing is duplicated.
  A pocket one player picked is picked for both.
- Trading with your partner: talk to their character and choose "Show me your wares!". The game's own trade window opens
  with their goods and gold; your partner confirms every trade.
- Your partner on the map and the compass: an arrow with their name on the map, an arrow at the compass's edge.
- Instead of game over, a knocked-out player waits; the partner helps them up by talking to them and choosing
  "I will help you." (costs a healing potion).
- Knocked out by a guard: as in single-player, you stand up by yourself and the guard may rob you of half your gold;
  an NPC that sees a player steal goes after the thief, whichever PC simulates it.
- Town reputation as one record for both, with shared guilt, or each player's own (`reputation` in the host's INI).
- Overheard dialogs show their subtitles (with the game's subtitles on).
- Duels and the arena work as in single-player.
- Saving and loading: the host saves; when the host loads a save, the other player is loaded into it automatically.
- The host checks that both installations match (patches, difficulty, balancing) and says what differs; on-screen messages
  tell you when the connection is lost or back.

## Known limitations

- Trading was tested by bots through the game's own trade functions; dragging items with the mouse in the trade window has
  not been tried by a person yet.
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
