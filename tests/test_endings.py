"""Every way a game can end, plus the PGN that comes out of it."""

import chess
import pytest

from app.game import DRAW_REASONS, Game, Reason, Status, parse_pgn_moves


def game_from(fen: str, **kwargs) -> Game:
    game = Game(**kwargs)
    game.board = chess.Board(fen)
    return game


def test_checkmate_ends_the_game_for_the_mating_side(clock):
    # Fool's mate: 1. f3 e5 2. g4 Qh4#
    game = Game()
    for move in ["f2f3", "e7e5", "g2g4", "d8h4"]:
        game.apply_move(move)
    assert game.status is Status.FINISHED
    assert game.reason is Reason.CHECKMATE
    assert game.result == "0-1"
    assert game.winner == chess.BLACK


def test_scholars_mate_is_a_win_for_white(clock):
    game = Game()
    for move in ["e2e4", "e7e5", "f1c4", "b8c6", "d1h5", "g8f6", "h5f7"]:
        game.apply_move(move)
    assert game.reason is Reason.CHECKMATE
    assert game.result == "1-0"


def test_stalemate_is_a_draw(clock):
    # Qf2-f7 leaves Black with no legal move and no check: Kg6 and Qf7 cover
    # g7, h7 and g8, while h8 itself is not attacked.
    game = game_from("7k/8/6K1/8/8/8/5Q2/8 w - - 0 1")
    game.apply_move("f2f7")
    assert game.board.is_stalemate()
    assert game.status is Status.FINISHED
    assert game.reason is Reason.STALEMATE
    assert game.result == "1/2-1/2"


def test_insufficient_material_is_a_draw(clock):
    # Capturing the last black piece leaves king vs king.
    game = game_from("8/8/4k3/8/8/8/4n3/4K3 w - - 0 1")
    game.apply_move("e1e2")
    assert game.reason is Reason.INSUFFICIENT_MATERIAL
    assert game.result == "1/2-1/2"


def test_lone_bishop_against_a_lone_king_is_insufficient(clock):
    game = game_from("8/8/4k3/5b2/8/8/8/4K3 w - - 0 1")
    assert game.board.is_insufficient_material()


def test_same_coloured_bishops_are_a_dead_draw(clock):
    """Neither side can ever mate, so this is drawn the moment it arises."""
    game = game_from("8/8/4k3/5b2/8/8/4B3/4K3 w - - 0 1")
    game.apply_move("e2f3")  # both bishops now on light squares
    assert game.reason is Reason.INSUFFICIENT_MATERIAL
    assert game.result == "1/2-1/2"


def test_opposite_coloured_bishops_are_still_playable(clock):
    """White's bishop is on a dark square, Black's on a light one, so a mate
    is still possible in principle and the game goes on."""
    game = game_from("8/8/4k3/5b2/8/4B3/8/4K3 w - - 0 1")
    assert not game.board.is_insufficient_material()
    game.apply_move("e3d4")
    assert game.status is Status.PLAYING


def test_fifty_move_rule_is_a_draw(clock):
    """Halfmove clock at 99, so one more quiet move claims the draw."""
    game = game_from("8/8/4k3/8/8/4K3/8/5R2 w - - 99 120")
    game.apply_move("f1f2")
    assert game.reason is Reason.FIFTY_MOVES
    assert game.result == "1/2-1/2"


def test_a_capture_resets_the_fifty_move_counter(clock):
    """A capture on move 99 restarts the count, so no draw is claimable."""
    game = game_from("4k3/8/8/5r2/8/8/5R2/4K3 w - - 99 120")
    game.apply_move("f2f5")  # Rxf5
    assert game.board.halfmove_clock == 0
    assert game.status is Status.PLAYING


def test_threefold_repetition_is_a_draw(clock):
    game = Game()
    # Knights out and back, three times over, repeating the start position.
    cycle = ["g1f3", "g8f6", "f3g1", "f6g8"]
    for _ in range(2):
        for move in cycle:
            if game.status is Status.FINISHED:
                break
            game.apply_move(move)
    assert game.status is Status.FINISHED
    assert game.reason is Reason.THREEFOLD
    assert game.result == "1/2-1/2"


def test_resignation_hands_the_win_to_the_opponent(clock):
    game = Game()
    game.apply_move("e2e4")
    game.resign(chess.BLACK)
    assert game.result == "1-0"
    assert game.reason is Reason.RESIGNATION
    game2 = Game()
    game2.resign(chess.WHITE)
    assert game2.result == "0-1"


def test_resigning_twice_changes_nothing(clock):
    game = Game()
    game.resign(chess.WHITE)
    game.resign(chess.BLACK)
    assert game.result == "0-1"


def test_draw_by_agreement(clock):
    game = Game()
    game.apply_move("e2e4")
    game.agree_draw()
    assert game.result == "1/2-1/2"
    assert game.reason is Reason.AGREEMENT


def test_every_draw_reason_yields_a_half_point(clock):
    for reason in DRAW_REASONS:
        game = Game()
        game._finish("1/2-1/2", reason)
        assert game.human_score() == 0.5


def test_human_score_follows_the_players_colour(clock):
    white = Game(human_color=chess.WHITE)
    white._finish("1-0", Reason.CHECKMATE)
    assert white.human_score() == 1.0

    black = Game(human_color=chess.BLACK)
    black._finish("1-0", Reason.CHECKMATE)
    assert black.human_score() == 0.0

    black_wins = Game(human_color=chess.BLACK)
    black_wins._finish("0-1", Reason.RESIGNATION)
    assert black_wins.human_score() == 1.0


def test_human_score_is_none_while_the_game_runs(clock):
    assert Game().human_score() is None


