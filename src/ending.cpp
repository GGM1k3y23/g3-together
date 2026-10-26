// SPDX-License-Identifier: GPL-3.0-or-later
#include "ending.h"

#include "hud.h"
#include "i18n.h"
#include "log.h"

#include <Game.h>
#include <Script.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

namespace g3t::ending {
namespace {
constexpr uint8_t Kind = 17;
constexpr uint32_t CheckEvery = 250;   // ms
constexpr uint32_t TellAfter = 3000;    // ms busy before the player is told the epilogue waits for this hero
constexpr uint32_t TellAgain = 60000;  // ms between reminders while it waits
constexpr uint32_t OldLimit = 60000;   // ms; QA endinglimit: the limit before the review (26.10.), as a negative control
bool on = true;
bool limited = false;   // QA endinglimit
uint32_t holdUntil = 0;  // QA endinghold: this hero counts as busy until then
std::string announced;  // the epilogue info that started here, by this hero or for another player
std::string waiting;    // the one another player started, to start here
uint8_t waitingFrom = 0;
bool knew = false;  // the epilogue's events reached this hero once (its first info clears Epilog_FirstInfo again)
uint32_t waitingSince = 0, nextCheck = 0, lastTold = 0;
unsigned sent = 0, received = 0, started = 0, givenUp = 0, told = 0, refused = 0;

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
    if (!info || !IsEpilogueStart(info)) {  // only the start of an epilogue is played for another player (review 26.10.)
        Logf("ending: %s from player %u is not an epilogue's start here; not played", waiting.c_str(), waitingFrom);
        waiting.clear();
        ++refused;
        return;
    }
    // Review 26.10.: no time limit (it was 60 s); a long dialog or fight must not cost this player the end. Not even the
    // other player leaving ends the wait, since their game closes at the epilogue's last info. A load or another world does.
    if (limited && now - waitingSince > OldLimit) {
        Logf("ending: the epilogue %s of player %u does not start here (QA endinglimit: this hero stayed busy)", waiting.c_str(), waitingFrom);
        waiting.clear();
        ++givenUp;
        return;
    }
    // The events count once they were here: the other player's first info clears Epilog_FirstInfo a few seconds later, and
    // that clear reaches this hero too (stageEndingWait 26.10.: known for 7 s during the hold, then never again).
    if (!knew && Knows(self, info)) knew = true;
    const bool held = static_cast<int32_t>(now - holdUntil) < 0;  // QA endinghold
    if (held || manager->AccessIsRunning() || !knew || !manager->StartInfoManager(*hero, *hero)) {
        // After this hero's own dialog, menu or fight, once the events are here. Until then the player knows why.
        if (now - waitingSince >= TellAfter && (!told || now - lastTold >= TellAgain)) {
            lastTold = now;
            ++told;
            hud::Message(hud::Info, i18n::Text(i18n::EpilogueWaits));
            Logf("ending: the epilogue %s of player %u waits for this hero (%u s)", waiting.c_str(), waitingFrom, (now - waitingSince) / 1000);
        }
        return;
    }
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
    told = 0;
    knew = false;
    Logf("ending: player %u started the epilogue %s; it plays here too once this hero is free", from, name.c_str());
}

void OnWorldChange()
{
    announced.clear();
    waiting.clear();
    knew = false;
}

void Report(LogFn log)
{
    char line[300];
    Format(line, "ENDING on=%d sent=%u received=%u started=%u given_up=%u told=%u refused=%u limited=%d announced=%s waiting=%s",
           on ? 1 : 0, sent, received, started, givenUp, told, refused, limited ? 1 : 0, announced.empty() ? "-" : announced.c_str(),
           waiting.empty() ? "-" : waiting.c_str());
    log(line);
}

void Hold(char const* seconds, LogFn log)
{
    const int s = std::atoi(seconds);
    holdUntil = net::Now() + static_cast<uint32_t>(s > 0 ? s : 0) * 1000;
    char line[80];
    Format(line, "ENDINGHOLD seconds=%d", s > 0 ? s : 0);
    log(line);
}

void Limit(LogFn log)
{
    limited = true;
    log("ENDINGLIMIT ok=1");
}

void Off(LogFn log)
{
    on = false;
    log("ENDINGSYNCOFF ok=1");
}
}
