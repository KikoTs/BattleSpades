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


def test_play_timeout_music_uses_ending_track():
    srv = FakeServer()
    audio.play_timeout_music(srv)
    pkt = PlayMusic(ByteReader(srv.sent[0][1:]))
    assert pkt.name == audio.ENDING_MUSIC


def test_play_gameplay_music_uses_ingame_bed():
    srv = FakeServer()
    audio.play_gameplay_music(srv)
    pkt = PlayMusic(ByteReader(srv.sent[0][1:]))
    assert pkt.name == audio.GAMEPLAY_MUSIC
    assert "last_man_standing" in pkt.name


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
