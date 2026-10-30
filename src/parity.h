// SPDX-License-Identifier: GPL-3.0-or-later
// Install parity: a fingerprint of everything that changes the rules of the game (game and script DLLs,
// data archives, balancing options). The host refuses a client whose fingerprint differs, naming the parts.
#pragma once

#include <cstdint>

namespace g3t::parity {
enum Item : uint8_t { GameDll, ScriptDll, EngineDll, ScriptsFolder, DataRules, DataOther, Options, ItemCount };
char const* ItemName(int item);
// Hash the install and read the options; logs one PARITY line per item. Call once before the network starts.
void Compute();
uint64_t const* Local();  // ItemCount values, valid after Compute
char const* OptionsText();  // this install's options in a few words, for a refusal (valid after Compute)
}
