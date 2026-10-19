// SPDX-License-Identifier: GPL-3.0-or-later
#include "hero.h"

#include "log.h"

#include <Game.h>
#include <Script.h>
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace g3t::hero {
namespace {
// Attribute tags of the hero's PlayerMemory. "HP" and "STR"/"SMT" are confirmed by static analysis
// (SetHitPoints -> SetBaseValue("HP"), dialog BoostAttribs); the others follow the same naming and are
// checked against the named getters by the QA dump.
char const* const Tags[] = {"STR", "DEX", "INT", "ALC", "SMT", "THF", "HP", "MP", "SP"};
bool restoring = true;

struct Stack
{
    std::string id;  // template ID, 32 hex digits
    int amount = 0;
    unsigned quality = 0;
    int slot = 0;      // gESlot it is equipped to, 0 if none
    int category = 0;  // gEItemCategory (10: learned skill)
};

std::string Hex(bCPropertyID const& id)
{
    char out[33];
    auto const* bytes = reinterpret_cast<unsigned char const*>(&id.GetCore());
    for (int i = 0; i < 16; ++i) sprintf_s(out + 2 * i, 3, "%02x", bytes[i]);
    return out;
}

Template FromHex(std::string const& hex)
{
    _GUID raw;
    auto* bytes = reinterpret_cast<unsigned char*>(&raw);
    for (int i = 0; i < 16; ++i) {
        unsigned v = 0;
        sscanf_s(hex.c_str() + 2 * i, "%2x", &v);
        bytes[i] = static_cast<unsigned char>(v);
    }
    return Template(bCPropertyID(bCGuid(raw)));
}

std::vector<Stack> Stacks(Entity& self)
{
    std::vector<Stack> out;
    for (int i = 0; i < self.Inventory.GetStackCount(); ++i) {
        Stack s;
        const Template item = self.Inventory.GetTemplate(i);
        bCPropertyID const& id = item;
        s.id = Hex(id);
        s.amount = self.Inventory.GetAmount(i);
        s.quality = self.Inventory.GetQuality(i);
        s.slot = static_cast<int>(self.Inventory.GetSlotFromStack(i));
        s.category = static_cast<int>(self.Inventory.GetCategory(i));
        out.push_back(s);
    }
    std::sort(out.begin(), out.end(), [](Stack const& a, Stack const& b) { return a.id != b.id ? a.id < b.id : a.slot < b.slot; });
    return out;
}

eCDynamicEntity* Hero() { return gCSession::GetInstance().GetHero(); }
}

bool SaveRecord(char const* path)
{
    eCDynamicEntity* hero = Hero();
    FILE* file = nullptr;
    if (!hero || fopen_s(&file, path, "w") || !file) return false;
    Entity self(hero);
    fprintf(file, "G3THERO 1\n");
    fprintf(file, "level %u %u\n", static_cast<unsigned>(self.NPC.Level), static_cast<unsigned>(self.NPC.LevelMax));
    fprintf(file, "xp %d\nlp %d %d\n", static_cast<int>(self.PlayerMemory.XP), static_cast<int>(self.PlayerMemory.LPAttribs),
            static_cast<int>(self.PlayerMemory.LPPerks));
    for (char const* tag : Tags)
        fprintf(file, "attr %s %d %d\n", tag, self.PlayerMemory.GetBaseValue(bCString(tag)), self.PlayerMemory.GetMaximum(bCString(tag)));
    for (Stack const& s : Stacks(self)) fprintf(file, "stack %s %d %u %d %d\n", s.id.c_str(), s.amount, s.quality, s.slot, s.category);
    fclose(file);
    return true;
}

bool LoadRecord(char const* path)
{
    eCDynamicEntity* hero = Hero();
    FILE* file = nullptr;
    if (!hero || fopen_s(&file, path, "r") || !file) return false;
    char line[256];
    if (!fgets(line, sizeof(line), file) || std::strncmp(line, "G3THERO 1", 9)) { fclose(file); return false; }
    if (!restoring) { fclose(file); return true; }
    Entity self(hero);
    std::vector<Stack> wanted;
    unsigned level = 0, levelMax = 0;
    int xp = 0, lpa = 0, lpp = 0;
    struct Attr { std::string tag; int base, max; };
    std::vector<Attr> attrs;
    while (fgets(line, sizeof(line), file)) {
        char a[64] = {};
        Stack s;
        int b = 0, c = 0;
        if (sscanf_s(line, "level %u %u", &level, &levelMax) == 2) continue;
        if (sscanf_s(line, "xp %d", &xp) == 1) continue;
        if (sscanf_s(line, "lp %d %d", &lpa, &lpp) == 2) continue;
        if (sscanf_s(line, "attr %63s %d %d", a, 64u, &b, &c) == 3) { attrs.push_back({a, b, c}); continue; }
        if (sscanf_s(line, "stack %63s %d %u %d %d", a, 64u, &s.amount, &s.quality, &s.slot, &s.category) == 5) {
            s.id = a;
            wanted.push_back(s);
        }
    }
    fclose(file);
    // Inventory first: equipment modifies attributes, so the base values go on afterwards.
    self.Inventory.HoldStacks(-1, -1);
    for (int i = self.Inventory.GetStackCount() - 1; i >= 0; --i) {
        if (static_cast<int>(self.Inventory.GetCategory(i)) == gEItemCategory_Skill) continue;  // skills are taught, below
        if (self.Inventory.GetSlotFromStack(i) != gESlot_None) self.Inventory.UnEquipStack(i);
        self.Inventory.DeleteStack(i);
    }
    for (Stack const& s : wanted) {
        const Template item = FromHex(s.id);
        if (s.category == gEItemCategory_Skill) {
            if (!self.Inventory.HasLearnedSkill(item)) self.Inventory.TeachSkill(item);
            continue;
        }
        const GEInt index = self.Inventory.CreateItems(item, s.quality, s.amount);
        if (index >= 0 && s.slot != gESlot_None) self.Inventory.EquipStackToSlot(index, static_cast<gESlot>(s.slot));
    }
    self.NPC.Level = level;
    self.NPC.LevelMax = levelMax;
    self.PlayerMemory.XP = xp;
    self.PlayerMemory.LPAttribs = lpa;
    self.PlayerMemory.LPPerks = lpp;
    for (Attr const& at : attrs) {
        self.PlayerMemory.SetMaximum(bCString(at.tag.c_str()), at.max);
        self.PlayerMemory.SetBaseValue(bCString(at.tag.c_str()), at.base);
    }
    return true;
}

void Dump(LogFn log)
{
    eCDynamicEntity* hero = Hero();
    if (!hero) { log("HERODUMP none"); return; }
    Entity self(hero);
    std::string text;
    char part[160];
    sprintf_s(part, "level=%u xp=%d lp=%d/%d", static_cast<unsigned>(self.NPC.Level), static_cast<int>(self.PlayerMemory.XP),
              static_cast<int>(self.PlayerMemory.LPAttribs), static_cast<int>(self.PlayerMemory.LPPerks));
    text += part;
    for (char const* tag : Tags) {
        sprintf_s(part, " %s=%d/%d", tag, self.PlayerMemory.GetBaseValue(bCString(tag)), self.PlayerMemory.GetMaximum(bCString(tag)));
        text += part;
    }
    sprintf_s(part, " named=STR%d,DEX%d,INT%d,HP%d/%d", self.PlayerMemory.GetStrength(), self.PlayerMemory.GetDexterity(),
              self.PlayerMemory.GetIntelligence(), self.PlayerMemory.GetHitPoints(), self.PlayerMemory.GetHitPointsMax());
    text += part;
    unsigned long long hash = 1469598103934665603ull;
    int count = 0;
    for (Stack const& s : Stacks(self)) {
        sprintf_s(part, "%s:%d:%u:%d;", s.id.c_str(), s.amount, s.quality, s.slot);
        for (char const* c = part; *c; ++c) { hash ^= static_cast<unsigned char>(*c); hash *= 1099511628211ull; }
        ++count;
    }
    sprintf_s(part, " stacks=%d inventory=%016llx", count, hash);
    text += part;
    log(("HERODUMP " + text).c_str());
}

std::string RecordPath(char const* name)
{
    std::string folder = gCWorld::GetSaveGameFolder().GetText();
    if (!folder.empty() && folder.back() != '\\' && folder.back() != '/') folder += '\\';
    return folder + "g3t-hero-" + name + ".txt";
}

void SaveCommand(char const* name, LogFn log)
{
    char line[300];
    const std::string path = RecordPath(name);
    sprintf_s(line, "HEROSAVE name=%s ok=%d", name, SaveRecord(path.c_str()) ? 1 : 0);
    log(line);
}

void LoadCommand(char const* name, LogFn log)
{
    char line[300];
    const std::string path = RecordPath(name);
    sprintf_s(line, "HEROLOAD name=%s ok=%d restoring=%d", name, LoadRecord(path.c_str()) ? 1 : 0, restoring ? 1 : 0);
    log(line);
}

void RestoreOff(LogFn log)
{
    restoring = false;
    log("HERORESTOREOFF ok=1");
}
}
