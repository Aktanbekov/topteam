"""The board link: the MessagePack we hand-rolled, and seek-safe replay.

Neither of these needs a board. The encoder is pure bytes and the replay logic
is pure bookkeeping, which is the point - the parts that can be checked without
hardware should be, so the time in front of the actual board is spent on the
things only the board can answer.
"""

import arduino_bridge as ab
import pytest
from unoq_mcp import EventReplay


# -------------------------------------------------------------- MessagePack
# The board has no pip, no internet and no msgpack, so arduino_bridge encodes
# the format itself. These pin every shape the router RPC actually sends.
def test_the_exact_bytes_of_a_real_alert():
    """notify alert(3) -> 93 02 a5 "alert" 91 03, verified against the router."""
    assert ab.pack([ab.NOTIFY, "alert", [3]]) == b"\x93\x02\xa5alert\x91\x03"


def test_the_exact_bytes_of_a_request():
    assert ab.pack([ab.REQUEST, 1, "mcu_ping", []]) == (
        b"\x94\x00\x01\xa8mcu_ping\x90"
    )


@pytest.mark.parametrize(
    "value",
    [
        0,
        1,
        127,
        -1,
        -32,
        255,
        65535,
        -128,
        -32768,
        None,
        True,
        False,
        "",
        "alert",
        "reset",
        "mcu_ping",
        "x" * 40,
        [],
        [1, 2, 3],
        [ab.NOTIFY, "alert", [2]],
        [ab.REQUEST, 7, "mcu_ping", []],
    ],
)
def test_pack_then_unpack_returns_the_same_value(value):
    decoded, index = ab.unpack(ab.pack(value))
    assert decoded == value
    assert index == len(ab.pack(value))


def test_a_router_response_decodes():
    """[1, msgid, error, result] - what mcu_ping comes back as."""
    raw = ab.pack([ab.RESPONSE, 1, None, "pong"])
    decoded, _ = ab.unpack(raw)
    assert decoded == [ab.RESPONSE, 1, None, "pong"]


def test_an_unencodable_type_is_a_clear_error_not_a_guess():
    with pytest.raises(ab.BridgeError):
        ab.pack({"a": 1.5})


def test_a_truncated_reply_is_a_clear_error():
    with pytest.raises(ab.BridgeError):
        ab.unpack(b"\xa5al")


# ------------------------------------------------------------------- replay
class FakeBoard:
    """Stands in for UnoQ. Records what the sketch would have been told."""

    def __init__(self, fail=False):
        self.levels = []
        self.resets = 0
        self.fail = fail
        self.last_error = "board says no" if fail else None

    def reset_drive(self):
        self.resets += 1
        return not self.fail

    def set_level(self, level):
        if self.fail:
            return False
        self.levels.append(level)
        return True


def test_playing_forward_sends_every_transition_once():
    board = FakeBoard()
    replay = EventReplay(board)
    replay.reset()
    for seq, level in enumerate([1, 3, 0]):
        assert replay.apply(level, seq) == "sent"
    assert board.levels == [1, 3, 0]


def test_seeking_back_does_not_buzz_or_strike_twice():
    """The reason this class exists.

    The sketch counts the transition INTO a level, so replaying a level 2 would
    tick the strike counter again and replaying a level 3 would buzz again. A
    reviewer scrubbing back to look at the critical moment must not inflate
    their own score by doing so.
    """
    board = FakeBoard()
    replay = EventReplay(board)
    replay.reset()
    replay.apply(1, 0)
    replay.apply(3, 1)
    replay.apply(0, 2)

    # Viewer drags the scrubber back and plays over the same stretch.
    assert replay.apply(1, 0) == "skipped"
    assert replay.apply(3, 1) == "skipped"
    assert board.levels == [1, 3, 0]
    assert replay.skipped == 2


def test_playing_past_the_furthest_point_resumes():
    board = FakeBoard()
    replay = EventReplay(board)
    replay.reset()
    replay.apply(1, 0)
    replay.apply(0, 1)
    replay.apply(1, 0)  # scrubbed back
    assert replay.apply(3, 2) == "sent"  # now past where we had got to
    assert board.levels == [1, 0, 3]


def test_replaying_the_drive_resets_the_board_and_runs_again():
    board = FakeBoard()
    replay = EventReplay(board)
    replay.reset()
    replay.apply(1, 0)
    replay.apply(3, 1)

    replay.reset()
    assert board.resets == 2
    assert replay.apply(1, 0) == "sent"
    assert board.levels == [1, 3, 1]


def test_the_same_level_twice_running_is_not_resent():
    board = FakeBoard()
    replay = EventReplay(board)
    replay.reset()
    replay.apply(1, 0)
    assert replay.apply(1, 1) == "skipped"
    assert board.levels == [1]


def test_a_board_that_refuses_reports_failure_and_keeps_going():
    """A dead board must never take the review page down with it."""
    board = FakeBoard(fail=True)
    replay = EventReplay(board)
    replay.reset()
    assert replay.apply(2, 0) == "failed"
    assert replay.status()["error"] == "board says no"
    assert replay.status()["events_sent"] == 0


def test_status_is_json_safe():
    import json

    json.dumps(EventReplay(FakeBoard()).status())


# -------------------------------------------------------------------- calm
def test_calm_returns_the_board_to_level_zero(monkeypatch):
    """The "make it stop" path.

    The sketch holds its last level forever and has no way to know the laptop
    has gone away, so a drive that ended on a critical error leaves the strip
    flashing until something says otherwise.
    """
    import calm_board

    board = FakeBoard()
    board.connect = lambda: True
    board.close = lambda: None
    board.last_error = None
    monkeypatch.setattr(calm_board, "UnoQ", lambda url=None: board)

    assert calm_board.calm(quiet=True) is True
    assert board.levels == [0]
    assert board.resets == 1


def test_calm_is_quiet_and_successful_when_there_is_no_board(monkeypatch):
    """Absent hardware must never make a command fail."""
    import calm_board

    class NoBoard:
        last_error = "no MCP server"

        def connect(self):
            return False

    monkeypatch.setattr(calm_board, "UnoQ", lambda url=None: NoBoard())
    assert calm_board.calm(quiet=True) is False
