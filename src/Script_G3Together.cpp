// SPDX-License-Identifier: GPL-3.0-or-later
// G3 Together: co-op for Gothic 3, built on the unofficial Gothic 3 SDK.
// Runs once per game frame on the engine thread: reports the local hero, exchanges player states
// with the other players and shows them as puppets.
#include "ai.h"
#include "combat.h"
#include "hitprobe.h"
#include "net.h"
#include "story.h"
#include "talk.h"
#include "downed.h"
#include "range.h"
#include "journal.h"
#include "persist.h"
#include "clock.h"
#include "infocmd.h"
#include "partner.h"
#include "items.h"
#include "containers.h"
#include "config.h"
#include "trace.h"
#include "log.h"
#include "npcs.h"
#include "qa.h"
#include "remote.h"
#include "summon.h"

#include <Game.h>
#include <Windows.h>
#include <cmath>
#include <cstdio>

namespace {
unsigned long long frames = 0;
DWORD lastReport = 0;
bool faulted = false;

// Yaw in degrees around the up axis (Y in Gothic 3).
float Yaw(bCQuaternion const& q)
{
    const float x = q.GetX(), y = q.GetY(), z = q.GetZ(), w = q.GetW();
    return std::atan2(2.0f * (w * y + x * z), 1.0f - 2.0f * (y * y + x * x)) * 57.2957795f;
}

void Report()
{
    gCSession& session = gCSession::GetInstance();
    gCWorld* world = gCWorld::GetCurrentWorld();
    eCDynamicEntity* hero = session.GetHero();
    const bCString worldName = world ? world->GetFileBaseName() : bCString("none");
    if (!hero || !session.IsGameRunning()) {
        g3t::Logf("frame=%llu world=%s running=%d hero=none", frames, worldName.GetText(), session.IsGameRunning());
        return;
    }
    const bCVector& p = hero->GetWorldPosition();
    g3t::Logf("frame=%llu world=%s running=1 hero=%s pos=%.1f,%.1f,%.1f yaw=%.1f", frames, worldName.GetText(),
              hero->GetName().GetText(), p.GetX(), p.GetY(), p.GetZ(), Yaw(hero->GetWorldRotation()));
}

void Frame()
{
    if (faulted) return;
    __try {
        ++frames;
        const DWORD now = GetTickCount();
        if (now - lastReport >= 1000) {
            lastReport = now;
            Report();
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        faulted = true;
        g3t::Log("FAIL exception in frame callback; mod disabled for this session");
    }
}

// The engine's own extension point: a module registered with eCModuleAdmin gets Process()
// every frame and world activation/shutdown callbacks.
// Every virtual is overridden and no base version is called: the base versions log "not implemented"
// through the engine's property type registry, which knows nothing about this class. In engine shutdown
// that lookup crashed the game (Engine.dll eCEngineComponentBase::PreShutdown).
class Module : public eCEngineComponentBase
{
  public:
    bCGuid const& GetID() const override
    {
        // {6F3A1C52-8E2B-4D7A-9B51-3C0E2A7F4D19}
        static const _GUID raw = {0x6f3a1c52, 0x8e2b, 0x4d7a, {0x9b, 0x51, 0x3c, 0x0e, 0x2a, 0x7f, 0x4d, 0x19}};
        static const bCGuid id(raw);
        return id;
    }
    bEResult Initialize() override { return bEResult_Ok; }
    bEResult PostInitialize() override { return bEResult_Ok; }
    void OnAppInitialized() override {}
    // Forget every entity the mod holds. Loading a save (gCHUDFileManager::LoadGame) replaces the world's
    // entities and only activates the world again, without deactivating it first: the puppets kept from before
    // were deleted and the next frame crashed on them. So forget on both.
    static void ForgetWorld()
    {
        g3t::remote::OnWorldDeactivate();
        g3t::npcs::OnWorldDeactivate();
        g3t::talk::OnWorldChange();
        g3t::journal::OnWorldChange();
        g3t::infocmd::OnWorldChange();
    }
    bEResult WorldActivate() override
    {
        g3t::Log("world activated");
        ForgetWorld();
        return bEResult_Ok;
    }
    bEResult WorldDeactivate() override
    {
        g3t::Log("world deactivated");
        ForgetWorld();
        return bEResult_Ok;
    }
    bEResult PreShutdown() override
    {
        g3t::net::Stop();
        const GEBool removed = eCModuleAdmin::GetInstance().UnregisterModule(*this);
        g3t::Logf("pre-shutdown: module unregistered=%d", removed);
        return bEResult_Ok;
    }
    bEResult Shutdown() override { return bEResult_Ok; }
    bEResult ResetResources(GEBool) override { return bEResult_Ok; }
    bEResult RestoreResources(GEBool) override { return bEResult_Ok; }
    void Process() override
    {
        Frame();
        g3t::remote::Tick();
        g3t::npcs::Tick();
        g3t::ai::Tick();
        g3t::combat::Tick();
        g3t::summon::Tick();
        g3t::hitprobe::Tick();
        g3t::story::Tick();
        g3t::talk::Tick();
        g3t::downed::Tick();
        g3t::range::Tick();
        g3t::partner::Tick();
        g3t::journal::Tick();
        g3t::persist::Tick();
        g3t::clock::Tick();
        g3t::infocmd::Tick();
        g3t::containers::Tick();
        g3t::qa::Tick();
    }
    void PostProcess() override {}
    void OnBeginRenderSolid() override {}
    void OnEndRenderSolid() override {}
    void OnBeginRenderAlpha() override {}
    void OnEndRenderAlpha() override {}
    void OnPostRender() override {}
    void CreateMenu(eCMenu*) override {}
    void GetDebugLines(bTObjArray<eSModuleDebugLine>&) const override {}
};

Module* module = nullptr;
}

extern "C" __declspec(dllexport) gSScriptInit const* GE_STDCALL ScriptInit()
{
    static gSScriptInit scripts;
    g3t::OpenLog("g3together.log");
    g3t::qa::Init();
    g3t::LoadConfig();
    g3t::trace::Configure(g3t::GetConfig().trace);
    g3t::remote::Start();
    g3t::npcs::Start();
    g3t::combat::Start();
    g3t::summon::Start();
    g3t::story::Start();
    g3t::talk::Start();
    g3t::items::Start();
    g3t::containers::Start();
    g3t::journal::Start();
    g3t::persist::Install();
    g3t::clock::Start();
    g3t::infocmd::Start();
    // The game unloads script DLLs before eCModuleAdmin::DoShutdownModules calls every registered
    // module, which then jumped into unmapped memory. Keep this DLL loaded until the process ends.
    HMODULE pinned = nullptr;
    const BOOL pin = GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
                                        reinterpret_cast<LPCSTR>(&ScriptInit), &pinned);
    if (!pin) g3t::Log("WARN could not pin the mod DLL; engine shutdown may crash");
    module = new Module;
    module->Enable(GETrue);
    const GEBool registered = eCModuleAdmin::GetInstance().RegisterModule(*module);
    g3t::Logf("READY G3 Together stage 1: module registered=%d enabled=%d", registered, module->IsEnabled());
    return &scripts;
}
