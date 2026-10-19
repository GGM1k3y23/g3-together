// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

namespace g3t {
struct Config
{
    bool host = true;               // role = host | client
    char address[64] = "127.0.0.1"; // host address for clients
    unsigned short port = 34567;
    char name[24] = "Player";
    char puppetTemplate[64] = "PC_Hero";  // template used to show other players (the hero himself)
    char partnerKey[16] = "PAGEDOWN";     // key that jumps to the other player (partner.cpp)
};

// Reads <game dir>\g3together.ini (key = value lines, # comments). Missing file: defaults, host role.
Config const& LoadConfig();
Config const& GetConfig();
}
