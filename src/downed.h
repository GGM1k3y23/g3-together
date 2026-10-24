// SPDX-License-Identifier: GPL-3.0-or-later
// No game over while a partner can help: when the game would let the local hero die, it is knocked out instead
// and waits until a partner pays a healing potion to help it up. When nobody can help (alone, everybody down),
// the hero dies as in the single-player game.
#pragma once

class eCEntity;

namespace g3t::downed {
// Called by the AI wrapper before an AI state runs for the local hero; true when it replaced a death state.
bool RedirectDeath(eCEntity* self, char const* state);
// Once per frame: a knocked-out hero stays down while a partner could help; a partner who talks to its puppet and
// chooses "Ich werde dir helfen." pays a healing potion and stands it up (owner 23.10.). Nobody able to help for 5 s: the hero dies. Other players are told
// when someone goes down or dies.
void Tick();
void Revived(int byPlayer);           // network: that partner paid a potion for us
void HelperHasNoPotion(int byPlayer); // network: that partner stood next to us without one
bool Held();             // the local hero lies knocked out, waiting for help
bool NativeDown();       // ... or knocked out by the game's own rules (a guard, a duel): it stands up by itself; a winner may rob it
using LogFn = void (*)(char const*);
void Off(LogFn log);     // QA negative control: let the hero die again
void HoldOff(LogFn log); // QA negative control: the hero gets up by itself again
void PotionOff(LogFn log); // QA negative control: helping up costs nothing
void LethalOnlyOff(LogFn log);  // QA negative control: every knock-out waits for help, not only an intercepted death
void GivePotion(char const* argument, LogFn log);  // QA: "Template[-Amount]" into the hero's inventory ("-0" takes all away)
void Potions(LogFn log); // QA: POTION lines for the hero's potions
void Report(LogFn log);  // QA: DOWNED line
void AutoHelpOn(LogFn log);     // QA: standing next to a downed partner for 3 s helps it up again (as before 23.10.)
void HelpOptionOff(LogFn log);  // QA negative control: a downed partner's puppet offers no help option
}
