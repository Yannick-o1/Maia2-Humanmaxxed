"""FastAPI server: one process, in-memory live games, SQLite for everything kept.

Clocks, legality and game endings are decided here and only here. The browser
gets a state snapshot after every change and animates between snapshots.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from typing import Any

import chess
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import config, engine, state
from .bot import (
    MaiaBot,
    bucket_index,
    bucket_label,
    bucket_nominal_rating,
    should_accept_draw,
    should_resign,
)
from .db import HUMAN_ID, Store, bucket_key
from .game import Game, IllegalMove, NotYourTurn, Status
from .glicko2 import rate_one_game

log = logging.getLogger("maia2app")
# Share uvicorn's console handler so the bot's own lines (which move it chose,
# how likely that move was, its win probability) show up in the server log.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")

store: Store
bot = MaiaBot()
games: dict[str, Game] = {}
sockets: dict[str, set[WebSocket]] = {}
watchdogs: dict[str, asyncio.Task] = {}
bot_tasks: dict[str, asyncio.Task] = {}
reviews: dict[str, engine.ReviewProgress] = {}
bot_ready = asyncio.Event()
bot_error: str | None = None


# --- lifecycle ------------------------------------------------------------
@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    """Open the database, then load Maia-2 in the background.

    Loading takes about ten seconds (download on first run, plus one warm-up
    forward pass to get the MPS kernel compilation out of the way), so the
    server comes up immediately and the first move waits on `bot_ready`.
    """
    global store
    store = Store()
    loader = asyncio.create_task(_load_bot())
    try:
        yield
    finally:
        loader.cancel()
        for task in list(watchdogs.values()) + list(bot_tasks.values()):
            task.cancel()


app = FastAPI(title="Maia-2 chess", lifespan=lifespan)


async def _load_bot() -> None:
    global bot_error
    try:
        await run_in_threadpool(bot.load)
        log.info("Maia-2 %s ready on %s", config.MAIA_MODEL_TYPE, bot.device)
    except Exception as exc:  # pragma: no cover - startup diagnostics
        bot_error = str(exc)
        log.exception("could not load Maia-2")
    finally:
        bot_ready.set()


# --- websockets -----------------------------------------------------------
async def broadcast(game: Game, extra: dict[str, Any] | None = None) -> None:
    payload = state.serialise(game, extra)
    dead = []
    for socket in sockets.get(game.id, set()):
        try:
            await socket.send_json(payload)
        except Exception:
            dead.append(socket)
    for socket in dead:
        sockets.get(game.id, set()).discard(socket)


async def send_event(game_id: str, event: dict[str, Any]) -> None:
    dead = []
    for socket in sockets.get(game_id, set()):
        try:
            await socket.send_json(event)
        except Exception:
            dead.append(socket)
    for socket in dead:
        sockets.get(game_id, set()).discard(socket)


@app.websocket("/ws/{game_id}")
async def websocket_endpoint(socket: WebSocket, game_id: str) -> None:
    await socket.accept()
    sockets.setdefault(game_id, set()).add(socket)
    game = games.get(game_id)
    if game:
        await socket.send_json(state.serialise(game))
    try:
        while True:
            await socket.receive_text()  # the client only listens
    except WebSocketDisconnect:
        pass
    finally:
        sockets.get(game_id, set()).discard(socket)


# --- clock watchdog -------------------------------------------------------
async def watch_clock(game: Game) -> None:
    """Catch a flag even when nobody is moving or looking."""
    try:
        while game.status is Status.PLAYING:
            remaining = game.time_until_flag()
            if remaining is None:
                return
            await asyncio.sleep(min(max(remaining, 0.05), 1.0))
            if game.check_flag():
                await finish_game(game)
                return
    except asyncio.CancelledError:
        pass


def arm_watchdog(game: Game) -> None:
    existing = watchdogs.get(game.id)
    if existing and not existing.done():
        return
    watchdogs[game.id] = asyncio.create_task(watch_clock(game))


# --- ratings and persistence ---------------------------------------------
def persist_game(game: Game, rating_change: dict[str, Any] | None) -> None:
    record = {
        "id": game.id,
        "created_at": game.created_at.isoformat(),
        "finished_at": game.finished_at.isoformat() if game.finished_at else None,
        "rated": 1 if game.rated else 0,
        "human_color": "white" if game.human_color == chess.WHITE else "black",
        "time_initial": game.initial_seconds,
        "time_increment": game.increment_seconds,
        "temperature": game.temperature,
        "bucket_index": game.bucket_index,
        "bucket_label": game.bucket_label,
        "conditioning_elo": game.conditioning_elo,
        "result": game.result,
        "reason": game.reason.value if game.reason else None,
        "status_text": game.status_text(),
        "ply_count": len(game.moves),
        "pgn": game.to_pgn(),
        "human_rating_before": game.human_rating,
        "human_rd_before": game.human_rd,
        "human_rating_after": (rating_change or {}).get("humanRatingAfter"),
        "human_rd_after": (rating_change or {}).get("humanRdAfter"),
        "bot_rating_before": game.bot_rating,
        "bot_rating_after": (rating_change or {}).get("botRatingAfter"),
    }
    store.save_game(record)


def apply_rating_change(game: Game) -> dict[str, Any] | None:
    """Update the player and the bucket together, each as the other's opponent.

    Maia plays above the rating it is labelled with, so the conditioning rating
    is not used as the bot's strength. The bucket carries its own Glicko-2
    record and that is what the player is rated against, which lets the pair
    calibrate itself over time.
    """
    score = game.human_score()
    if not game.rated or score is None:
        return None

    human_before = store.get_rating(HUMAN_ID)
    key = bucket_key(game.bucket_index)
    bot_before = store.get_rating(key)

    human_after = rate_one_game(human_before, bot_before, score)
    bot_after = rate_one_game(bot_before, human_before, 1.0 - score)

    store.save_rating(HUMAN_ID, human_after)
    store.save_rating(key, bot_after)

    return {
        "rated": True,
        "score": score,
        "humanRatingBefore": round(human_before.rating, 1),
        "humanRdBefore": round(human_before.rd, 1),
        "humanRatingAfter": round(human_after.rating, 1),
        "humanRdAfter": round(human_after.rd, 1),
        "humanRatingDelta": round(human_after.rating - human_before.rating, 1),
        "humanRdDelta": round(human_after.rd - human_before.rd, 1),
        "botRatingBefore": round(bot_before.rating, 1),
        "botRatingAfter": round(bot_after.rating, 1),
        "botRatingDelta": round(bot_after.rating - bot_before.rating, 1),
        "bucket": game.bucket_label,
    }


async def finish_game(game: Game) -> None:
    task = bot_tasks.pop(game.id, None)
    if task and not task.done():
        task.cancel()
    watchdog = watchdogs.pop(game.id, None)
    if watchdog and not watchdog.done():
        watchdog.cancel()

    change = apply_rating_change(game)
    persist_game(game, change)
    await broadcast(game, {"ratingChange": change, "gameOver": True})


# --- the bot's turn -------------------------------------------------------
async def play_bot_move(game: Game) -> None:
    """Think, pause like a person, then move. Also decides when to resign."""
    try:
        await bot_ready.wait()
        if not bot.ready:
            await send_event(game.id, {"type": "error", "message": bot_error or "bot unavailable"})
            return
        if not game.is_bot_turn():
            return

        await broadcast(game, {"botThinking": True})
        board = game.board.copy(stack=False)
        decision = await run_in_threadpool(
            bot.decide, board, game.conditioning_elo, game.temperature
        )
        if game.status is not Status.PLAYING or not game.is_bot_turn():
            return

        game.last_bot_win_prob = round(decision.bot_win_prob, 4)
        if decision.bot_win_prob < config.RESIGN_WIN_PROB:
            game.bot_low_win_prob_streak += 1
        else:
            game.bot_low_win_prob_streak = 0

        if should_resign(game.bot_low_win_prob_streak, len(game.moves)):
            await asyncio.sleep(min(1.5, decision.think_seconds))
            if game.status is Status.PLAYING:
                game.resign(game.bot_color)
                await finish_game(game)
            return

        await asyncio.sleep(decision.think_seconds)
        if game.status is not Status.PLAYING or not game.is_bot_turn():
            return
        if game.check_flag():
            await finish_game(game)
            return

        try:
            game.apply_move(decision.uci, by=game.bot_color)
        except (IllegalMove, NotYourTurn) as exc:
            log.warning("bot move rejected: %s", exc)
            return

        log.info(
            "bot %s (p=%.3f%s) win_prob=%.3f think=%.1fs",
            decision.san,
            decision.sampled_prob,
            "" if decision.was_top_move else ", not top move",
            decision.bot_win_prob,
            decision.think_seconds,
        )
        extra = {
            "botThinking": False,
            "lastBotMove": {
                "san": decision.san,
                "uci": decision.uci,
                "probability": round(decision.sampled_prob, 4),
                "wasTopMove": decision.was_top_move,
                "winProb": round(decision.bot_win_prob, 4),
                "thinkSeconds": round(decision.think_seconds, 2),
            },
        }
        if game.status is Status.FINISHED:
            await finish_game(game)
        else:
            arm_watchdog(game)
            await broadcast(game, extra)
    except asyncio.CancelledError:
        pass
    except Exception:  # pragma: no cover
        log.exception("bot turn failed")
    finally:
        bot_tasks.pop(game.id, None)


def schedule_bot(game: Game) -> None:
    if game.status is not Status.PLAYING or not game.is_bot_turn():
        return
    existing = bot_tasks.get(game.id)
    if existing and not existing.done():
        return
    bot_tasks[game.id] = asyncio.create_task(play_bot_move(game))


# --- helpers --------------------------------------------------------------
def live_game(game_id: str) -> Game:
    game = games.get(game_id)
    if game is None:
        raise HTTPException(404, "no such live game")
    return game


# --- API ------------------------------------------------------------------
@app.get("/api/bootstrap")
async def bootstrap() -> dict[str, Any]:
    human = store.get_rating(HUMAN_ID)
    return {
        "player": {
            "name": config.PLAYER_NAME,
            "rating": round(human.rating, 1),
            "rd": round(human.rd, 1),
            "volatility": round(human.volatility, 5),
            "games": store.get_games_played(HUMAN_ID),
        },
        "ratings": store.all_ratings(),
        "presets": config.TIME_PRESETS,
        "defaultPreset": config.DEFAULT_PRESET,
        "settings": {
            "temperature": store.get_setting("temperature", config.DEFAULT_TEMPERATURE),
            "reviewDepth": store.get_setting("review_depth", config.REVIEW_DEPTH),
            "reviewMovetime": store.get_setting("review_movetime", config.REVIEW_MOVETIME),
            "startRating": store.get_setting("start_rating", config.DEFAULT_START_RATING),
            "startRd": store.get_setting("start_rd", config.GLICKO_DEFAULT_RD),
            "sound": store.get_setting("sound", True),
        },
        "bot": {
            "name": config.BOT_NAME,
            "model": config.MAIA_MODEL_TYPE,
            "ready": bot.ready,
            "device": bot.device,
            "error": bot_error,
        },
        "thresholds": {
            "resignWinProb": config.RESIGN_WIN_PROB,
            "resignConsecutiveMoves": config.RESIGN_CONSECUTIVE_MOVES,
            "drawAcceptMin": config.DRAW_ACCEPT_WIN_PROB_MIN,
            "drawAcceptMax": config.DRAW_ACCEPT_WIN_PROB_MAX,
            "inaccuracy": config.INACCURACY_WIN_DROP,
            "mistake": config.MISTAKE_WIN_DROP,
            "blunder": config.BLUNDER_WIN_DROP,
        },
    }


@app.post("/api/settings")
async def update_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    mapping = {
        "temperature": "temperature",
        "reviewDepth": "review_depth",
        "reviewMovetime": "review_movetime",
        "sound": "sound",
    }
    for key, column in mapping.items():
        if key in payload:
            store.set_setting(column, payload[key])
    if "startRating" in payload or "startRd" in payload:
        rating = float(payload.get("startRating", store.get_setting("start_rating", config.DEFAULT_START_RATING)))
        rd = float(payload.get("startRd", store.get_setting("start_rd", config.GLICKO_DEFAULT_RD)))
        store.set_setting("start_rating", rating)
        store.set_setting("start_rd", rd)
        if payload.get("resetRating"):
            store.reset_human_rating(rating, rd)
    return await bootstrap()


@app.post("/api/games")
async def create_game(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    colour_choice = str(payload.get("color", "white")).lower()
    if colour_choice == "random":
        import random

        human_color = random.choice([chess.WHITE, chess.BLACK])
    else:
        human_color = chess.BLACK if colour_choice == "black" else chess.WHITE

    initial = int(payload.get("initial", 600))
    increment = int(payload.get("increment", 5))
    if not (1 <= initial <= 10800) or not (0 <= increment <= 180):
        raise HTTPException(400, "time control out of range")

    rated = bool(payload.get("rated", True))
    temperature = float(
        payload.get("temperature", store.get_setting("temperature", config.DEFAULT_TEMPERATURE))
    )
    temperature = max(0.0, min(5.0, temperature))

    human = store.get_rating(HUMAN_ID)
    index = bucket_index(human.rating)
    bot_rating = store.get_rating(bucket_key(index))

    game = Game(
        human_color=human_color,
        rated=rated,
        initial_seconds=initial,
        increment_seconds=increment,
        temperature=temperature,
        human_rating=human.rating,
        human_rd=human.rd,
        bot_rating=bot_rating.rating,
        bucket_index=index,
        bucket_label=bucket_label(index),
        conditioning_elo=bucket_nominal_rating(index),
    )
    games[game.id] = game
    arm_watchdog(game)
    schedule_bot(game)
    return state.serialise(game, {"botReady": bot.ready})


@app.get("/api/live/{game_id}")
async def live_state(game_id: str) -> dict[str, Any]:
    game = live_game(game_id)
    if game.check_flag():
        await finish_game(game)
    return state.serialise(game)


@app.post("/api/live/{game_id}/move")
async def post_move(game_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    game = live_game(game_id)
    if game.check_flag():
        await finish_game(game)
        return state.serialise(game)
    if not game.is_human_turn():
        raise HTTPException(409, "not your turn")
    try:
        game.apply_move(str(payload.get("uci", "")), by=game.human_color)
    except IllegalMove as exc:
        raise HTTPException(400, str(exc)) from exc
    except NotYourTurn as exc:
        if game.status is Status.FINISHED:
            await finish_game(game)
        raise HTTPException(409, str(exc)) from exc

    if game.status is Status.FINISHED:
        await finish_game(game)
    else:
        arm_watchdog(game)
        await broadcast(game)
        schedule_bot(game)
    return state.serialise(game)


@app.post("/api/live/{game_id}/resign")
async def post_resign(game_id: str) -> dict[str, Any]:
    game = live_game(game_id)
    if game.status is Status.PLAYING:
        game.resign(game.human_color)
        await finish_game(game)
    return state.serialise(game)


@app.post("/api/live/{game_id}/draw")
async def post_draw_offer(game_id: str) -> dict[str, Any]:
    """Offer a draw and get the bot's answer straight away."""
    game = live_game(game_id)
    if game.status is not Status.PLAYING:
        return state.serialise(game)

    await bot_ready.wait()
    accepted = False
    bot_win_prob = game.last_bot_win_prob
    if bot.ready:
        _, white_win_prob = await run_in_threadpool(
            bot.evaluate, game.board.fen(), game.conditioning_elo, game.conditioning_elo
        )
        bot_win_prob = (
            white_win_prob if game.bot_color == chess.WHITE else 1.0 - white_win_prob
        )
        game.last_bot_win_prob = round(bot_win_prob, 4)
        accepted = should_accept_draw(bot_win_prob, game.is_simplified(), len(game.moves))

    if accepted:
        game.agree_draw()
        await finish_game(game)
    else:
        game.draw_offer_from = None
        await broadcast(
            game,
            {
                "drawDeclined": True,
                "drawReason": {
                    "winProb": round(bot_win_prob, 4) if bot_win_prob is not None else None,
                    "simplified": game.is_simplified(),
                    "material": game.non_king_material(),
                    "ply": len(game.moves),
                },
            },
        )
    return state.serialise(
        game,
        {
            "drawAccepted": accepted,
            "drawDeclined": not accepted,
            "botWinProb": round(bot_win_prob, 4) if bot_win_prob is not None else None,
        },
    )


