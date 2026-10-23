// SPDX-License-Identifier: GPL-3.0-or-later
#include "hero.h"

#include "log.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
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
bool checking = true;     // QA negative control: recordcheckoff (a record is applied as read, as before 22.10.)
bool writeFault = false;  // QA (stage W2): recordwritefault breaks off the next write before it replaces the record

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

// Written to <path>.tmp and moved over the old record only once every line and the close went through, with an end line
// that names the number of stacks (Codex review 22.10. R1: "w" emptied the record at once, and a write that broke off left
// a record whose next load wiped the inventory and set level and experience to zero).
bool SaveRecord(char const* path)
{
    eCDynamicEntity* hero = Hero();
    if (!hero) return false;
    const std::string temp = std::string(path) + ".tmp";
    FILE* file = nullptr;
    if (fopen_s(&file, temp.c_str(), "w") || !file) return false;
    Entity self(hero);
    const std::vector<Stack> stacks = Stacks(self);
    bool ok = fprintf(file, "G3THERO 2\n") > 0;
    ok = ok && fprintf(file, "level %u %u\n", static_cast<unsigned>(self.NPC.Level), static_cast<unsigned>(self.NPC.LevelMax)) > 0;
    ok = ok && fprintf(file, "xp %d\nlp %d %d\n", static_cast<int>(self.PlayerMemory.XP), static_cast<int>(self.PlayerMemory.LPAttribs),
                       static_cast<int>(self.PlayerMemory.LPPerks)) > 0;
    for (char const* tag : Tags)
        ok = ok && fprintf(file, "attr %s %d %d\n", tag, self.PlayerMemory.GetBaseValue(bCString(tag)), self.PlayerMemory.GetMaximum(bCString(tag))) > 0;
    for (Stack const& s : stacks) ok = ok && fprintf(file, "stack %s %d %u %d %d\n", s.id.c_str(), s.amount, s.quality, s.slot, s.category) > 0;
    ok = ok && fprintf(file, "end %zu\n", stacks.size()) > 0;
    ok = fflush(file) == 0 && ok;
    ok = fclose(file) == 0 && ok;
    if (ok && writeFault) ok = writeFault = false;  // QA (stage W2): the write breaks off before the record is replaced
    if (ok) ok = MoveFileExA(temp.c_str(), path, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) != 0;
    if (!ok) {
        DeleteFileA(temp.c_str());
        Logf("hero: record %s not written; the one before stays", path);
    }
    return ok;
}

