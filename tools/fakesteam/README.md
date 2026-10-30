# Testing the Steam transport without two Steam accounts

`transport = steam` (src/steam.cpp) needs two Steam accounts on two PCs for a real test. Everything else runs on one PC
with `fake_steam_api.c`, a stand-in for `steam_api.dll` that passes the same flat API calls over 127.0.0.1 (lobbies are
files in a folder, every other copy's lobby counts as a friend's). It keeps the real 32-bit layouts (the sender identity
at offset 12, GetSteamID64 answering 0 for a wrong offset), so a reader at the wrong offset fails here as with Steam.

Both are built with the mod (`fake_steam_api.dll` and `g3t_relaytest.exe` in `artifacts/fakesteam`); neither goes into
the player package.

## The transport alone: g3t_relaytest

`net.cpp`, `steam.cpp` and ENet without the game. The host waits for a client, sends it a 2 MB "save" as file chunks and
exchanges player states; the client must find the host among its friends, be welcomed, get the states and the whole save
(CRC), then quit; the host must see it gone at once.

```bat
set G3T_STEAM_DLL=%CD%\artifacts\fakesteam\fake_steam_api.dll
set G3T_FAKESTEAM_DIR=%TEMP%\g3t-relaytest
rmdir /s /q %G3T_FAKESTEAM_DIR%
start "host" cmd /c "set G3T_FAKESTEAM_ID=1&& set G3T_FAKESTEAM_NAME=Host&& artifacts\fakesteam\g3t_relaytest host 34600 60"
set G3T_FAKESTEAM_ID=2&& artifacts\fakesteam\g3t_relaytest client 34600 60
```

Each prints `RESULT: PASS` or `RESULT: FAIL (...)`. Checked on 27.10. (mingw build under Wine, the same sources):

| Run | Client | Expected | Result |
|---|---|---|---|
| plain | finds the host | PASS | PASS both, save whole, host saw the client leave within 1 ms |
| `steam_host` | 4th argument `host` (any case) | PASS | PASS |
| invite | `G3T_FAKESTEAM_INVITE=1` | PASS | PASS (joins through the accepted invite) |
| negative: no Steam | `G3T_FAKESTEAM_INIT=2` | FAIL, falls back to the address | FAIL, "Steam not available (Steam is not running); connecting to the address instead" |
| negative: wrong friend | 4th argument `Nobody` | FAIL | FAIL, keeps looking |
| negative: foreign lobby | a friend in a Spacewar lobby without the G3 Together tag, `steam_host` = that friend | FAIL | FAIL, "not a G3 Together lobby", skipped for good |

## The game: qa/together.py

Any stage over the stand-in:

```bat
set G3_FAKESTEAM=%CD%\artifacts\fakesteam\fake_steam_api.dll
set G3_INI_EXTRA=transport = steam
python qa\together.py stage2
```

Each copy gets its own Steam id (pair × 10 + 1 for the host, + 2 for the client) and each pair its own lobby folder
(`%TEMP%\g3t-fakesteam-<pair>`), so pairs never see each other. The host's lobby folder is emptied at its launch. Real
Steam is never touched. The QA command `steamstatus` logs `STEAMSTATUS ready=… role=… lobby=… linked=… peer=<id>:<linked>:<to ENet>:<to Steam>:<rtt>`.
Negative control: the same stage with `G3_INI_EXTRA=transport = steam;steam_host = Nobody` must fail to join.

## Two PCs, one Steam account each, or none

The stand-in also works between two PCs without Steam: on each, `G3T_FAKESTEAM_REMOTE=<other PC's IP>:<its port>:<its id>`
and `G3T_FAKESTEAM_PORT`, plus `G3T_FAKESTEAM_LISTREMOTE=1` on the client. For the real thing, both PCs use
`<game>\G3Together\steam_api.dll` (the setup puts it there) and two Steam accounts that are friends; the rows to check are
in docs/PLAYTEST.md ("over Steam").
