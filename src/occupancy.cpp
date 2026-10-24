// SPDX-License-Identifier: GPL-3.0-or-later
#include "occupancy.h"

#include "hud.h"
#include "i18n.h"
#include "log.h"
#include "net.h"
#include "remote.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <map>
#include <string>

namespace g3t::occupancy {
namespace {
constexpr uint8_t KindUse = 13;  // net::WorldState kind
constexpr DWORD LookEvery = 250;  // ms
bool enabled = true;  // QA negative control: usesyncoff
DWORD lastLook = 0;
eCEntity* used = nullptr;   // the object this machine's hero uses, as last reported
uint8_t usedGuid[16] = {};
std::map<std::string, uint8_t> marked;  // objects whose user here is another player's puppet: by GUID, that player
unsigned occupied = 0, released = 0, applied = 0, cleared = 0, busy = 0, refused = 0;
eCEntity* refusedObject = nullptr;  // the last place this hero was kept from (one message per try)

std::string Key(uint8_t const guid[16]) { return std::string(reinterpret_cast<char const*>(guid), 16); }

void GuidOf(eCEntity* entity, uint8_t out[16])
{
    const eCEntityProxy proxy(entity);
    std::memcpy(out, &proxy.GetEntityID().GetCore(), 16);
}

bool UsedBy(eCEntity* object, eCEntity* user)
{
    return object && user && object->GetPropertySet(eEPropertySetType_Interaction) && !object->GetPropertySet(eEPropertySetType_NPC) &&
           Entity(object).Interaction.GetUser().GetInstance() == user;
}

void Send(uint8_t const guid[16], bool use)
{
    net::WorldState s{};
    s.kind = KindUse;
    std::memcpy(s.guid, guid, 16);
    s.guard = net::LocalId();
    s.npc = use ? 1 : 0;
    net::SendWorldState(s, 0);
}

// The player whose puppet uses this object here, 0 if none.
uint8_t TakenBy(eCEntity* object)
{
    if (!object || !object->GetPropertySet(eEPropertySetType_Interaction) || object->GetPropertySet(eEPropertySetType_NPC)) return 0;
    uint8_t guid[16];
    GuidOf(object, guid);
    const auto it = marked.find(Key(guid));
    return it != marked.end() ? it->second : 0;
}

// The hero sets out to use a place another player uses (PS_Interact walks it there, then makes it the user): stop it, as
// an NPC sitting there would keep it off, and keep the other player's puppet as the user.
void Refuse(eCEntity* hero)
{
    Entity self(hero);
    if (self.Routine.GetCurrentTask() != bCString("PS_Interact")) { refusedObject = nullptr; return; }
    eCEntity* object = self.NPC.GetCurrentTarget().GetInstance();
    uint8_t player = TakenBy(object);
    if (!player) {
        object = self.Focus.GetFocusEntity().GetInstance();
        player = TakenBy(object);
    }
    eCEntity* puppet = player ? remote::PuppetOf(player) : nullptr;
    if (!puppet) return;
    self.Routine.SetTask(bCString("PS_Interact_End"));
    Entity(object).Interaction.SetUser(Entity(puppet));
    ++refused;
    Logf("occupancy: this player wanted to use %s, which player %u uses; refused", object->GetName().GetText(), player);
    if (object == refusedObject) return;
    refusedObject = object;
    wchar_t name[64] = {}, text[160];
    MultiByteToWideChar(CP_UTF8, 0, net::PeerName(player), -1, name, 64);
    swprintf_s(text, i18n::Text(i18n::PlaceInUse), name[0] ? name : L"?");
    hud::Message(hud::Info, text);
}

// The object the hero uses: the game sets it as the object's user; the hero's target or focus points at it.
eCEntity* InUse(eCEntity* hero)
{
    if (UsedBy(used, hero)) return used;
    Entity self(hero);
    if (eCEntity* target = self.NPC.GetCurrentTarget().GetInstance(); UsedBy(target, hero)) return target;
    if (eCEntity* focus = self.Focus.GetFocusEntity().GetInstance(); UsedBy(focus, hero)) return focus;
    return nullptr;
}
}

void Tick()
{
    if (!net::Connected() || !gCSession::GetInstance().IsGameRunning()) return;
    if (enabled && !marked.empty())
        if (eCEntity* hero = remote::ControlledBody()) Refuse(hero);  // every frame: before the hero gets there
    const DWORD now = GetTickCount();
    if (now - lastLook < LookEvery) return;
    lastLook = now;
    eCEntity* hero = remote::ControlledBody();
    eCEntity* object = hero ? InUse(hero) : nullptr;
    if (object == used) return;
    if (used && enabled) {
        Send(usedGuid, false);
        ++released;
        Logf("occupancy: this player let go of %s", used->GetName().GetText());
    }
    used = object;
    if (!object) return;
    GuidOf(object, usedGuid);
    if (!enabled) return;
    Send(usedGuid, true);
    ++occupied;
    Logf("occupancy: this player uses %s", object->GetName().GetText());
}

void OnState(uint8_t from, net::WorldState const& s)
{
    const uint8_t player = s.guard ? s.guard : from;
    _GUID raw;
    std::memcpy(&raw, s.guid, 16);
    Entity object = Entity(bCPropertyID(bCGuid(raw)));
    eCEntity* instance = object.GetInstance();
    if (!instance || !instance->GetPropertySet(eEPropertySetType_Interaction) || instance->GetPropertySet(eEPropertySetType_NPC)) return;
    eCEntity* puppet = remote::PuppetOf(player);
    eCEntity* user = object.Interaction.GetUser().GetInstance();
    if (s.npc) {
        if (!puppet) return;
        if (user && user != puppet && !remote::PuppetOwner(user)) {
            ++busy;  // someone here sits there already (this hero or an NPC): it stays theirs
            Logf("occupancy: player %u uses %s, which %s uses here", player, instance->GetName().GetText(), Entity(user).GetName().GetText());
            return;
        }
        object.Interaction.SetUser(Entity(puppet));
        marked[Key(s.guid)] = player;
        ++applied;
        Logf("occupancy: player %u uses %s; taken here too", player, instance->GetName().GetText());
        return;
    }
    marked.erase(Key(s.guid));
    if (user && remote::PuppetOwner(user) == player) {
        object.Interaction.SetUser(Entity());
        ++cleared;
        Logf("occupancy: player %u let go of %s; free here too", player, instance->GetName().GetText());
    }
}

void Forget(int player, eCEntity* puppet)
{
    for (auto it = marked.begin(); it != marked.end();) {
        if (it->second != player) { ++it; continue; }
        _GUID raw;
        std::memcpy(&raw, it->first.data(), 16);
        Entity object = Entity(bCPropertyID(bCGuid(raw)));
        if (object.GetInstance() && puppet && object.Interaction.GetUser().GetInstance() == puppet) {
            object.Interaction.SetUser(Entity());
            ++cleared;
        }
        it = marked.erase(it);
    }
}

void OnWorldChange()
{
    used = nullptr;
    refusedObject = nullptr;
    marked.clear();
}

void Report(LogFn log)
{
    char line[200];
    sprintf_s(line, "OCCUPANCY enabled=%d using=%s occupied=%u released=%u applied=%u cleared=%u busy=%u marked=%u refused=%u", enabled ? 1 : 0,
              used ? used->GetName().GetText() : "-", occupied, released, applied, cleared, busy, static_cast<unsigned>(marked.size()), refused);
    log(line);
}

void Off(LogFn log)
{
    enabled = false;
    log("USESYNCOFF ok=1");
}

void StandUp(LogFn log)
{
    char line[160];
    eCEntity* hero = remote::ControlledBody();
    if (!hero) { log("REFUSED unuse: no hero"); return; }
    Entity self(hero);
    self.Routine.SetTask(bCString("PS_Interact_End"));
    sprintf_s(line, "UNUSE task=%s", self.Routine.GetCurrentTask().GetText());
    log(line);
}
}
