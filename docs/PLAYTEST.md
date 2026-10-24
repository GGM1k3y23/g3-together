# Checklist: first human co-op playtest

**English** · [Deutsch](TESTPLAN.md)

## Purpose

A first session for two players, one host and one joining player: joining, combat, companions,
knock-outs, dialogs, quests, trading, looting, separate regions, saving, loading and rejoining.
This checklist also covers recording bugs and collecting logs with `tools\collect_logs.py`.

## Before you start

### Players and installations

- [ ] Assign exactly two roles: host and joining player. The host starts or loads the shared campaign and saves it.
- [ ] Both PCs use the tested baseline: Gothic 3 from Steam, then **Community Patch 1.75.14**, then
  [Parallel Universe Patch **1.1.1**](https://www.worldofgothic.de/dl/download_678.htm), then the same G3 Together
  build and protocol version. Compatibility without PUP has not been established. Remove other co-op mods
  from `scripts`; preferably use a separate installation copy for co-op. See [installation instructions](PLAYING.md).
- [ ] Back up both players' Gothic 3 save folders. Use a clearly named test save during the session.
- [ ] Choose a LAN or private VPN connection. Share the host's address and firewall details privately with the other
  player. The host needs incoming UDP on port 34567. Human play over the real internet remains unverified.
- [ ] Both players record their role and local time zone for later bug reports.

### INI and compatibility

Configuration file: `<Gothic 3 folder>\g3together.ini`.

Host:

```ini
role = host
port = 34567
name = Host
language = en
```

Joining player:

```ini
role = client
address = <host LAN or VPN address>
port = 34567
name = Player2
language = en
```

`puppet_template` is optional and defaults to `PC_Hero`. `look = <character template>` (for example
`look = Diego`) is optional too: it is how you appear to the other player. `offer_accept_key` and
`offer_decline_key` (default Insert and Delete) answer a trade offer. A missing INI starts the mod in host mode,
so check that the joining PC has `role = client` before starting.

English is the default for mod messages, as quoted below. Set `language = de` for German. Players
can choose independently; the setting changes only mod messages.

- [ ] Both players have identical mod DLLs and installation files. The host checks Game.dll, Script.dll, Engine.dll,
  DLLs in `scripts`, rules archives, names and sizes of other Data archives, game options and the entire `[Game]`
  section of `Ini\ge3.ini`. Loose files in Data subfolders and custom difficulty sliders are not covered.
- [ ] Compare difficulty, alternative balancing, alternative AI, minimum hit duration, XP factor and Quick Loot.
  If joining is refused, read the reason in `g3together.log`.
- [ ] If the host begins a new game, the other player does too. If the host has loaded or saved, the other player
  joins from the main menu and waits for the campaign transfer.

## A 90-minute session

| Time | Stage | Steps and observations |
|---|---|---|
| 0–8 min | Start and join | The host loads or starts the test campaign and exactly one other player joins. Check that both see each other, names match, movement is visible and no compatibility rejection occurs. Each player changes armor or helmet once: the other sees the change within a few seconds. When a player starts walking, their legs should move at once on the other screen instead of gliding. Open the map: the partner is marked there with their name; on the compass their mark points their way. |
| 8–18 min | Shared combat | Fight a small encounter near the host. Both attack the same NPC. Check that hits and deaths appear on both PCs and the actual killer earns the experience. Record quest state and counters; one success does not verify every kill-quest case. |
| 18–28 min | Recruit and dismiss a companion | The joining player recruits a suitable NPC. Check that it follows that player, appears on both PCs and is not simulated twice. The same player then dismisses it; both check that it no longer acts as their companion. |
| 28–38 min | Knock-out and help | In a safe setting, let one player be knocked down in a controlled way. Their partner first stands next to them for a few seconds: nothing should happen. Then the partner talks to them and chooses "I will help you.". Check the recovery, notice and consumption of one healing potion. Without a potion, both should receive a notice and the player should remain down. Optionally at the end, before reloading: with both players down, both die and reach game over. |
| 38–50 min | Dialog, quest, trade and loot | Speak to an NPC, trigger a known quest step, perform a clearly identifiable trade and take an item from a chest or corpse. Compare quest and dialog state, gold and item counts. Then trade with each other using the mouse: one player talks to the other's character and chooses "Show me your wares!" (on the very first trade, click Ok on the tip), takes one item, gives one and closes the window; the other accepts with Insert. Compare items and gold on both PCs. Trade once more and decline with Delete: nothing should change hands. Bots could not test the trade window's mouse controls. |
| 50–70 min | Distance, separate regions and regrouping | Move apart. Beyond about 35 m, check for "Far from host" and that the host is told who is away. While separated, each player fights a different NPC in their region; record whether results agree and whether anything appears to be simulated twice. Regroup within about 25 m and check for "Back near the host." and matching world state. Accept a kill quest and kill a target in the joining player's region: the counter in the host's quest log should increase. |
| 70–82 min | Save and load | The host saves with a distinct name. Check that the other player keeps their hero for this save. Use a separate test save or backup for loading. Let the world and players settle, then compare their state. If the joining player had a companion, it should follow them again after loading and rejoining. |
| 82–90 min | Rejoin and bug report | If time remains, test a clean disconnect and rejoin, in the same process or after restarting. Record campaign, hero record, NPC ownership and companion state. If something failed, collect both players' logs as described below and give them privately to the host. |

With only 60 minutes, prioritize joining, combat, helping up, recruiting and dismissing a companion,
and saving on the host. Test separate regions and rejoining in a follow-up session.

## Recording a bug

Each player records a separate entry for each problem:

- local time, time zone and role;
- world or location and approximate distance between players;
- the checklist step and exact actions immediately before the problem;
- expected and observed behavior, affected PCs and whether it reproduced;
- whether a dialog, fight, knock-out, save/load or join/leave was in progress;
- a screenshot or short video if it shows the problem.

Then each player can collect their logs with the included tool:

1. Open PowerShell in the extracted package folder. Python is required.
2. Run the script with the Gothic 3 installation folder:

   ```powershell
   python "tools\collect_logs.py" "<Gothic 3 folder>"
   ```

   If Windows Documents is in another location, pass it as the second argument:

   ```powershell
   python "tools\collect_logs.py" "<Gothic 3 folder>" "<Documents folder>"
   ```

3. The script prints the new ZIP's name. It is created in the package folder, usually as
   `g3together-logs-<timestamp>.zip`.

The tool collects `g3together.log`, `g3together.ini` and, if present, `g3trace.log` from the game folder.
It also collects available engine logs from `%USERPROFILE%\Documents\gothic3\`:

- `paru\g3debug.log` (Parallel Universe logging, if present);
- `ge3log*.log`;
- `Lastlog_GE3.log`.

The Documents folder defaults to `%USERPROFILE%\Documents`; another path can be passed as the second
argument. The collector removes Windows user paths, IP addresses and the INI's `address` line.
Review the ZIP before sharing it. Give logs and screenshots privately to the host first. Do not
share unredacted INI files, saves, campaign sidecars or a complete Documents folder.

## Known limitations

- The features were checked with bots. A first short human test with two windows on one PC found
  two bugs, both fixed and checked with bots. Real internet play remains untested.
- Only two players were tested.
- Companions are restored after loading by player name. Changing your name loses that association.
- If the host loads another save during a session, the other player must disconnect and rejoin.
- Untested: monsters attacking downed heroes, the actual bed dialog, and the
  mouse controls of the trade window (dragging items, its buttons). Bots trade through the game's own trade functions.