def test_moves_are_refused_once_the_game_is_over(clock):
    game = Game()
    game.resign(chess.WHITE)
    with pytest.raises(Exception):
        game.apply_move("e2e4")


def test_illegal_and_malformed_moves_are_both_refused(clock):
    from app.game import IllegalMove

    game = Game()
    with pytest.raises(IllegalMove):
        game.apply_move("e2e5")
    with pytest.raises(IllegalMove):
        game.apply_move("not-a-move")
    with pytest.raises(IllegalMove):
        game.apply_move("")


def test_moving_out_of_turn_is_refused(clock):
    from app.game import NotYourTurn

    game = Game()
    with pytest.raises(NotYourTurn):
        game.apply_move("e7e5", by=chess.BLACK)


def test_promotion_is_handled(clock):
    game = game_from("8/4P1k1/8/8/8/8/8/4K3 w - - 0 1")
    record = game.apply_move("e7e8q")
    assert record.san == "e8=Q"
    assert game.board.piece_at(chess.E8).piece_type == chess.QUEEN


def test_underpromotion_is_handled(clock):
    game = game_from("8/4P1k1/8/8/8/8/8/4K3 w - - 0 1")
    game.apply_move("e7e8n")
    assert game.board.piece_at(chess.E8).piece_type == chess.KNIGHT


def test_castling_uses_king_destination_notation(clock):
    game = game_from("r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQkq - 0 1")
    assert "g1" in game.legal_destinations()["e1"]
    record = game.apply_move("e1g1")
    assert record.san == "O-O"


def test_en_passant_is_legal_and_recorded(clock):
    game = game_from("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 2")
    record = game.apply_move("e5d6")
    assert record.san == "exd6"


def test_status_text_reads_naturally(clock):
    game = Game(human_color=chess.WHITE)
    game._finish("1-0", Reason.CHECKMATE)
    assert game.status_text() == "You win - checkmate"

    loss = Game(human_color=chess.WHITE)
    loss._finish("0-1", Reason.TIMEOUT)
    assert loss.status_text() == "Maia-2 wins - time out"

    drawn = Game()
    drawn._finish("1/2-1/2", Reason.THREEFOLD)
    assert drawn.status_text() == "Draw - threefold repetition"


def test_legal_destinations_cover_every_legal_move(clock):
    game = Game()
    dests = game.legal_destinations()
    total = sum(len(targets) for targets in dests.values())
    assert total == game.board.legal_moves.count() == 20


def test_promotion_destinations_are_not_duplicated(clock):
    game = game_from("8/4P1k1/8/8/8/8/8/4K3 w - - 0 1")
    assert game.legal_destinations()["e7"] == ["e8"]


def test_material_count_and_simplification(clock):
    full = Game()
    assert full.non_king_material() == 78
    assert full.is_simplified() is False

    endgame = game_from("8/8/4k3/8/8/4K3/4P3/8 w - - 0 1")
    assert endgame.non_king_material() == 1
    assert endgame.is_simplified() is True


def test_check_square_is_reported_for_the_king_in_check(clock):
    game = game_from("4k3/8/8/8/8/8/8/4K2R w - - 0 1")
    assert game.check_square() is None
    game.apply_move("h1h8")
    assert game.board.is_check()
    assert game.check_square() == "e8"


# --- PGN ------------------------------------------------------------------
def test_pgn_has_the_headers_the_brief_asks_for(clock):
    game = Game(human_color=chess.WHITE, rated=True, initial_seconds=600, increment_seconds=5)
    game.human_rating = 1452.3
    game.bot_rating = 1501.8
    game.bucket_label = "1400-1499"
    game.conditioning_elo = 1450
    for move in ["e2e4", "e7e5", "g1f3"]:
        game.apply_move(move)
    game.resign(chess.BLACK)

    pgn = game.to_pgn()
    for header in (
        '[White "You"]',
        '[Black "Maia-2"]',
        '[Result "1-0"]',
        '[WhiteElo "1452"]',
        '[BlackElo "1502"]',
        '[TimeControl "600+5"]',
        '[Termination "Normal"]',
        '[Variant "Standard"]',
    ):
        assert header in pgn, header
    assert "1400-1499" in pgn  # annotator line records the conditioning bucket
    assert "[%clk" in pgn  # per-move clock comments
    assert pgn.rstrip().endswith("1-0")


def test_pgn_marks_a_flag_as_a_time_forfeit(clock):
    game = Game()
    game.apply_move("e2e4")
    clock.advance(10_000)
    game.check_flag()
    assert '[Termination "Time forfeit"]' in game.to_pgn()


def test_pgn_colours_follow_the_player(clock):
    game = Game(human_color=chess.BLACK)
    game.human_rating = 1300
    game.bot_rating = 1700
    pgn = game.to_pgn()
    assert '[White "Maia-2"]' in pgn
    assert '[Black "You"]' in pgn
    assert '[WhiteElo "1700"]' in pgn
    assert '[BlackElo "1300"]' in pgn


def test_pgn_round_trips_back_into_moves(clock):
    game = Game()
    played = ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6"]
    for move in played:
        game.apply_move(move)
    game.agree_draw()
    recovered = parse_pgn_moves(game.to_pgn())
    assert [move.uci() for move in recovered] == played


def test_unfinished_game_pgn_is_marked_open(clock):
    game = Game()
    game.apply_move("e2e4")
    pgn = game.to_pgn()
    assert '[Result "*"]' in pgn
    assert '[Termination "Unterminated"]' in pgn


def test_parse_pgn_moves_tolerates_rubbish(clock):
    assert parse_pgn_moves("") == []
