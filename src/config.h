// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

namespace g3t {
struct Config
{
    bool host = true;               // role = host | client
    char address[64] = "127.0.0.1"; // host address for clients
    unsigned short port = 34567;
    char name[24] = "Player";
    char language[5] = "en";       // local HUD language: en | de | pl | auto (the game's text language); not a network setting
    char puppetTemplate[64] = "PC_Hero";  // template used to show other players (the hero himself) who chose no look
    char look[32] = "";                  // this player's own look: the template the others show it as (empty: theirs)
    char partnerKey[16] = "PAGEDOWN";     // key that jumps to the other player (partner.cpp)
    char offerAcceptKey[16] = "INSERT";   // keys that accept / decline another player's trade offer (offer.cpp)
    char offerDeclineKey[16] = "DELETE";
    char emoteKey[16] = "HOME";           // key that picks and plays an emote (emote.cpp)
    char qaHostHint[16] = "";             // QA negative controls, only with the QA bot: off (no menu hints), norefusal (no refusal there)
    bool qaKeyCheckOff = false;           // QA negative control (qa_keycheck = off, only with the QA bot): no key clash check
    char trace[8] = "off";                // off | on | all: runtime trace to g3trace.log (trace.cpp)
    // Town reputation (enclaves.cpp; the host's setting counts): shared = one town counter for both, the guard pursues
    // the one who did it; together = shared, and both answer for it; personal = each player's own counters.
    char reputation[12] = "shared";
};

// Reads <game dir>\g3together.ini (key = value lines, # comments). Missing file: defaults, host role.
Config const& LoadConfig();
Config const& GetConfig();
}
