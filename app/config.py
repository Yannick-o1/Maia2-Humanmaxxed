"""Tunable constants for the whole app.

Everything a player might reasonably want to change lives here, plus the
thresholds the brief asks to be named constants rather than magic numbers.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
DB_PATH = Path(os.environ.get("CHESS_DB", PROJECT_DIR / "data" / "chess.sqlite3"))
MODEL_DIR = PROJECT_DIR / "maia2_models"

# --- Maia-2 ---------------------------------------------------------------
MAIA_MODEL_TYPE = "rapid"
MAIA_DEVICE = "auto"  # auto -> cuda, then Apple MPS, then cpu

# Rating buckets the model was trained with: 100 wide from 1100 to 1999,
# plus an open bucket at each end. Confirmed against maia2.utils.create_elo_dict
# and by checking that 1400/1450/1499 give byte-identical distributions.
BUCKET_WIDTH = 100
BUCKET_START = 1100
BUCKET_END = 2000

# --- Bot behaviour --------------------------------------------------------
DEFAULT_TEMPERATURE = 1.0
MIN_TEMPERATURE = 0.05  # below this we just take the argmax

# Maia-2's value head is White-perspective (verified empirically: the
# board is mirrored for Black before inference, and the 1-x flip in
# maia2.inference converts side-to-move back to White). It is also
# compressed and tactically blind -- measured on this build of the rapid
# model, with both Elo inputs at 1450:
#
#   equal pawn endgame .................. 0.499 / 0.502
#   White up a queen .................... 0.719 (w to move) / 0.891 (b)
#   Black up a queen .................... 0.109 (w to move) / 0.281 (b)
#   White three queens down ............. 0.013
#   mate-in-one against White ........... 0.366  <-- does not see tactics
#   mate-in-one for White ............... 0.996
#
# So 0.03 is reachable but only in utterly lost positions, which makes the
# bot resign rarely and late. That is faithful to the brief; raise
# RESIGN_WIN_PROB to around 0.10 if you want it to give up more readily.
RESIGN_WIN_PROB = 0.03
RESIGN_CONSECUTIVE_MOVES = 3

DRAW_ACCEPT_WIN_PROB_MIN = 0.35
DRAW_ACCEPT_WIN_PROB_MAX = 0.65

# "Simplified" for the purpose of accepting a draw: total non-king material
# left on the board, counting P=1 N=B=3 R=5 Q=9. A full army is 78; this
# threshold is roughly "queens are off and not much else is left".
SIMPLIFIED_MATERIAL_MAX = 20

# The value head is noisy in the opening (0.401 at the start position but
# 0.709 after 1.e4), so draw and resign decisions are gated until the game
# has actually begun.
MIN_PLY_FOR_DRAW_OR_RESIGN = 20

# Human-like thinking delay, in seconds. Maia-2 itself answers in ~10ms, so
# all of this is deliberate.
THINK_SECONDS_MIN = 1.0
THINK_SECONDS_MAX = 4.0
THINK_SECONDS_FORCED = 0.35  # only one legal move
THINK_OBVIOUS_PROB = 0.85    # one move this likely is played quickly
THINK_JITTER = 0.2           # +/- 20% random variation

# --- Ratings --------------------------------------------------------------
GLICKO_TAU = 0.5
GLICKO_DEFAULT_RD = 350.0
GLICKO_DEFAULT_VOLATILITY = 0.06
DEFAULT_START_RATING = 1450.0

# --- Clock ----------------------------------------------------------------
TIME_PRESETS = [
    {"name": "10+5", "initial": 600, "increment": 5},
    {"name": "15+10", "initial": 900, "increment": 10},
    {"name": "10+0", "initial": 600, "increment": 0},
    {"name": "5+3", "initial": 300, "increment": 3},
]
DEFAULT_PRESET = "10+5"
LOW_TIME_SECONDS = 20  # clock turns orange/red below this

# --- Post-game review -----------------------------------------------------
STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "/opt/homebrew/bin/stockfish")
REVIEW_DEPTH = 18
REVIEW_MOVETIME = 0.0  # seconds per move; 0 means use REVIEW_DEPTH instead
REVIEW_THREADS = 2
REVIEW_HASH_MB = 256

# Lichess move classification, by drop in win percentage.
INACCURACY_WIN_DROP = 10.0
MISTAKE_WIN_DROP = 20.0
BLUNDER_WIN_DROP = 30.0

# Lichess win% curve: 50 + 50 * (2 / (1 + exp(MULTIPLIER * cp)) - 1)
# from lichess-org/scalachess core/src/main/scala/eval.scala
WIN_PERCENT_MULTIPLIER = -0.00368208
CP_CEILING = 1000  # centipawns are clamped to this before the curve

# Lichess accuracy curve, from lila modules/analyse AccuracyPercent.scala
ACCURACY_A = 103.1668100711649
ACCURACY_K = 0.04354415386753951
ACCURACY_B = -3.166924740191411
ACCURACY_UNCERTAINTY_BONUS = 1.0

PLAYER_NAME = "You"
BOT_NAME = "Maia-2"
