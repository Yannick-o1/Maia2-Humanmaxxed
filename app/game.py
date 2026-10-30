"""Game state: the board, the clock, how games end, and PGN output.

The server is the only authority on time. The browser runs a countdown purely
for display; every clock figure it shows was computed here.
"""

from __future__ import annotations

import datetime as dt
import io
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

import chess
import chess.pgn

from . import config


def now() -> float:
    """The monotonic clock, in one place so tests can drive it."""
    return time.monotonic()


PIECE_VALUES = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
}


class Status(str, Enum):
    PLAYING = "playing"
    FINISHED = "finished"


class Reason(str, Enum):
    """Why the game ended. Maps onto a PGN Termination tag in `pgn_termination`."""

    CHECKMATE = "checkmate"
    RESIGNATION = "resignation"
    TIMEOUT = "timeout"
    TIMEOUT_INSUFFICIENT_MATERIAL = "timeout_vs_insufficient_material"
    STALEMATE = "stalemate"
    INSUFFICIENT_MATERIAL = "insufficient_material"
    FIFTY_MOVES = "fifty_move_rule"
    THREEFOLD = "threefold_repetition"
    AGREEMENT = "draw_by_agreement"


DRAW_REASONS = {
    Reason.STALEMATE,
    Reason.INSUFFICIENT_MATERIAL,
    Reason.FIFTY_MOVES,
    Reason.THREEFOLD,
    Reason.AGREEMENT,
    Reason.TIMEOUT_INSUFFICIENT_MATERIAL,
}


@dataclass
class MoveRecord:
    ply: int
    uci: str
    san: str
    # Clock readings *after* the move, so a takeback can restore them exactly.
    white_clock: float
    black_clock: float
    seconds_spent: float


class IllegalMove(Exception):
    pass


class NotYourTurn(Exception):
    pass


