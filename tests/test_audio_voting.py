"""Audio cue + vote-kick tests."""
import sys
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

from shared.bytes import ByteReader  # noqa: E402
from shared.packet import PlaySound, PlayMusic, GenericVoteMessage  # noqa: E402
from server import audio, voting  # noqa: E402


class FakeServer:
    def __init__(self):
        self.sent = []
        self.players = {}
        self.connections = {}

    def broadcast(self, data):
        self.sent.append(data)


class FakePlayer:
    def __init__(self, pid, name="P"):
        self.id = pid
        self.name = name
        self.sent = []
        self.disconnected = None
        self.connection = SimpleNamespace(in_game=True)

    def send(self, data):
        self.sent.append(data)

    def disconnect(self, reason=0):
        self.disconnected = reason


# --- audio -------------------------------------------------------------------

def test_play_sound_broadcasts_positioned():
    srv = FakeServer()
    audio.play_sound(srv, audio.SND_CRATE, volume=0.5, position=(10, 20, 30))
    pkt = PlaySound(ByteReader(srv.sent[0][1:]))
    assert pkt.sound_id == audio.SND_CRATE
    assert pkt.positioned
    assert abs(pkt.x - 10) < 0.1 and abs(pkt.z - 30) < 0.1


def test_play_sound_to_single_player():
    p = FakePlayer(1)
    audio.play_sound_to(p, audio.SND_EVENT_POSITIVE)
    assert len(p.sent) == 1
    pkt = PlaySound(ByteReader(p.sent[0][1:]))
    assert pkt.sound_id == audio.SND_EVENT_POSITIVE
    assert not pkt.positioned


def test_play_timeout_music_sends_stop_then_specific_ending():
    srv = FakeServer()
    audio.play_timeout_music(srv)
    # StopMusic(27) FIRST (clears any playing track), then PlayMusic(26).
    assert srv.sent[0][0] == 27
    pkt = PlayMusic(ByteReader(srv.sent[1][1:]))
    assert pkt.name in audio.GAME_ENDING_TRACKS   # a SPECIFIC track, not a range


def test_map_ambient_packet_matches_client_wire():
    # The client reads CreateAmbientSound as: id, name(null-term), loop_id(byte),
    # count(byte), points(3x short). Verified byte-identical to the client's own
    # canonical output for amb_arctic (2026-07-08).
    ca = audio._ambient_packet("amb_arctic", 1, [(100, 200, 40), (300, 400, 41)])
    expected = (b"\x16" + b"amb_arctic\x00" + b"\x01\x02"
                + b"\x64\x00\xc8\x00\x28\x00\x2c\x01\x90\x01\x29\x00")
    assert ca == expected


def test_send_map_ambient_picks_map_track_and_grid():
    # Parse the raw wire bytes directly (id, name\0, loop_id byte, count byte)
    # — the compiled CreateAmbientSound.read still has a loop_id read bug the
    # .pyx source fixes on next rebuild; the WRITE is what ships and is correct.
    srv = FakeServer()
    srv.world_manager = SimpleNamespace(map_name="ArcticBase",
                                        map_size_x=512, map_size_y=512)
    player = FakePlayer(1)
    audio.send_map_ambient(srv, player)
    assert len(player.sent) == 1
    data = player.sent[0]
    assert data[0] == 22                    # CreateAmbientSound id
    assert b"amb_arctic\x00" in data[:16]   # null-terminated map ambient name
    nul = data.index(0, 1)                  # end of the name string
    loop_id = data[nul + 1]
    count = data[nul + 2]
    assert loop_id == 1
    assert count == 64                      # 8x8 grid over 512x512 @ step 64


def test_send_map_ambient_falls_back_for_unknown_map():
    srv = FakeServer()
    srv.world_manager = SimpleNamespace(map_name="SomeCustomMap",
                                        map_size_x=512, map_size_y=512)
    player = FakePlayer(1)
    audio.send_map_ambient(srv, player)
    assert audio.DEFAULT_AMBIENT.encode() + b"\x00" in player.sent[0][:16]


def test_play_gameplay_music_sends_stop_then_specific_track():
    srv = FakeServer()
    audio.play_gameplay_music(srv)
    assert srv.sent[0][0] == 27                   # StopMusic first
    pkt = PlayMusic(ByteReader(srv.sent[1][1:]))
    assert pkt.name in audio.GAMEPLAY_TRACKS      # a SPECIFIC track
    assert "last_man_standing" in pkt.name
    assert "-" not in pkt.name                    # never a range string


# --- voting ------------------------------------------------------------------

def _vote_server(n_players):
    srv = FakeServer()
    for i in range(n_players):
        p = FakePlayer(i, "P%d" % i)
        srv.players[i] = p
        srv.connections[i] = p.connection
    return srv


def test_kick_vote_opens_and_broadcasts_start():
    srv = _vote_server(4)
    vm = voting.VoteManager(srv)
    ok = vm.start_kick(srv.players[0], srv.players[1], voting.KICK_ABUSE, now=100.0)
    assert ok and vm.active
    pkt = GenericVoteMessage(ByteReader(srv.sent[0][1:]))
    assert pkt.message_type == voting.VOTE_START
    assert pkt.candidates[0]["votes"] == 1   # starter's implicit yes


def test_kick_vote_passes_at_majority():
    srv = _vote_server(4)  # 4 players -> needed = (4-1)//2+1 = 2 yes
    vm = voting.VoteManager(srv)
    vm.start_kick(srv.players[0], srv.players[3], voting.KICK_ABUSE, now=100.0)
    assert vm.active
    vm.cast(srv.players[1], yes=True)  # second yes -> passes
    assert not vm.active
    assert srv.players[3].disconnected == 2   # DISCONNECT_KICKED


def test_kick_vote_target_cannot_vote():
    srv = _vote_server(4)
    vm = voting.VoteManager(srv)
    vm.start_kick(srv.players[0], srv.players[1], voting.KICK_ABUSE, now=100.0)
    vm.cast(srv.players[1], yes=False)   # target tries to vote no — ignored
    assert 1 not in vm.no


def test_kick_vote_auto_fails_after_timeout():
    srv = _vote_server(6)   # needs 3 yes; only starter votes
    vm = voting.VoteManager(srv)
    vm.start_kick(srv.players[0], srv.players[5], voting.KICK_ABUSE, now=100.0)
    vm.tick(now=100.0 + voting.VOTE_DURATION + 1)
    assert not vm.active
    assert srv.players[5].disconnected is None   # not enough yes -> not kicked


def test_only_one_vote_at_a_time():
    srv = _vote_server(4)
    vm = voting.VoteManager(srv)
    assert vm.start_kick(srv.players[0], srv.players[1], 2, now=100.0)
    assert not vm.start_kick(srv.players[2], srv.players[3], 2, now=101.0)


def test_cancel_reason_closes_vote():
    srv = _vote_server(4)
    vm = voting.VoteManager(srv)
    vm.start_kick(srv.players[0], srv.players[1], voting.KICK_ABUSE, now=100.0)
    vm.cancel()
    assert not vm.active
    # last broadcast is a CLOSED message
    pkt = GenericVoteMessage(ByteReader(srv.sent[-1][1:]))
    assert pkt.message_type == voting.VOTE_CLOSED