@app.post("/api/live/{game_id}/takeback")
async def post_takeback(game_id: str) -> dict[str, Any]:
    game = live_game(game_id)
    if game.rated:
        raise HTTPException(403, "takebacks are not allowed in rated games")
    task = bot_tasks.pop(game.id, None)
    if task and not task.done():
        task.cancel()
    try:
        undone = game.takeback()
    except NotYourTurn as exc:
        raise HTTPException(409, str(exc)) from exc
    arm_watchdog(game)
    await broadcast(game, {"takeback": undone})
    schedule_bot(game)
    return state.serialise(game)


@app.get("/api/live/{game_id}/hint")
async def get_hint(game_id: str) -> dict[str, Any]:
    """A nudge from Stockfish. Casual games only, as the brief requires."""
    game = live_game(game_id)
    if game.rated:
        raise HTTPException(403, "no hints in rated games")
    if game.status is not Status.PLAYING:
        raise HTTPException(409, "game is over")

    board = game.board.copy(stack=False)

    def think() -> dict[str, Any]:
        import chess.engine

        with chess.engine.SimpleEngine.popen_uci(config.STOCKFISH_PATH) as sf:
            info = sf.analyse(board, chess.engine.Limit(time=0.3))
        pv = list(info.get("pv") or [])
        if not pv:
            return {}
        return {
            "uci": pv[0].uci(),
            "san": board.san(pv[0]),
            "from": chess.square_name(pv[0].from_square),
            "to": chess.square_name(pv[0].to_square),
        }

    return await run_in_threadpool(think)


