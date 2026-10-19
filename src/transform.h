// SPDX-License-Identifier: GPL-3.0-or-later
// Players turned into animals (transformation spells and potions): QA commands to transform the local hero the way
// the game's MagicTransform script does, to turn it back with the game's TransformBack script, and to report it.
#pragma once

namespace g3t::transform {
using LogFn = void (*)(char const*);
void Start(char const* creatureTemplate, LogFn log);  // QA: "transform Wolf"
void Back(LogFn log);                                 // QA: "transformback"
void Report(LogFn log);                               // QA: TRANSFORM line
void Walk(int direction, LogFn log);  // QA: the body the player controls runs 6 m (movement goal) towards 45-degree direction
}
