# Playing G3 Together

**English** · [Deutsch](SPIELEN.md)

G3 Together adds co-op to Gothic 3 for two players. More players are planned but have not been tested.
One player is the **host**: their world is the shared campaign, and their save is everyone's campaign save.
The other player joins them.

This is a test version. See the known limitations below.

## Requirements (the same on both PCs)

- Gothic 3 from Steam with **Community Patch 1.75.14**.
- **Parallel Universe Patch 1.1.1**. Steam, CP 1.75.14 and PUP 1.1.1 are the tested baseline;
  compatibility without PUP has not been established.
- The same G3 Together version (the same `Script_G3Together.dll`) and the same game files.
- Matching difficulty, alternative balancing, alternative AI, XP factor, minimum hit duration, Quick Loot,
  and the entire `[Game]` section of `Ini\ge3.ini`. The mod checks these when a player joins and refuses
  mismatched installations. The joining player's `<Gothic 3>\g3together.log` records the reason on a line
  containing `REFUSED by host`. Loose files in Data subfolders are not checked.
- No other co-op mods in `scripts`, such as Myrtana Accord's `Script_G3Coop.dll`.
- Prefer a separate **copy** of the Gothic 3 installation for co-op.

## Installation

1. Install Gothic 3 from Steam, then **Community Patch 1.75.14**.
2. Install [Parallel Universe Patch **1.1.1**](https://www.worldofgothic.de/dl/download_678.htm)
   into the same game installation. Both players must use the same patch versions.
3. Use the same G3 Together package on both PCs. Copy `Script_G3Together.dll` into `<Gothic 3>\scripts\`.
4. Copy the appropriate `g3together.ini.host-example` or `g3together.ini.client-example` from the package
   into `<Gothic 3>\`, rename it to `g3together.ini`, and edit it:

   Host:

   ```ini
   role = host
   port = 34567
   name = YourName
   ```

   Joining player:

   ```ini
   role = client
   address = <host IP address>
   port = 34567
   name = YourName
   ```

   The joining player must use `role = client`. If the INI is missing or the role has another value,
   the game starts as a host.

### Mod message language

English is the default (`language = en`). To use German messages, add `language = de` to your own
`g3together.ini` and restart the game. Each player may choose independently. This setting does not
change Gothic 3's own language. The message examples in this guide assume English.

## Connecting over the internet

The mod connects directly over UDP port 34567. A private virtual network, such as **Tailscale** or
**ZeroTier**, allows the players to connect without forwarding a router port. Install the chosen
service on both PCs and use the host's virtual-network address as `address`.
Alternatively, the host can forward UDP port 34567 on their router to their PC.

## Starting and saving

1. The host starts Gothic 3 and begins a new game or loads the co-op campaign.
2. The other player starts Gothic 3.
   - If the host has loaded or saved a campaign, stay in the main menu. The campaign is transferred
     and loaded automatically when you join.
   - If the host has started a new game, start a new game as well.
3. The **host** saves the campaign. Each joining player automatically keeps their own hero record
   for that save: level, experience, attributes, inventory and so on. It is restored when they rejoin.
   On the very first join from the main menu, a player uses a copy of the host's hero. To keep your
   own new hero, start a new game before joining.

## What is shared

- Players see each other, including movement, combat animations and falls.
- Everyone sees the same NPCs, fights and deaths. Each region is simulated by one player: the host
  near the host, or the first player to arrive in a distant region. Another player entering that
  region sees the same simulation.
- The PC simulating an NPC decides damage to it. The player who kills it earns the experience.
  Kills by a player's companions or summons reward that player; kills by other NPCs reward nobody.
- Companions follow the player who recruited them, even far from the host. Dismissing them returns
  their ownership to the host.
- Quest state and previously completed dialogs are shared. Quest rewards go to everyone. Kill
  quests also count enemies killed in the joining player's region.
- Items, chests, corpses and trading are shared: what one player takes is gone for everyone.
- Everyone uses the same time of day. Sleeping advances time for all players.
- Helping up a downed player costs one healing potion. A player who would die stays down while a
  connected partner in the same world, who is not downed, can help. Stand within 2.5 m for three
  seconds to help them up. The cheapest healing potion in the helper's inventory is consumed.
  Without a potion, both players see a notice and the downed player waits for their partner to
  return with one.
- If nobody can help because everyone is down, or the partner is disconnected, in another world
  or out of the game for more than five seconds, the hero dies as in single-player. Load a save;
  the joining player then rejoins.
- Dead NPCs remain dead, including those killed far from the host.
- Opening a door or picking a lock opens it for everyone, including players who join later.
- NPCs respond to attacks from either player as in single-player: they fight, flee or call for help.
- Players see each other's arrows, bolts and fireballs in flight; each hit counts once. Healing
  spells also work on the other player.

## Reporting a bug

- Record the time, who was hosting, what you did and what each player saw.
- Both players should run `tools\collect_logs.py` from the extracted package folder and send the
  resulting ZIP. Commands are included in the [playtest checklist](PLAYTEST.md).
- For reproducible NPC or enemy problems, such as a dead orc standing upright on the other PC,
  add `trace = on` to the INI and restart. The game writes `g3trace.log` in its installation folder:
  NPC animations, tasks and the game code that triggered them. The file is limited to about
  50,000 lines and is recreated on each start. The log collector includes it. Remove the setting
  after collecting the report.

## Known limitations

- Beyond about 35 m from the host, the joining player sees "Far from host": that region is now
  simulated on their PC. Below about 25 m, the message is "Back near the host."
- **Jump to your partner:** **Page Down** puts you 1.5 m behind the other player in the same world,
  including a downed partner you want to help. There is a five-second cooldown. Staying together
  is preferable, but you can separate and regroup. To change the key, use `partner_key = F9` in
  the INI (F1–F24, PAGEUP, HOME, END, INSERT, DELETE or a letter).
- Dialog changes to NPCs, such as trader, teacher, guard and routine settings, apply to everyone,
  as do dialog-triggered duels, attacks and companion recruitment. Dialog items go to the player
  speaking.
- The host's save records which player owns each companion. After loading, a companion returns to
  its player when that player rejoins, identified by their INI name. Do not change that name.
  After a disconnect without loading, companions initially belong to the host.
- If the host loads a different save during a session, the other player must disconnect and rejoin.
- Trading and looting have been tested with bots, not through the real trading interface.
- Sleep advances the clock for everyone, but only heals the player who sleeps.
- Transformations: the other player sees the creature moving, then sees the hero again after the
  transformation ends. Wolf and Bloodfly were tested using the same setup as the game's spell.
  Combat in creature form, including hits to and from it, and saving while transformed remain
  untested and may have bugs.
- Enemies attack both players. Their attitude toward a joining player comes from the hero on the
  PC currently simulating the NPC. For example, the host's poor standing with orcs also applies
  to the other player for NPCs simulated by the host.
- Theft and reputation in towns and camps (who steals, who sees it, and what guards say) have
  not been adapted or tested for two players.
- Untested: monsters continuing to attack a downed hero, robbing a downed hero, real internet
  play, and more than two players. The features above were checked with bots. A first short human
  test with two windows on one PC found two bugs: enemies ignored the joining player, and combat
  was not visible on that player's PC. Both were fixed and checked with bots, but have not yet
  been checked again by human players.