# --- history and review ---------------------------------------------------
@app.get("/api/games")
async def list_games(limit: int = 50) -> dict[str, Any]:
    rows = store.list_games(limit)
    for row in rows:
        row["hasReview"] = store.get_review(row["id"]) is not None
    return {"games": rows}


@app.get("/api/games/{game_id}")
async def get_game(game_id: str) -> dict[str, Any]:
    row = store.get_game(game_id)
    if row is None:
        raise HTTPException(404, "no such game")
    row["review"] = store.get_review(game_id)
    return row


@app.get("/api/games/{game_id}/pgn", response_class=PlainTextResponse)
async def get_pgn(game_id: str) -> str:
    row = store.get_game(game_id)
    if row is None:
        raise HTTPException(404, "no such game")
    return row["pgn"] or ""


async def run_review(game_id: str, pgn: str, human_color: bool, depth: int, movetime: float) -> None:
    from .game import parse_pgn_moves

    progress = reviews[game_id]
    loop = asyncio.get_running_loop()

    def on_progress(done: int, total: int) -> None:
        progress.done, progress.total = done, total
        asyncio.run_coroutine_threadsafe(
            send_event(
                game_id,
                {"type": "reviewProgress", "done": done, "total": total},
            ),
            loop,
        )

    try:
        moves = parse_pgn_moves(pgn)
        result = await run_in_threadpool(
            engine.analyse_game,
            moves,
            human_color,
            depth,
            movetime,
            config.STOCKFISH_PATH,
            on_progress,
        )
        store.save_review(game_id, result)
        progress.result = result
        progress.finished = True
        await send_event(game_id, {"type": "reviewDone", "review": result})
    except Exception as exc:  # pragma: no cover
        log.exception("review failed")
        progress.error = str(exc)
        progress.finished = True
        await send_event(game_id, {"type": "reviewError", "message": str(exc)})


