// SPDX-License-Identifier: GPL-3.0-or-later
// Dialog lines for the other players (owner 22.10., re/findings/dialog-audio-c.md, version 1): the machine where a
// player's dialog runs sends each line its hero or dialog partner starts to speak; the others play the same voice file
// at the speaker (the NPC, or the player's puppet) on a channel of their own, within 20 m, faded out up to 25 m. Nothing
// of the dialog runs there: no ZS_Talk, no camera, no subtitles.
#pragma once

namespace g3t::dialogaudio {
void Start();
void Tick();
void OnWorldChange();  // stop every channel of ours (world change, shutdown)
using LogFn = void (*)(char const*);
void Report(LogFn log);   // QA: DIALOGAUDIO counters
void Disable(LogFn log);  // QA negative control: lines are neither sent nor played
void MuteGame(char const* on, LogFn log);  // QA listening test: "1" silences this game (master group), "0" restores it
}
