"""The Maia-2 bot.

Maia-2 gives a probability for every legal move, conditioned on the ratings of
both players. We sample from that distribution rather than taking the top move,
which is what makes the bot non-deterministic and human-like: the same position
gets different replies, and sometimes it picks the human mistake.

Two things about the model are worth knowing and are easy to get wrong:

* `inference_each` returns `(move_probs, win_prob)` where `win_prob` is from
  **White's** perspective, not the side to move's. The library mirrors the board
  for Black before inference, and the `1 - x` flip in `maia2.inference` converts
  the side-to-move value back to White's. Verified against Stockfish on matched
  positions; see the table in `config.py`.
* The probabilities are rounded to four decimal places, so they sum to roughly
  0.9998-1.0003 rather than exactly 1. Anything that samples from them has to
  renormalise.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

import chess

from . import config


def bucket_count() -> int:
    """Number of rating buckets Maia-2 was trained with (11 for this model)."""
    return (config.BUCKET_END - config.BUCKET_START) // config.BUCKET_WIDTH + 2


def bucket_index(elo: float) -> int:
    """Which conditioning bucket a rating falls into.

    Mirrors `maia2.utils.map_to_category`: an open bucket below 1100, then 100
    wide up to 1999, then an open bucket from 2000 up.
    """
    if elo < config.BUCKET_START:
        return 0
    if elo >= config.BUCKET_END:
        return bucket_count() - 1
    return int((elo - config.BUCKET_START) // config.BUCKET_WIDTH) + 1


def bucket_label(index: int) -> str:
    if index <= 0:
        return f"<{config.BUCKET_START}"
    if index >= bucket_count() - 1:
        return f">={config.BUCKET_END}"
    low = config.BUCKET_START + (index - 1) * config.BUCKET_WIDTH
    return f"{low}-{low + config.BUCKET_WIDTH - 1}"


def bucket_nominal_rating(index: int) -> int:
    """The rating we initialise a bucket's Glicko record at: its midpoint.

    Also the value we feed the model, since every rating inside a bucket gives
    byte-identical output -- 1400, 1450 and 1499 all return the same
    distribution -- so snapping to the midpoint keeps conditioning stable as the
    player's rating drifts inside a bucket.
    """
    if index <= 0:
        return config.BUCKET_START - config.BUCKET_WIDTH // 2
    if index >= bucket_count() - 1:
        return config.BUCKET_END + config.BUCKET_WIDTH // 2
    low = config.BUCKET_START + (index - 1) * config.BUCKET_WIDTH
    return low + config.BUCKET_WIDTH // 2


@dataclass
class BotDecision:
    uci: str
    san: str
    think_seconds: float
    white_win_prob: float
    bot_win_prob: float
    move_probs: dict[str, float]
    sampled_prob: float
    was_top_move: bool


def legal_distribution(board: chess.Board, raw: dict[str, float]) -> dict[str, float]:
    """Keep only legal moves and renormalise.

    Maia-2 masks illegal moves itself, but we never trust that: a stray key, a
    castling or promotion spelled differently, or a model change would otherwise
    put an illegal move on the board.
    """
    legal = {move.uci() for move in board.legal_moves}
    kept = {uci: p for uci, p in raw.items() if uci in legal and p > 0.0}
    total = sum(kept.values())
    if not kept or total <= 0.0:
        # Fall back to a uniform choice so the bot always has a move.
        return {uci: 1.0 / len(legal) for uci in legal} if legal else {}
    return {uci: p / total for uci, p in kept.items()}


def sample_move(
    distribution: dict[str, float],
    temperature: float = config.DEFAULT_TEMPERATURE,
    rng: random.Random | None = None,
) -> str:
    """Draw a move, sharpened or flattened by `temperature`.

    Weights are p ** (1 / T): T = 1 samples the model's own distribution, lower
    plays more predictably, higher blunders more. Very low T is the argmax.
    """
    if not distribution:
        raise ValueError("no moves to sample from")
    rng = rng or random
    if temperature <= config.MIN_TEMPERATURE:
        return max(distribution, key=lambda uci: distribution[uci])

    exponent = 1.0 / temperature
    weights = {uci: p**exponent for uci, p in distribution.items()}
    total = sum(weights.values())
    if total <= 0.0 or not math.isfinite(total):
        return max(distribution, key=lambda uci: distribution[uci])
    threshold = rng.random() * total
    cumulative = 0.0
    for uci, weight in weights.items():
        cumulative += weight
        if cumulative >= threshold:
            return uci
    return next(reversed(weights))


def thinking_seconds(
    distribution: dict[str, float],
    legal_count: int,
    rng: random.Random | None = None,
) -> float:
    """How long to pause before moving.

    Maia-2 answers in about 10ms, so this is entirely for the feel of the game.
    Complex positions -- many legal moves, no obvious candidate -- take longer;
    forced and obvious moves come back quickly.
    """
    rng = rng or random
    if legal_count <= 1:
        return config.THINK_SECONDS_FORCED

    probs = [p for p in distribution.values() if p > 0.0]
    if len(probs) > 1:
        entropy = -sum(p * math.log(p) for p in probs)
        normalised_entropy = entropy / math.log(len(probs))
    else:
        normalised_entropy = 0.0

    breadth = min(1.0, legal_count / 40.0)
    complexity = 0.6 * normalised_entropy + 0.4 * breadth

    span = config.THINK_SECONDS_MAX - config.THINK_SECONDS_MIN
    seconds = config.THINK_SECONDS_MIN + span * complexity

    if distribution and max(distribution.values()) >= config.THINK_OBVIOUS_PROB:
        seconds = config.THINK_SECONDS_MIN + 0.25 * (seconds - config.THINK_SECONDS_MIN)

    jitter = 1.0 + rng.uniform(-config.THINK_JITTER, config.THINK_JITTER)
    return min(config.THINK_SECONDS_MAX, max(config.THINK_SECONDS_MIN, seconds * jitter))


def bot_win_probability(white_win_prob: float, bot_color: bool) -> float:
    """Convert Maia-2's White-perspective value into the bot's own."""
    return white_win_prob if bot_color == chess.WHITE else 1.0 - white_win_prob


class MaiaBot:
    """Thin wrapper over the maia2 package, loaded once and reused."""

    def __init__(self) -> None:
        self._model = None
        self._prepared = None
        self.device = "unloaded"

    @property
    def ready(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Download if needed, move to the best device, and warm up.

        The first forward pass costs several seconds on MPS while kernels
        compile; later ones are around 10ms. Doing it here keeps that cost out
        of the bot's clock during a game.
        """
        if self._model is not None:
            return
        from maia2 import inference
        from maia2 import model as maia_model

        config.MODEL_DIR.mkdir(parents=True, exist_ok=True)
        self._model = maia_model.from_pretrained(
            type=config.MAIA_MODEL_TYPE,
            device=config.MAIA_DEVICE,
            save_root=str(config.MODEL_DIR),
        )
        self._prepared = inference.prepare()
        try:
            self.device = str(next(self._model.parameters()).device)
        except Exception:  # pragma: no cover - diagnostic only
            self.device = config.MAIA_DEVICE
        self.evaluate(chess.STARTING_FEN, 1450, 1450)

    def evaluate(self, fen: str, elo_self: int, elo_oppo: int) -> tuple[dict[str, float], float]:
        """Raw model output: (move probabilities by UCI, White-perspective value)."""
        if self._model is None:
            raise RuntimeError("model not loaded")
        from maia2 import inference

        move_probs, white_win_prob = inference.inference_each(
            self._model, self._prepared, fen, elo_self, elo_oppo
        )
        return move_probs, float(white_win_prob)

    def decide(
        self,
        board: chess.Board,
        conditioning_elo: int,
        temperature: float = config.DEFAULT_TEMPERATURE,
        rng: random.Random | None = None,
    ) -> BotDecision:
        """Pick a move for the side to move, and say how long to take over it."""
        raw, white_win_prob = self.evaluate(board.fen(), conditioning_elo, conditioning_elo)
        distribution = legal_distribution(board, raw)
        uci = sample_move(distribution, temperature, rng=rng)
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:  # pragma: no cover - belt and braces
            move = next(iter(board.legal_moves))
            uci = move.uci()

        legal_count = board.legal_moves.count()
        top = max(distribution, key=lambda key: distribution[key]) if distribution else uci
        return BotDecision(
            uci=uci,
            san=board.san(move),
            think_seconds=thinking_seconds(distribution, legal_count, rng=rng),
            white_win_prob=white_win_prob,
            bot_win_prob=bot_win_probability(white_win_prob, board.turn),
            move_probs=distribution,
            sampled_prob=distribution.get(uci, 0.0),
            was_top_move=uci == top,
        )


def should_accept_draw(bot_win_prob: float, simplified: bool, ply: int) -> bool:
    """Accept only from a balanced position that has actually simplified.

    A human does not agree to a draw while winning, while losing, or on move 6
    with all the pieces on.
    """
    if ply < config.MIN_PLY_FOR_DRAW_OR_RESIGN:
        return False
    if not simplified:
        return False
    return config.DRAW_ACCEPT_WIN_PROB_MIN <= bot_win_prob <= config.DRAW_ACCEPT_WIN_PROB_MAX


def should_resign(low_streak: int, ply: int) -> bool:
    """Resign once the position has looked hopeless for several moves running."""
    if ply < config.MIN_PLY_FOR_DRAW_OR_RESIGN:
        return False
    return low_streak >= config.RESIGN_CONSECUTIVE_MOVES
