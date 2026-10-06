// BattleSpades Steam host: lets players reach a dedicated server through
// Steam's relay network (Steam Datagram Relay) instead of its IP address.
//
// Some providers block the server's address and our web services while Steam
// stays reachable. This helper logs on to Steam as a game server, listens for
// Steam P2P connections and forwards each player's game datagrams to the
// server's UDP port on this machine, in the BattleSpades client's own tunnel
// protocol (a reliable "BSP2P-HELLO\x01<server id>" once the route exists,
// then raw ENet datagrams, unreliable, both ways).
//
// It follows Valve's SpaceWar sample: game server logon, CreateListenSocketP2P
// and connection events through STEAM_GAMESERVER_CALLBACK. The global
// connection-status callback is deliberately not used: it never fires in a
// game-server process, so no connection would ever be accepted.
//
// Control protocol (with --control; the game server is the parent process).
// One line per message, fields separated by a single space.
//   helper -> parent   READY <steamid> <anonymous 0|1> <app id>
//                      PEER <connection> <loopback port> <player steamid>
//                      DROP <connection>
//                      LOG <text>
//                      ERROR <text>
//   parent -> helper   ALLOW <connection>
//                      DENY <connection>
//                      QUIT
// A player's datagrams reach the game port only after ALLOW, so the server
// applies bans and kicks to "steam:<id>" before any gameplay byte arrives.
// Without --control every player is allowed (standalone use).

#if defined(_WIN32)
#define FD_SETSIZE 512
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

#include <steam/steam_api.h>
#include <steam/steam_gameserver.h>
#include <steam/isteamnetworkingsockets.h>
#include <steam/isteamnetworkingutils.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <mutex>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

namespace {

#if defined(_WIN32)
using Socket = SOCKET;
constexpr Socket invalid_socket = INVALID_SOCKET;
void close_socket(Socket socket) { closesocket(socket); }
#else
using Socket = int;
constexpr Socket invalid_socket = -1;
void close_socket(Socket socket) { close(socket); }
#endif

using Clock = std::chrono::steady_clock;

constexpr std::string_view hello_magic{"BSP2P-HELLO\x01", 12};
constexpr std::size_t maximum_datagram_bytes{2048};
constexpr auto idle_timeout = std::chrono::seconds{120};
constexpr auto allow_timeout = std::chrono::seconds{15};

struct Options {
    std::string game_host{"127.0.0.1"};
    std::uint16_t game_port{27015};
    std::uint32_t app_id{224540};
    std::string token_file;
    std::string server_id;
    std::size_t max_clients{64};
    bool control{};
};

struct Peer {
    HSteamNetConnection connection{k_HSteamNetConnection_Invalid};
    std::uint64_t steam_id{};
    Socket socket{invalid_socket};
    std::uint16_t local_port{};
    bool connected{};
    bool allowed{};
    bool hello_sent{};
    Clock::time_point created{Clock::now()};
    Clock::time_point last_seen{Clock::now()};
};

std::mutex output_mutex;

void emit(const std::string& line) {
    const std::scoped_lock lock{output_mutex};
    std::fputs(line.c_str(), stdout);
    std::fputc('\n', stdout);
    std::fflush(stdout);
}

/** One line, no control characters: the parent parses by newline. */
std::string clean(std::string_view text) {
    std::string result;
    result.reserve(text.size());
    for (const char character : text) {
        result.push_back(static_cast<unsigned char>(character) < 0x20U ? ' ' : character);
    }
    return result;
}

void log_line(std::string_view text) { emit("LOG " + clean(text)); }

bool set_nonblocking(Socket socket) {
#if defined(_WIN32)
    u_long enabled = 1;
    return ioctlsocket(socket, FIONBIO, &enabled) == 0;
#else
    const int flags = fcntl(socket, F_GETFL, 0);
    return flags >= 0 && fcntl(socket, F_SETFL, flags | O_NONBLOCK) == 0;
#endif
}

/**
 * A UDP socket on an ephemeral loopback port, connected to the game server.
 * The game server sees one 127.0.0.1:<port> per Steam player; the parent maps
 * that port back to the player's SteamID.
 */
Socket open_route(const Options& options, std::uint16_t& local_port) {
    const Socket socket = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (socket == invalid_socket) return invalid_socket;
    sockaddr_in local{};
    local.sin_family = AF_INET;
    local.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    sockaddr_in remote{};
    remote.sin_family = AF_INET;
    remote.sin_port = htons(options.game_port);
    socklen_t length = sizeof local;
    if (inet_pton(AF_INET, options.game_host.c_str(), &remote.sin_addr) != 1 ||
        bind(socket, reinterpret_cast<const sockaddr*>(&local), sizeof local) != 0 ||
        connect(socket, reinterpret_cast<const sockaddr*>(&remote), sizeof remote) != 0 ||
        getsockname(socket, reinterpret_cast<sockaddr*>(&local), &length) != 0 || !set_nonblocking(socket)) {
        close_socket(socket);
        return invalid_socket;
    }
    local_port = ntohs(local.sin_port);
    return socket;
}

class Host {
public:
    explicit Host(Options options) : options_{std::move(options)} {}

