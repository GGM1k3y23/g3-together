# G3 Together: joining a host

**English** · [Deutsch](MITSPIELER.md)

## Before you start

- Both PCs should use the tested baseline: Gothic 3 from Steam, **Community Patch 1.75.14** and
  **Parallel Universe Patch 1.1.1**. Use the same G3 Together DLL and the same game files.
  Compatibility without PUP has not been established.
- Difficulty, alternative balancing, alternative AI, XP factor, minimum hit duration, Quick Loot
  and the `[Game]` section in `Ini\ge3.ini` must match the host.
- There must be no other co-op mod in the `scripts` folder.

## Installation

**Setup:** once Gothic 3 and both patches are installed, unpack the whole package anywhere and double-click
`Setup.cmd` (`Einrichten.cmd` for German). It does the copying and the INI for you: it finds Gothic 3 (Steam, GOG or the
folder you name), checks Community Patch 1.75.14 and Parallel Universe Patch 1.1.1, moves another co-op mod out of
`scripts`, copies the mod into `scripts` and writes `g3together.ini` from your answers (host or joining player, your name,
the host's address). Replaced files are kept in `G3Together-backup` in the game folder. The setup was checked on test
folders, not yet by players; the steps below do the same by hand.

1. Install Gothic 3 from Steam, then **Community Patch 1.75.14**, then
   [Parallel Universe Patch **1.1.1**](https://www.worldofgothic.de/dl/download_678.htm)
   into the same game installation.
2. Copy `Script_G3Together.dll` from the same package as the host into `<Gothic 3 folder>\scripts\`.
3. Copy `g3together.ini.client-example` from the package into `<Gothic 3 folder>\`, rename it to
   `g3together.ini`, and enter your details:

   ```ini
   role = client
   address = <host address>
   port = 34567
   name = <your name>
   language = en
   ```

Replace both placeholders. **With Hamachi**, use the host's Hamachi address (`25.x.x.x`, shown at the
top of their Hamachi window). Both players must be in the same Hamachi network; prefer a direct
connection. Your PC must use `role = client`. Keep your player name unchanged: the host uses it to
restore your companions after loading a save.

English is the default message language. Set `language = de` for German mod messages. Each player
can choose independently; this does not change Gothic 3's own language.

## Joining

The host starts Gothic 3 and begins a new campaign or loads the co-op campaign. Start your game
afterwards. If the host has loaded or saved a campaign, stay in the main menu and wait for the
campaign to transfer and load. If the host starts a completely new game, start a new game too.

If joining is refused, open `g3together.log` in the Gothic 3 folder and find the line containing
`REFUSED by host`. Compare versions and game options with the host. The reason also stays in the top line of the main menu.

If your game cannot reach the host at all, the top line of the main menu says so after about 15 seconds, with the address
and port from your INI (in the game: a message every 30 seconds). Check that the host's game is running, the address and port,
and the host's firewall. The mod keeps trying; once it reaches the host, the line is back to normal.

## While playing

- If you are downed, wait for your partner. A connected player in the same world can help you up by
  talking to you and choosing "I will help you.". This costs them a healing potion. Without one, you stay
  down until they have a potion. If everyone is down or you are alone, you die as in single-player:
  game over, then load a save.
- Carry healing potions for your partner.
- Beyond about 35 m from the host, you see **"Far from host"**; below about 25 m, **"Back near the host."**
- **Page Down** puts you directly behind your partner, including when they are downed. There is a
  five-second cooldown. To choose another key, set `partner_key = F9` in the INI (F1–F24, PAGEUP,
  HOME, END, INSERT, DELETE or a letter; Insert and Delete are the trade keys unless you change those).
- Your partner is marked on your map and compass.
- Your partner sees the armor and helmet you wear. `look = Diego` in your INI shows you as another character.
- Trade with your partner: talk to their character, "Show me your wares!"; they confirm with Insert (Delete declines).
  Other keys: `offer_accept_key` and `offer_decline_key` in the INI; the mod warns at the start if two of your keys are the same.
- The host saves the campaign.
- See **Known issues** in the [full player guide](PLAYING.md) for problems and workarounds.

## If something goes wrong

Open PowerShell in the extracted package folder, where this file is located, and run the following
command. Python is required:

```powershell
python "tools\collect_logs.py" "<Gothic 3 folder>"
```

Send the resulting `g3together-logs-<timestamp>.zip` to the host. It is created in the package folder.
Without Python, collect `g3together.log` from the Gothic 3 folder and the `.log` files from
`Documents\gothic3`, including its `paru` subfolder, manually.
