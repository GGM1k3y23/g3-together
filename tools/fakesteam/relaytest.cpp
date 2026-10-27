// SPDX-License-Identifier: GPL-3.0-or-later
// The Steam transport without the game: net.cpp, steam.cpp and ENet in a small program, run twice (host and client) with
// the stand-in fake_steam_api.dll. Checks what the game relies on: the client finds the host among its "friends", ENet
// connects through the relay, the hello is welcomed, player states pass both ways, a 2 MB save transfer (file chunks,
// reliable, ENet fragments) arrives whole, and a client that quits is gone on the host at once.
//
//   set G3T_STEAM_DLL=<build>\fake_steam_api.dll
//   set G3T_FAKESTEAM_DIR=%TEMP%\g3t-relaytest
//   relaytest host 34600 60          (G3T_FAKESTEAM_ID=1)
//   relaytest client 34600 60        (G3T_FAKESTEAM_ID=2), optional 4th argument: steam_host (a friend's Steam name)
// Each prints RESULT: PASS or RESULT: FAIL with what was missing. Negative control: G3T_FAKESTEAM_INIT=2 on the client
// (Steam not running) makes it fall back to 127.0.0.1:<port+1>, where nobody listens: it must FAIL.
#include <winsock2.h>
#include <windows.h>

#include "config.h"
#include "log.h"
#include "net.h"
#include "parity.h"
#include "steam.h"

#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

namespace g3t {
namespace {
Config config;
char const* role = "?";
}
Config const& GetConfig() { return config; }
Config const& LoadConfig() { return config; }

int FormatV(char* out, size_t size, const char* format, va_list args)
{
    if (!size) return 0;
    const int n = std::vsnprintf(out, size, format, args);
    if (n < 0) { *out = '\0'; return 0; }
    return n < static_cast<int>(size) ? n : static_cast<int>(size) - 1;
}

int Format(char* out, size_t size, const char* format, ...)
{
    va_list args;
    va_start(args, format);
    const int n = FormatV(out, size, format, args);
    va_end(args);
    return n;
}

void Log(const char* text) { std::printf("[%s %6lu] %s\n", role, GetTickCount() % 1000000, text); std::fflush(stdout); }

void Logf(const char* format, ...)
{
    char line[1024];
    va_list args;
    va_start(args, format);
    FormatV(line, sizeof(line), format, args);
    va_end(args);
    Log(line);
}

namespace parity {
uint64_t const* Local()
{
    static uint64_t values[ItemCount] = {1, 2, 3, 4, 5, 6, 7};
    return values;
}
char const* ItemName(int) { return "test"; }
}
}

using namespace g3t;

namespace {
constexpr uint32_t SaveSize = 2u << 20;
constexpr uint16_t Chunk = 16000;
std::vector<uint8_t> received(SaveSize);
uint32_t receivedBytes = 0;
bool fileDone = false;
uint32_t doneCrc = 0;

uint32_t Crc(uint8_t const* data, size_t size)
{
    uint32_t crc = 0xffffffffu;
    for (size_t i = 0; i < size; ++i) {
        crc ^= data[i];
        for (int k = 0; k < 8; ++k) crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1)));
    }
    return ~crc;
}

void OnChunk(uint8_t, uint32_t total, uint32_t offset, uint8_t const* data, uint16_t length)
{
    if (total != SaveSize || offset + length > SaveSize) return;
    std::memcpy(received.data() + offset, data, length);
    receivedBytes += length;
}

void OnDone(char const*, uint32_t const size[net::FileCount], uint32_t const crc[net::FileCount])
{
    fileDone = size[0] == SaveSize;
    doneCrc = crc[0];
}

void PrintStatus(char const* line) { Log(line); }
}

