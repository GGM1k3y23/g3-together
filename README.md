# G3 Together

Co-op for Gothic 3 (Steam, Community Patch 1.75.14), built on the unofficial
[Gothic 3 SDK](https://github.com/georgeto/gothic3sdk). GPL-3.0-or-later (see COPYING).
Bundles [ENet](https://github.com/lsalzman/enet) 1.3.18 (MIT, thirdparty/enet/LICENSE).

## Status

Follows the build order of the [coop-anything](https://github.com/cranebeaker/coop-anything) playbook.
Each stage counts as done only after its bot suite passed three times with two real game clients,
with a negative control that fails.

| Stage | What | State |
|---|---|---|
| 1 | Script plugin registers an engine module, reports the hero every frame, test bot, clean shutdown | passing |
| 2 | ENet direct-IP transport, remote players as puppets with walk/run motions | passing |
| 3a | NPCs owned by the host, mirrored on clients (position, hit points, death), client AI off | passing |
| 3b | Damage with host authority: client hits on host NPCs go through the host, host hits on puppets reach the player's hero, deaths mirrored | passing (bot hits via OnDamage; real swings not bot-driven yet) |
| 3c | Experience goes to whoever earned it: the killer is rewarded on its own machine, mirrored deaths pay nobody else; quest rewards are shared | passing |
| A | No game over while a partner can help: a hero the game would let die is knocked out instead; hits on a client's hero are judged on that client (armour, stumble, knock-out), and everyone sees the fall | passing (monsters attacking a downed hero, robbery and partner revive not checked) |
| A2 | A knocked-out player waits for a partner: stays down while someone could help; a partner next to it for 3 s pays one healing potion (the cheapest It_Potion_Health*) and stands it up; without a potion both are told and it stays down; when nobody can help for 5 s (everybody down, alone) it dies as in the single-player game; on-screen messages | passing (the only healing potion template found is It_Potion_Health) |
| B | Distance notice: a client more than 35 m from the host is told that the area now runs on its machine (stage E), the host is told who is far away, both get an all-clear back under 25 m | passing (screenshots checked) |
| C | World journal for deaths: deaths in a player's own region reach the host and the other players and are applied once they process that NPC | passing (deaths only; items, containers and persistence with the save come later) |
| D1 | Saves: every save (menu or QA) removes puppets first, so it loads; the world journal goes into a sidecar <save>.g3t and comes back with that save | passing (menu save path assumed to use the same function) |
| D2 | Each player's own hero as a record: level, experience, learn points, attributes, inventory with quality and equipment, learned skills | passing (position, quick slots, effects, fame not yet) |
| D3 | Campaigns: when the host saves, every client keeps its own hero for that save; a client that joins gets the host's save over the network, loads it by itself and gets its own hero back | passing |
| G | One time of day: the host's clock goes to every client; a sleep on any machine moves time on for everyone (also when a clock update arrives in the same frame as the sleep) | passing (real bed dialog not bot-driven) |
| H | Dialog effects: story events a dialog sets on one hero are shared; after a dialog command that changes an NPC (trade/teach flags, guard status, routine, class, alignment, position, enclave raid/revolution) or a sector, the resulting state reaches every machine | passing (Attack, Spawn, inventory and party commands not yet) |
| H3 | Fights started by dialogs: a client's duel or "attack" dialog with a host NPC makes that NPC attack the client on the host (AssessTarget) | passing |
| E | Regional ownership: NPCs a client simulates far from the host are sent to everyone; the host mirrors them when it arrives (no double simulation) and takes them back when the client leaves; hits go to the NPC's owner both ways, also NPC against NPC where two regions meet (E4); a companion a client recruits is taken over by that client and follows it everywhere, goes back to the host when dismissed, when its player disconnects or goes silent for 15 s, and the first of two players to recruit it keeps it | passing (tested with 2 players only) |
| E5 | Companions across the host's save: the host's save sidecar records whose companion each NPC is (by player name); after a load the companion is handed back when that player joins, and the client recruits it again with the game's own JoinPlayerSlavery script | passing |
| Q | Kill quests across regions: a kill in a client's region (the death script runs only there) counts once in the host's quest counters, which the host keeps and shares | passing |
| V1 | Enemies fight remote players by themselves (first human test: they ignored them): NPCs judge another player's puppet as the local hero (the game's GetAttitude is wrapped); their hits reach that player | passing (attitude toward the puppet is the local hero's: its crimes and fame) |
| V2 | Fights look the same for everyone: mirrored NPCs hold the owner's weapons and stance, and their attacks, parades and staggers replay as the game's own combat moves (so they run to their end instead of being replaced by the stance's idle loop or freezing) | passing (client mirrors without a running motion 1-4 %; 37-55 % of the host's sampled attacks seen running on the client) |
| V3 | Another player's puppet holds that player's weapons | passing |
| V4 | Players see each other's swings: a puppet replays its player's attacks as combat moves | passing (14-15 of 15 bot swings seen running on the other machine; bot swings set the melee state as a key press does) |
| T | Transformed players (spells and potions that turn a player into a creature): the creature, which the game makes the player (Entity::GetPlayer) while gCSession::GetHero stays the hidden human body, is what travels; the other machine shows a puppet of the creature's template with its motions and walking, and the hero again once turned back; creature idles restart on the puppet | passing (Wolf, Bloodfly; transformation set up like MagicTransform, walking by AI goto; fights and saves while transformed not checked) |
| V5 | An NPC fleeing from another player's puppet keeps judging it as Panic (the GetAttitude wrapper leaves that case alone) and does not turn on it | passing (flee set by the bot as AssessTarget does on Panic; turning around itself was not seen within 6 s even without the fix) |
| L | Soft leash: a key (INI partner_key, default Page Down) puts the hero 1.5 m behind the other player (also when that player is down), 5 s cooldown; no hard distance limit | passing (key press itself not bot-driven; QA command uses the same function) |
| P3a | Kills by a player's companions and summons across machines pay that player (party flag in the NPC hit) | passing |
| 4 | Shared quest state with host authority: late joiners get the host's story, changes travel both ways through the game's own quest functions (journal, XP) | passing (quest status only; dialog and world state not yet) |
| 4b | Shared dialog state: which dialog options were already told (InfoGiven), same mechanism as quests | passing |
| 4c | A client's hero holds dialogs with host-owned NPCs: the host holds its NPC meanwhile, what was told reaches the host | passing (dialog started through AssessTalk by the bot; teaching and trading not checked yet) |
| 5 | World items with host authority: what one player takes is gone for everyone, the first claim wins, a refused take is given back | passing (taking from the ground; NPC pickups, drops, chests, corpses and trade not yet) |
| 5b | Plunder with host authority: chests and corpses show the host's contents, takes travel as transactions, a refused take is undone | passing |
| 5c | Trade with host authority: the client sees the host's goods, a purchase moves item and gold on the host in one transaction, a refused purchase is undone | passing (bot trades through PlayerOpenTrade and inventory transfers, not the trade screen) |
| P | Install parity: the host refuses a client whose game DLLs, script DLLs, rules archives or balancing options differ, and says which | passing (loose files in Data subfolders and custom difficulty sliders not covered) |

The test bot is compiled in and only active in disposable copies that contain `qa-enable.txt`.

## Layout

```
dev/
  g3-together/     this repository: src/, thirdparty/enet/, qa/ (bot suites)
  g3-workspace/    private, never committed: test copies of the game, isolation receipts,
                   Gothic 3 SDK checkout, builds, run logs, reverse-engineering output
  coop-anything/   the playbook clone; findings go back there as pull requests
```

## Build

The SDK is not part of this repository. By default CMake looks for it in
`../g3-workspace/tools/gothic3sdk`; override with `-DG3SDK_DIR=<path>`. MSVC, 32-bit:

```
cmake -S . -B <build dir> -G "Visual Studio 18 2026" -A Win32
cmake --build <build dir> --config Release
```

In the workspace, `build-mod.ps1` does both.

## Bot suites

`qa/together.py --suite stage1|stage2|stage3a|stage3b [--negative-control]` launches the isolated test
copies from the workspace (`../g3-workspace`, or the `G3_WORKSPACE` environment variable), drives them
through the mod's QA command file and prints `RESULT: PASS/FAIL`. It refuses to run unless the workspace's
isolation checks pass, and it hashes the owner's real save folder before and after every run.