@app.post("/api/games/{game_id}/review")
async def start_review(game_id: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    row = store.get_game(game_id)
    if row is None:
        raise HTTPException(404, "no such game")
    cached = store.get_review(game_id)
    if cached and not payload.get("force"):
        return {"status": "done", "review": cached}

    existing = reviews.get(game_id)
    if existing and not existing.finished:
        return {"status": "running", "done": existing.done, "total": existing.total}

    depth = int(payload.get("depth", store.get_setting("review_depth", config.REVIEW_DEPTH)))
    movetime = float(
        payload.get("movetime", store.get_setting("review_movetime", config.REVIEW_MOVETIME))
    )
    total = (row["ply_count"] or 0) + 1
    reviews[game_id] = engine.ReviewProgress(done=0, total=total)
    human_color = chess.WHITE if row["human_color"] == "white" else chess.BLACK
    asyncio.create_task(run_review(game_id, row["pgn"] or "", human_color, depth, movetime))
    return {"status": "running", "done": 0, "total": total}


@app.get("/api/games/{game_id}/review")
async def review_status(game_id: str) -> dict[str, Any]:
    cached = store.get_review(game_id)
    if cached:
        return {"status": "done", "review": cached}
    progress = reviews.get(game_id)
    if progress is None:
        return {"status": "none"}
    if progress.error:
        return {"status": "error", "message": progress.error}
    if progress.finished and progress.result:
        return {"status": "done", "review": progress.result}
    return {"status": "running", "done": progress.done, "total": progress.total}


# --- static ---------------------------------------------------------------
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(config.BASE_DIR / "static" / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "botReady": bot.ready,
        "device": bot.device,
        "error": bot_error,
        "liveGames": len(games),
        "time": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


app.mount("/static", StaticFiles(directory=config.BASE_DIR / "static"), name="static")
