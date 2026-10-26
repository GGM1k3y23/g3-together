// SPDX-License-Identifier: GPL-3.0-or-later
#include "config.h"

#include "log.h"

#include <Windows.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace g3t {
namespace {
Config config;

void Trim(char* text)
{
    char* start = text;
    while (*start == ' ' || *start == '\t') ++start;
    std::memmove(text, start, std::strlen(start) + 1);
    for (size_t n = std::strlen(text); n && (text[n - 1] == ' ' || text[n - 1] == '\t' || text[n - 1] == '\r' || text[n - 1] == '\n'); --n)
        text[n - 1] = '\0';
    const size_t n = std::strlen(text);
    if (n >= 2 && text[0] == '"' && text[n - 1] == '"') {
        std::memmove(text, text + 1, n - 2);
        text[n - 2] = '\0';
    }
}
}

Config const& GetConfig() { return config; }

Config const& LoadConfig()
{
    char path[MAX_PATH] = {};
    HMODULE self = nullptr;
    if (!GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            reinterpret_cast<LPCSTR>(&LoadConfig), &self) ||
        !GetModuleFileNameA(self, path, MAX_PATH))
        return config;
    for (int part = 0; part != 2; ++part) {
        char* separator = std::strrchr(path, '\\');
        if (separator) *separator = '\0';
    }
    strcat_s(path, "\\g3together.ini");
    FILE* file = nullptr;
    if (fopen_s(&file, path, "r") || !file) {
        Log("config: g3together.ini not found, using defaults (host)");
        return config;
    }
    char line[256];
    while (fgets(line, sizeof(line), file)) {
        char* hash = std::strchr(line, '#');
        if (hash) *hash = '\0';
        char* equals = std::strchr(line, '=');
        if (!equals) continue;
        *equals = '\0';
        char key[64] = {}, value[192] = {};
        strncpy_s(key, line, _TRUNCATE);
        strncpy_s(value, equals + 1, _TRUNCATE);
        Trim(key);
        Trim(value);
        if (!_stricmp(key, "role")) config.host = _stricmp(value, "client") != 0;
        else if (!_stricmp(key, "address")) strncpy_s(config.address, value, _TRUNCATE);
        else if (!_stricmp(key, "port")) config.port = static_cast<unsigned short>(std::atoi(value));
        else if (!_stricmp(key, "name")) strncpy_s(config.name, value, _TRUNCATE);
        else if (!_stricmp(key, "language")) {
            if (!_stricmp(value, "de")) strcpy_s(config.language, "de");
            else if (!_stricmp(value, "pl")) strcpy_s(config.language, "pl");
            else if (!_stricmp(value, "auto")) strcpy_s(config.language, "auto");
            else {
                strcpy_s(config.language, "en");
                if (_stricmp(value, "en")) Logf("config: unknown language '%s', using en", value);
            }
        }
        else if (!_stricmp(key, "puppet_template")) strncpy_s(config.puppetTemplate, value, _TRUNCATE);
        else if (!_stricmp(key, "look")) strncpy_s(config.look, value, _TRUNCATE);
        else if (!_stricmp(key, "partner_key")) strncpy_s(config.partnerKey, value, _TRUNCATE);
        else if (!_stricmp(key, "offer_accept_key")) strncpy_s(config.offerAcceptKey, value, _TRUNCATE);
        else if (!_stricmp(key, "offer_decline_key")) strncpy_s(config.offerDeclineKey, value, _TRUNCATE);
        else if (!_stricmp(key, "qa_keycheck")) config.qaKeyCheckOff = !_stricmp(value, "off");
        else if (!_stricmp(key, "qa_hosthint")) strncpy_s(config.qaHostHint, value, _TRUNCATE);
        else if (!_stricmp(key, "trace")) strncpy_s(config.trace, value, _TRUNCATE);
        else if (!_stricmp(key, "reputation")) strncpy_s(config.reputation, value, _TRUNCATE);
    }
    fclose(file);
    Logf("config: role=%s address=%s port=%u name=%s puppet_template=%s look=%s language=%s", config.host ? "host" : "client",
         config.address, config.port, config.name, config.puppetTemplate, *config.look ? config.look : "-", config.language);
    return config;
}
}
