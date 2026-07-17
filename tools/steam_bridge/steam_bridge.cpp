// BattleSpades legacy SteamGameServer011 bridge.
//
// Ace of Spades shipped a 32-bit Steamworks runtime while BattleSpades ships
// native 64-bit servers.  This helper owns the old DLL, callbacks, query port,
// and master heartbeats.  The server talks to it over a tiny line protocol so
// a Steam crash or outage cannot corrupt authoritative gameplay state.

#include <windows.h>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

namespace {

constexpr std::size_t kSetProduct = 4;
constexpr std::size_t kSetGameDescription = 8;
constexpr std::size_t kSetModDir = 12;
constexpr std::size_t kSetDedicatedServer = 16;
constexpr std::size_t kLogOnAnonymous = 24;
constexpr std::size_t kLogOff = 28;
constexpr std::size_t kBLoggedOn = 32;
constexpr std::size_t kBSecure = 36;
constexpr std::size_t kGetSteamId = 40;
constexpr std::size_t kSetMaxPlayerCount = 48;
constexpr std::size_t kSetBotPlayerCount = 52;
constexpr std::size_t kSetServerName = 56;
constexpr std::size_t kSetMapName = 60;
constexpr std::size_t kSetPasswordProtected = 64;
constexpr std::size_t kSetGameTags = 84;
constexpr std::size_t kSetRegion = 92;
constexpr std::size_t kGetPublicIp = 144;
constexpr std::size_t kEnableHeartbeats = 156;
constexpr std::size_t kForceHeartbeat = 164;

using SteamGameServerInitFn = bool(__cdecl *)(
    std::uint32_t,
    std::uint16_t,
    std::uint16_t,
    std::uint16_t,
    int,
    const char *);
using SteamGameServerFn = void *(__cdecl *)();
using SteamGameServerRunCallbacksFn = void(__cdecl *)();
using SteamGameServerShutdownFn = void(__cdecl *)();

template <typename Return, typename... Args>
Return VCall(void *object, std::size_t byte_offset, Args... args) {
    auto **vtable = *reinterpret_cast<void ***>(object);
    auto function = reinterpret_cast<Return(__thiscall *)(void *, Args...)>(
        vtable[byte_offset / sizeof(void *)]);
    return function(object, args...);
}

std::string Narrow(const std::wstring &value) {
    if (value.empty()) {
        return {};
    }
    const int size = WideCharToMultiByte(
        CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0,
        nullptr, nullptr);
    std::string result(static_cast<std::size_t>(size), '\0');
    WideCharToMultiByte(
        CP_UTF8, 0, value.data(), static_cast<int>(value.size()), result.data(),
        size, nullptr, nullptr);
    return result;
}

std::vector<std::string> Split(const std::string &line, char separator) {
    std::vector<std::string> fields;
    std::size_t begin = 0;
    while (true) {
        const std::size_t end = line.find(separator, begin);
        if (end == std::string::npos) {
            fields.emplace_back(line.substr(begin));
            return fields;
        }
        fields.emplace_back(line.substr(begin, end - begin));
        begin = end + 1;
    }
}

int Base64Value(unsigned char value) {
    if (value >= 'A' && value <= 'Z') return value - 'A';
    if (value >= 'a' && value <= 'z') return value - 'a' + 26;
    if (value >= '0' && value <= '9') return value - '0' + 52;
    if (value == '+') return 62;
    if (value == '/') return 63;
    return -1;
}

bool DecodeBase64(const std::string &encoded, std::string *output) {
    output->clear();
    std::uint32_t accumulator = 0;
    int bits = 0;
    for (const unsigned char byte : encoded) {
        if (byte == '=') break;
        const int value = Base64Value(byte);
        if (value < 0) return false;
        accumulator = (accumulator << 6) | static_cast<std::uint32_t>(value);
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            output->push_back(static_cast<char>((accumulator >> bits) & 0xff));
            accumulator &= bits == 0 ? 0u : ((1u << bits) - 1u);
        }
    }
    return true;
}

struct Advertisement {
    std::string name;
    std::string map;
    int max_players = 24;
    int players = 0;
    std::string tags;
    std::string region;
};

bool ParseSet(const std::vector<std::string> &fields, Advertisement *value) {
    if (fields.size() != 7) return false;
    Advertisement parsed;
    if (!DecodeBase64(fields[1], &parsed.name) ||
        !DecodeBase64(fields[2], &parsed.map) ||
        !DecodeBase64(fields[5], &parsed.tags) ||
        !DecodeBase64(fields[6], &parsed.region)) {
        return false;
    }
    try {
        parsed.max_players = std::stoi(fields[3]);
        parsed.players = std::stoi(fields[4]);
    } catch (...) {
        return false;
    }
    *value = std::move(parsed);
    return true;
}