    bool start();
    void run();
    void stop();
    /** Queues a parent command; it is applied on the Steam thread. */
    void enqueue(std::string line) {
        const std::scoped_lock lock{queue_mutex_};
        queue_.push_back(std::move(line));
    }
    std::atomic_bool quit{};

private:
    STEAM_GAMESERVER_CALLBACK(Host, on_logged_on, SteamServersConnected_t);
    STEAM_GAMESERVER_CALLBACK(Host, on_logon_failure, SteamServerConnectFailure_t);
    STEAM_GAMESERVER_CALLBACK(Host, on_disconnected, SteamServersDisconnected_t);
    STEAM_GAMESERVER_CALLBACK(Host, on_connection, SteamNetConnectionStatusChangedCallback_t);

    void control(const std::string& line);
    Peer* find(HSteamNetConnection connection);
    void drop(HSteamNetConnection connection, const char* reason);
    void maybe_hello(Peer& peer);
    void pump();

    Options options_;
    std::vector<Peer> peers_;
    HSteamListenSocket listen_{k_HSteamListenSocket_Invalid};
    HSteamNetPollGroup group_{k_HSteamNetPollGroup_Invalid};
    std::string relay_status_;
    std::mutex queue_mutex_;
    std::vector<std::string> queue_;
};

Peer* Host::find(HSteamNetConnection connection) {
    const auto found = std::ranges::find_if(
        peers_, [connection](const Peer& peer) { return peer.connection == connection; });
    return found == peers_.end() ? nullptr : &*found;
}

void Host::drop(HSteamNetConnection connection, const char* reason) {
    SteamGameServerNetworkingSockets()->CloseConnection(connection, 0, reason, false);
    const auto found = std::ranges::find_if(
        peers_, [connection](const Peer& peer) { return peer.connection == connection; });
    if (found == peers_.end()) return;
    if (found->socket != invalid_socket) close_socket(found->socket);
    emit("DROP " + std::to_string(connection));
    peers_.erase(found);
}

void Host::maybe_hello(Peer& peer) {
    if (!peer.connected || !peer.allowed || peer.hello_sent) return;
    // The player's tunnel holds its game back until this arrives, so the
    // server id always precedes the first game datagram.
    std::string hello{hello_magic};
    hello += options_.server_id;
    if (SteamGameServerNetworkingSockets()->SendMessageToConnection(
            peer.connection, hello.data(), static_cast<uint32>(hello.size()), k_nSteamNetworkingSend_Reliable,
            nullptr) == k_EResultOK) {
        peer.hello_sent = true;
        peer.last_seen = Clock::now();
    }
}

void Host::on_logged_on(SteamServersConnected_t*) {
    const CSteamID id = SteamGameServer()->GetSteamID();
    emit("READY " + std::to_string(id.ConvertToUint64()) + (id.BAnonGameServerAccount() ? " 1 " : " 0 ") +
         std::to_string(options_.app_id));
}

void Host::on_logon_failure(SteamServerConnectFailure_t* failure) {
    const std::string text = "Steam logon failed (EResult " + std::to_string(failure->m_eResult) + ")" +
                             (failure->m_bStillRetrying ? ", still retrying" : "");
    if (failure->m_bStillRetrying) {
        log_line(text);
    } else {
        emit("ERROR " + text);
        quit = true;
    }
}

void Host::on_disconnected(SteamServersDisconnected_t* lost) {
    log_line("disconnected from Steam (EResult " + std::to_string(lost->m_eResult) + "); Steam reconnects by itself");
}

void Host::on_connection(SteamNetConnectionStatusChangedCallback_t* change) {
    const auto& info = change->m_info;
    const HSteamNetConnection connection = change->m_hConn;
    if (info.m_hListenSocket != k_HSteamListenSocket_Invalid &&
        change->m_eOldState == k_ESteamNetworkingConnectionState_None &&
        info.m_eState == k_ESteamNetworkingConnectionState_Connecting) {
        const CSteamID player = info.m_identityRemote.GetSteamID();
        // Only real player accounts; a relay identity is verified by Steam.
        if (!player.IsValid() || !player.BIndividualAccount()) {
            SteamGameServerNetworkingSockets()->CloseConnection(connection, 0, "players only", false);
            return;
        }
        // A player reconnecting replaces their old route.
        for (std::size_t index{}; index < peers_.size();) {
            if (peers_[index].steam_id == player.ConvertToUint64()) {
                drop(peers_[index].connection, "replaced by a new connection");
            } else {
                ++index;
            }
        }
        if (peers_.size() >= options_.max_clients) {
            SteamGameServerNetworkingSockets()->CloseConnection(connection, 0, "server relay is full", false);
            return;
        }
        Peer peer;
        peer.connection = connection;
        peer.steam_id = player.ConvertToUint64();
        peer.socket = open_route(options_, peer.local_port);
        if (peer.socket == invalid_socket ||
            SteamGameServerNetworkingSockets()->AcceptConnection(connection) != k_EResultOK) {
            if (peer.socket != invalid_socket) close_socket(peer.socket);
            SteamGameServerNetworkingSockets()->CloseConnection(connection, 0, "could not open a route", false);
            return;
        }
        SteamGameServerNetworkingSockets()->SetConnectionPollGroup(connection, group_);
        peer.allowed = !options_.control;
        peers_.push_back(peer);
        emit("PEER " + std::to_string(connection) + " " + std::to_string(peer.local_port) + " " +
             std::to_string(peer.steam_id));
        return;
    }
    if (info.m_eState == k_ESteamNetworkingConnectionState_Connected) {
        if (Peer* const peer = find(connection); peer != nullptr) {
            peer->connected = true;
            maybe_hello(*peer);
        }
        return;
    }
    if (info.m_eState == k_ESteamNetworkingConnectionState_ClosedByPeer ||
        info.m_eState == k_ESteamNetworkingConnectionState_ProblemDetectedLocally) {
        drop(connection, "closed");
    }
}

void Host::control(const std::string& line) {
    const auto space = line.find(' ');
    const std::string verb = line.substr(0U, space);
    if (verb == "QUIT") {
        quit = true;
        return;
    }
    if (space == std::string::npos) return;
    const auto connection = static_cast<HSteamNetConnection>(std::strtoul(line.c_str() + space + 1U, nullptr, 10));
    if (verb == "ALLOW") {
        if (Peer* const peer = find(connection); peer != nullptr) {
            peer->allowed = true;
            maybe_hello(*peer);
        }
    } else if (verb == "DENY") {
        drop(connection, "not allowed on this server");
    }
}

bool Host::start() {
#if defined(_WIN32)
    WSADATA data{};
    if (WSAStartup(MAKEWORD(2, 2), &data) != 0) {
        emit("ERROR Winsock did not start");
        return false;
    }
    _putenv_s("SteamAppId", std::to_string(options_.app_id).c_str());
    _putenv_s("SteamGameId", std::to_string(options_.app_id).c_str());
#else
    setenv("SteamAppId", std::to_string(options_.app_id).c_str(), 1);
    setenv("SteamGameId", std::to_string(options_.app_id).c_str(), 1);
#endif
    std::string token;
    if (!options_.token_file.empty()) {
        std::ifstream in{options_.token_file};
        std::getline(in, token);
        while (!token.empty() && (token.back() == '\r' || token.back() == '\n' || token.back() == ' ')) {
            token.pop_back();
        }
        if (token.empty()) {
            emit("ERROR the game server token file is missing or empty");
            return false;
        }
    }
    SteamErrMsg error{};
    // The listing is not advertised from here and the query port is shared,
    // so no extra UDP port is opened: this process only carries relay traffic.
    if (SteamGameServer_InitEx(0, options_.game_port, STEAMGAMESERVER_QUERY_PORT_SHARED,
                               eServerModeNoAuthentication, "1.0.0.0", &error) != k_ESteamAPIInitResult_OK) {
        emit("ERROR Steam game server init failed: " + clean(error));
        return false;
    }
    SteamGameServer()->SetModDir("aceofspades");
    SteamGameServer()->SetProduct("aceofspades");
    SteamGameServer()->SetGameDescription("BattleSpades relay host");
    SteamGameServer()->SetDedicatedServer(true);
    if (token.empty()) {
        SteamGameServer()->LogOnAnonymous();
    } else {
        SteamGameServer()->LogOn(token.c_str());
    }
    SteamNetworkingUtils()->InitRelayNetworkAccess();
    listen_ = SteamGameServerNetworkingSockets()->CreateListenSocketP2P(0, 0, nullptr);
    group_ = SteamGameServerNetworkingSockets()->CreatePollGroup();
    if (listen_ == k_HSteamListenSocket_Invalid || group_ == k_HSteamNetPollGroup_Invalid) {
        emit("ERROR Steam would not open the relay listen socket");
        return false;
    }
    log_line(std::string{"logging on to Steam as app "} + std::to_string(options_.app_id) +
             (token.empty() ? " (anonymous)" : " (game server token)") + ", forwarding to " + options_.game_host +
             ":" + std::to_string(options_.game_port));
    return true;
}

void Host::pump() {
    // Steam -> game server.
    std::array<SteamNetworkingMessage_t*, 64U> inbound{};
    for (;;) {
        const int received = SteamGameServerNetworkingSockets()->ReceiveMessagesOnPollGroup(
            group_, inbound.data(), static_cast<int>(inbound.size()));
        if (received <= 0) break;
        for (int index{}; index < received; ++index) {
            auto* const message = inbound[static_cast<std::size_t>(index)];
            if (Peer* const peer = find(message->m_conn);
                peer != nullptr && peer->allowed && peer->hello_sent && message->m_cbSize > 0 &&
                static_cast<std::size_t>(message->m_cbSize) <= maximum_datagram_bytes) {
                peer->last_seen = Clock::now();
                ::send(peer->socket, static_cast<const char*>(message->m_pData), message->m_cbSize, 0);
            }
            message->Release();
        }
    }
    // Game server -> Steam.
    std::array<char, maximum_datagram_bytes> buffer{};
    for (auto& peer : peers_) {
        for (;;) {
            const auto bytes = ::recv(peer.socket, buffer.data(), static_cast<int>(buffer.size()), 0);
            if (bytes <= 0) break;
            SteamGameServerNetworkingSockets()->SendMessageToConnection(
                peer.connection, buffer.data(), static_cast<uint32>(bytes),
                k_nSteamNetworkingSend_Unreliable | k_nSteamNetworkingSend_NoNagle, nullptr);
        }
    }
    // Silent or never-allowed players give their route back.
    const auto now = Clock::now();
    for (std::size_t index{}; index < peers_.size();) {
        const Peer& peer = peers_[index];
        const bool idle = now - peer.last_seen > idle_timeout;
        const bool unanswered = !peer.allowed && now - peer.created > allow_timeout;
        if (idle || unanswered) {
            drop(peer.connection, idle ? "idle" : "no answer from the server");
        } else {
            ++index;
        }
    }
}

void Host::run() {
    while (!quit) {
        SteamGameServer_RunCallbacks();
        {
            std::vector<std::string> pending;
            {
                const std::scoped_lock lock{queue_mutex_};
                pending.swap(queue_);
            }
            for (const auto& line : pending) control(line);
        }
        pump();
        SteamRelayNetworkStatus_t relay{};
        const auto availability = SteamNetworkingUtils()->GetRelayNetworkStatus(&relay);
        const std::string status = availability == k_ESteamNetworkingAvailability_Current ? "ready"
                                   : availability == k_ESteamNetworkingAvailability_Failed ? "failed"
                                                                                           : "connecting";
        if (status != relay_status_) {
            relay_status_ = status;
            log_line("Steam relay network: " + status);
        }
        // Sleep until a game datagram arrives, at most two milliseconds: the
        // Steam side is polled, so this bounds the added latency.
        fd_set readable;
        FD_ZERO(&readable);
        Socket highest = 0;
        for (const auto& peer : peers_) {
            FD_SET(peer.socket, &readable);
            highest = std::max(highest, peer.socket);
        }
        timeval timeout{0, 2000};
        if (peers_.empty()) {
            std::this_thread::sleep_for(std::chrono::milliseconds{5});
        } else {
            select(static_cast<int>(highest) + 1, &readable, nullptr, nullptr, &timeout);
        }
    }
}

void Host::stop() {
    while (!peers_.empty()) drop(peers_.back().connection, "server relay stopping");
    if (listen_ != k_HSteamListenSocket_Invalid) SteamGameServerNetworkingSockets()->CloseListenSocket(listen_);
    SteamGameServer()->LogOff();
    SteamGameServer_Shutdown();
#if defined(_WIN32)
    WSACleanup();
#endif
}

bool parse(int argc, char** argv, Options& options) {
    for (int index = 1; index < argc; ++index) {
        const std::string_view argument{argv[index]};
        const auto value = [&]() -> const char* { return index + 1 < argc ? argv[++index] : nullptr; };
        if (argument == "--control") {
            options.control = true;
        } else if (argument == "--game-host") {
            const char* text = value();
            if (text == nullptr) return false;
            options.game_host = text;
        } else if (argument == "--game-port") {
            const char* text = value();
            if (text == nullptr) return false;
            const long port = std::strtol(text, nullptr, 10);
            if (port < 1 || port > 65535) return false;
            options.game_port = static_cast<std::uint16_t>(port);
        } else if (argument == "--app-id") {
            const char* text = value();
            if (text == nullptr) return false;
            options.app_id = static_cast<std::uint32_t>(std::strtoul(text, nullptr, 10));
            if (options.app_id == 0U) return false;
        } else if (argument == "--token-file") {
            const char* text = value();
            if (text == nullptr) return false;
            options.token_file = text;
        } else if (argument == "--server-id") {
            const char* text = value();
            if (text == nullptr) return false;
            options.server_id = text;
        } else if (argument == "--max-clients") {
            const char* text = value();
            if (text == nullptr) return false;
            options.max_clients = static_cast<std::size_t>(std::clamp(std::strtol(text, nullptr, 10), 1L, 256L));
        } else {
            return false;
        }
    }
    return true;
}

}  // namespace

int main(int argc, char** argv) {
    Options options;
    if (!parse(argc, argv, options)) {
        std::fputs("usage: battlespades-steam-host [--control] [--game-host 127.0.0.1] [--game-port 27015]\n"
                   "       [--app-id 224540] [--token-file PATH] [--server-id TEXT] [--max-clients 64]\n",
                   stderr);
        return 2;
    }
    // Never destroyed: the stdin reader below may still be blocked on it when
    // the process leaves.
    auto* const host = new Host{options};
    if (!host->start()) return 1;

    // The parent's commands arrive on stdin. Its end of file means the game
    // server is gone, and a relay for a dead server must not linger.
    if (options.control) {
        std::thread{[host] {
            std::string line;
            while (std::getline(std::cin, line)) host->enqueue(line);
            host->quit = true;
        }}.detach();
    }
    host->run();
    host->stop();
    std::fflush(stdout);
    std::_Exit(0);
    return 0;
}
