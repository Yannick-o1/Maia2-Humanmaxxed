"""Post-game review with Stockfish, scored the way lichess scores it.

The formulas here are not approximations. They were read off the current
lichess sources rather than from memory:

* win percentage -- `WinPercent.winningChances` in lichess-org/scalachess,
  `core/src/main/scala/eval.scala`
* accuracy, and the mean-of-weighted-and-harmonic aggregation --
  `AccuracyPercent` in lichess-org/lila, `modules/analyse/src/main/`

Move classification by drop in win percentage uses the thresholds in
`config.py` (inaccuracy 10, mistake 20, blunder 30).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import chess
import chess.engine

from . import config

GLYPHS = {"inaccuracy": "?!", "mistake": "?", "blunder": "??"}


# --- lichess formulas -----------------------------------------------------
def winning_chances(centipawns: float) -> float:
    """[-1, +1] from White's point of view."""
    clamped = max(-config.CP_CEILING, min(config.CP_CEILING, centipawns))
    raw = 2.0 / (1.0 + math.exp(config.WIN_PERCENT_MULTIPLIER * clamped)) - 1.0
    return max(-1.0, min(1.0, raw))


def win_percent(centipawns: float) -> float:
    """[0, 100] from White's point of view."""
    return 50.0 + 50.0 * winning_chances(centipawns)


def accuracy_from_win_percents(before: float, after: float) -> float:
    """How accurate one move was, given the win% it gave up.

    `before` and `after` are both from the mover's point of view.
    """
    if after >= before:
        return 100.0
    drop = before - after
    raw = config.ACCURACY_A * math.exp(-config.ACCURACY_K * drop) + config.ACCURACY_B
    return max(0.0, min(100.0, raw + config.ACCURACY_UNCERTAINTY_BONUS))


def classify(drop: float) -> str | None:
    if drop >= config.BLUNDER_WIN_DROP:
        return "blunder"
    if drop >= config.MISTAKE_WIN_DROP:
        return "mistake"
    if drop >= config.INACCURACY_WIN_DROP:
        return "inaccuracy"
    return None


