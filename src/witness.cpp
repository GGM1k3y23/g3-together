// SPDX-License-Identifier: GPL-3.0-or-later
#include "witness.h"

#include "ai.h"
#include "enclaves.h"
#include "log.h"
#include "net.h"
#include "npcs.h"
#include "remote.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace g3t::witness {
namespace {
// AssessTarget (Script_Game.dll 0x31470) is where a witness decides: AssessTakeItem calls it directly with the attack reason
// Theft (C re/findings/social-crime-identity-c.md 2.), as other perceptions do with theirs; it remembers the crime
// (MemorizePlayerCrime, only for the local player) and sets the NPC's target and task. A mirror's task does not run (ai.cpp),
// so its machine never saw the reaction. The table entry alone would miss the direct calls: jump from the function itself.
constexpr uintptr_t AssessRva = 0x31470;
gFScript originalAssess = nullptr;
bool installed = false, native = false;
DWORD lastTry = 0;
bool enabled = true;  // QA negative control: witnessoff
int depth = 0;        // a reaction applied here (OnWitness) is not passed on again
unsigned seen = 0, sent = 0, applied = 0, refused = 0, holdsSent = 0, holdsApplied = 0, judgesSent = 0, judgesApplied = 0;
// AssessMurder (Script_Game.dll 0x2eee0): SendPerceptionMurder (0x32720, self = the dying NPC, its CurrentAttacker the
// killer) asks every NPC that perceives it; the witness flees, or judges the killer (AssessTarget with reason Murder, or
// Livestock for a farm animal). DoLogicalDamage sends it where its hit judgement (AssessHit) says Die, LieDead or
// LiePiercedDead, OnAI_FinishingAttack with the finishing blow (re/out/p7-witness-b.c, scriptgame-damage.c).
constexpr uintptr_t MurderRva = 0x2eee0;
gFScript originalMurder = nullptr, originalSendMurder = nullptr;
unsigned murdersSent = 0, murderAsked = 0, murderReacted = 0;
bool seenEnabled = true;  // QA negative control: murderseenoff
unsigned hitsSeen = 0, knockOutsSeen = 0, murdersSeen = 0;

bool CrimeReason(GEInt reason)
{
    return reason == gEAttackReason_Theft || reason == gEAttackReason_UseInteractObject || reason == gEAttackReason_Murder;
}

// What a witness of a death here judges another player's puppet for (AssessMurder): that player's machine never saw it.
bool KillReason(GEInt reason) { return reason == gEAttackReason_Murder || reason == gEAttackReason_Livestock; }

// The game's own member, not the script wrapper (which looks properties up by name; see infocmd.cpp).
int LastCrime(eCEntity* npc)
{
    auto* ps = static_cast<gCNPC_PS*>(npc->GetPropertySet(eEPropertySetType_NPC));
    return static_cast<int>(static_cast<gEPlayerCrime const&>(ps->AccessLastPlayerCrime()));
}

void SetLastCrime(eCEntity* npc, int crime)
{
    static_cast<gCNPC_PS*>(npc->GetPropertySet(eEPropertySetType_NPC))->AccessLastPlayerCrime() = static_cast<gEPlayerCrime>(crime);
}

void GuidOf(eCEntity* entity, uint8_t out[16])
{
    std::memset(out, 0, 16);
    if (!entity) return;
    const eCEntityProxy proxy(entity);
    std::memcpy(out, &proxy.GetEntityID().GetCore(), 16);
}

// This build: push ebp; mov ebp,esp; and esp,-8; sub esp,0x154 (re/out/npc-spells-detail-c.asm).
unsigned char* NativeAssess()
{
    static const unsigned char expected[] = {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8, 0x81, 0xec, 0x54, 0x01, 0x00, 0x00};
    HMODULE module = GetModuleHandleA("Script_Game.dll");
    if (!module) return nullptr;
    auto* code = reinterpret_cast<unsigned char*>(module) + AssessRva;
    return std::memcmp(code, expected, sizeof(expected)) == 0 ? code : nullptr;
}

// The scripts' common prologue in this build: push ebp; mov ebp,esp; and esp,-8; sub esp,<frame>.
unsigned char* NativeAt(uintptr_t rva, uint16_t frame)
{
    const unsigned char expected[] = {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8, 0x81, 0xec, static_cast<unsigned char>(frame), static_cast<unsigned char>(frame >> 8), 0x00, 0x00};
    HMODULE module = GetModuleHandleA("Script_Game.dll");
    if (!module) return nullptr;
    auto* code = reinterpret_cast<unsigned char*>(module) + rva;
    return std::memcmp(code, expected, sizeof(expected)) == 0 ? code : nullptr;
}

char const* Who(eCEntity* entity)
{
    if (!entity) return "none";
    if (entity == gCSession::GetInstance().GetHero()) return "hero";
    if (remote::IsPuppet(entity)) return "puppet";
    return npcs::IsMirrored(entity) ? "mirror" : "npc";
}

GEInt GE_STDCALL SendMurder(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    ++murdersSent;
    return originalSendMurder(spu, self, other, args);
}

// A puppet perceives nothing: its player's own game judges what that player sees.
GEInt GE_STDCALL Murder(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    eCEntity* npc = self ? static_cast<Entity*>(self)->GetInstance() : (spu ? spu->GetSelfEntity() : nullptr);
    eCEntity* victim = other ? static_cast<Entity*>(other)->GetInstance() : (spu ? spu->GetOtherEntity() : nullptr);
    if (npc && remote::IsPuppet(npc)) return 0;
    const GEInt result = originalMurder(spu, self, other, args);
    ++murderAsked;
    if (!result || !npc || !victim || !npc->GetPropertySet(eEPropertySetType_NPC)) return result;
    ++murderReacted;
    Entity witness(npc), dead(victim);
    Entity killer = victim->GetPropertySet(eEPropertySetType_NPC) ? dead.NPC.GetCurrentAttacker() : Entity();
    Logf("witness: %s (%s) saw %s die, killer %s (%s): task %s, target %s", witness.GetName().GetText(), Who(npc), dead.GetName().GetText(),
         killer.GetInstance() ? killer.GetName().GetText() : "-", Who(killer.GetInstance()), witness.Routine.GetCurrentTask().GetText(),
         witness.NPC.GetCurrentTarget().GetInstance() ? witness.NPC.GetCurrentTarget().GetName().GetText() : "-");
    return result;
}

GEInt GE_STDCALL Assess(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    if (!depth && enabled && KillReason(args)) {
        // A witness here judged another player's puppet for a killing this machine applied (MurderSeen): the game
        // remembers a crime only against its local player (MemorizePlayerCrime), so the killer's machine judges its hero.
        eCEntity* npc = self ? static_cast<Entity*>(self)->GetInstance() : (spu ? spu->GetSelfEntity() : nullptr);
        eCEntity* target = other ? static_cast<Entity*>(other)->GetInstance() : (spu ? spu->GetOtherEntity() : nullptr);
        const int player = target ? remote::PuppetOwner(target) : 0;
        if (player && npc && npc->GetPropertySet(eEPropertySetType_NPC) && net::Connected()) {
            const GEInt result = originalAssess(spu, self, other, args);
            net::WitnessNote note;
            note.op = net::WitnessJudge;
            GuidOf(npc, note.npc);
            note.value = args;
            net::SendWitness(static_cast<uint8_t>(player), note);
            ++judgesSent;
            Logf("witness: %s judged player %d's puppet (reason %d): task %s; that player's machine judges its hero", Entity(npc).GetName().GetText(),
                 player, args, Entity(npc).Routine.GetCurrentTask().GetText());
            return result;
        }
    }
    if (depth || !enabled || !CrimeReason(args)) return originalAssess(spu, self, other, args);
    eCEntity* npc = self ? static_cast<Entity*>(self)->GetInstance() : (spu ? spu->GetSelfEntity() : nullptr);
    eCEntity* target = other ? static_cast<Entity*>(other)->GetInstance() : (spu ? spu->GetOtherEntity() : nullptr);
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!npc || !hero || target != hero || !net::Connected() || !npc->GetPropertySet(eEPropertySetType_NPC))
        return originalAssess(spu, self, other, args);
    Entity witness(npc);
    const int before = LastCrime(npc);
    const GEInt result = originalAssess(spu, self, other, args);
    ++seen;
    net::WitnessNote note;
    GuidOf(npc, note.npc);
    if (const uint8_t owner = npcs::OwnerOf(npc)) {
        note.op = net::WitnessSaw;
        note.value = args;
        net::SendWitness(owner, note);
        ++sent;
        Logf("witness: %s saw this player's crime (reason %d); player %u runs it and lets it react", witness.GetName().GetText(), args, owner);
    }
    const int after = LastCrime(npc);
    if (enclaves::Together() && after > before) {
        note.op = net::WitnessHolds;
        note.value = after;
        net::SendWitness(0, note);
        ++holdsSent;
        Logf("witness: %s holds crime %d against this player; together: against everyone's", witness.GetName().GetText(), after);
    }
    return result;
}

