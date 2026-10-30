// SPDX-License-Identifier: GPL-3.0-or-later
#include "range.h"

#include "body.h"
#include "hud.h"
#include "i18n.h"
#include "log.h"
#include "net.h"
#include "remote.h"

#include <Game.h>
#include <Windows.h>
#include <cstdio>

namespace g3t::range {
namespace {
// Each player's surroundings run on that player's PC and stay the same for both (stage E), so distance alone needs
// no notice any more. Only a partner in another world is told, to host and client alike.
constexpr uint32_t Reminder = 60000;  // ms between reminders while still in another world
constexpr uint32_t StaleState = 5000;

bool enabled = true;
bool isFar[net::MaxPlayers] = {};        // in another world; client: index 1 = the host; host: each client
uint32_t lastTold[net::MaxPlayers] = {};
unsigned warnings = 0, allClears = 0;
float distance[net::MaxPlayers] = {};

bool Live(net::Remote const& r, uint32_t now)
{
    return r.connected && r.latestTicks && now - r.latestTicks < StaleState && (r.latest.flags & 1);
}
}

void Tick()
{
    gCSession& session = gCSession::GetInstance();
    eCDynamicEntity* hero = session.GetHero();
    if (!enabled || !hero || !session.IsGameRunning() || !net::Connected()) return;
    const uint32_t now = net::Now();
    const bCVector& at = remote::ControlledBody()->GetWorldPosition();  // a transformed player: its creature
    const float here[3] = {at.GetX(), at.GetY(), at.GetZ()};
    net::Remote const* remotes = net::Remotes();
    const bool host = net::IsHost();
    for (int i = 1; i < net::MaxPlayers; ++i) {
        if (i == net::LocalId() || (!host && i != 1)) continue;  // a client measures against the host only
        net::Remote const& r = remotes[i];
        if (!Live(r, now)) { isFar[i] = false; continue; }
        const bool otherWorld = r.latest.world != remote::CurrentWorld();
        distance[i] = otherWorld ? 1e9f : body::Distance(r.latest.position, here);
        const bool wasFar = isFar[i];
        isFar[i] = otherWorld;
        if (isFar[i] && (!wasFar || now - lastTold[i] > Reminder)) {
            lastTold[i] = now;
            ++warnings;
            wchar_t text[200], name[32] = {};
            if (!MultiByteToWideChar(CP_UTF8, 0, r.latest.name, -1, name, 32)) name[0] = 0;
            name[31] = 0;
            swprintf_s(text, i18n::Text(i18n::PartnerInOtherWorld), name);
            hud::Message(hud::Warning, text);
            Logf("range: player %d in another world", i);
        } else if (!isFar[i] && wasFar) {
            ++allClears;
            Logf("range: player %d in this world again (%.0f m)", i, distance[i] / 100.0f);
        }
    }
}

void Off(LogFn log)
{
    enabled = false;
    log("RANGEOFF ok=1");
}

void Report(LogFn log)
{
    char line[200];
    const int other = net::IsHost() ? 2 : 1;
    Format(line, "RANGE enabled=%d far=%d distance_m=%.0f warnings=%u all_clears=%u", enabled ? 1 : 0, isFar[other] ? 1 : 0,
              distance[other] / 100.0f, warnings, allClears);
    log(line);
}
}
