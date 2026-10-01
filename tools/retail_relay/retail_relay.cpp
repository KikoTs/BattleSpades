// SPDX-License-Identifier: AGPL-3.0-or-later
// See LICENSING.md for the additional Steamworks linking permission.
// Independent retail bridge. No dependencies on BattleSpadesClient.
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <steam/steam_api.h>
#include <steam/isteamnetworkingsockets.h>
#include <steam/isteamnetworkingutils.h>
#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
constexpr AppId_t app_id = 224540;
constexpr size_t max_packet = 65507;
constexpr size_t max_control = 262144;
constexpr const char* product = "aos-retail-relay-1";
using Clock = std::chrono::steady_clock;
using Fields = std::vector<std::string>;

uint64 number(const std::string& value, uint64 maximum) {
    if (value.empty() || value.size() > 20) throw std::runtime_error("invalid number");
    uint64 result = 0;
    for (char c : value) {
        if (c < '0' || c > '9' || result > maximum / 10) throw std::runtime_error("invalid number");
        result = result * 10 + static_cast<unsigned>(c - '0');
        if (result > maximum) throw std::runtime_error("number out of range");
    }
    return result;
}
bool valid_id(const std::string& value) {
    return value.size() == 32 && value.find_first_not_of("0123456789abcdef") == std::string::npos;
}
std::string hex(const std::string& value) {
    const char* digits = "0123456789abcdef";
    std::string result;
    for (unsigned char c : value) { result += digits[c >> 4]; result += digits[c & 15]; }
    return result;
}
std::string unhex(const std::string& value, size_t limit = 160) {
    if (value.size() % 2 || value.size() > limit * 2) throw std::runtime_error("invalid text length");
    auto digit = [](char c) -> unsigned {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        throw std::runtime_error("invalid hex");
    };
    std::string result;
    for (size_t i = 0; i < value.size(); i += 2) {
        char c = static_cast<char>((digit(value[i]) << 4) | digit(value[i + 1]));
        if (static_cast<unsigned char>(c) < 32 || c == 127) throw std::runtime_error("control character in text");
        result += c;
    }
    return result;
}
Fields split(const std::string& line) {
    Fields result;
    size_t begin = 0;
    while (true) {
        auto end = line.find('\t', begin);
        result.push_back(line.substr(begin, end == std::string::npos ? end : end - begin));
        if (result.size() > 24) throw std::runtime_error("too many fields");
        if (end == std::string::npos) return result;
        begin = end + 1;
    }
}
void nonblocking(SOCKET socket) {
    u_long enabled = 1;
    if (ioctlsocket(socket, FIONBIO, &enabled)) throw std::runtime_error("nonblocking socket failed");
}
sockaddr_in loopback(unsigned short port) {
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = htons(port);
    return address;
}
SOCKET udp(unsigned short destination, unsigned short& bound) {
    SOCKET socket = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (socket == INVALID_SOCKET) throw std::runtime_error("UDP socket failed");
    auto local = loopback(0);
    auto remote = loopback(destination);
    int length = sizeof(local);
    if (bind(socket, reinterpret_cast<sockaddr*>(&local), sizeof(local)) ||
        getsockname(socket, reinterpret_cast<sockaddr*>(&local), &length) ||
        (destination && connect(socket, reinterpret_cast<sockaddr*>(&remote), sizeof(remote)))) {
        closesocket(socket); throw std::runtime_error("UDP bind/connect failed");
    }
    try { nonblocking(socket); } catch (...) { closesocket(socket); throw; }
    bound = ntohs(local.sin_port);
    return socket;
}
struct Channel {
    SOCKET socket = INVALID_SOCKET;
    std::string input, output;
    ~Channel() { if (socket != INVALID_SOCKET) closesocket(socket); }
    void send(const Fields& fields) {
        std::string line;
        for (const auto& value : fields) {
            if (value.find_first_of("\r\n\t") != std::string::npos) throw std::runtime_error("bad control field");
            if (!line.empty()) line += '\t';
            line += value;
        }
        if (output.size() + line.size() > max_control) throw std::runtime_error("control consumer stalled");
        output += line + '\n';
    }
    bool pump(Fields& fields) {
        if (!output.empty()) {
            int sent = ::send(socket, output.data(), static_cast<int>(output.size()), 0);
            if (sent > 0) output.erase(0, sent);
            else if (sent == 0 || WSAGetLastError() != WSAEWOULDBLOCK) throw std::runtime_error("controller disconnected");
        }
        std::array<char, 4096> buffer{};
        for (int i = 0; i < 8; ++i) {
            int received = recv(socket, buffer.data(), static_cast<int>(buffer.size()), 0);
            if (received > 0) input.append(buffer.data(), received);
            else if (received == 0 || WSAGetLastError() != WSAEWOULDBLOCK) throw std::runtime_error("controller disconnected");
            else break;
            if (input.size() > 8192) throw std::runtime_error("control input too large");
        }
        auto end = input.find('\n');
        if (end == std::string::npos) return false;
        fields = split(input.substr(0, end)); input.erase(0, end + 1);
        return true;
    }
};

