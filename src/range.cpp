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
// The engine processes entities within Entity.ROI (4000 cm in ge3.ini) of the camera, plus a 250 cm movement
// threshold (static analysis; ~45 m measured in exploration 1). Past 35 m a client's own surroundings start to
// fall outside the host's range; back under 25 m counts as together again (no flicker at the edge).
constexpr float FarAt = 3500.0f;
constexpr float NearAt = 2500.0f;
constexpr uint32_t Reminder = 60000;  // ms between reminders while still far away
constexpr uint32_t StaleState = 5000;

bool enabled = true;
bool isFar[net::MaxPlayers] = {};        // client: index 1 = me from the host; host: each client
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
        if (!wasFar && distance[i] > FarAt) isFar[i] = true;
        else if (wasFar && distance[i] < NearAt) isFar[i] = false;
        wchar_t text[200], name[32];
        MultiByteToWideChar(CP_UTF8, 0, r.latest.name, -1, name, 32);
        if (isFar[i] && (!wasFar || now - lastTold[i] > Reminder)) {
            lastTold[i] = now;
            ++warnings;
            if (host) {
                if (otherWorld) swprintf_s(text, i18n::Text(i18n::PartnerInOtherWorld), name);
                else swprintf_s(text, i18n::Text(i18n::PartnerFarAway), name, static_cast<int>(distance[i] / 100.0f));
                hud::Message(hud::Warning, text);
            } else {
                hud::Message(hud::Warning, i18n::Text(i18n::FarFromHost));
            }
            Logf("range: player %d far away (%.0f m)", i, distance[i] / 100.0f);
        } else if (!isFar[i] && wasFar) {
            ++allClears;
            if (host) {
                swprintf_s(text, i18n::Text(i18n::PartnerNearby), name);
                hud::Message(hud::Good, text);
            } else {
                hud::Message(hud::Good, i18n::Text(i18n::NearHost));
            }
            Logf("range: player %d near again (%.0f m)", i, distance[i] / 100.0f);
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
    sprintf_s(line, "RANGE enabled=%d far=%d distance_m=%.0f warnings=%u all_clears=%u", enabled ? 1 : 0, isFar[other] ? 1 : 0,
              distance[other] / 100.0f, warnings, allClears);
    log(line);
}
}
