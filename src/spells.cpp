// SPDX-License-Identifier: GPL-3.0-or-later
#include "spells.h"

#include "log.h"
#include "net.h"
#include "remote.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace g3t::spells {
namespace {
// The scripts that act on their target (other) and are replayed on the target player's own hero.
char const* const Replayed[] = {"MagicHealOther"};
constexpr int Count = sizeof(Replayed) / sizeof(Replayed[0]);
gFScript originals[Count] = {};
bool installed = false, mirror = true;
DWORD lastTry = 0;
unsigned sent = 0, received = 0, replayed = 0, refused = 0, noHero = 0;

template <typename T>
T* Find(bTValMap<bCString, T*>& map, char const* name)
{
    T* found = nullptr;
    return map.GetAt(bCString(name), found) ? found : nullptr;
}

int IndexOf(char const* name)
{
    for (int i = 0; i < Count; ++i)
        if (!std::strcmp(Replayed[i], name)) return i;
    return -1;
}

// One wrapper per script: the index picks the original and the name to send.
template <int I>
GEInt GE_STDCALL Wrap(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    eCEntity* caster = self ? static_cast<Entity*>(self)->GetInstance() : (spu ? spu->GetSelfEntity() : nullptr);
    eCEntity* target = other ? static_cast<Entity*>(other)->GetInstance() : (spu ? spu->GetOtherEntity() : nullptr);
    const GEInt result = originals[I](spu, self, other, args);
    const int player = target ? remote::PuppetOwner(target) : 0;
    if (mirror && player && caster && caster == remote::ControlledBody()) {
        net::SendPlayerSpell(static_cast<uint8_t>(player), Replayed[I]);
        ++sent;
        Logf("spells: %s on player %d's puppet, sent", Replayed[I], player);
    }
    return result;
}

const gFScript wrappers[Count] = {&Wrap<0>};

void OnPlayerSpell(uint8_t from, char const* script)
{
    ++received;
    const int index = IndexOf(script);
    if (index < 0 || !originals[index]) {
        ++refused;
        Logf("spells: %s from player %u refused (not replayed here)", script, from);
        return;
    }
    eCEntity* hero = remote::ControlledBody();
    if (!hero) {
        ++noHero;
        return;
    }
    // self = the caster's puppet when there is one (the scripts read the target, other), other = our own hero.
    eCEntity* caster = remote::PuppetOf(from);
    Entity self(caster ? caster : hero), target(hero);
    originals[index](nullptr, &self, &target, 0);
    ++replayed;
    Logf("spells: %s from player %u replayed on our hero", script, from);
}
}

void Start() { net::SetPlayerSpellHandler(&OnPlayerSpell); }

void Tick()
{
    const DWORD now = GetTickCount();
    if (installed || !gCSession::GetInstance().IsGameRunning() || now - lastTry < 1000) return;
    lastTry = now;
    auto& scripts = GetScriptAdminExt().GetScripts();
    for (int i = 0; i < Count; ++i) {
        gSScript* script = Find(scripts, Replayed[i]);
        if (!script) return;  // tried again later
        if (!originals[i]) {
            originals[i] = script->m_funcScript;
            script->m_funcScript = wrappers[i];
        }
    }
    installed = true;
    Log("spells: wrappers installed (MagicHealOther)");
}

void Report(LogFn log)
{
    char line[200];
    sprintf_s(line, "SPELLSTATE installed=%d mirror=%d sent=%u received=%u replayed=%u refused=%u no_hero=%u", installed, mirror, sent, received,
              replayed, refused, noHero);
    log(line);
}

void MirrorOff(LogFn log)
{
    mirror = false;
    log("SPELLMIRROROFF casts on puppets are not sent");
}

void PuppetGuid(char const* player, LogFn log)
{
    char line[120];
    eCEntity* puppet = remote::PuppetOf(std::atoi(player));
    if (!puppet) { log("PUPPET refused no puppet of that player here"); return; }
    const eCEntityProxy proxy(puppet);
    bCPropertyID::bSCore core;
    std::memcpy(&core, &proxy.GetEntityID().GetCore(), 16);
    sprintf_s(line, "PUPPET player=%s guid=%08X-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X", player, core.Data1, core.Data2, core.Data3,
              core.Data4[0], core.Data4[1], core.Data4[2], core.Data4[3], core.Data4[4], core.Data4[5], core.Data4[6], core.Data4[7]);
    log(line);
}

void SetHp(char const* value, LogFn log)
{
    char line[120];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) { log("SETHP refused no hero"); return; }
    Entity self(hero);
    const int before = self.PlayerMemory.GetHitPoints();
    self.PlayerMemory.SetHitPoints(std::atoi(value));
    sprintf_s(line, "SETHP before=%d after=%d max=%d", before, self.PlayerMemory.GetHitPoints(), self.PlayerMemory.GetHitPointsMax());
    log(line);
}
}
