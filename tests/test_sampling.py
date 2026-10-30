"""Move sampling: always legal, genuinely varied, and steered by temperature.

The pure functions are tested directly. At the end there are a few tests
against the real Maia-2 model, since the point of sampling is that the bot is
not deterministic and that is only worth asserting on real output.
"""

import random

import chess
import pytest

from app import config
from app.bot import (
    MaiaBot,
    bot_win_probability,
    bucket_count,
    bucket_index,
    bucket_label,
    bucket_nominal_rating,
    legal_distribution,
    sample_move,
    should_accept_draw,
    should_resign,
    thinking_seconds,
)

START = chess.Board()


# --- the distribution -----------------------------------------------------
def test_illegal_moves_are_dropped():
    raw = {"e2e4": 0.5, "e2e5": 0.3, "a1a8": 0.2}  # last two are illegal
    distribution = legal_distribution(START, raw)
    assert set(distribution) == {"e2e4"}
    assert distribution["e2e4"] == pytest.approx(1.0)


def test_distribution_is_renormalised():
    """Maia-2 rounds to four places, so its output does not sum to exactly 1."""
    raw = {"e2e4": 0.3242, "d2d4": 0.0752, "g1f3": 0.2399}
    distribution = legal_distribution(START, raw)
    assert sum(distribution.values()) == pytest.approx(1.0)
    # Relative weights survive renormalisation.
    assert distribution["e2e4"] > distribution["g1f3"] > distribution["d2d4"]


def test_zero_probability_moves_are_dropped():
    distribution = legal_distribution(START, {"e2e4": 1.0, "d2d4": 0.0})
    assert set(distribution) == {"e2e4"}


def test_empty_model_output_falls_back_to_uniform_legal_moves():
    distribution = legal_distribution(START, {})
    assert len(distribution) == START.legal_moves.count()
    assert sum(distribution.values()) == pytest.approx(1.0)


def test_all_illegal_model_output_falls_back_to_uniform():
    distribution = legal_distribution(START, {"a1a8": 0.9, "h1h8": 0.1})
    assert len(distribution) == 20
    assert all(value == pytest.approx(0.05) for value in distribution.values())


def test_fallback_is_empty_in_a_terminal_position():
    mated = chess.Board("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")
    assert mated.is_checkmate()
    assert legal_distribution(mated, {}) == {}


# --- sampling -------------------------------------------------------------
def test_sampled_moves_are_always_legal():
    rng = random.Random(7)
    board = chess.Board("r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4")
    distribution = legal_distribution(board, {move.uci(): 1.0 for move in board.legal_moves})
    for _ in range(600):
        uci = sample_move(distribution, 1.0, rng=rng)
        assert chess.Move.from_uci(uci) in board.legal_moves


def test_sampling_actually_varies():
    """The whole point: the same position does not always get the same move."""
    rng = random.Random(11)
    distribution = {"e2e4": 0.35, "g1f3": 0.3, "d2d4": 0.2, "c2c4": 0.15}
    seen = {sample_move(distribution, 1.0, rng=rng) for _ in range(300)}
    assert len(seen) >= 3


def test_sampling_respects_the_distribution():
    rng = random.Random(3)
    distribution = {"e2e4": 0.8, "g1f3": 0.1, "d2d4": 0.1}
    draws = [sample_move(distribution, 1.0, rng=rng) for _ in range(4000)]
    share = draws.count("e2e4") / len(draws)
    assert 0.76 < share < 0.84


def test_zero_temperature_is_deterministic():
    distribution = {"e2e4": 0.4, "g1f3": 0.45, "d2d4": 0.15}
    picks = {sample_move(distribution, 0.0) for _ in range(50)}
    assert picks == {"g1f3"}


def test_temperature_below_the_floor_is_also_the_argmax():
    distribution = {"e2e4": 0.4, "g1f3": 0.45}
    assert sample_move(distribution, config.MIN_TEMPERATURE / 2) == "g1f3"


def test_low_temperature_plays_the_top_move_more_often_than_high():
    distribution = {"e2e4": 0.5, "g1f3": 0.3, "d2d4": 0.2}
    cold = [sample_move(distribution, 0.4, rng=random.Random(5)) for _ in range(1500)]
    hot = [sample_move(distribution, 2.0, rng=random.Random(5)) for _ in range(1500)]
    assert cold.count("e2e4") / 1500 > hot.count("e2e4") / 1500


def test_high_temperature_flattens_towards_uniform():
    distribution = {"e2e4": 0.7, "g1f3": 0.2, "d2d4": 0.1}
    hot = [sample_move(distribution, 5.0, rng=random.Random(9)) for _ in range(3000)]
    share = hot.count("e2e4") / 3000
    assert share < 0.55


