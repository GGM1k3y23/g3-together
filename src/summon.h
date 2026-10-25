// SPDX-License-Identifier: GPL-3.0-or-later
// Summoned things (P1 of DESIGN-2026-10-19; the meteor first). A summon exists only where it was cast: the game's
// script MagicSummon spawns it on the caster's machine with the caster as its Interaction owner. Here a player's own
// summon gets a net ID and is announced; the others replay MagicSummon on that player's puppet, so the copy belongs
// to the puppet and the combat filter drops its hits (rule 1): it shows everywhere and acts once, on the caster's
// machine, where hits on others' NPCs go to their owners as claims.
#pragma once

namespace g3t::summon {
void Start();
void Tick();  // once per frame: installs the wrappers, announces new summons, binds copies, notices summons ending
using LogFn = void (*)(char const*);
void Report(LogFn log);      // QA: SUMMONSTATE line (counters) and SMNHIT lines (hits seen while a summon acts)
void CopyHeroOff(LogFn log);  // QA negative control: copies belong to our own hero, so their hits count again
void MirrorOff(LogFn log);    // QA negative control: summons are not announced
void LateCopyCommand(char const* argument, LogFn log);  // QA (stage W8): "<ms>": copies of others' creatures come that late, no Rebind
void AreaTickOn(LogFn log);
void Creatures(LogFn log);    // QA: CREATURE lines, the local hero's summoned creatures   // QA negative control: copies of area spells pick targets and judge hits again
void WatchHits();
void WeatherFollowOff(LogFn log);  // QA negative control (P6): a copy's weather (hailstorm) stays at this PC's own hero
void WeatherReport(LogFn log);     // QA: WEATHER line             // QA: log the hits of the next 4 s as SMNHIT lines too (after a shot)
}
