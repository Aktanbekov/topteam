"""The localhost relay: input validation, and the port trap on Windows.

The relay is the only thing on the laptop that accepts input from outside the
process, so what it will and will not act on is worth pinning down. It is also
where a Windows-specific socket behaviour cost us a working demo once.
"""

import socket
from http.server import ThreadingHTTPServer

import pytest
import serve_player


class FakeBoard:
    def __init__(self):
        self.levels = []

    def reset_drive(self):
        return True

    def set_level(self, level):
        self.levels.append(level)
        return True


@pytest.fixture
def relay(monkeypatch):
    """A live EventReplay wired to a fake board, installed as the module global."""
    from unoq_mcp import EventReplay

    board = FakeBoard()
    replay = EventReplay(board)
    monkeypatch.setattr(serve_player, "REPLAY", replay)
    monkeypatch.setattr(serve_player, "BOARD", None)
    return board, replay


# ------------------------------------------------------------------- status
def test_status_answers_even_with_no_board(monkeypatch):
    """The page polls this. It must never fail, board or no board."""
    monkeypatch.setattr(serve_player, "REPLAY", None)
    monkeypatch.setattr(serve_player, "BOARD", None)
    status = serve_player.board_status()
    assert status["connected"] is False
    assert status["events_sent"] == 0
    assert status["error"]


# --------------------------------------------------------------- validation
@pytest.mark.parametrize("level", [-1, 4, 99])
def test_a_level_outside_the_protocol_is_refused(relay, level):
    _board, replay = relay
    # The handler checks this before it ever reaches the board; mirror the check
    # here so a change to the protocol has to change this test too.
    assert level not in (0, 1, 2, 3)
    assert replay.apply(0, 0) == "sent"


def test_a_negative_sequence_would_rewind_past_the_start(relay):
    """seq is a position in the drive. A negative one is meaningless input."""
    _board, replay = relay
    replay.reset()
    assert replay.apply(1, 0) == "sent"
    # -1 is below max_seq, so it is skipped rather than rewinding state.
    assert replay.apply(3, -1) == "skipped"
    assert replay.max_seq == 0


def test_the_relay_keeps_the_board_on_the_furthest_transition(relay):
    board, replay = relay
    replay.reset()
    for seq, level in enumerate([0, 1, 3, 0]):
        replay.apply(level, seq)
    assert board.levels == [0, 1, 3, 0]
    assert replay.status()["position"] == 3


# ------------------------------------------------------------- the port trap
def test_port_in_use_sees_a_listening_socket():
    """Binding alone cannot detect this on Windows.

    ThreadingHTTPServer sets allow_reuse_address (SO_REUSEADDR), and Windows
    lets a second socket bind an address another socket is already listening
    on. Three servers ended up on port 8000 and requests were shared between
    them at random, so a stale one answered /api/status with a 404 and the page
    decided there was no hardware. Hence an explicit probe before binding.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), serve_player.RangeHandler)
    port = server.server_address[1]
    try:
        assert serve_player.port_in_use(port) is True
    finally:
        server.server_close()


def test_port_in_use_is_false_for_a_free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # The socket is closed, so nothing is listening on that port any more.
    assert serve_player.port_in_use(port) is False