def test_sampling_a_single_move_always_returns_it():
    assert sample_move({"e2e4": 1.0}, 1.0) == "e2e4"


def test_sampling_an_empty_distribution_raises():
    with pytest.raises(ValueError):
        sample_move({}, 1.0)


# --- thinking time --------------------------------------------------------
def test_thinking_time_stays_in_range():
    rng = random.Random(2)
    for size in (2, 5, 20, 40, 60):
        distribution = {f"m{i}": 1.0 / size for i in range(size)}
        seconds = thinking_seconds(distribution, size, rng=rng)
        assert config.THINK_SECONDS_MIN <= seconds <= config.THINK_SECONDS_MAX


def test_a_forced_move_is_played_almost_at_once():
    seconds = thinking_seconds({"e1e2": 1.0}, 1)
    assert seconds == config.THINK_SECONDS_FORCED
    assert seconds < config.THINK_SECONDS_MIN


def test_an_obvious_move_is_faster_than_a_murky_one():
    rng = random.Random(1)
    obvious = {"e2e4": 0.93, "d2d4": 0.04, "g1f3": 0.03}
    murky = {f"m{i}": 1.0 / 30 for i in range(30)}
    fast = min(thinking_seconds(obvious, 30, rng=rng) for _ in range(40))
    slow = max(thinking_seconds(murky, 30, rng=rng) for _ in range(40))
    assert fast < slow


def test_a_complex_position_takes_longer_than_a_simple_one():
    rng = random.Random(4)
    simple = {"a": 0.5, "b": 0.5}
    complex_position = {f"m{i}": 1.0 / 45 for i in range(45)}
    simple_mean = sum(thinking_seconds(simple, 2, rng=rng) for _ in range(200)) / 200
    complex_mean = sum(thinking_seconds(complex_position, 45, rng=rng) for _ in range(200)) / 200
    assert complex_mean > simple_mean


# --- win probability perspective -----------------------------------------
def test_white_perspective_is_converted_for_the_bot():
    """Maia-2's value is White's. Getting this backwards inverts resign logic."""
    assert bot_win_probability(0.9, chess.WHITE) == pytest.approx(0.9)
    assert bot_win_probability(0.9, chess.BLACK) == pytest.approx(0.1)
    assert bot_win_probability(0.5, chess.WHITE) == pytest.approx(0.5)
    assert bot_win_probability(0.5, chess.BLACK) == pytest.approx(0.5)


# --- buckets --------------------------------------------------------------
def test_bucket_boundaries():
    assert bucket_count() == 11
    assert bucket_index(500) == 0
    assert bucket_index(1099) == 0
    assert bucket_index(1100) == 1
    assert bucket_index(1450) == 4
    assert bucket_index(1999) == 9
    assert bucket_index(2000) == 10
    assert bucket_index(3000) == 10


def test_bucket_labels_and_nominal_ratings():
    assert bucket_label(0) == "<1100"
    assert bucket_label(4) == "1400-1499"
    assert bucket_label(10) == ">=2000"
    assert bucket_nominal_rating(4) == 1450
    assert bucket_nominal_rating(0) == 1050
    assert bucket_nominal_rating(10) == 2050


def test_our_bucketing_matches_maia2s_own():
    """Guards against the library changing its buckets under us."""
    from maia2.utils import create_elo_dict, map_to_category

    elo_dict = create_elo_dict()
    assert len(elo_dict) == bucket_count()
    for elo in range(600, 2600, 7):
        assert bucket_index(elo) == map_to_category(elo, elo_dict), elo
    for index, label in enumerate(elo_dict):
        assert bucket_label(index) == label


def test_every_nominal_rating_lands_back_in_its_own_bucket():
    for index in range(bucket_count()):
        assert bucket_index(bucket_nominal_rating(index)) == index


# --- draw and resign decisions -------------------------------------------
def test_draw_is_accepted_only_from_a_balanced_simplified_position():
    assert should_accept_draw(0.5, simplified=True, ply=40) is True
    assert should_accept_draw(0.5, simplified=False, ply=40) is False
    assert should_accept_draw(0.8, simplified=True, ply=40) is False
    assert should_accept_draw(0.1, simplified=True, ply=40) is False


def test_draw_window_edges_are_inclusive():
    assert should_accept_draw(config.DRAW_ACCEPT_WIN_PROB_MIN, True, 40) is True
    assert should_accept_draw(config.DRAW_ACCEPT_WIN_PROB_MAX, True, 40) is True
    assert should_accept_draw(config.DRAW_ACCEPT_WIN_PROB_MIN - 0.01, True, 40) is False
    assert should_accept_draw(config.DRAW_ACCEPT_WIN_PROB_MAX + 0.01, True, 40) is False