struct Metadata {
    std::string name, map, mode, skin;
    int maximum = 24, players = 0, password = 0, classic = 0;
    void parse(const Fields& f, size_t n) {
        if (f.size() != n + 8) throw std::runtime_error("bad metadata fields");
        name = unhex(f[n], 96); map = unhex(f[n+1], 96); mode = unhex(f[n+2], 16);
        maximum = static_cast<int>(number(f[n+3], 255));
        players = static_cast<int>(number(f[n+4], 255));
        password = static_cast<int>(number(f[n+5], 1)); classic = static_cast<int>(number(f[n+6], 1));
        skin = unhex(f[n+7], 32);
        if (name.empty() || map.empty() || mode.empty() || maximum < 1 || players > maximum)
            throw std::runtime_error("invalid server metadata");
    }
    Fields fields() const {
        return {hex(name), hex(map), hex(mode), std::to_string(maximum), std::to_string(players),
                std::to_string(password), std::to_string(classic), hex(skin)};
    }
};
struct Peer {
    SOCKET socket = INVALID_SOCKET;
    unsigned short port = 0;
    uint64 steam = 0;
    bool allowed = false, connected = false, hello = false;
    Clock::time_point seen = Clock::now();
    Clock::time_point created = Clock::now();
};

#ifdef AOS_RELAY_LOOPBACK_TEST
// Compiled into a separately named test executable, never the shipping ZIP.
SteamNetworkingIPAddr test_address() {
    char text[16]{};
    GetEnvironmentVariableA("AOS_RELAY_TEST_PORT",text,sizeof(text));
    SteamNetworkingIPAddr address{};
    address.SetIPv4(0x7f000001,static_cast<uint16>(number(text,65535)));
    if (!address.m_port) throw std::runtime_error("missing loopback test port");
    return address;
}
#endif

class Bridge {
    Channel& channel;
    ISteamNetworkingSockets* sockets = SteamNetworkingSockets();
    ISteamMatchmaking* matchmaking = SteamMatchmaking();
    CCallback<Bridge, SteamNetConnectionStatusChangedCallback_t> status_callback;
    CCallResult<Bridge, LobbyCreated_t> created_callback;
    CCallResult<Bridge, LobbyMatchList_t> list_callback;
    std::map<HSteamNetConnection, Peer> peers;
    HSteamListenSocket listener = k_HSteamListenSocket_Invalid;
    HSteamNetConnection joining = k_HSteamNetConnection_Invalid;
    SOCKET game_socket = INVALID_SOCKET;
    unsigned short game_port = 0, server_port = 0;
    sockaddr_in game_address{};
    bool game_known = false, joined = false, stopped = false;
    int virtual_port = 168;
    std::string join_request, host_request, list_request, instance, expected_instance;
    Clock::time_point join_started{}, host_started{}, list_started{};
    CSteamID lobby;
    Metadata metadata;
    uint64 dropped = 0;
    Clock::time_point next_statistics = Clock::now();

