from types import SimpleNamespace

from client_patches.retail_mousefix.aos_steam_bridge import encode
from server.bans import address_host
from server.join_password import peer_host
from server.steam_p2p import SteamP2PService


class Peer:
    def __init__(self, port, host='127.0.0.1'):
        self.address = SimpleNamespace(host=host, port=port)


def fixture():
    commands = []
    server = SimpleNamespace(connections={}, ban_manager=SimpleNamespace(is_banned=lambda key: None))
    relay = SteamP2PService(server)
    relay.bridge = SimpleNamespace(send=lambda *fields: commands.append(fields))
    server.steam_p2p = relay
    return server, relay, commands


def test_identity_is_registered_before_allowing_gameplay():
    server, relay, commands = fixture()
    relay.event(['PEER', '10', '40000', '76561198000000001'])
    assert relay.routes[40000] == ('10', '76561198000000001')
    assert commands == [('ALLOW', '10')]
    assert address_host(Peer(40000), server) == 'steam:76561198000000001'


def test_separate_players_do_not_share_bans_or_password_lockouts():
    server, relay, _ = fixture()
    for number in (1, 2):
        relay.event(['PEER', str(number), str(40000 + number), str(76561198000000000 + number)])
    connections = [SimpleNamespace(peer=Peer(40000 + n), server=server) for n in (1, 2)]
    assert address_host(connections[0].peer, server) != address_host(connections[1].peer, server)
    assert peer_host(connections[0]) != peer_host(connections[1])


def test_identity_is_pinned_across_udp_port_reuse():
    server, relay, _ = fixture()
    relay.event(['PEER', '1', '40000', '76561198000000001'])
    old = Peer(40000)
    assert address_host(old, server) == 'steam:76561198000000001'
    relay.event(['DROP', '1', '40000'])
    relay.event(['PEER', '2', '40000', '76561198000000002'])
    assert address_host(old, server) == 'steam:76561198000000001'
    assert address_host(Peer(40000), server) == 'steam:76561198000000002'
    relay.event(['DROP', '1', '40000'])
    assert 40000 in relay.routes
    relay.forget_peer(old)
    assert old not in relay.peers


def test_banned_or_kicked_steam_peer_never_reaches_enet():
    server, relay, commands = fixture()
    server.ban_manager.is_banned = lambda key: {'reason': 'test'}
    relay.event(['PEER', '1', '40000', '76561198000000001'])
    assert commands == [('DENY', '1')]
    assert relay.routes == {}
    server.ban_manager.is_banned = lambda key: None
    server.vote_manager = SimpleNamespace(match_kick_reason=lambda key: 1)
    relay.event(['PEER', '2', '40001', '76561198000000002'])
    assert commands[-1] == ('DENY', '2')


def test_existing_direct_clients_cannot_claim_a_registered_loopback_port():
    server, relay, _ = fixture()
    relay.event(['PEER', '1', '40000', '76561198000000001'])
    remote = Peer(40000, '192.0.2.1')
    assert relay.identity_for(remote) is None
    assert relay.peers == {}


def test_duplicate_port_is_rejected_without_changing_owner():
    _, relay, commands = fixture()
    relay.event(['PEER', '1', '40000', '76561198000000001'])
    relay.event(['PEER', '2', '40000', '76561198000000002'])
    assert commands[-1] == ('DENY', '2')
    assert relay.routes[40000][0] == '1'


def test_cli_hosting_options_do_not_rewrite_or_enable_legacy_master():
    from server.launcher import build_parser, _apply_network_options
    args = build_parser().parse_args(['--steam-p2p','--steam-p2p-port','169'])
    config = SimpleNamespace(port=28630, steam=SimpleNamespace(enabled=False))
    _apply_network_options(config,args)
    assert config.steam_p2p_enabled and config.steam_p2p_port == 169
    assert not config.steam.enabled and config.port == 28630


def test_cli_rejects_invalid_virtual_port():
    import pytest
    from server.launcher import build_parser
    with pytest.raises(SystemExit):
        build_parser().parse_args(['--steam-p2p-port','1000'])