void OnWitness(uint8_t from, net::WitnessNote const& note)
{
    eCEntity* npc = npcs::NpcByGuid(note.npc);
    if (!npc || !npc->GetPropertySet(eEPropertySetType_NPC)) return;
    Entity witness(npc);
    if (note.op == net::WitnessSaw) {
        eCEntity* puppet = remote::PuppetOf(from);
        if (!enabled || !puppet || npcs::IsMirrored(npc) || witness.IsDead() || witness.IsDown()) {
            ++refused;
            Logf("witness: %s saw player %u's crime, but it does not react here (%s)", witness.GetName().GetText(), from,
                 !enabled ? "off" : !puppet ? "no puppet" : npcs::IsMirrored(npc) ? "not run here" : "down");
            return;
        }
        ++depth;
        const GEInt result = GetScriptAdminExt().CallScript(bCString("AssessTarget"), npc, puppet, note.value);
        --depth;
        ++applied;
        Logf("witness: %s saw player %u's crime (reason %d); it reacts here: result %d, task %s", witness.GetName().GetText(), from,
             note.value, result, witness.Routine.GetCurrentTask().GetText());
    } else if (note.op == net::WitnessJudge) {
        eCEntity* hero = gCSession::GetInstance().GetHero();
        if (!enabled || !hero || !KillReason(note.value) || witness.IsDead()) {
            ++refused;
            return;
        }
        // As if it had judged this player itself: it remembers the crime, and reacts if it runs here (a mirror's task does
        // not run). Not passed on again (depth); together: what it now holds goes to the others as for a theft.
        const int before = LastCrime(npc);
        ++depth;
        const GEInt result = GetScriptAdminExt().CallScript(bCString("AssessTarget"), npc, hero, note.value);
        --depth;
        ++judgesApplied;
        const int after = LastCrime(npc);
        Logf("witness: %s judged this player (reason %d) on player %u's machine: result %d, crime %d -> %d, task %s%s", witness.GetName().GetText(),
             note.value, from, result, before, after, witness.Routine.GetCurrentTask().GetText(), npcs::IsMirrored(npc) ? " (mirror)" : "");
        if (enclaves::Together() && after > before) {
            net::WitnessNote hold;
            hold.op = net::WitnessHolds;
            std::memcpy(hold.npc, note.npc, 16);
            hold.value = after;
            net::SendWitness(0, hold);
            ++holdsSent;
        }
    } else if (note.op == net::WitnessHolds) {
        if (note.value > LastCrime(npc) && note.value < gEPlayerCrime_Count) SetLastCrime(npc, note.value);
        ++holdsApplied;
        Logf("witness: %s holds crime %d against player %u; together: against this player too", witness.GetName().GetText(), note.value, from);
    }
}
}

