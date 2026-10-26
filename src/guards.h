// SPDX-License-Identifier: GPL-3.0-or-later
// Guards and the other players (P7, owner 25.10.: as in single-player). The game checks a drawn weapon and a guarded
// place only against its own player: AssessActivePerception hands the player it perceived to AssessPlayerZone and
// AssessPlayerWeapon, which give up for anyone else (IsPlayer, GetPlayer), and the guard's reaction states follow
// Entity::GetPlayer (ZS_ReactToZone_Loop, ZS_GotoPlayerZone, ZS_ObserveSuspect_Loop). So a guard of this machine never
// warned the other player (trespassdiag 25.10.: the host's hero drew its weapon at Brenton or walked into Muntasir's
// place and was attacked, the client's nothing). Here an NPC this machine runs that perceived nothing of its own checks
// the same two rules against each puppet near it, and while it does, and while one of those reaction states runs with a
// puppet as its target, the game's Entity::GetPlayer and IsPlayer (Script_Game.dll's imports) name that puppet.
#pragma once

class eCEntity;

namespace g3t::guards {
void Tick();  // installs the hooks once the game's scripts are registered

// While alive, the game's player is `puppet` for Script_Game.dll (none: no change).
class Acting {
public:
    explicit Acting(eCEntity* puppet);
    ~Acting();
    Acting(Acting const&) = delete;
    Acting& operator=(Acting const&) = delete;

private:
    eCEntity* before;
};

// The puppet that stands for the game's player right now, none for the local hero (QA, p7probe.cpp).
eCEntity* CurrentActor();

// ai.cpp, before an AI state of `self` runs: the puppet it reacts to when that state follows the player, else none.
eCEntity* StateActor(eCEntity* self, char const* state);

using LogFn = void (*)(char const*);
void Report(LogFn log);  // QA: GUARDS line
void Off(LogFn log);     // QA negative control (trespassoff): no checks against puppets
void ZoneFixOff(LogFn log);    // QA negative control (zonefixoff): puppets keep whatever zone their navigation has
void FightsOff(LogFn log);     // QA negative control (fightsoff): the intent to kill a puppet as before 25.10. night
void ChapterCopyOff(LogFn log);  // QA negative control (chaptercopyoff): a puppet standing for the player keeps chapter 0
void SneakCheckOn(LogFn log);  // QA negative control (sneakcheckon): sneaking is checked against puppets again
void EnterZoneOn(LogFn log);   // QA negative control (enterzoneon): a puppet entering a zone is sent to the NPCs again
void AnimalWakeOff(LogFn log); // QA negative control (animalwakeoff): a sleeping animal wakes only for this machine's hero
void FreeDestOff(LogFn log);   // QA negative control (freedestoff): an NPC waits for its destination only for this machine's hero
void Sneak(char const* on, LogFn log);  // QA: "1" the local hero sneaks, "0" it runs again
void Homes(char const* argument, LogFn log);  // QA: HOME lines, NPCs near the hero away from (or with "all" at) their home zone
}