void ApplyAdvertisement(void *server, const Advertisement &value) {
    VCall<void, const char *>(server, kSetServerName, value.name.c_str());
    VCall<void, const char *>(server, kSetMapName, value.map.c_str());
    VCall<void, int>(server, kSetMaxPlayerCount, value.max_players);
    // GameServer011 derives authenticated humans from Steam sessions.  The
    // revived protocol does not yet authenticate tickets, so use this field
    // for total occupancy to keep the browser's players/max display accurate.
    VCall<void, int>(server, kSetBotPlayerCount, value.players);
    VCall<void, bool>(server, kSetPasswordProtected, false);
    VCall<void, const char *>(server, kSetGameTags, value.tags.c_str());
    if (!value.region.empty()) {
        VCall<void, const char *>(server, kSetRegion, value.region.c_str());
    }
}

std::uint64_t GetSteamId(void *server) {
    std::uint64_t value = 0;
    // CSteamID is returned through a hidden pointer in this 32-bit MSVC ABI;
    // this exact call shape is visible in the retail steam.pyd wrapper.
    VCall<void, std::uint64_t *>(server, kGetSteamId, &value);
    return value;
}

template <typename Function>
Function Import(HMODULE module, const char *name) {
    return reinterpret_cast<Function>(GetProcAddress(module, name));
}

void EmitFatal(const std::string &code, const std::string &message) {
    std::cout << "FATAL\t" << code << "\t" << message << std::endl;
}

}  // namespace