def test_no_draws_agreed_in_the_opening():
    assert should_accept_draw(0.5, simplified=True, ply=4) is False


def test_resignation_needs_the_full_streak():
    needed = config.RESIGN_CONSECUTIVE_MOVES
    assert should_resign(needed - 1, ply=40) is False
    assert should_resign(needed, ply=40) is True
    assert should_resign(needed + 5, ply=40) is True


def test_no_resignation_in_the_opening():
    assert should_resign(config.RESIGN_CONSECUTIVE_MOVES, ply=4) is False


# --- against the real model ----------------------------------------------
@pytest.fixture(scope="session")
def maia():
    bot = MaiaBot()
    try:
        bot.load()
    except Exception as exc:  # pragma: no cover - no model available
        pytest.skip(f"Maia-2 unavailable: {exc}")
    return bot


@pytest.mark.slow
def test_real_model_only_ever_offers_legal_moves(maia):
    positions = [
        chess.STARTING_FEN,
        "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3",
        "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1",
        "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
        "6k1/5p1p/6p1/8/8/6P1/5P1P/6K1 w - - 0 1",
        "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 b - - 0 1",
    ]
    for fen in positions:
        board = chess.Board(fen)
        raw, _ = maia.evaluate(fen, 1450, 1450)
        distribution = legal_distribution(board, raw)
        legal = {move.uci() for move in board.legal_moves}
        assert set(distribution) <= legal
        assert sum(distribution.values()) == pytest.approx(1.0)
        for _ in range(60):
            assert sample_move(distribution, 1.0) in legal


@pytest.mark.slow
def test_real_model_refuses_a_position_with_no_legal_moves(maia):
    """maia2 raises rather than returning an empty distribution.

    The server never asks for a move in a finished position -- `Game` detects
    the end first -- but pin the behaviour so a change here is noticed.
    """
    stalemate = "8/8/8/8/8/4k3/4p3/4K3 w - - 0 1"
    assert chess.Board(stalemate).legal_moves.count() == 0
    with pytest.raises(ValueError):
        maia.evaluate(stalemate, 1450, 1450)


@pytest.mark.slow
def test_real_model_is_not_deterministic_at_temperature_one(maia):
    """The same position must not always get the same reply."""
    board = chess.Board()
    raw, _ = maia.evaluate(board.fen(), 1450, 1450)
    distribution = legal_distribution(board, raw)
    rng = random.Random(0)
    picks = {sample_move(distribution, 1.0, rng=rng) for _ in range(200)}
    assert len(picks) >= 4


@pytest.mark.slow
def test_real_model_is_deterministic_at_temperature_zero(maia):
    board = chess.Board()
    raw, _ = maia.evaluate(board.fen(), 1450, 1450)
    distribution = legal_distribution(board, raw)
    assert len({sample_move(distribution, 0.0) for _ in range(20)}) == 1


@pytest.mark.slow
def test_real_model_value_head_is_white_perspective(maia):
    """The README and the library's code disagree; this pins down the truth."""
    _, white_crushing = maia.evaluate("7k/8/8/8/8/8/5PPP/5QRK w - - 0 1", 1450, 1450)
    _, black_crushing = maia.evaluate("5qrk/5ppp/8/8/8/8/8/7K w - - 0 1", 1450, 1450)
    assert white_crushing > 0.6
    assert black_crushing < 0.4
    # Same evaluation sign regardless of whose turn it is to move.
    _, white_crushing_black_to_move = maia.evaluate(
        "7k/8/8/8/8/8/5PPP/5QRK b - - 0 1", 1450, 1450
    )
    assert white_crushing_black_to_move > 0.6


@pytest.mark.slow
def test_real_model_decide_returns_a_playable_move(maia):
    board = chess.Board()
    decision = maia.decide(board, 1450, 1.0, rng=random.Random(1))
    assert chess.Move.from_uci(decision.uci) in board.legal_moves
    assert decision.san
    assert config.THINK_SECONDS_FORCED <= decision.think_seconds <= config.THINK_SECONDS_MAX
    assert 0.0 <= decision.bot_win_prob <= 1.0


@pytest.mark.slow
def test_conditioning_is_stable_inside_a_bucket(maia):
    """Every rating in a bucket gives identical output, which is why we snap."""
    low, _ = maia.evaluate(chess.STARTING_FEN, 1400, 1400)
    mid, _ = maia.evaluate(chess.STARTING_FEN, 1450, 1450)
    high, _ = maia.evaluate(chess.STARTING_FEN, 1499, 1499)
    assert low == mid == high
    other, _ = maia.evaluate(chess.STARTING_FEN, 1950, 1950)
    assert other != mid