namespace {
// The victim's CurrentAttacker is whom the game's perceptions name (SendPerceptionFightSound and AssessMurder read it):
// the puppet, which remote.cpp clears again when it goes.
void Send(char const* script, eCEntity* victim, eCEntity* attacker, GEInt args)
{
    Entity target(victim);
    target.NPC.SetCurrentAttacker(Entity(attacker));
    GetScriptAdminExt().CallScript(bCString(script), victim, nullptr, args);
}

bool Seen(eCEntity* victim, eCEntity* attacker)
{
    return seenEnabled && victim && attacker && attacker != victim && victim->GetPropertySet(eEPropertySetType_NPC) &&
           gCSession::GetInstance().IsGameRunning();
}
}

void HitSeen(eCEntity* victim, eCEntity* attacker)
{
    if (!Seen(victim, attacker)) return;
    Send("SendPerceptionFightSound", victim, attacker, gEAction_Stumble);  // the reaction most hits get (AssessHit)
    ++hitsSeen;
}

void KnockOutSeen(eCEntity* victim, eCEntity* attacker)
{
    if (!Seen(victim, attacker)) return;
    Send("SendPerceptionDefeat", victim, attacker, 0);
    ++knockOutsSeen;
}

void MurderSeen(eCEntity* victim, eCEntity* attacker)
{
    if (!Seen(victim, attacker)) return;
    // As OnAI_FinishingAttack: the killer as CurrentAttacker, the perception, then death. The one before afterwards: the
    // death states keep their killer, and a puppet that goes later must not be among them (combat.cpp, stage E2).
    Entity target(victim);
    const Entity before = target.NPC.GetCurrentAttacker();
    const unsigned reacted = murderReacted;
    Send("SendPerceptionMurder", victim, attacker, 0);
    target.NPC.SetCurrentAttacker(before);
    ++murdersSeen;
    Logf("witness: the murder of %s by %s (player %d) is seen here: %u witnesses react", target.GetName().GetText(),
         Entity(attacker).GetName().GetText(), remote::PuppetOwner(attacker), murderReacted - reacted);
}

