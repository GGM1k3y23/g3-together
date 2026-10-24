// SPDX-License-Identifier: GPL-3.0-or-later
// The other players on the map and on the compass (owner's point 9, phase 3; C re/findings/map-compass-c.md). After the
// game has painted its map control or its compass, a mark for every other player in this world is drawn on top: on the
// map at the player's place with its name, on the compass at the edge in its direction as seen from the camera. Nothing
// goes into the map's own marker list (that is saved with the map) and no puppet pointer is kept: each frame copies the
// players' positions, the paint draws the copies.
#pragma once

namespace g3t::mapmarks {
void Start();  // hooks both paints, if Game.dll is the examined build (its virtual paint slots hold the expected functions)
void Tick();   // once per frame: the other players' positions and names
using LogFn = void (*)(char const*);
void Report(LogFn log);           // QA: MAPMARKS line (hooks, paints, what was drawn where)
void Off(LogFn log);              // QA negative control: nothing is drawn
void OpenMap(LogFn log);          // QA: the hero opens its world map (one is given if it has none)
void CloseMap(LogFn log);         // QA: closes the open page (the map)
}
