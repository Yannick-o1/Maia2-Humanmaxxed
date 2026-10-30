"""The lichess scoring formulas used by the post-game review."""

import chess
import pytest

from app import config
from app.engine import (
    accuracy_from_win_percents,
    analyse_game,
    classify,
    game_accuracy,
    win_percent,
    winning_chances,
)


def test_win_percent_is_fifty_at_a_dead_level_evaluation():
    assert win_percent(0) == pytest.approx(50.0)


def test_win_percent_is_symmetric():
    for centipawns in (50, 150, 400, 900):
        assert win_percent(centipawns) + win_percent(-centipawns) == pytest.approx(100.0)


def test_win_percent_matches_known_lichess_values():
    """Spot values from the curve lichess uses (multiplier -0.00368208)."""
    assert win_percent(100) == pytest.approx(59.1, abs=0.1)
    assert win_percent(300) == pytest.approx(75.1, abs=0.1)
    assert win_percent(1000) == pytest.approx(97.5, abs=0.1)


def test_win_percent_is_clamped_at_the_ceiling():
    """Centipawns are clamped to +/-1000 before the curve, as lichess does."""
    assert win_percent(5000) == win_percent(config.CP_CEILING)
    assert win_percent(-5000) == win_percent(-config.CP_CEILING)
    assert -1.0 <= winning_chances(100_000) <= 1.0
    assert winning_chances(100_000) == winning_chances(config.CP_CEILING)
    assert win_percent(config.CP_CEILING) == pytest.approx(97.5, abs=0.1)


def test_a_move_that_does_not_lose_ground_is_perfect():
    assert accuracy_from_win_percents(50.0, 50.0) == 100.0
    assert accuracy_from_win_percents(50.0, 70.0) == 100.0


def test_accuracy_falls_as_the_drop_grows():
    values = [accuracy_from_win_percents(80.0, 80.0 - drop) for drop in (5, 10, 20, 40, 60)]
    assert values == sorted(values, reverse=True)
    assert all(0.0 <= value <= 100.0 for value in values)


def test_accuracy_tracks_the_sample_points_lichess_fitted_to():
    """lila's curve was fitted to drop/accuracy pairs recorded in its source."""
    for drop, expected in ((0, 100), (10, 60), (20, 42), (40, 20), (60, 5)):
        got = accuracy_from_win_percents(60.0, 60.0 - drop)
        assert got == pytest.approx(expected, abs=5.0), (drop, got)


def test_accuracy_never_leaves_the_percentage_range():
    assert accuracy_from_win_percents(100.0, 0.0) >= 0.0
    assert accuracy_from_win_percents(0.1, 0.0) <= 100.0


def test_classification_thresholds():
    assert classify(0.0) is None
    assert classify(config.INACCURACY_WIN_DROP - 0.1) is None
    assert classify(config.INACCURACY_WIN_DROP) == "inaccuracy"
    assert classify(config.MISTAKE_WIN_DROP - 0.1) == "inaccuracy"
    assert classify(config.MISTAKE_WIN_DROP) == "mistake"
    assert classify(config.BLUNDER_WIN_DROP - 0.1) == "mistake"
    assert classify(config.BLUNDER_WIN_DROP) == "blunder"
    assert classify(95.0) == "blunder"


def test_perfect_play_scores_full_accuracy_for_both_sides():
    flat = [50.0] * 21
    accuracy = game_accuracy(flat, chess.WHITE)
    assert accuracy["white"] == pytest.approx(100.0, abs=0.1)
    assert accuracy["black"] == pytest.approx(100.0, abs=0.1)


def test_one_sided_collapse_only_hurts_that_side():
    """A drop across an even-indexed step is White's move, so White pays.

    The evaluations are indexed by position, so the step from index i to i+1 is
    move i+1: even indices are White's moves when White started.
    """
    percents = [50.0] * 21 + [5.0]  # the change sits at index 20, a White move
    accuracy = game_accuracy(percents, chess.WHITE)
    assert accuracy["white"] < 90.0
    assert accuracy["black"] == pytest.approx(100.0, abs=0.1)


def test_blacks_blunder_is_attributed_to_black():
    """A rise in White-POV win% across an odd-indexed step is Black's doing."""
    percents = [50.0] * 20 + [95.0]  # the change sits at index 19, a Black move
    accuracy = game_accuracy(percents, chess.WHITE)
    assert accuracy["black"] < 90.0
    assert accuracy["white"] == pytest.approx(100.0, abs=0.1)


def test_a_gain_is_never_penalised():
    """A fall in White-POV win% on Black's move means Black improved."""
    percents = [50.0] * 20 + [5.0]  # index 19 is a Black move when White starts
    accuracy = game_accuracy(percents, chess.WHITE)
    assert accuracy["white"] == pytest.approx(100.0, abs=0.1)
    assert accuracy["black"] == pytest.approx(100.0, abs=0.1)


def test_the_starting_colour_decides_whose_move_each_step_is():
    """The same evaluations are read differently depending on who began."""
    percents = [50.0] * 21 + [95.0]  # the change sits at index 20
    # White starting: index 20 is White's move, and the rise favours White.
    white_start = game_accuracy(percents, chess.WHITE)
    assert white_start["white"] == pytest.approx(100.0, abs=0.1)
    # Black starting: index 20 is Black's move, and that same rise is a loss.
    black_start = game_accuracy(percents, chess.BLACK)
    assert black_start["black"] < 90.0


def test_accuracy_is_none_for_a_game_with_no_moves():
    assert game_accuracy([50.0], chess.WHITE) == {"white": None, "black": None}
    assert game_accuracy([], chess.WHITE) == {"white": None, "black": None}


def test_accuracy_handles_a_very_short_game():
    accuracy = game_accuracy([50.0, 48.0, 52.0], chess.WHITE)
    assert accuracy["white"] is not None
    assert accuracy["black"] is not None


# --- against the real engine ---------------------------------------------
@pytest.mark.slow
def test_stockfish_review_runs_end_to_end():
    import shutil

    if not shutil.which(config.STOCKFISH_PATH) and not shutil.which("stockfish"):
        pytest.skip("Stockfish not installed")

    # Fool's mate: Black's two moves are fine, White's two are catastrophic.
    moves = [chess.Move.from_uci(uci) for uci in ("f2f3", "e7e5", "g2g4", "d8h4")]
    review = analyse_game(moves, chess.WHITE, depth=10)

    assert len(review["positions"]) == len(moves) + 1
    assert len(review["moves"]) == len(moves)
    assert review["accuracy"]["black"] > review["accuracy"]["white"]
    # Every move carries the fields the UI reads.
    for entry in review["moves"]:
        assert set(entry) >= {"san", "winBefore", "winAfter", "drop", "judgment", "bestSan"}
    # 2. g4 hands over a forced mate, so it must be flagged.
    assert review["moves"][2]["judgment"] == "blunder"
    assert review["judgmentCounts"]["white"]["blunder"] >= 1


@pytest.mark.slow
def test_review_progress_is_reported_for_every_position():
    import shutil

    if not shutil.which(config.STOCKFISH_PATH) and not shutil.which("stockfish"):
        pytest.skip("Stockfish not installed")

    seen = []
    moves = [chess.Move.from_uci(uci) for uci in ("e2e4", "e7e5")]
    analyse_game(moves, chess.WHITE, depth=8, on_progress=lambda done, total: seen.append((done, total)))
    assert seen == [(1, 3), (2, 3), (3, 3)]
