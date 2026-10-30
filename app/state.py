"""Turning a Game into the JSON the browser renders."""

from __future__ import annotations

from typing import Any

import chess

from . import config
from .game import Game, Status


def move_pairs(game: Game) -> list[dict[str, Any]]:
    """The move list as rows of (white, black) for the two-column panel."""
    rows: list[dict[str, Any]] = []
    for index, record in enumerate(game.moves):
        if index % 2 == 0:
            rows.append(
                {
                    "number": index // 2 + 1,
                    "white": {"ply": record.ply, "san": record.san, "uci": record.uci},
                    "black": None,
                }
            )
        else:
            rows[-1]["black"] = {"ply": record.ply, "san": record.san, "uci": record.uci}
    return rows


def player_block(game: Game, color: bool) -> dict[str, Any]:
    is_human = color == game.human_color
    if is_human:
        return {
            "name": config.PLAYER_NAME,
            "rating": round(game.human_rating),
            "rd": round(game.human_rd),
            "isBot": False,
        }
    return {
        "name": config.BOT_NAME,
        "rating": round(game.bot_rating),
        "rd": None,
        "isBot": True,
        "bucket": game.bucket_label,
        "conditioningElo": game.conditioning_elo,
    }


def serialise(game: Game, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": game.id,
        "fen": game.board.fen(),
        "turn": "white" if game.board.turn == chess.WHITE else "black",
        "humanColor": "white" if game.human_color == chess.WHITE else "black",
        "botColor": "white" if game.bot_color == chess.WHITE else "black",
        "rated": game.rated,
        "temperature": game.temperature,
        "status": game.status.value,
        "result": game.result,
        "reason": game.reason.value if game.reason else None,
        "statusText": game.status_text(),
        "lastMove": game.last_move_squares(),
        "checkSquare": game.check_square(),
        "inCheck": game.board.is_check(),
        "dests": game.legal_destinations() if game.is_human_turn() else {},
        "isHumanTurn": game.is_human_turn(),
        "isBotTurn": game.is_bot_turn(),
        "moves": move_pairs(game),
        "plyCount": len(game.moves),
        "clocks": {
            "white": round(game.remaining(chess.WHITE), 2),
            "black": round(game.remaining(chess.BLACK), 2),
        },
        "initialSeconds": game.initial_seconds,
        "incrementSeconds": game.increment_seconds,
        "lowTimeSeconds": config.LOW_TIME_SECONDS,
        "white": player_block(game, chess.WHITE),
        "black": player_block(game, chess.BLACK),
        "drawOfferFrom": (
            None
            if game.draw_offer_from is None
            else ("white" if game.draw_offer_from == chess.WHITE else "black")
        ),
        "canTakeback": (
            not game.rated and game.status is Status.PLAYING and len(game.moves) >= 1
        ),
        "canHint": not game.rated and game.status is Status.PLAYING,
        "lastBotWinProb": game.last_bot_win_prob,
    }
    if extra:
        payload.update(extra)
    return payload