    void error(const std::string& request, const std::string& reason) {
        channel.send({"ERROR", request, hex(reason)});
    }
    void close_join(const std::string& reason, bool notify = true) {
        if (joining != k_HSteamNetConnection_Invalid) sockets->CloseConnection(joining, 0, reason.c_str(), false);
        if (game_socket != INVALID_SOCKET) closesocket(game_socket);
        joining = k_HSteamNetConnection_Invalid; game_socket = INVALID_SOCKET;
        joined = false; game_known = false;
        if (notify && !join_request.empty()) channel.send({"LEFT", join_request, hex(reason)});
        join_request.clear();
    }
    void drop_peer(HSteamNetConnection connection, const char* reason) {
        auto it = peers.find(connection);
        if (it == peers.end()) return;
        channel.send({"DROP", std::to_string(connection), std::to_string(it->second.port)});
        closesocket(it->second.socket); peers.erase(it);
        sockets->CloseConnection(connection, 0, reason, false);
    }
    void hello(HSteamNetConnection connection, Peer& peer) {
        if (!peer.allowed || !peer.connected || peer.hello) return;
        const auto message = std::string("ARL1H") + instance;
        if (sockets->SendMessageToConnection(connection, message.data(), static_cast<uint32>(message.size()),
                k_nSteamNetworkingSend_Reliable | k_nSteamNetworkingSend_NoNagle, nullptr) != k_EResultOK) {
            drop_peer(connection, "host greeting failed"); return;
        }
        peer.hello = true;
    }
    void status(SteamNetConnectionStatusChangedCallback_t* event) {
        if (event->m_hConn == joining) {
            if (event->m_info.m_eState == k_ESteamNetworkingConnectionState_ClosedByPeer ||
                event->m_info.m_eState == k_ESteamNetworkingConnectionState_ProblemDetectedLocally)
                close_join(event->m_info.m_szEndDebug);
            return;
        }
        if (listener == k_HSteamListenSocket_Invalid || event->m_info.m_hListenSocket != listener) return;
        if (event->m_info.m_eState == k_ESteamNetworkingConnectionState_Connecting) {
            const auto steam = event->m_info.m_identityRemote.GetSteamID();
            if (!steam.IsValid() || !steam.BIndividualAccount() || peers.size() >= static_cast<size_t>(metadata.maximum)) {
                sockets->CloseConnection(event->m_hConn, 0, "server full or unauthenticated peer", false); return;
            }
            if (sockets->AcceptConnection(event->m_hConn) != k_EResultOK) return;
            Peer peer;
            try { peer.socket = udp(server_port, peer.port); }
            catch (const std::exception&) { sockets->CloseConnection(event->m_hConn, 0, "local socket failed", false); return; }
            peer.steam = steam.ConvertToUint64();
            peers.emplace(event->m_hConn, peer);
            channel.send({"PEER", std::to_string(event->m_hConn), std::to_string(peer.port), std::to_string(peer.steam)});
        } else if (event->m_info.m_eState == k_ESteamNetworkingConnectionState_Connected) {
            auto it = peers.find(event->m_hConn);
            if (it != peers.end()) { it->second.connected = true; hello(it->first, it->second); }
        } else if (event->m_info.m_eState == k_ESteamNetworkingConnectionState_ClosedByPeer ||
                   event->m_info.m_eState == k_ESteamNetworkingConnectionState_ProblemDetectedLocally) {
            drop_peer(event->m_hConn, "remote connection ended");
        }
    }
    void advertise() {
        if (!lobby.IsValid()) return;
        const auto set = [&](const char* key, const std::string& value) {
            if (!matchmaking->SetLobbyData(lobby, key, value.c_str())) throw std::runtime_error("Steam refused lobby metadata");
        };
        set("arl_product", product); set("arl_protocol", "168");
        set("arl_host", std::to_string(SteamUser()->GetSteamID().ConvertToUint64()));
        set("arl_port", std::to_string(virtual_port)); set("arl_instance", instance);
        set("arl_name", hex(metadata.name)); set("arl_map", hex(metadata.map)); set("arl_mode", hex(metadata.mode));
        set("arl_max", std::to_string(metadata.maximum)); set("arl_players", std::to_string(metadata.players));
        set("arl_password", std::to_string(metadata.password)); set("arl_classic", std::to_string(metadata.classic));
        set("arl_skin", hex(metadata.skin));
        SteamNetworkPingLocation_t location{};
        if (SteamNetworkingUtils()->GetLocalPingLocation(location) >= 0) {
            char text[k_cchMaxSteamNetworkingPingLocationString]{};
            SteamNetworkingUtils()->ConvertPingLocationToString(location, text, sizeof(text)); set("arl_ping", text);
        }
    }
    void created(LobbyCreated_t* result, bool failed) {
        if (failed || result->m_eResult != k_EResultOK) {
            error(host_request, "Steam could not create the server advertisement");
            stop_host(); return;
        }
        lobby = CSteamID(result->m_ulSteamIDLobby);
        advertise();
        // Players read metadata without joining this lobby. Occupancy comes from the server.
        matchmaking->SetLobbyJoinable(lobby, true);
        channel.send({"HOSTED", host_request, std::to_string(lobby.ConvertToUint64()),
                      std::to_string(SteamUser()->GetSteamID().ConvertToUint64()), std::to_string(virtual_port)});
        host_request.clear();
    }
    void listed(LobbyMatchList_t* result, bool failed) {
        const auto request = list_request; list_request.clear();
        if (failed) { error(request, "Steam server search failed"); return; }
        for (uint32 i = 0; i < std::min(result->m_nLobbiesMatching, 100U); ++i) {
            const auto id = matchmaking->GetLobbyByIndex(static_cast<int>(i));
            auto get = [&](const char* key) { return std::string(matchmaking->GetLobbyData(id, key)); };
            try {
                if (get("arl_product") != product || get("arl_protocol") != "168") continue;
                const auto host = number(get("arl_host"), UINT64_MAX);
                if (matchmaking->GetLobbyOwner(id).ConvertToUint64() != host || !CSteamID(host).BIndividualAccount()) continue;
                auto session = get("arl_instance"); if (!valid_id(session)) continue;
                auto port = number(get("arl_port"), 999);
                Metadata entry;
                entry.parse({get("arl_name"),get("arl_map"),get("arl_mode"),get("arl_max"),get("arl_players"),
                             get("arl_password"),get("arl_classic"),get("arl_skin")}, 0);
                int ping = -1; SteamNetworkPingLocation_t location{};
                if (SteamNetworkingUtils()->ParsePingLocationString(get("arl_ping").c_str(), location))
                    ping = SteamNetworkingUtils()->EstimatePingTimeFromLocalHost(location);
                Fields row{"ROW",request,std::to_string(id.ConvertToUint64()),std::to_string(host),std::to_string(port),session};
                const auto values = entry.fields(); row.insert(row.end(), values.begin(), values.end());
                row.push_back(std::to_string(ping)); channel.send(row);
            } catch (const std::exception&) { /* Untrusted advertisements are skipped individually. */ }
        }
        channel.send({"DONE",request});
    }
    void stop_host() {
        created_callback.Cancel();
        if (lobby.IsValid()) matchmaking->LeaveLobby(lobby);
        lobby.Clear();
        while (!peers.empty()) drop_peer(peers.begin()->first, "host stopped");
        if (listener != k_HSteamListenSocket_Invalid) sockets->CloseListenSocket(listener);
        listener = k_HSteamListenSocket_Invalid; host_request.clear();
    }
    void send_game(HSteamNetConnection connection, const char* bytes, int size) {
        std::string packet("ARL1D"); packet.append(bytes, size);
        auto result = sockets->SendMessageToConnection(connection, packet.data(), static_cast<uint32>(packet.size()),
            k_nSteamNetworkingSend_Unreliable | k_nSteamNetworkingSend_NoNagle, nullptr);
        if (result != k_EResultOK) ++dropped; // ENet retries its reliable datagrams; never grow a second retry queue.
    }
    void receive(HSteamNetConnection connection, Peer* peer) {
        std::array<SteamNetworkingMessage_t*, 32> messages{};
        int count = sockets->ReceiveMessagesOnConnection(connection, messages.data(), static_cast<int>(messages.size()));
        bool invalid = false;
        for (int i = 0; i < count; ++i) {
            auto* message = messages[i];
            auto size = message->m_cbSize;
            const auto* data = static_cast<const char*>(message->m_pData);
            if (size < 5 || size > static_cast<int>(max_packet + 5) || std::memcmp(data,"ARL1",4)) invalid = true;
            else if (data[4] == 'H' && !peer && !joined) {
                if (std::string(data + 5, size - 5) != expected_instance) invalid = true;
                else { joined = true; channel.send({"JOINED",join_request,std::to_string(game_port)}); }
            } else if (data[4] == 'D' && size > 5) {
                int sent = -1;
                if (peer && peer->hello) { peer->seen = Clock::now(); sent = ::send(peer->socket,data+5,size-5,0); }
                else if (!peer && joined && game_known)
                    sent = ::sendto(game_socket,data+5,size-5,0,reinterpret_cast<sockaddr*>(&game_address),sizeof(game_address));
                if (sent != size - 5) ++dropped;
            } else invalid = true;
            message->Release();
        }
        if (invalid) {
            if (peer) drop_peer(connection,"invalid bridge frame");
            else close_join("Host sent an invalid or stale bridge greeting");
        }
    }
public:
    explicit Bridge(Channel& value) : channel(value), status_callback(this,&Bridge::status) {
        if (!sockets || !matchmaking) throw std::runtime_error("modern Steam interfaces unavailable");
        SteamNetworkingUtils()->InitRelayNetworkAccess();
    }
    ~Bridge() {
        // The controller may already have gone away; cleanup never sends IPC here.
        for (auto& entry : peers) { closesocket(entry.second.socket); sockets->CloseConnection(entry.first,0,"exit",false); }
        if (listener != k_HSteamListenSocket_Invalid) sockets->CloseListenSocket(listener);
        if (lobby.IsValid()) matchmaking->LeaveLobby(lobby);
        close_join("exit",false);
    }
    void command(const Fields& f) {
        if (f.empty()) return;
        try {
            if (f[0] == "LIST" && f.size() == 2) {
                number(f[1], UINT32_MAX); list_callback.Cancel(); list_request = f[1]; list_started = Clock::now();
                matchmaking->AddRequestLobbyListStringFilter("arl_product",product,k_ELobbyComparisonEqual);
                matchmaking->AddRequestLobbyListStringFilter("arl_protocol","168",k_ELobbyComparisonEqual);
                matchmaking->AddRequestLobbyListDistanceFilter(k_ELobbyDistanceFilterWorldwide);
                matchmaking->AddRequestLobbyListResultCountFilter(100);
                list_callback.Set(matchmaking->RequestLobbyList(),this,&Bridge::listed);
            } else if (f[0] == "JOIN" && f.size() == 5) {
                number(f[1], UINT32_MAX); auto host = number(f[2], UINT64_MAX);
                const auto port = static_cast<int>(number(f[3],999));
                if (!CSteamID(host).IsValid() || !CSteamID(host).BIndividualAccount() || !valid_id(f[4])) throw std::runtime_error("invalid destination");
                close_join("another join started"); join_request = f[1]; expected_instance = f[4]; join_started = Clock::now();
                game_socket = udp(0,game_port);
                SteamNetworkingIdentity identity{}; identity.SetSteamID64(host);
                SteamNetworkingConfigValue_t options[2];
                options[0].SetInt32(k_ESteamNetworkingConfig_TimeoutInitial,30000);
                options[1].SetInt32(k_ESteamNetworkingConfig_SendBufferSize,256*1024);
#ifdef AOS_RELAY_LOOPBACK_TEST
                (void)port;
                joining = sockets->ConnectByIPAddress(test_address(),2,options);
#else
                joining = sockets->ConnectP2P(identity,port,2,options);
#endif
                if (joining == k_HSteamNetConnection_Invalid) close_join("Steam refused the connection");
            } else if (f[0] == "CANCEL" && f.size() == 2) {
                if (f[1] == join_request) close_join("cancelled");
            } else if (f[0] == "HOST" && f.size() == 14) {
                if (listener != k_HSteamListenSocket_Invalid) throw std::runtime_error("already hosting");
                number(f[1],UINT32_MAX); server_port = static_cast<unsigned short>(number(f[2],65535));
                virtual_port = static_cast<int>(number(f[3],999)); instance = f[4];
                bool publish = number(f[5],1) != 0; metadata.parse(f,6);
                if (!server_port || !valid_id(instance)) throw std::runtime_error("invalid host configuration");
                SteamNetworkingConfigValue_t options[2];
                options[0].SetInt32(k_ESteamNetworkingConfig_TimeoutInitial,30000);
                options[1].SetInt32(k_ESteamNetworkingConfig_SendBufferSize,256*1024);
#ifdef AOS_RELAY_LOOPBACK_TEST
                listener = sockets->CreateListenSocketIP(test_address(),2,options);
#else
                listener = sockets->CreateListenSocketP2P(virtual_port,2,options);
#endif
                if (listener == k_HSteamListenSocket_Invalid) throw std::runtime_error("Steam listen port unavailable");
                host_request = f[1]; host_started = Clock::now();
#ifdef AOS_RELAY_LOOPBACK_TEST
                (void)publish;
                channel.send({"HOSTED",host_request,"0",std::to_string(SteamUser()->GetSteamID().ConvertToUint64()),std::to_string(virtual_port)});
                host_request.clear();
#else
                created_callback.Set(matchmaking->CreateLobby(publish ? k_ELobbyTypePublic : k_ELobbyTypeFriendsOnly,250),this,&Bridge::created);
#endif
            } else if (f[0] == "META") {
                Metadata next; next.parse(f,1); metadata = next; advertise();
            } else if (f[0] == "ALLOW" && f.size() == 2) {
                auto it = peers.find(static_cast<HSteamNetConnection>(number(f[1],UINT32_MAX)));
                if (it != peers.end()) { it->second.allowed = true; hello(it->first,it->second); }
            } else if (f[0] == "DENY" && f.size() == 2) {
                drop_peer(static_cast<HSteamNetConnection>(number(f[1],UINT32_MAX)),"server admission refused");
            } else if (f[0] == "STOP" && f.size() == 1) stopped = true;
            else throw std::runtime_error("unsupported command");
        } catch (const std::exception& e) {
            error(f.size() > 1 ? f[1] : "0",e.what());
        }
    }
    bool tick() {
        SteamAPI_RunCallbacks();
        const auto now = Clock::now();
        if (!joined && !join_request.empty() && now - join_started > std::chrono::seconds(40)) close_join("Steam route or host greeting timed out");
        if (!host_request.empty() && now - host_started > std::chrono::seconds(25)) {
            error(host_request,"Steam advertisement timed out"); stop_host();
        }
        if (!list_request.empty() && now - list_started > std::chrono::seconds(22)) {
            error(list_request,"Steam server search timed out"); list_request.clear(); list_callback.Cancel();
        }
        // Snapshot ids: receive/status processing can retire a peer.
        std::vector<HSteamNetConnection> ids;
        for (const auto& entry : peers) ids.push_back(entry.first);
        std::array<char,max_packet> buffer{};
        for (auto id : ids) {
            auto it = peers.find(id); if (it == peers.end()) continue;
            if ((!it->second.hello && now - it->second.created > std::chrono::seconds(35)) || now - it->second.seen > std::chrono::seconds(120)) {
                drop_peer(id,"peer timeout"); continue;
            }
            receive(id,&it->second);
            it = peers.find(id); if (it == peers.end() || !it->second.hello) continue;
            for (int n = 0; n < 64; ++n) {
                int size = recv(it->second.socket,buffer.data(),static_cast<int>(buffer.size()),0);
                if (size <= 0) break;
                send_game(id,buffer.data(),size);
            }
        }
        if (joining != k_HSteamNetConnection_Invalid) {
            receive(joining,nullptr);
            if (joined) for (int n = 0; n < 64; ++n) {
                sockaddr_in from{}; int length = sizeof(from);
                int size = recvfrom(game_socket,buffer.data(),static_cast<int>(buffer.size()),0,reinterpret_cast<sockaddr*>(&from),&length);
                if (size <= 0) break;
                if (from.sin_addr.s_addr != htonl(INADDR_LOOPBACK)) continue;
                if (game_known && (from.sin_port != game_address.sin_port || from.sin_addr.s_addr != game_address.sin_addr.s_addr)) continue;
                game_address = from; game_known = true;
                send_game(joining,buffer.data(),size);
            }
        }
        if (now >= next_statistics) {
            SteamRelayNetworkStatus_t relay{}; SteamNetworkingUtils()->GetRelayNetworkStatus(&relay);
            channel.send({"STATUS",std::to_string(static_cast<int>(relay.m_eAvail)),std::to_string(dropped)});
            next_statistics = now + std::chrono::seconds(5);
        }
        return !stopped;
    }
};
}

