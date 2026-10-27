# Playing G3 Together

**English** · [Deutsch](SPIELEN.md) · [Polski](GRANIE.md)

G3 Together adds co-op to Gothic 3 for two players. More players are planned but have not been tested.
One player is the **host**: their world is the shared campaign, and their save is everyone's campaign save.
The other player joins them.

This is a test version. See the known issues and limitations below.

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

**Setup:** once Gothic 3 and both patches are installed, unpack the whole package anywhere and double-click
`Setup.cmd` (`Einrichten.cmd` for German, `Instalacja.cmd` for Polish). It does the copying and the INI for you: it finds Gothic 3 (Steam, GOG or the
folder you name), checks Community Patch 1.75.14 and Parallel Universe Patch 1.1.1, moves another co-op mod out of
`scripts`, copies the mod into `scripts` and writes `g3together.ini` from your answers (host or joining player, your name,
the host's address). Replaced files are kept in `G3Together-backup` in the game folder. The setup was checked on test
folders, not yet by players; the steps below do the same by hand.

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

`language = auto` (written by the setup and the example INIs) shows the mod's messages in the language of your
Gothic 3 texts: German for a German game, Polish for a Polish one, English otherwise. To fix a language, set
`language = en`, `language = de` or `language = pl` in your own `g3together.ini` and restart the game. Without a
`language` line the messages are English. Each player may choose independently. This setting does not change
Gothic 3's own language. The message examples in this guide assume English.

## Connecting over the internet

The mod connects directly over UDP port 34567. A private virtual network, such as **Tailscale** or
**ZeroTier**, allows the players to connect without forwarding a router port. Install the chosen
service on both PCs and use the host's virtual-network address as `address`.
Alternatively, the host can forward UDP port 34567 on their router to their PC.

### Using Hamachi

1. Both players install Hamachi and join the **same** Hamachi network.
2. Check the connection status shown for the other player. Prefer a direct connection; a relayed
   connection adds latency and may cause stuttering. Resolve connection errors before starting.
3. The host's Hamachi address appears at the top of their Hamachi window (`25.x.x.x`). The joining player
   puts that address in their INI: `address = 25.x.x.x`, `port = 34567`.
4. **Host firewall:** Windows often treats Hamachi as a public network. When Windows asks whether to
   allow Gothic 3 through the firewall, allow it on the network profile used by Hamachi, including
   **Public networks** if applicable.
5. The joining player can run `ping 25.x.x.x` in a command prompt, using the host's address, to check
   connectivity and latency. Lower latency is preferable; a ping reply alone does not verify that
   the game's UDP connection is allowed.
6. If the joining player's game cannot reach the host, the top line of their main menu says so after about 15 seconds,
   with the address and port they entered (in the game: a message every 30 seconds).

The host accepts connections through any network adapter, including Hamachi. Testing described by
these guides used two game copies on one PC; play over the real internet remains unverified.

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
  Kills by a player's companions or summons reward that player; kills by other NPCs reward nobody. As in single-player,
  your last hit on a human or orc knocks them out (experience, no murder); only a blow on them lying there kills them.
- Companions follow the player who recruited them, even far from the host. Dismissing them returns
  their ownership to the host.
- An NPC that guides you or that you escort (follow and escort quests) goes with the player who took the quest, also the
  joining player, and after the host loads a save that player leads again. When the escort ends far from the host, the
  NPC stands at the same place on both PCs.
- Quest state and previously completed dialogs are shared. Quest rewards go to everyone. Kill
  quests also count enemies killed in the joining player's region.
- The main story is shared to the end: when one player hands over the artefacts at a god's shrine, the chapter changes
  for both, and when one player starts the epilogue, the other sees the same pictures and the game's end at the same time.
  If the other player is in a dialog, a menu or a fight just then, their epilogue starts as soon as that is over (a message
  says so).
- A stool, bench, campfire or other place has one user: while your partner uses it, you are kept off ("<Name> is
  using this."), and NPCs do not take it either.
- Items, chests, corpses and trading are shared: what one player takes is gone for everyone. An NPC whose pocket one
  player picked cannot be picked again by the other.
- Everyone uses the same time of day. Sleeping advances time for all players, and so does waiting until morning: the one
  who waits jumps straight to 9 o'clock.
- Animals that come back after some days (scavengers, deer, wolves and the like) come back on both PCs or on neither.
- Helping up a downed player costs one healing potion. A player who would die stays down while a
  connected partner in the same world, who is not downed, can help. Talk to the downed player's
  character and choose "I will help you." to help them up. The cheapest healing potion in the
  helper's inventory is consumed.
  Without a potion, both players see a notice and the downed player waits for their partner to
  return with one.
- If nobody can help because everyone is down, or the partner is disconnected, in another world
  or out of the game for more than five seconds, the hero dies as in single-player. The host loads a
  save; the other player is loaded into it automatically.
- A knock-out of the game's own works as in single-player: when a guard or another person beats you in a fight (without
  killing you), you stand up by yourself after a while, nobody needs to help, and the winner may rob you of half your gold.
  When an NPC of the other player's game does it, you see "<Name> robbed you of N gold." Your partner cannot loot you.
- An NPC that sees a player steal reacts as in single-player, also when the other player's PC simulates it: it goes after
  the thief. Fights and murders by either player are seen the same way: onlookers and guards come in during a beating,
  and only the murderer is pursued. Guards warn and then attack a player who draws a weapon near them or walks into a
  place they guard. A murder by a player's companion is noticed as in single-player.
- Onlookers come over to either player's fight and see a knock-out, whoever did it. The opponent and the onlookers
  remember how a fight ended, and an NPC that is still angry at one player holds it against that player only.
- Blocking works as in single-player for both players: a block costs stamina and the shield talent counts. An NPC that
  parries blocks either player's blows the same way.
- A player within about 20 m hears the other player's dialog. With the game's subtitles on, the lines' text shows as
  well, in your own game language, unless you are in a dialog yourself. You also see the answers your partner can pick
  and then the one they picked; the one who talks chooses.
- An NPC talks to one player at a time: while your partner talks to it, it is busy for you ("<NPC> is talking to
  <player>."). It gestures and faces the speaker on both PCs.
- An NPC with important news for a player (a guard, a quest giver) walks up to that player and talks to them, as it
  does in single-player, also to the joining player.
- Dead NPCs remain dead, including those killed far from the host.
- Opening a door or picking a lock opens it for everyone, including players who join later.
- NPCs respond to attacks from either player as in single-player: they fight, flee or call for help.
- Players see each other's arrows, bolts and fireballs in flight; each hit counts once. Healing
  spells also work on the other player.
- NPC spells such as sleep, fear, charm, poison and disease apply on both PCs. Summoned creatures
  are visible to the other player and join the fight.

## Reporting a bug

- Record the time, who was hosting, what you did and what each player saw.
- Both players should run `tools\collect_logs.py` from the extracted package folder and send the
  resulting ZIP. Commands are included in the [playtest checklist](PLAYTEST.md).
- For reproducible NPC or enemy problems, such as a dead orc standing upright on the other PC,
  add `trace = on` to the INI and restart. The game writes `g3trace.log` in its installation folder:
  NPC animations, tasks and the game code that triggered them. The file is limited to about
  50,000 lines and is recreated on each start. The log collector includes it. Remove the setting
  after collecting the report.

## Known issues (21 October 2026)

| What can happen | What to do |
|---|---|
| The game may crash during combat or shortly after an enemy dies. The known orc-raid crash has a guard, but other crashes remain possible. | The host should save before difficult fights. Record the time and enemy, and preserve both players' logs before restarting. The host loads the last good save; the other player is loaded into it automatically (if their own game crashed, they restart it and wait in the main menu). |
| The other player's character briefly lags behind while being hit, or shows a movement inaccurately. | Regroup and wait briefly; use **Page Down** if needed. If enemies, health or positions remain clearly different, stop the fight and record the problem. |
| Enemies or companions behave differently on the two PCs after joining. | Check that both players see the same characters and movements near the host. If the difference persists, preserve the logs and restart together from the last good save. |
| "<Name> is using this." when you want to sit down or use a campfire, anvil or other place | Your partner uses it right now; wait until they get up. Only stools were tested by bots; if someone still gets stuck at such a place, record the bug and reload together. |
| Saving while transformed into a creature has not been checked. | Transform back before saving. |
| A controlled or tamed NPC does not follow reliably or leaves the party. | Do not rely on these abilities yet. Record the NPC and time; casting the spell again is not a verified workaround. |
| Stun and hypnosis cast by the joining player only take effect through the host. | Record failures; the host can cast the spell instead. |
| A summoned creature stops moving on the other PC when players move far apart. | Use summons while together. Record the location and creature if this occurs. |
| Poison or disease is not fully visible on the other PC, and a poison death may award no experience. | Record the discrepancy. |
| A guard who talks to you about crimes counts the town's whole record (with `shared` or `together`: your partner's deeds too). | Avoid theft and deliberate attacks on peaceful residents if that bothers you. Record who acted and whom the guard addressed or attacked. |

## Known limitations

- Beyond about 35 m from the host, the joining player sees "Far from host": that region is now
  simulated on their PC. Below about 25 m, the message is "Back near the host."
- **Jump to your partner:** **Page Down** puts you 1.5 m behind the other player in the same world,
  including a downed partner you want to help. There is a five-second cooldown. Staying together
  is preferable, but you can separate and regroup. To change the key, use `partner_key = F9` in
  the INI (F1–F24, PAGEUP, HOME, END, INSERT, DELETE or a letter; Insert and Delete are the trade keys below).
- **Emotes:** **Home** shows an emote at the top of the screen, each further press the next one: cheer, applaud,
  dance, cross arms, hands on hips, think, nod, point, threaten, pee. One second without a press and your hero plays it; your partner
  sees it on your character. Applauding, dancing and the like go on until you walk off. They are motions the game's NPCs
  already have. To change the key, use `emote_key = END` in the INI (the same names as for `partner_key`).
- **Map and compass:** your partner is marked on your map, with their name, and on the compass in their direction from
  where you look.
- **Your look:** the other player sees the armor and helmet you wear. To be seen as another character, set
  `look = Diego` (a character template of the game) in your own INI; without it, the other player's
  `puppet_template` decides (default: the hero).
- **Trading with your partner:** talk to the other player's character and choose "Show me your wares!". The game's trade
  window opens with what your partner can trade (not what they wear, no quest items) and their gold; prices are the
  same both ways; a sharpened, blessed or forged weapon stays so. On your very first trade a tip box comes first: click Ok. Swap as often as you like, then close the
  window: your partner gets one offer and accepts with Insert or declines with Delete within 30 seconds. Until then
  you have neither what you get nor what you gave; afterwards everything is where it belongs. Other keys:
  `offer_accept_key = F7` and `offer_decline_key = F8` in the INI (the same names as for `partner_key`). If two of
  these three keys are the same, one press does both: the mod says so on the screen at the start and in `g3together.log`.
- Dialog changes to NPCs, such as trader, teacher, guard and routine settings, apply to everyone,
  as do dialog-triggered duels, attacks and companion recruitment. Dialog items go to the player
  speaking.
- The host's save records which player owns each companion. After loading, a companion returns to
  its player when that player rejoins, identified by their INI name. Do not change that name.
  After a disconnect without loading, companions initially belong to the host.
- If the host loads a save during a session, the other players are loaded into it automatically. Until they
  are done, you do not see each other.
- Trading and looting have been tested with bots through the game's own trade functions; the trade window's mouse
  controls (dragging items, its buttons) have not been tried by a person yet.
- Sleep advances the clock for everyone, but only heals the player who sleeps.
- Transformations: the other player sees the creature moving, then sees the hero again after the
  transformation ends. Wolf and Bloodfly were tested using the same setup as the game's spell.
  In creature form you fight as yourself: your hits count as yours (knock-outs, kills, experience, murders), and an
  enemy's hits hurt the creature, not your hidden human body (Wolf tested by bots). Saving while transformed remains
  untested.
- A partner's hailstorm: its weather now stays at your partner on your PC (it used to darken the sky and strike around your
  own hero; checked by bots by position and screenshot). Damage comes only from the caster's own hailstorm, as in
  single-player.
