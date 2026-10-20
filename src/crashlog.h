// SPDX-License-Identifier: GPL-3.0-or-later
// QA diagnostic for crashes (owner, 20.10.: the host crashed in Script.dll Entity::AttachTo while the raid was ended;
// the game's own crash log has no usable call stack). With QA enabled, an access violation is logged to g3together.log
// before the game handles it: registers and every value on the stack that points into the game's modules or ours, as
// module+RVA. First chance, so exceptions the game catches itself show up too; at most a few are logged.
#pragma once

namespace g3t::crashlog {
void Tick();  // installs the handler once QA is enabled
}