def _population_stdev(values: list[float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(variance)


def game_accuracy(white_win_percents: list[float], start_color: bool = chess.WHITE) -> dict[str, float | None]:
    """Per-side accuracy, as lichess computes it.

    `white_win_percents` has one entry per position including the starting one,
    all from White's point of view. Each side's figure is the mean of a
    volatility-weighted mean and a harmonic mean of its per-move accuracies, so
    a blunder in a sharp position counts for more than one in a dead position.
    """
    count = len(white_win_percents)
    if count < 2:
        return {"white": None, "black": None}

    window_size = max(2, min(8, (count - 1) // 10))
    # Pad the front so there is one weight per move, matching lila exactly.
    windows: list[list[float]] = [white_win_percents[:window_size]] * max(
        0, min(window_size, count) - 2
    )
    windows += [
        white_win_percents[i : i + window_size]
        for i in range(0, max(1, count - window_size + 1))
    ]
    weights = [max(0.5, min(12.0, _population_stdev(window))) for window in windows]

    per_color: dict[bool, list[tuple[float, float]]] = {chess.WHITE: [], chess.BLACK: []}
    for index in range(count - 1):
        previous, following = white_win_percents[index], white_win_percents[index + 1]
        weight = weights[index] if index < len(weights) else weights[-1]
        mover = chess.WHITE if ((index % 2 == 0) == (start_color == chess.WHITE)) else chess.BLACK
        if mover == chess.WHITE:
            before, after = previous, following
        else:
            # The numbers are White-POV, so swapping them gives Black's view.
            before, after = following, previous
        per_color[mover].append((accuracy_from_win_percents(before, after), weight))

    out: dict[str, float | None] = {}
    for color, name in ((chess.WHITE, "white"), (chess.BLACK, "black")):
        entries = per_color[color]
        if not entries:
            out[name] = None
            continue
        total_weight = sum(weight for _, weight in entries)
        weighted = (
            sum(value * weight for value, weight in entries) / total_weight
            if total_weight
            else 0.0
        )
        reciprocals = 0.0
        for value, _ in entries:
            if value <= 0.0:
                reciprocals = math.inf
                break
            reciprocals += 1.0 / value
        harmonic = 0.0 if math.isinf(reciprocals) else len(entries) / reciprocals
        out[name] = round((weighted + harmonic) / 2.0, 1)
    return out


# --- running the engine ---------------------------------------------------
@dataclass
class ReviewProgress:
    done: int = 0
    total: int = 0
    finished: bool = False
    error: str | None = None
    result: dict[str, Any] | None = field(default=None)


def _cp_white(score: chess.engine.PovScore) -> tuple[float, int | None]:
    """White-POV centipawns, with mates mapped onto the ceiling like lichess."""
    white = score.white()
    mate = white.mate()
    if mate is not None:
        signed = config.CP_CEILING if mate > 0 else -config.CP_CEILING
        return float(signed), mate
    return float(white.score() or 0), None


def analyse_game(
    moves: list[chess.Move],
    human_color: bool,
    depth: int = config.REVIEW_DEPTH,
    movetime: float = config.REVIEW_MOVETIME,
    engine_path: str = config.STOCKFISH_PATH,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Analyse every position in the game and classify each move.

    Runs synchronously; the server calls it on a worker thread and relays
    `on_progress` to the browser over the websocket.
    """
    total = len(moves) + 1
    limit = (
        chess.engine.Limit(time=movetime)
        if movetime and movetime > 0
        else chess.engine.Limit(depth=depth)
    )

    board = chess.Board()
    positions: list[dict[str, Any]] = []

    engine = chess.engine.SimpleEngine.popen_uci(engine_path)
    try:
        engine.configure({"Threads": config.REVIEW_THREADS, "Hash": config.REVIEW_HASH_MB})
        for index in range(total):
            fen = board.fen()
            if board.is_game_over(claim_draw=True):
                # Nothing to search: record the terminal evaluation.
                outcome = board.outcome(claim_draw=True)
                if outcome is not None and outcome.winner is not None:
                    cp = config.CP_CEILING if outcome.winner == chess.WHITE else -config.CP_CEILING
                else:
                    cp = 0.0
                positions.append(
                    {
                        "ply": index,
                        "fen": fen,
                        "cpWhite": cp,
                        "winWhite": win_percent(cp),
                        "mate": None,
                        "best": None,
                        "bestSan": None,
                        "pv": [],
                        "pvSan": [],
                    }
                )
            else:
                info = engine.analyse(board, limit)
                cp, mate = _cp_white(info["score"])
                pv = list(info.get("pv") or [])
                best = pv[0] if pv else None
                pv_san: list[str] = []
                scratch = board.copy(stack=False)
                for move in pv[:12]:
                    try:
                        pv_san.append(scratch.san(move))
                        scratch.push(move)
                    except (AssertionError, ValueError):
                        break
                positions.append(
                    {
                        "ply": index,
                        "fen": fen,
                        "cpWhite": cp,
                        "winWhite": win_percent(cp),
                        "mate": mate,
                        "best": best.uci() if best else None,
                        "bestSan": board.san(best) if best else None,
                        "pv": [move.uci() for move in pv[:12]],
                        "pvSan": pv_san,
                    }
                )

            if on_progress:
                on_progress(index + 1, total)
            if index < len(moves):
                board.push(moves[index])
    finally:
        engine.quit()

    # Replay to attach SAN and judgments to each move.
    board = chess.Board()
    analysed: list[dict[str, Any]] = []
    for index, move in enumerate(moves):
        mover = board.turn
        before_white = positions[index]["winWhite"]
        after_white = positions[index + 1]["winWhite"]
        if mover == chess.WHITE:
            before, after = before_white, after_white
        else:
            before, after = 100.0 - before_white, 100.0 - after_white
        drop = max(0.0, before - after)
        best = positions[index]["best"]
        judgment = None if best == move.uci() else classify(drop)
        san = board.san(move)
        analysed.append(
            {
                "ply": index + 1,
                "moveNumber": index // 2 + 1,
                "uci": move.uci(),
                "san": san,
                "color": "white" if mover == chess.WHITE else "black",
                "isHuman": mover == human_color,
                "winBefore": round(before, 1),
                "winAfter": round(after, 1),
                "drop": round(drop, 1),
                "judgment": judgment,
                "glyph": GLYPHS.get(judgment or "", ""),
                "accuracy": round(accuracy_from_win_percents(before, after), 1),
                "best": best,
                "bestSan": positions[index]["bestSan"],
                "bestPv": positions[index]["pv"],
                "bestPvSan": positions[index]["pvSan"],
                "cpWhiteAfter": positions[index + 1]["cpWhite"],
                "winWhiteAfter": round(after_white, 1),
            }
        )
        board.push(move)

    accuracy = game_accuracy([position["winWhite"] for position in positions], chess.WHITE)
    counts = {"inaccuracy": 0, "mistake": 0, "blunder": 0}
    per_side = {
        "white": dict(counts),
        "black": dict(counts),
    }
    for entry in analysed:
        if entry["judgment"]:
            per_side[entry["color"]][entry["judgment"]] += 1

    return {
        "depth": depth,
        "movetime": movetime,
        "humanColor": "white" if human_color == chess.WHITE else "black",
        "positions": positions,
        "moves": analysed,
        "accuracy": accuracy,
        "judgmentCounts": per_side,
    }