void SeenOff(LogFn log)
{
    seenEnabled = false;
    log("MURDERSEENOFF ok=1");
}

void Start() { net::SetWitnessHandler(&OnWitness); }

void Tick()
{
    if (installed || !gCSession::GetInstance().IsGameRunning()) return;
    const DWORD now = GetTickCount();
    if (now - lastTry < 1000) return;
    lastTry = now;
    gSScript* script = nullptr;
    if (!GetScriptAdminExt().GetScripts().GetAt(bCString("AssessTarget"), script) || !script || !script->m_funcScript) return;
    if (unsigned char* code = NativeAssess(); code && reinterpret_cast<void*>(script->m_funcScript) == code) {
        originalAssess = reinterpret_cast<gFScript>(ai::JumpTo(code, reinterpret_cast<void*>(&Assess)));
        native = originalAssess != nullptr;
    }
    if (!native) {  // another build: calls by name only (perceptions call it directly and are missed)
        originalAssess = script->m_funcScript;
        script->m_funcScript = &Assess;
    }
    installed = true;
    Logf("witness: AssessTarget wrapped (%s)", native ? "native jump" : "script table only");
    if (unsigned char* code = NativeAt(MurderRva, 0x5f4)) originalMurder = reinterpret_cast<gFScript>(ai::JumpTo(code, reinterpret_cast<void*>(&Murder)));
    if (unsigned char* code = NativeAt(0x32720, 0x204)) originalSendMurder = reinterpret_cast<gFScript>(ai::JumpTo(code, reinterpret_cast<void*>(&SendMurder)));
    Logf("witness: AssessMurder %s, SendPerceptionMurder %s", originalMurder ? "wrapped" : "not wrapped (other build)",
         originalSendMurder ? "counted" : "not counted (other build)");
}

void Report(LogFn log)
{
    char line[400];
    sprintf_s(line, "WITNESS enabled=%d installed=%d native=%d together=%d seen=%u sent=%u applied=%u refused=%u holds_sent=%u holds_applied=%u judges_sent=%u judges_applied=%u"
              " seen_on=%d hits_seen=%u knockouts_seen=%u murders_seen=%u murder_sent=%u murder_asked=%u murder_reacted=%u",
              enabled ? 1 : 0, installed ? 1 : 0, native ? 1 : 0, enclaves::Together() ? 1 : 0, seen, sent, applied, refused, holdsSent,
              holdsApplied, judgesSent, judgesApplied, seenEnabled ? 1 : 0, hitsSeen, knockOutsSeen, murdersSeen, murdersSent, murderAsked,
              murderReacted);
    log(line);
}

void Off(LogFn log)
{
    enabled = false;
    log("WITNESSOFF ok=1");
}

// The last step of the game's own judgement (AssessTakeItem ends in AssessTarget(witness, thief, Theft)).
void Saw(char const* argument, LogFn log)
{
    char line[300], name[96] = {};
    char const* sep = std::strrchr(argument, '_');
    if (!sep || sep - argument >= static_cast<ptrdiff_t>(sizeof(name))) { log("REFUSED witness: Name_Reason"); return; }
    std::memcpy(name, argument, sep - argument);
    const int reason = std::atoi(sep + 1);
    Entity npc = Entity::GetEntity(bCString(name));
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!npc.GetInstance() || !hero) { log("REFUSED witness: no such NPC or no hero"); return; }
    const unsigned sentBefore = sent;
    const GEInt result = GetScriptAdminExt().CallScript(bCString("AssessTarget"), npc.GetInstance(), hero, reason);
    sprintf_s(line, "WITNESSSAW name=%s reason=%d result=%d passed_on=%d mirrored=%d last_crime=%d", name, reason, result, sent != sentBefore ? 1 : 0,
              npcs::IsMirrored(npc.GetInstance()) ? 1 : 0, npc.GetInstance()->GetPropertySet(eEPropertySetType_NPC) ? LastCrime(npc.GetInstance()) : -1);
    log(line);
}
}
