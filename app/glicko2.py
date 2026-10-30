"""Glicko-2, implemented from Mark Glickman's "Example of the Glicko-2 system".

Lichess uses this same system. The one place implementations usually differ is
step 5, finding the new volatility: it has no closed form, so the paper
prescribes the Illinois variant of regula falsi, which is what `_new_volatility`
below does. `tests/test_glicko2.py` checks the whole thing against the worked
example in the paper.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import GLICKO_DEFAULT_RD, GLICKO_DEFAULT_VOLATILITY, GLICKO_TAU

# The Glicko-2 scale is the Glicko scale divided by this constant.
SCALE = 173.7178
CENTER = 1500.0
CONVERGENCE = 1e-6

WIN = 1.0
DRAW = 0.5
LOSS = 0.0


@dataclass(frozen=True)
class Rating:
    """A player's rating, deviation and volatility on the usual Glicko scale."""

    rating: float = CENTER
    rd: float = GLICKO_DEFAULT_RD
    volatility: float = GLICKO_DEFAULT_VOLATILITY

    @property
    def mu(self) -> float:
        return (self.rating - CENTER) / SCALE

    @property
    def phi(self) -> float:
        return self.rd / SCALE


def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def _expected(mu: float, mu_j: float, phi_j: float) -> float:
    return 1.0 / (1.0 + math.exp(-_g(phi_j) * (mu - mu_j)))


def _new_volatility(phi: float, v: float, delta: float, volatility: float, tau: float) -> float:
    """Step 5 of the paper: solve for sigma' by Illinois regula falsi."""
    a = math.log(volatility * volatility)
    delta_sq = delta * delta
    phi_sq = phi * phi

    def f(x: float) -> float:
        ex = math.exp(x)
        numerator = ex * (delta_sq - phi_sq - v - ex)
        denominator = 2.0 * (phi_sq + v + ex) ** 2
        return numerator / denominator - (x - a) / (tau * tau)

    lower = a
    if delta_sq > phi_sq + v:
        upper = math.log(delta_sq - phi_sq - v)
    else:
        k = 1
        while f(a - k * tau) < 0.0:
            k += 1
        upper = a - k * tau

    f_lower, f_upper = f(lower), f(upper)
    while abs(upper - lower) > CONVERGENCE:
        mid = lower + (lower - upper) * f_lower / (f_upper - f_lower)
        f_mid = f(mid)
        if f_mid * f_upper <= 0.0:
            lower, f_lower = upper, f_upper
        else:
            f_lower /= 2.0
        upper, f_upper = mid, f_mid

    return math.exp(lower / 2.0)


def rate(player: Rating, results: list[tuple[Rating, float]], tau: float = GLICKO_TAU) -> Rating:
    """Return the player's new rating after a rating period.

    `results` pairs each opponent's rating with the player's score against it
    (1 win, 0.5 draw, 0 loss). We treat one game as one rating period, which is
    what lichess does for its live ratings.
    """
    if not results:
        return decay(player)

    mu, phi = player.mu, player.phi

    variance_sum = 0.0
    score_sum = 0.0
    for opponent, score in results:
        g_j = _g(opponent.phi)
        expected = _expected(mu, opponent.mu, opponent.phi)
        variance_sum += g_j * g_j * expected * (1.0 - expected)
        score_sum += g_j * (score - expected)

    v = 1.0 / variance_sum
    delta = v * score_sum

    volatility = _new_volatility(phi, v, delta, player.volatility, tau)
    phi_star = math.sqrt(phi * phi + volatility * volatility)
    new_phi = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    new_mu = mu + new_phi * new_phi * score_sum

    return Rating(
        rating=new_mu * SCALE + CENTER,
        rd=new_phi * SCALE,
        volatility=volatility,
    )


def decay(player: Rating) -> Rating:
    """A rating period with no games: the rating holds, the deviation grows."""
    phi_star = math.sqrt(player.phi * player.phi + player.volatility * player.volatility)
    return Rating(
        rating=player.rating,
        rd=phi_star * SCALE,
        volatility=player.volatility,
    )


def rate_one_game(player: Rating, opponent: Rating, score: float, tau: float = GLICKO_TAU) -> Rating:
    return rate(player, [(opponent, score)], tau=tau)
