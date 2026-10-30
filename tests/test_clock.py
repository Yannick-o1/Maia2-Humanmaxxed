"""Clock, increment and timeout behaviour, including lichess's flag rules."""

import chess
import pytest

from app.game import Game, NotYourTurn, Reason, Status


def new_game(initial=600, increment=5, **kwargs):
    return Game(initial_seconds=initial, increment_seconds=increment, **kwargs)


def test_both_clocks_start_at_the_initial_time(clock):
    game = new_game(300, 3)
    assert game.remaining(chess.WHITE) == 300
    assert game.remaining(chess.BLACK) == 300


def test_only_the_side_to_move_ticks_down(clock):
    game = new_game(600, 0)
    clock.advance(10)
    assert game.remaining(chess.WHITE) == pytest.approx(590)
    assert game.remaining(chess.BLACK) == pytest.approx(600)


def test_increment_is_added_after_the_move(clock):
    game = new_game(600, 5)
    clock.advance(8)
    game.apply_move("e2e4")
    # 600 - 8 spent + 5 increment
    assert game.remaining(chess.WHITE) == pytest.approx(597)
    assert game.remaining(chess.BLACK) == pytest.approx(600)


def test_zero_increment_just_deducts(clock):
    game = new_game(600, 0)
    clock.advance(8)
    game.apply_move("e2e4")
    assert game.remaining(chess.WHITE) == pytest.approx(592)


def test_clock_passes_to_the_opponent_after_a_move(clock):
    game = new_game(600, 0)
    clock.advance(5)
    game.apply_move("e2e4")
    clock.advance(12)
    assert game.remaining(chess.WHITE) == pytest.approx(595)
    assert game.remaining(chess.BLACK) == pytest.approx(588)


def test_running_out_loses_the_game(clock):
    game = new_game(60, 0)
    clock.advance(61)
    assert game.check_flag() is True
    assert game.status is Status.FINISHED
    assert game.result == "0-1"  # White flagged, Black wins
    assert game.reason is Reason.TIMEOUT


def test_flagging_is_a_draw_when_the_opponent_cannot_mate(clock):
    """Lichess rule: time out against a bare king and it is a draw."""
    # White has king and bishop (cannot force mate); Black has a bare king.
    game = new_game(60, 0)
    game.board = chess.Board("8/8/8/4k3/8/8/8/4KB2 b - - 0 1")
    clock.advance(61)
    assert game.check_flag() is True
    assert game.result == "1/2-1/2"
    assert game.reason is Reason.TIMEOUT_INSUFFICIENT_MATERIAL


def test_flagging_loses_when_the_opponent_has_a_rook(clock):
    game = new_game(60, 0)
    game.board = chess.Board("8/8/8/4k3/8/8/8/4KR2 b - - 0 1")
    clock.advance(61)
    assert game.check_flag() is True
    assert game.result == "1-0"
    assert game.reason is Reason.TIMEOUT


def test_two_knights_can_mate_so_flagging_loses(clock):
    """Two knights cannot force mate but can mate, which is what counts."""
    game = new_game(60, 0)
    game.board = chess.Board("8/8/8/4k3/8/8/8/3KNN2 b - - 0 1")
    clock.advance(61)
    assert game.check_flag() is True
    assert game.result == "1-0"


def test_check_flag_is_a_no_op_with_time_left(clock):
    game = new_game(600, 0)
    clock.advance(1)
    assert game.check_flag() is False
    assert game.status is Status.PLAYING


def test_check_flag_is_a_no_op_once_finished(clock):
    game = new_game(600, 0)
    game.resign(chess.WHITE)
    clock.advance(10_000)
    assert game.check_flag() is False
    assert game.reason is Reason.RESIGNATION


def test_moving_after_the_flag_is_refused(clock):
    game = new_game(30, 0)
    clock.advance(31)
    with pytest.raises(NotYourTurn):
        game.apply_move("e2e4")
    assert game.status is Status.FINISHED


def test_remaining_never_goes_negative(clock):
    game = new_game(10, 0)
    clock.advance(500)
    assert game.remaining(chess.WHITE) == 0.0


def test_time_until_flag_tracks_the_side_to_move(clock):
    game = new_game(100, 0)
    clock.advance(40)
    assert game.time_until_flag() == pytest.approx(60)
    game.apply_move("e2e4")
    clock.advance(10)
    assert game.time_until_flag() == pytest.approx(90)


def test_time_until_flag_is_none_when_finished(clock):
    game = new_game(100, 0)
    game.resign(chess.WHITE)
    assert game.time_until_flag() is None


def test_seconds_spent_is_recorded_per_move(clock):
    game = new_game(600, 5)
    clock.advance(3.5)
    record = game.apply_move("e2e4")
    assert record.seconds_spent == pytest.approx(3.5)
    assert record.white_clock == pytest.approx(601.5)


def test_takeback_restores_the_clocks(clock):
    game = new_game(600, 5, rated=False)
    clock.advance(10)
    game.apply_move("e2e4")  # white: 595
    clock.advance(20)
    game.apply_move("e7e5")  # black: 585
    white_after_one = game.remaining(chess.WHITE)

    clock.advance(30)
    game.apply_move("g1f3")  # white: 570
    clock.advance(5)
    game.apply_move("b8c6")

    assert game.takeback() == 2
    assert game.remaining(chess.WHITE) == pytest.approx(white_after_one)
    assert len(game.moves) == 2
    assert game.board.turn == chess.WHITE


def test_takeback_to_the_start_restores_the_initial_time(clock):
    game = new_game(600, 5, rated=False)
    clock.advance(10)
    game.apply_move("e2e4")
    clock.advance(10)
    game.apply_move("e7e5")
    game.takeback()
    assert game.remaining(chess.WHITE) == pytest.approx(600)
    assert game.remaining(chess.BLACK) == pytest.approx(600)
    assert game.moves == []


def test_takeback_is_refused_in_a_rated_game(clock):
    game = new_game(600, 5, rated=True)
    game.apply_move("e2e4")
    with pytest.raises(NotYourTurn):
        game.takeback()


def test_takeback_cancels_a_draw_offer(clock):
    game = new_game(600, 5, rated=False)
    game.apply_move("e2e4")
    game.draw_offer_from = chess.BLACK
    game.takeback()
    assert game.draw_offer_from is None
