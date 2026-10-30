"""Glicko-2 checked against the worked example in Glickman's paper.

"Example of the Glicko-2 system" (http://www.glicko.net/glicko/glicko2.pdf)
takes a player at 1500/200/0.06 with tau=0.5 through one rating period against
three opponents, and reports every intermediate quantity. We check those too,
not just the final rating, so a regression points at the step that broke.
"""

import math

import pytest

from app.glicko2 import DRAW, LOSS, SCALE, WIN, Rating, _g, decay, rate, rate_one_game

# The player and the three opponents from section 3 of the paper.
PLAYER = Rating(1500.0, 200.0, 0.06)
RESULTS = [
    (Rating(1400.0, 30.0, 0.06), WIN),
    (Rating(1550.0, 100.0, 0.06), LOSS),
    (Rating(1700.0, 300.0, 0.06), LOSS),
]


def test_scale_conversion():
    assert PLAYER.mu == pytest.approx(0.0, abs=1e-9)
    assert PLAYER.phi == pytest.approx(1.1513, abs=5e-5)
    assert RESULTS[0][0].mu == pytest.approx(-0.5756, abs=5e-5)
    assert RESULTS[1][0].mu == pytest.approx(0.2878, abs=5e-5)
    assert RESULTS[2][0].mu == pytest.approx(1.1513, abs=5e-5)


def test_g_and_expected_scores():
    """Table 1 of the paper: g(phi_j) and E for each opponent."""
    expected_g = [0.9955, 0.9531, 0.7242]
    expected_e = [0.639, 0.432, 0.303]
    for (opponent, _), want_g, want_e in zip(RESULTS, expected_g, expected_e):
        assert _g(opponent.phi) == pytest.approx(want_g, abs=5e-5)
        e = 1.0 / (1.0 + math.exp(-_g(opponent.phi) * (PLAYER.mu - opponent.mu)))
        assert e == pytest.approx(want_e, abs=5e-4)


def test_paper_example_final_values():
    """The paper's answer: r' = 1464.06, RD' = 151.52, sigma' = 0.05999."""
    new = rate(PLAYER, RESULTS, tau=0.5)
    assert new.rating == pytest.approx(1464.06, abs=0.01)
    assert new.rd == pytest.approx(151.52, abs=0.01)
    assert new.volatility == pytest.approx(0.05999, abs=1e-5)


def test_paper_example_intermediates():
    """v = 1.7785 and Delta = -0.4834, reconstructed from the public helpers."""
    variance_sum = 0.0
    score_sum = 0.0
    for opponent, score in RESULTS:
        g_j = _g(opponent.phi)
        e = 1.0 / (1.0 + math.exp(-g_j * (PLAYER.mu - opponent.mu)))
        variance_sum += g_j * g_j * e * (1.0 - e)
        score_sum += g_j * (score - e)
    v = 1.0 / variance_sum
    assert v == pytest.approx(1.7785, abs=5e-4)
    # The paper quotes Delta = -0.4834, but computes it from the values it
    # already rounded in Table 1, which costs about 5e-4 of precision. The
    # final rating below is the exact check.
    assert v * score_sum == pytest.approx(-0.4834, abs=1e-3)

    # New rating on the internal scale: mu' = -0.2069, phi' = 0.8722.
    new = rate(PLAYER, RESULTS, tau=0.5)
    assert (new.rating - 1500.0) / SCALE == pytest.approx(-0.2069, abs=5e-4)
    assert new.rd / SCALE == pytest.approx(0.8722, abs=5e-4)


def test_winning_raises_and_losing_lowers():
    opponent = Rating(1500.0, 50.0, 0.06)
    assert rate_one_game(PLAYER, opponent, WIN).rating > PLAYER.rating
    assert rate_one_game(PLAYER, opponent, LOSS).rating < PLAYER.rating


def test_deviation_shrinks_when_you_play():
    after = rate_one_game(PLAYER, Rating(1500.0, 50.0, 0.06), DRAW)
    assert after.rd < PLAYER.rd


def test_draw_against_equal_opponent_barely_moves_rating():
    """A settled player drawing an identical opponent should stay put."""
    settled = Rating(1500.0, 40.0, 0.06)
    after = rate_one_game(settled, Rating(1500.0, 40.0, 0.06), DRAW)
    assert after.rating == pytest.approx(1500.0, abs=0.5)


def test_upset_moves_rating_more_than_expected_result():
    """Beating a much stronger player gains more than beating a weaker one."""
    strong_win = rate_one_game(PLAYER, Rating(2000.0, 50.0, 0.06), WIN)
    weak_win = rate_one_game(PLAYER, Rating(1000.0, 50.0, 0.06), WIN)
    assert strong_win.rating - PLAYER.rating > weak_win.rating - PLAYER.rating


def test_high_deviation_player_moves_further():
    """A provisional player's rating should react faster than a settled one's."""
    provisional = Rating(1500.0, 350.0, 0.06)
    settled = Rating(1500.0, 50.0, 0.06)
    opponent = Rating(1500.0, 50.0, 0.06)
    jump = rate_one_game(provisional, opponent, WIN).rating - 1500.0
    nudge = rate_one_game(settled, opponent, WIN).rating - 1500.0
    assert jump > nudge


def test_no_games_decays_deviation_only():
    after = rate(PLAYER, [])
    assert after.rating == PLAYER.rating
    assert after.volatility == PLAYER.volatility
    assert after.rd > PLAYER.rd
    # phi* = sqrt(phi^2 + sigma^2) on the internal scale
    assert after.rd == pytest.approx(
        math.sqrt(PLAYER.phi**2 + PLAYER.volatility**2) * SCALE, abs=1e-6
    )
    assert decay(PLAYER) == after


def test_deviation_is_capped_in_practice_by_repeated_decay():
    """Decay must converge upward slowly, not explode."""
    r = Rating(1500.0, 350.0, 0.06)
    for _ in range(50):
        r = decay(r)
    assert r.rd < 600.0