int wmain(int argc, wchar_t **argv) {
    static_assert(sizeof(void *) == 4, "Build this bridge for Win32/x86");
    SetConsoleOutputCP(CP_UTF8);

    std::unordered_map<std::wstring, std::wstring> options;
    for (int index = 1; index + 1 < argc; index += 2) {
        options[argv[index]] = argv[index + 1];
    }
    const wchar_t *required[] = {
        L"--runtime", L"--app-id", L"--steam-port", L"--game-port",
        L"--query-port", L"--server-mode", L"--version", L"--name-b64",
        L"--map-b64", L"--max-players", L"--players", L"--tags-b64",
        L"--region-b64",
    };
    for (const wchar_t *key : required) {
        if (!options.count(key)) {
            EmitFatal("arguments", "missing required option");
            return 2;
        }
    }

    int app_id = 0;
    int steam_port = 0;
    int game_port = 0;
    int query_port = 0;
    int server_mode = 0;
    Advertisement initial;
    try {
        app_id = std::stoi(options[L"--app-id"]);
        steam_port = std::stoi(options[L"--steam-port"]);
        game_port = std::stoi(options[L"--game-port"]);
        query_port = std::stoi(options[L"--query-port"]);
        server_mode = std::stoi(options[L"--server-mode"]);
        initial.max_players = std::stoi(options[L"--max-players"]);
        initial.players = std::stoi(options[L"--players"]);
    } catch (...) {
        EmitFatal("arguments", "numeric option is malformed");
        return 2;
    }
    if (!DecodeBase64(Narrow(options[L"--name-b64"]), &initial.name) ||
        !DecodeBase64(Narrow(options[L"--map-b64"]), &initial.map) ||
        !DecodeBase64(Narrow(options[L"--tags-b64"]), &initial.tags) ||
        !DecodeBase64(Narrow(options[L"--region-b64"]), &initial.region)) {
        EmitFatal("arguments", "base64 option is malformed");
        return 2;
    }

    const std::wstring app_text = std::to_wstring(app_id);
    SetEnvironmentVariableW(L"SteamAppId", app_text.c_str());
    SetEnvironmentVariableW(L"SteamGameId", app_text.c_str());
    const std::wstring runtime = options[L"--runtime"];
    SetDllDirectoryW(runtime.c_str());
    std::wstring api_path = runtime;
    if (!api_path.empty() && api_path.back() != L'\\' && api_path.back() != L'/') {
        api_path.push_back(L'\\');
    }
    api_path += L"steam_api.dll";
    HMODULE module = LoadLibraryExW(
        api_path.c_str(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (module == nullptr) {
        EmitFatal("load_library", std::to_string(GetLastError()));
        return 3;
    }
    std::cout << "TRACE\tsteam_api_loaded" << std::endl;

    const auto initialize = Import<SteamGameServerInitFn>(
        module, "SteamGameServer_Init");
    const auto get_server = Import<SteamGameServerFn>(module, "SteamGameServer");
    const auto run_callbacks = Import<SteamGameServerRunCallbacksFn>(
        module, "SteamGameServer_RunCallbacks");
    const auto shutdown = Import<SteamGameServerShutdownFn>(
        module, "SteamGameServer_Shutdown");
    if (!initialize || !get_server || !run_callbacks || !shutdown) {
        EmitFatal("exports", "steam_api.dll lacks GameServer exports");
        FreeLibrary(module);
        return 3;
    }

    const std::string version = Narrow(options[L"--version"]);
    std::cout << "TRACE\tgame_server_init_begin" << std::endl;
    if (!initialize(
            0,
            static_cast<std::uint16_t>(steam_port),
            static_cast<std::uint16_t>(game_port),
            static_cast<std::uint16_t>(query_port),
            server_mode,
            version.c_str())) {
        EmitFatal("initialize", "SteamGameServer_Init returned false");
        FreeLibrary(module);
        return 4;
    }
    std::cout << "TRACE\tgame_server_init_complete" << std::endl;
    void *server = get_server();
    if (server == nullptr) {
        EmitFatal("interface", "SteamGameServer returned null");
        shutdown();
        FreeLibrary(module);
        return 4;
    }

    // Exact identity and order recovered from retail shared/steam.pyd.
    VCall<void, const char *>(server, kSetProduct, "aos");
    VCall<void, const char *>(
        server, kSetGameDescription, "Ace of Spades");
    VCall<void, const char *>(server, kSetModDir, "aceofspades");
    VCall<void, bool>(server, kSetDedicatedServer, true);
    ApplyAdvertisement(server, initial);
    VCall<void, bool>(server, kEnableHeartbeats, true);
    VCall<void>(server, kLogOnAnonymous);
    VCall<void, bool>(server, kSetPasswordProtected, false);
    std::cout << "READY\t1" << std::endl;

    std::atomic<bool> running{true};
    std::mutex command_mutex;
    std::string pending_set;
    std::thread reader([&]() {
        std::string line;
        while (std::getline(std::cin, line)) {
            if (line == "QUIT") {
                running.store(false);
                return;
            }
            if (line.rfind("SET\t", 0) == 0) {
                std::lock_guard<std::mutex> lock(command_mutex);
                pending_set = std::move(line);  // coalesce to the newest state
            }
        }
        running.store(false);
    });

    bool previous_logged_on = false;
    bool previous_secure = false;
    std::uint64_t previous_steam_id = 0;
    std::uint32_t previous_public_ip = 0;
    auto next_status = std::chrono::steady_clock::now();
    while (running.load()) {
        run_callbacks();
        std::string command;
        {
            std::lock_guard<std::mutex> lock(command_mutex);
            command.swap(pending_set);
        }
        if (!command.empty()) {
            Advertisement update;
            if (ParseSet(Split(command, '\t'), &update)) {
                initial = std::move(update);
                ApplyAdvertisement(server, initial);
                VCall<void>(server, kForceHeartbeat);
            } else {
                std::cerr << "malformed SET command" << std::endl;
            }
        }

        const bool logged_on = VCall<bool>(server, kBLoggedOn);
        const bool secure = VCall<bool>(server, kBSecure);
        const std::uint64_t steam_id = logged_on ? GetSteamId(server) : 0;
        const std::uint32_t public_ip = VCall<std::uint32_t>(server, kGetPublicIp);
        if (logged_on && !previous_logged_on) {
            // Metadata is installed before LogOnAnonymous, but the old
            // GameServer011 runtime may discard a forced heartbeat until its
            // backend connection has completed. Re-assert the advertisement
            // and force exactly one heartbeat on the disconnected->logged-on
            // edge so a quiet server does not wait for a later map/player
            // change before entering the public master list.
            ApplyAdvertisement(server, initial);
            VCall<void>(server, kForceHeartbeat);
        }
        const auto now = std::chrono::steady_clock::now();
        if (now >= next_status || logged_on != previous_logged_on ||
            secure != previous_secure || steam_id != previous_steam_id ||
            public_ip != previous_public_ip) {
            std::cout << "STATUS\t" << (logged_on ? 1 : 0) << '\t'
                      << (secure ? 1 : 0) << '\t' << steam_id << '\t'
                      << public_ip << std::endl;
            previous_logged_on = logged_on;
            previous_secure = secure;
            previous_steam_id = steam_id;
            previous_public_ip = public_ip;
            next_status = now + std::chrono::seconds(1);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }

    VCall<void, bool>(server, kEnableHeartbeats, false);
    VCall<void>(server, kLogOff);
    run_callbacks();
    shutdown();
    FreeLibrary(module);
    if (reader.joinable()) reader.join();
    std::cout << "STOPPED" << std::endl;
    return 0;
}
