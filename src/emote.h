// SPDX-License-Identifier: GPL-3.0-or-later
// Emotes (owner 27.10.: "Kleine Spaß-Idee"): a key (INI emote_key, default Home) picks one of a few motions the game
// already has for its NPCs (cheering, dancing, arms crossed ...) and the local hero plays it. Each press shows the next
// emote on the screen; one second without a press plays it. Nothing new travels: the hero's motion on slot 0 is in every
// PlayerState already, and the other machines replay it on its puppet (body::Drive), loops included.
#pragma once

namespace g3t::emote {
using LogFn = void (*)(char const*);
void Tick();                                    // once per frame: the key, the pending choice, a running loop
void Command(char const* argument, LogFn log);  // QA: "emote <key|index>[:spu|:entity|:overlay]" plays one now (EMOTE line)
void Raw(char const* argument, LogFn log);      // QA: "emoteraw <motion>[:method]", any motion (without "Hero_": standing)
void State(LogFn log);                          // QA: EMOTESTATE, what the hero plays now and the last emote's result
}