- Enemies attack both players. Their attitude toward a joining player comes from the hero on the
  PC currently simulating the NPC. For example, the host's poor standing with orcs also applies
  to the other player for NPCs simulated by the host.
- A town or camp freed (or taken) by either player is freed for both: the town's state comes from the host.
- Town and camp reputation, chosen with `reputation` in the host's INI:
  - `shared` (default): one record per town for both players; thefts by either player count, and a guard who talks to
    you about crimes counts everybody's deeds. An NPC that saw a crime holds it against the one who did it.
  - `together`: as `shared`, and an NPC that saw one of you commit a crime holds it against both of you (both are
    refused).
  - `personal`: each player keeps their own record; what your partner does never changes yours.
  A murder counts for the victim's town as in single-player, also when the other player's PC simulates the victim
  (`personal`: in the murderer's own record). Beating a human or orc knocks them out first; only a blow on them lying
  there kills them.
- Soul travel: the other player sees your ghost moving, not the body you left behind.
- Ice and fire spells that put out burning or freezing (and make such projectiles fall apart) work on the other
  player's NPCs only on your own PC. You will rarely notice this.
- An NPC with news stops walking up to you when you go more than about 25 m away from it; talk to it yourself.
- Not checked yet, because the bots cannot set these scenes up: the uprising in Cape Dun, freeing a town in the middle of
  the game (bots checked Ardea in the opening), quests to drive someone away (their dialog fight) and plundering. They
  are on the playtest checklist.
- The main story was played by bots to Innos' ending (with the Nordmar fire chalices). The Adanos and Beliar endings,
  Xardas' plan and the fifth temple have not been played through together yet, and what happens to the connection after
  the game's end is not known yet.
- Swimming forward has not been tried by a person yet (the bots cannot swim forward).
- Untested: monsters continuing to attack a downed hero, real internet
  play, and more than two players. The features above were checked with bots. A first short human
  test with two windows on one PC found two bugs: enemies ignored the joining player, and combat
  was not visible on that player's PC. Both were fixed and checked with bots, but have not yet
  been checked again by human players.
