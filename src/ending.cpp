// SPDX-License-Identifier: GPL-3.0-or-later
#include "ending.h"

#include "log.h"

#include <Game.h>
#include <Script.h>
#include <cstdio>
#include <cstring>
#include <string>

namespace g3t::ending {
namespace {
constexpr uint8_t Kind = 17;
constexpr uint32_t CheckEvery = 250;   // ms
constexpr uint32_t WaitAtMost = 60000;  // ms for this hero to be free and to know the epilogue's events
bool on = true;
std::string announced;  // the epilogue info that started here, by this hero or for another player
std::string waiting;    // the one another player started, to start here
uint8_t waitingFrom = 0;
uint32_t waitingSince = 0, nextCheck = 0;
unsigned sent = 0, received = 0, started = 0, givenUp = 0;

gCInfoManager_PS* Infos() { return gCSession::GetInstance().IsGameRunning() ? gCInfoManager_PS::GetInstance() : nullptr; }

// The epilogue's first info: the hero's own, opened by Epilog_FirstInfo (BPANKRATZ31831 Innos, 31832/31833 Adanos, 31834
// Beliar in Infos.pak).
bool IsEpilogueStart(gCInfo_PS* info)
{
    if (!info || info->AccessOwner() != bCString("PC_Hero")) return false;
    auto const& knows = info->AccessConditionPlayerKnows();
    for (GEInt i = 0; i < knows.GetCount(); ++i)
        if (knows.GetAt(i) == bCString("Epilog_FirstInfo")) return true;
    return false;
}

// Its events have reached this hero (they travel with the story events).
bool Knows(Entity& hero, gCInfo_PS* info)
{
    auto const& knows = info->AccessConditionPlayerKnows();
    for (GEInt i = 0; i < knows.GetCount(); ++i)
        if (!hero.PlayerMemory.IsSetGameEvent(knows.GetAt(i))) return false;
    return true;
}
}

void Tick()
{
    if (!on) return;
    const uint32_t now = net::Now();
    if (static_cast<int32_t>(now - nextCheck) < 0) return;
    nextCheck = now + CheckEvery;
    gCInfoManager_PS* manager = Infos();
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!manager || !hero) return;
    gCInfo_PS* current = manager->AccessIsRunning() ? manager->GetCurrentInfo() : nullptr;
    if (current && IsEpilogueStart(current) && announced != current->AccessName().GetText()) {
        announced = current->AccessName().GetText();
        waiting.clear();
        net::WorldState s{};
        s.kind = Kind;
        strncpy_s(s.routine, announced.c_str(), _TRUNCATE);
        net::SendWorldState(s, 0);
        ++sent;
        Logf("ending: the epilogue %s starts here; everyone is told", announced.c_str());
        return;
    }
    if (waiting.empty()) return;
    gCInfo_PS* info = manager->GetInfo(bCString(waiting.c_str()));
    Entity self(hero);
    if (!info || now - waitingSince > WaitAtMost) {
        Logf("ending: the epilogue %s of player %u does not start here (%s)", waiting.c_str(), waitingFrom, info ? "this hero stayed busy" : "no such info");
        waiting.clear();
        ++givenUp;
        return;
    }
    if (manager->AccessIsRunning() || !Knows(self, info)) return;  // after this hero's own dialog, once the events are here
    if (!manager->StartInfoManager(*hero, *hero)) return;
    const GEBool ok = manager->ExecuteDialogInfo(info);
    Logf("ending: playing the epilogue %s that player %u started (ok=%d)", waiting.c_str(), waitingFrom, ok ? 1 : 0);
    announced = waiting;
    waiting.clear();
    ++started;
}

void OnState(uint8_t from, net::WorldState const& s)
{
    if (!on) return;
    ++received;
    const std::string name(s.routine);
    if (name.empty() || name == announced) return;  // it plays here already
    waiting = name;
    waitingFrom = from;
    waitingSince = net::Now();
    Logf("ending: player %u started the epilogue %s; it plays here too once this hero is free", from, name.c_str());
}

void OnWorldChange()
{
    announced.clear();
    waiting.clear();
}

void Report(LogFn log)
{
    char line[200];
    sprintf_s(line, "ENDING on=%d sent=%u received=%u started=%u given_up=%u announced=%s waiting=%s", on ? 1 : 0, sent, received, started,
              givenUp, announced.empty() ? "-" : announced.c_str(), waiting.empty() ? "-" : waiting.c_str());
    log(line);
}

void Off(LogFn log)
{
    on = false;
    log("ENDINGSYNCOFF ok=1");
}
}
