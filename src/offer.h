// SPDX-License-Identifier: GPL-3.0-or-later
// Trades between players (owner 23.10., point 2; g3-workspace/ENTWURF-HANDEL-2026-10-23.md). The buyer trades at the seller's
// puppet in the game's own trade window (B, trade.cpp); what it took and gave goes to the seller as an offer (Send). The
// seller sees it on the screen and accepts or declines it with a key within 30 s; accepted, it is checked against the
// seller's hero (everything it gives still there, not equipped, no quest item, no skill) and applied whole or not at all.
// The answer goes back to the buyer (the handler: trade::OnResult), which keeps or undoes its side; no answer within 40 s
// counts as declined. One offer at a time per seller; another one meanwhile is answered "busy".
#pragma once

#include <cstdint>

namespace g3t::offer {
struct Delta {
    uint8_t item[16];  // template ID
    int32_t amount;    // + goes to the buyer, - from the buyer
    uint16_t quality;  // gEItemQuality flags of what goes: a sharp sword stays sharp (protocol 46)
};
// Buyer: offer this to the seller (a player id); returns the offer's serial, 0 if it could not go out (no such player, too
// many entries). gold: + goes to the buyer, - from the buyer. At most net::MaxOfferEntries entries.
uint32_t Send(uint8_t seller, Delta const* deltas, int count, int32_t gold);
using ResultFn = void (*)(uint32_t serial, bool accepted);
void SetResultHandler(ResultFn handler);  // trade.cpp: trade::OnResult

void Start();         // the network handlers; the key clash check (partner_key, offer_accept_key, offer_decline_key, emote_key)
void Tick();          // once per frame: the seller's keys and 30 s, the buyer's 40 s
void OnWorldChange(); // an open offer ends (the seller answers "ran out", the buyer's are declined)

using LogFn = void (*)(char const*);
void SendCommand(char const* argument, LogFn log);  // QA: "<seller>_<gold>_<name>-<amount>[-<name>-<amount>]" (template names; n3 = -3)
void Decide(bool accept, LogFn log);                // QA: what the accept / decline key does
void Report(LogFn log);                             // QA: OFFERSTATE line
void PartialOn(LogFn log);                          // QA negative control: no check before, no undo on a failure (as a naive apply)
void AddCommand(char const* argument, LogFn log);   // QA: "<name>_<amount>" puts that many of the item into the hero's inventory
void CountCommand(char const* name, LogFn log);     // QA: HEROCOUNT line, how many of the item the hero has and can trade
void KeyCheck(LogFn log);                           // QA: KEYCHECK line, the three keys and the clashes found at the start
}
