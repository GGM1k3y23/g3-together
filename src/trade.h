// SPDX-License-Identifier: GPL-3.0-or-later
// Trading between players (owner 23.10., ENTWURF-HANDEL-2026-10-23.md): the game's own trade screen, with the other
// player's puppet as the partner.
// - Every machine sends what its player can trade whenever it changes: its unequipped stacks without mission items and
//   skills, and its gold apart (MsgGoods); the host passes it on.
// - A dialog with a puppet fills it with that player's goods (the game's trade option on it opens the screen without
//   PlayerOpenTrade); PlayerOpenTrade with a puppet does too.
// - A swap in the screen shows as a difference between the puppet and the goods it was filled with: one offer to that
//   player (offer.cpp, agent A), provisional until the answer; a refusal is undone on the hero and on the puppet.
#pragma once

class eCEntity;

namespace g3t::trade {
void Start();
void Tick();
void OnWorldChange();  // open offers and the open trade are forgotten (a decline must not undo on the new world's hero)
// PlayerOpenTrade with a puppet as partner (containers.cpp): fill it with its player's goods. False: not a puppet.
bool OnOpen(eCEntity* partner);
using LogFn = void (*)(char const*);
void Report(LogFn log);                   // QA: TRADESYNC line
// QA (playergoods): PLAYERGOODS line of player <id>'s goods as received here ("0": this machine's own; "puppet<id>": what
// that puppet offers now)
void GoodsReport(char const* argument, LogFn log);
void FillOff(LogFn log);                            // QA negative control: a puppet is not filled with its player's goods
void Price(char const* argument, LogFn log);        // QA (tradeprice): "<partner>_<Item>": buy and sell price of one piece
void PricesOff(LogFn log);                          // QA negative control (tradepricesoff): a puppet's prices as a trader's
void HoldOff(LogFn log);                            // QA negative control (tradeholdoff): the buyer keeps what it got before the seller answered
void QualityOff(LogFn log);                         // QA negative control (tradequalityoff): traded items arrive at quality 0 (as before 24.10.)
void GiveQuality(char const* argument, LogFn log);  // QA (givequality): "<Item>_<quality>" one of the item at that quality
void QualityCount(char const* name, LogFn log);     // QA (qualitycount): QUALITYCOUNT line, the hero's amount of the item per quality
void CloseCommand(LogFn log);                       // QA (tradeclose): the hero out of the trade window's PS_HUD, as closing it does
}