bool LoadRecord(char const* path)
{
    eCDynamicEntity* hero = Hero();
    FILE* file = nullptr;
    if (!hero || fopen_s(&file, path, "r") || !file) return false;
    char line[256];
    if (!fgets(line, sizeof(line), file) || std::strncmp(line, "G3THERO ", 8) || (line[8] != '1' && line[8] != '2')) { fclose(file); return false; }
    const int version = line[8] - '0';
    if (!restoring) { fclose(file); return true; }
    // The whole record is read and checked before the hero changes (R1): a record that broke off, a line that is no
    // record line, a missing field or a value out of range leaves the hero as it is. Version 1 records (before 22.10.)
    // have no end line; they pass when every field is there.
    std::vector<Stack> wanted;
    unsigned level = 0, levelMax = 0;
    int xp = 0, lpa = 0, lpp = 0;
    struct Attr { std::string tag; int base, max; };
    std::vector<Attr> attrs;
    bool haveLevel = false, haveXp = false, haveLp = false, ended = false;
    char const* refused = nullptr;
    size_t declared = 0;
    while (!refused && fgets(line, sizeof(line), file)) {
        char a[64] = {};
        Stack s;
        int b = 0, c = 0;
        if (ended) refused = "a line after the end line";
        else if (sscanf_s(line, "level %u %u", &level, &levelMax) == 2) haveLevel = true;
        else if (sscanf_s(line, "xp %d", &xp) == 1) haveXp = true;
        else if (sscanf_s(line, "lp %d %d", &lpa, &lpp) == 2) haveLp = true;
        else if (sscanf_s(line, "attr %63s %d %d", a, 64u, &b, &c) == 3) attrs.push_back({a, b, c});
        else if (sscanf_s(line, "stack %63s %d %u %d %d", a, 64u, &s.amount, &s.quality, &s.slot, &s.category) == 5) {
            s.id = a;
            wanted.push_back(s);
        } else if (sscanf_s(line, "end %zu", &declared) == 1) ended = true;
        else refused = "a line that is no record line";
    }
    fclose(file);
    if (!refused && checking) {
        if (!haveLevel || !haveXp || !haveLp) refused = "level, experience or learn points missing";
        else if (version >= 2 && !ended) refused = "no end line (the write broke off)";
        else if (ended && declared != wanted.size()) refused = "fewer stacks than the end line names";
        else if (level > 1000 || levelMax > 1000 || xp < 0 || xp > 100000000 || lpa < 0 || lpa > 100000 || lpp < 0 || lpp > 100000)
            refused = "level, experience or learn points out of range";
        for (char const* tag : Tags) {
            if (refused) break;
            if (std::count_if(attrs.begin(), attrs.end(), [tag](Attr const& at) { return at.tag == tag; }) != 1) refused = "an attribute missing or twice";
        }
        if (!refused && attrs.size() != sizeof(Tags) / sizeof(Tags[0])) refused = "an attribute this record does not know";
        for (Attr const& at : attrs)
            if (!refused && (at.base < -1000000 || at.base > 1000000 || at.max < -1000000 || at.max > 1000000)) refused = "an attribute out of range";
        for (Stack const& s : wanted) {
            if (refused) break;
            if (s.id.size() != 32 || s.id.find_first_not_of("0123456789abcdef") != std::string::npos || !FromHex(s.id).IsValid())
                refused = "a stack whose item does not exist";
            else if (s.amount < 1 || s.amount > 1000000 || s.quality >= 1024 || s.slot < gESlot_None || s.slot > gESlot_Helmet ||
                     s.category < 0 || s.category >= gEItemCategory_Count)
                refused = "a stack out of range";
        }
    }
    if (refused) {
        Logf("hero: record %s refused (%s); the hero stays as it is", path, refused);
        return false;
    }
    if (version < 2 && checking) Logf("hero: record %s is of version 1 (no end line); every field is there", path);
    Entity self(hero);
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

// QA (stage W2): the record <name> as a write that broke off leaves it: cut in the middle of its stack lines, or with
// "<name>_header" after its first line (the bot's arguments hold no spaces).
void CutCommand(char const* argument, LogFn log)
{
    std::string name = argument;
    const bool header = name.size() > 7 && !name.compare(name.size() - 7, 7, "_header");
    if (header) name.resize(name.size() - 7);
    const std::string path = RecordPath(name.c_str());
    std::vector<std::string> lines;
    FILE* file = nullptr;
    if (!fopen_s(&file, path.c_str(), "r") && file) {
        char text[256];
        while (fgets(text, sizeof(text), file)) lines.emplace_back(text);
        fclose(file);
    }
    size_t first = 0, stacks = 0;
    for (size_t i = 0; i < lines.size(); ++i)
        if (!lines[i].compare(0, 6, "stack ")) { if (!stacks++) first = i; }
    const size_t keep = header ? 1 : stacks ? first + stacks / 2 : lines.size() > 1 ? lines.size() - 1 : 0;
    bool ok = !lines.empty() && !fopen_s(&file, path.c_str(), "w") && file;
    if (ok) {
        for (size_t i = 0; i < keep && i < lines.size(); ++i) fputs(lines[i].c_str(), file);
        ok = fclose(file) == 0;
    }
    char line[300];
    sprintf_s(line, "HEROCUT name=%s ok=%d kept=%zu of=%zu", name.substr(0, 64).c_str(), ok ? 1 : 0, keep, lines.size());
    log(line);
}

void CheckOff(LogFn log)
{
    checking = false;
    log("RECORDCHECKOFF ok=1 (records are applied as read)");
}

void WriteFault(LogFn log)
{
    writeFault = true;
    log("RECORDWRITEFAULT ok=1 (the next record write breaks off)");
}
}
