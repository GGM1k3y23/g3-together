// SPDX-License-Identifier: GPL-3.0-or-later
// A guard robs a knocked-out partner as in single-player (owner 24.10.): the machine that runs the guard's AI sees the
// partner as its puppet. While that player lies knocked out by the game's own rules, its puppet lies down for the AI too
// and carries the player's gold; the game's own decision (ZS_Attack_End, WantToRansack) and robbery (ZS_RansackBody,
// half the gold) run unchanged. The gold taken goes to the player, who pays it from its real hero.
#pragma once

class gCScriptProcessingUnit;

namespace g3t::ransack {
void Start();
void Tick();
void OnWorldChange();
// The AI wrapper (ai.cpp): one of this machine's NPCs runs ZS_RansackBody now.
void OnRobbing(gCScriptProcessingUnit* spu);
using LogFn = void (*)(char const*);
void Report(LogFn log);  // QA: RANSACK line
void Off(LogFn log);     // QA negative control (ransackoff): a robber takes the puppet's gold, the player pays nothing
void NpcGold(char const* name, LogFn log);  // QA: NPCGOLD line, the gold of the NPC with that name
}