int main() {
    bool initialized = false;
    WSADATA winsock{};
    try {
        if (WSAStartup(MAKEWORD(2,2),&winsock)) throw std::runtime_error("Winsock unavailable");
        char port_text[16]{}, token[80]{};
        if (!GetEnvironmentVariableA("AOS_RELAY_CONTROL_PORT",port_text,sizeof(port_text)) ||
            GetEnvironmentVariableA("AOS_RELAY_CONTROL_TOKEN",token,sizeof(token)) != 64)
            throw std::runtime_error("launch this helper through the retail patch or server");
        auto port = static_cast<unsigned short>(number(port_text,65535));
        if (!port || std::string(token).find_first_not_of("0123456789abcdef") != std::string::npos) throw std::runtime_error("invalid controller");
        Channel channel;
        channel.socket = socket(AF_INET,SOCK_STREAM,IPPROTO_TCP);
        auto controller = loopback(port);
        if (connect(channel.socket,reinterpret_cast<sockaddr*>(&controller),sizeof(controller))) throw std::runtime_error("controller unavailable");
        nonblocking(channel.socket);
        channel.send({"HELLO","1",token});
        Fields command;
        std::vector<Fields> early_commands;
        if (channel.pump(command)) early_commands.push_back(command);
        SetEnvironmentVariableA("SteamAppId","224540"); SetEnvironmentVariableA("SteamGameId","224540");
        if (!SteamAPI_Init()) {
            channel.send({"ERROR","0",hex("Steam could not initialize for Ace of Spades (224540). Start Steam on this Windows account and verify game ownership.")});
            for (int i=0;i<20;++i) { channel.pump(command); Sleep(5); }
            return 3;
        }
        initialized = true;
        if (SteamUtils()->GetAppID() != app_id || !SteamUser()->BLoggedOn()) throw std::runtime_error("wrong Steam app or Steam is offline");
        {
            Bridge bridge(channel);
            channel.send({"READY",std::to_string(SteamUser()->GetSteamID().ConvertToUint64())});
            for (const auto& early : early_commands) bridge.command(early);
            while (true) {
                for (int n=0;n<16 && channel.pump(command);++n) bridge.command(command);
                if (!bridge.tick()) break;
                Sleep(1);
            }
        }
        SteamAPI_Shutdown(); WSACleanup(); return 0;
    } catch (const std::exception& e) {
        std::cerr << "retail relay: " << e.what() << '\n';
        if (initialized) SteamAPI_Shutdown();
        WSACleanup(); return 2;
    }
}
