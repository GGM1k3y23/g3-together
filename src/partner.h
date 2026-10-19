// SPDX-License-Identifier: GPL-3.0-or-later
// "Jump to partner" (owner, 2026-10-19: a leash is fine as long as it does not restrict too much): instead of a hard
// distance limit, a key (INI partner_key, default Page Down) puts the local hero next to the other player. Players who
// split up can meet again at once, so the mod's focus can stay on what happens when they play together.
#pragma once

namespace g3t::partner {
using LogFn = void (*)(char const*);
void Tick();                    // once per frame: watches the key while the game window has the focus
bool Jump(char const** reason); // jump to the partner now; false with a reason when it is not possible
void JumpQa(LogFn log);         // QA: PARTNERJUMP line
}