@dataclass
class Game:
    human_color: bool = chess.WHITE
    rated: bool = True
    initial_seconds: int = 600
    increment_seconds: int = 5
    temperature: float = config.DEFAULT_TEMPERATURE

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    board: chess.Board = field(default_factory=chess.Board)
    moves: list[MoveRecord] = field(default_factory=list)
    status: Status = Status.PLAYING
    result: str | None = None  # "1-0", "0-1" or "1/2-1/2"
    reason: Reason | None = None

    # Ratings as they stood when the game began, for the PGN headers.
    human_rating: float = config.DEFAULT_START_RATING
    human_rd: float = config.GLICKO_DEFAULT_RD
    bot_rating: float = config.DEFAULT_START_RATING
    bucket_index: int = 0
    bucket_label: str = ""
    conditioning_elo: int = 1450

    draw_offer_from: bool | None = None
    bot_low_win_prob_streak: int = 0
    last_bot_win_prob: float | None = None

    clocks: dict[bool, float] = field(default_factory=dict)
    turn_started_at: float | None = None
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    finished_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        if not self.clocks:
            self.clocks = {
                chess.WHITE: float(self.initial_seconds),
                chess.BLACK: float(self.initial_seconds),
            }
        if self.turn_started_at is None:
            self.turn_started_at = now()

    # --- identities -------------------------------------------------------
    @property
    def bot_color(self) -> bool:
        return not self.human_color

    def is_human_turn(self) -> bool:
        return self.status is Status.PLAYING and self.board.turn == self.human_color

    def is_bot_turn(self) -> bool:
        return self.status is Status.PLAYING and self.board.turn == self.bot_color

    # --- clock ------------------------------------------------------------
    def remaining(self, color: bool) -> float:
        """Seconds left for `color` right now, counting the running turn."""
        value = self.clocks[color]
        if self.status is Status.PLAYING and color == self.board.turn and self.turn_started_at:
            value -= now() - self.turn_started_at
        return max(0.0, value)

    def _elapsed_this_turn(self) -> float:
        if self.turn_started_at is None:
            return 0.0
        return now() - self.turn_started_at

    def time_until_flag(self) -> float | None:
        """Seconds until the player to move runs out, for the watchdog."""
        if self.status is not Status.PLAYING:
            return None
        return self.remaining(self.board.turn)

    def check_flag(self) -> bool:
        """End the game if the side to move has run out. True if it did."""
        if self.status is not Status.PLAYING:
            return False
        if self.remaining(self.board.turn) > 0.0:
            return False

        flagged = self.board.turn
        self.clocks[flagged] = 0.0
        opponent = not flagged
        # Lichess rule: running out of time is a draw if your opponent could
        # not have mated you with the material they have left.
        if self.board.has_insufficient_material(opponent):
            self._finish("1/2-1/2", Reason.TIMEOUT_INSUFFICIENT_MATERIAL)
        else:
            self._finish("1-0" if opponent == chess.WHITE else "0-1", Reason.TIMEOUT)
        return True

    # --- moves ------------------------------------------------------------
    def legal_destinations(self) -> dict[str, list[str]]:
        """Chessground's `movable.dests` shape: {"e2": ["e3", "e4"], ...}."""
        dests: dict[str, list[str]] = {}
        for move in self.board.legal_moves:
            dests.setdefault(chess.square_name(move.from_square), []).append(
                chess.square_name(move.to_square)
            )
        for squares in dests.values():
            # Promotions produce four moves to the same square.
            squares[:] = sorted(set(squares))
        return dests

    def parse(self, uci: str) -> chess.Move:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError as exc:
            raise IllegalMove(f"unparseable move {uci!r}") from exc
        if move not in self.board.legal_moves:
            raise IllegalMove(f"illegal move {uci!r} in {self.board.fen()}")
        return move

    def apply_move(self, uci: str, by: bool | None = None) -> MoveRecord:
        """Play a move, bill its time to the mover, then look for a game end."""
        if self.status is not Status.PLAYING:
            raise NotYourTurn("game is over")
        if by is not None and by != self.board.turn:
            raise NotYourTurn("not your turn")
        if self.check_flag():
            raise NotYourTurn("clock ran out")

        move = self.parse(uci)
        mover = self.board.turn
        spent = self._elapsed_this_turn()

        left = self.clocks[mover] - spent
        if left <= 0.0:
            self.check_flag()
            raise NotYourTurn("clock ran out")

        san = self.board.san(move)
        self.board.push(move)
        self.clocks[mover] = left + self.increment_seconds
        self.turn_started_at = now()
        # A move supersedes any draw offer that was on the table.
        self.draw_offer_from = None

        record = MoveRecord(
            ply=len(self.moves) + 1,
            uci=move.uci(),
            san=san,
            white_clock=self.clocks[chess.WHITE],
            black_clock=self.clocks[chess.BLACK],
            seconds_spent=spent,
        )
        self.moves.append(record)
        self._check_board_end()
        return record

    def takeback(self) -> int:
        """Undo back to the human's turn. Casual games only. Returns plies undone."""
        if self.status is not Status.PLAYING:
            raise NotYourTurn("game is over")
        if self.rated:
            raise NotYourTurn("takebacks are not allowed in rated games")

        undone = 0
        # Undo the bot's reply if it has already moved, then the human's move.
        while self.moves and undone < 2:
            self.board.pop()
            self.moves.pop()
            undone += 1
            if self.board.turn == self.human_color:
                break

        if undone:
            if self.moves:
                last = self.moves[-1]
                self.clocks[chess.WHITE] = last.white_clock
                self.clocks[chess.BLACK] = last.black_clock
            else:
                self.clocks = {
                    chess.WHITE: float(self.initial_seconds),
                    chess.BLACK: float(self.initial_seconds),
                }
            self.turn_started_at = now()
            self.draw_offer_from = None
        return undone

    # --- endings ----------------------------------------------------------
    def _check_board_end(self) -> None:
        board = self.board
        if board.is_checkmate():
            winner = not board.turn
            self._finish("1-0" if winner == chess.WHITE else "0-1", Reason.CHECKMATE)
        elif board.is_stalemate():
            self._finish("1/2-1/2", Reason.STALEMATE)
        elif board.is_insufficient_material():
            self._finish("1/2-1/2", Reason.INSUFFICIENT_MATERIAL)
        elif board.is_repetition(3):
            self._finish("1/2-1/2", Reason.THREEFOLD)
        elif board.can_claim_fifty_moves():
            self._finish("1/2-1/2", Reason.FIFTY_MOVES)

    def resign(self, color: bool) -> None:
        if self.status is not Status.PLAYING:
            return
        self._finish("0-1" if color == chess.WHITE else "1-0", Reason.RESIGNATION)

    def agree_draw(self) -> None:
        if self.status is not Status.PLAYING:
            return
        self._finish("1/2-1/2", Reason.AGREEMENT)

    def _finish(self, result: str, reason: Reason) -> None:
        self.status = Status.FINISHED
        self.result = result
        self.reason = reason
        self.finished_at = dt.datetime.now(dt.timezone.utc)
        self.draw_offer_from = None
        self.turn_started_at = None

    # --- derived facts ----------------------------------------------------
    @property
    def winner(self) -> bool | None:
        if self.result == "1-0":
            return chess.WHITE
        if self.result == "0-1":
            return chess.BLACK
        return None

    def human_score(self) -> float | None:
        """1, 0.5 or 0 from the human's point of view, or None if unfinished."""
        if self.result is None:
            return None
        if self.result == "1/2-1/2":
            return 0.5
        return 1.0 if self.winner == self.human_color else 0.0

    def non_king_material(self) -> int:
        total = 0
        for piece_type, value in PIECE_VALUES.items():
            total += value * len(self.board.pieces(piece_type, chess.WHITE))
            total += value * len(self.board.pieces(piece_type, chess.BLACK))
        return total

    def is_simplified(self) -> bool:
        return self.non_king_material() <= config.SIMPLIFIED_MATERIAL_MAX

    def last_move_squares(self) -> list[str] | None:
        if not self.moves:
            return None
        move = chess.Move.from_uci(self.moves[-1].uci)
        return [chess.square_name(move.from_square), chess.square_name(move.to_square)]

    def check_square(self) -> str | None:
        if not self.board.is_check():
            return None
        king = self.board.king(self.board.turn)
        return chess.square_name(king) if king is not None else None

    def status_text(self) -> str:
        """The one-line result summary shown under the board."""
        if self.status is not Status.FINISHED:
            return "Game in progress"
        names = {chess.WHITE: self.white_name(), chess.BLACK: self.black_name()}
        texts = {
            Reason.CHECKMATE: "Checkmate",
            Reason.RESIGNATION: "Resignation",
            Reason.TIMEOUT: "Time out",
            Reason.TIMEOUT_INSUFFICIENT_MATERIAL: "Time out vs insufficient material",
            Reason.STALEMATE: "Stalemate",
            Reason.INSUFFICIENT_MATERIAL: "Insufficient material",
            Reason.FIFTY_MOVES: "Fifty-move rule",
            Reason.THREEFOLD: "Threefold repetition",
            Reason.AGREEMENT: "Draw by agreement",
        }
        detail = texts.get(self.reason, "Game over")
        if self.winner is None:
            return f"Draw - {detail.lower()}"
        verb = "win" if self.winner == self.human_color else "wins"
        return f"{names[self.winner]} {verb} - {detail.lower()}"

    # --- PGN --------------------------------------------------------------
    def white_name(self) -> str:
        return config.PLAYER_NAME if self.human_color == chess.WHITE else config.BOT_NAME

    def black_name(self) -> str:
        return config.PLAYER_NAME if self.human_color == chess.BLACK else config.BOT_NAME

    def pgn_termination(self) -> str:
        if self.reason in (Reason.TIMEOUT, Reason.TIMEOUT_INSUFFICIENT_MATERIAL):
            return "Time forfeit"
        if self.reason is None:
            return "Unterminated"
        return "Normal"

    def to_pgn(self) -> str:
        game = chess.pgn.Game()
        headers = game.headers
        headers["Event"] = f"{'Rated' if self.rated else 'Casual'} game vs {config.BOT_NAME}"
        headers["Site"] = "http://localhost:8000"
        headers["Date"] = self.created_at.strftime("%Y.%m.%d")
        headers["Round"] = "-"
        headers["White"] = self.white_name()
        headers["Black"] = self.black_name()
        headers["Result"] = self.result or "*"
        human_elo = str(round(self.human_rating))
        bot_elo = str(round(self.bot_rating))
        if self.human_color == chess.WHITE:
            headers["WhiteElo"], headers["BlackElo"] = human_elo, bot_elo
        else:
            headers["WhiteElo"], headers["BlackElo"] = bot_elo, human_elo
        headers["TimeControl"] = f"{self.initial_seconds}+{self.increment_seconds}"
        headers["Termination"] = self.pgn_termination()
        headers["Variant"] = "Standard"
        headers["UTCDate"] = self.created_at.strftime("%Y.%m.%d")
        headers["UTCTime"] = self.created_at.strftime("%H:%M:%S")
        headers["Annotator"] = (
            f"maia2 {config.MAIA_MODEL_TYPE} @ {self.conditioning_elo} "
            f"(bucket {self.bucket_label}), temperature {self.temperature}"
        )

        node: chess.pgn.GameNode = game
        for record in self.moves:
            node = node.add_variation(chess.Move.from_uci(record.uci))
            clock = record.white_clock if node.turn() == chess.BLACK else record.black_clock
            node.comment = f"[%clk {_clock_tag(clock)}]"

        out = io.StringIO()
        exporter = chess.pgn.FileExporter(out)
        game.accept(exporter)
        return out.getvalue().strip() + "\n"


def _clock_tag(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def parse_pgn_moves(pgn: str) -> list[chess.Move]:
    """Read a stored PGN back into a move list, for the review pass."""
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None:
        return []
    return list(game.mainline_moves())