int main(int argc, char** argv)
{
    if (argc < 4) {
        std::printf("usage: relaytest host|client <port> <seconds> [steam_host]\n");
        return 2;
    }
    const bool host = !std::strcmp(argv[1], "host");
    role = host ? "host" : "client";
    const unsigned short port = static_cast<unsigned short>(std::atoi(argv[2]));
    const uint32_t seconds = static_cast<uint32_t>(std::atoi(argv[3]));
    config.host = host;
    config.steam = true;
    if (argc > 4) strncpy_s(config.steamHost, argv[4], _TRUNCATE);
    std::vector<uint8_t> save(SaveSize);
    for (uint32_t i = 0; i < SaveSize; ++i) save[i] = static_cast<uint8_t>((i * 2654435761u) >> 13);
    const uint32_t saveCrc = Crc(save.data(), save.size());
    net::SetFileHandlers(OnChunk, OnDone);

    // The client's fallback address is a port nobody listens on: without Steam it must not reach the host.
    if (!net::Start(host, "127.0.0.1", host ? port : static_cast<unsigned short>(port + 1), role, true)) {
        Log("RESULT: FAIL (net::Start)");
        return 1;
    }
    bool sawPeer = false, sentSave = false, sawState = false, peerLeft = false, joined = false;
    uint32_t joinedAt = 0, leftAfter = 0;
    const uint32_t start = GetTickCount();
    float x = 0;
    while (GetTickCount() - start < seconds * 1000) {
        net::PlayerState local{};
        local.flags = 1;
        local.position[0] = x += 1.0f;
        net::Tick(local, true);
        if (host) {
            if (!sawPeer && net::PeerJoined(2)) {
                sawPeer = true;
                net::SetInWorld(2, true);
                Log("host: client 2 joined, sending the save");
            }
            if (sawPeer && !sentSave) {
                bool ok = true;
                for (uint32_t offset = 0; offset < SaveSize && ok; offset += Chunk) {
                    const uint16_t length = static_cast<uint16_t>(SaveSize - offset < Chunk ? SaveSize - offset : Chunk);
                    ok = net::SendFileChunk(2, 0, SaveSize, offset, save.data() + offset, length);
                }
                uint32_t sizes[net::FileCount] = {SaveSize}, crcs[net::FileCount] = {saveCrc};
                ok = ok && net::SendFileDone(2, "relaytest", sizes, crcs);
                sentSave = true;
                Logf("host: save of %u bytes queued (%s)", SaveSize, ok ? "ok" : "FAILED");
            }
            if (sawPeer && net::Remotes()[2].latest.position[0] > 0 && !sawState) {
                sawState = true;
                Logf("host: client state arrived (x = %.0f)", net::Remotes()[2].latest.position[0]);
            }
            if (sawState && !net::PeerJoined(2) && !peerLeft) {
                peerLeft = true;
                Log("host: client 2 is gone");
                break;
            }
        } else {
            if (!joined && net::Joined()) {
                joined = true;
                joinedAt = GetTickCount();
                net::SetInWorld(net::LocalId(), true);
                Logf("client: welcomed as player %u", net::LocalId());
            }
            if (joined && net::Remotes()[1].latest.position[0] > 0 && !sawState) {
                sawState = true;
                Logf("client: host state arrived (x = %.0f)", net::Remotes()[1].latest.position[0]);
            }
            if (fileDone && sawState) {
                leftAfter = GetTickCount() - joinedAt;
                break;
            }
        }
        Sleep(16);
    }
    steam::Status(PrintStatus);
    bool pass = false;
    if (host) {
        pass = sawPeer && sentSave && sawState && peerLeft;
        Logf("RESULT: %s (peer %d, save sent %d, client state %d, client left %d)", pass ? "PASS" : "FAIL", sawPeer, sentSave,
             sawState, peerLeft);
    } else {
        const uint32_t got = Crc(received.data(), received.size());
        const bool whole = fileDone && receivedBytes == SaveSize && got == saveCrc && doneCrc == saveCrc;
        pass = joined && sawState && whole && *steam::HostName();
        Logf("RESULT: %s (welcomed %d, host state %d, save %u of %u bytes, crc %s, host %s, %u ms after the welcome)",
             pass ? "PASS" : "FAIL", joined, sawState, receivedBytes, SaveSize, got == saveCrc ? "ok" : "WRONG",
             *steam::HostName() ? steam::HostName() : "-", leftAfter);
        // Wait for the host's last messages, then quit as a player does: the host must notice at once.
    }
    net::Stop();
    for (int i = 0; i < 20; ++i) { Sleep(10); }
    return pass ? 0 : 1;
}
