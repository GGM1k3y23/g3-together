// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

namespace g3t {
struct Config
{
    bool host = true;               // role = host | client
    char address[64] = "127.0.0.1"; // host address for clients
    unsigned short port = 34567;
    char name[24] = "Player";
    char language[3] = "en";       // local HUD language: en | de (not a network setting)
    char puppetTemplate[64] = "PC_Hero";  // template used to show other players (the hero himself)
    char partnerKey[16] = "PAGEDOWN";     // key that jumps to the other player (partner.cpp)
    char trace[8] = "off";                // off | on | all: runtime trace to g3trace.log (trace.cpp)
    // Town reputation (enclaves.cpp; the host's setting counts): shared = one town counter for both, the guard pursues
    // the one who did it; together = shared, and both answer for it; personal = each player's own counters.
    char reputation[12] = "shared";
};

// Reads <game dir>\g3together.ini (key = value lines, # comments). Missing file: defaults, host role.
Config const& LoadConfig();
Config const& GetConfig();
}
