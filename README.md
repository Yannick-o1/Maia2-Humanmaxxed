# Maia-2 chess

A local chess app for rated games against a human-like bot. Maia-2 picks the
moves, Stockfish does the post-game review, and your rating is tracked with
Glicko-2. Nothing leaves your machine.

```bash
./run.sh
```

That installs anything missing (Python 3.12, Stockfish, the Python packages),
starts the server and opens <http://localhost:8000>. The first run also
downloads the Maia-2 rapid model, about 280MB, into `maia2_models/`.

Stop it with Ctrl-C. `PORT=8080 ./run.sh` runs it somewhere else.

## How the bot plays

Maia-2 returns a probability for every legal move in a position, conditioned on
the ratings of both players. This app **samples** from that distribution instead
of taking the top move, so the bot is not deterministic: the same position gets
different replies, and it plays the natural human mistakes rather than the best
move every time.

The **temperature** control in the New game panel sharpens or flattens that
distribution. Weights are `p ** (1 / T)`:

| Temperature | Effect |
| --- | --- |
| 0 | always the single most likely move — fully deterministic |
| 1 (default) | Maia-2's own distribution, unmodified |
| 2 | wanders noticeably; more blunders |

Every sampled move is checked against python-chess before it reaches the board,
and anything illegal is discarded.

### Rating conditioning

Maia-2 was trained on eleven rating buckets: an open bucket below 1100, then 100
wide up to 1999, then an open bucket from 2000. Feeding it 1400, 1450 or 1499
returns byte-identical output, so the app snaps your rating to the bucket
midpoint and uses that for **both** Elo inputs — the bot plays like someone of
your level facing someone of your level.

### Thinking time

Maia-2 answers in about 10ms on Apple silicon, so the pause is deliberate: 1–4
seconds scaled by how complex the position is (the entropy of the move
distribution and the number of legal moves), shortened when one move is obvious,
and 0.35s when there is only one legal move. The time is billed to the bot's
clock like any other move.

### Draw offers and resignation

Both use Maia-2's win-probability output. Note that this value is from **White's**
perspective, not the side to move's — the library's README and its code appear
to disagree, and the README is right; the app converts it to the bot's own
perspective before using it.

The bot accepts a draw only when its win probability is between 0.35 and 0.65
*and* the position has simplified (at most 20 points of non-king material). It
resigns after 3 consecutive moves below 0.03. All of these are constants at the
top of [`app/config.py`](app/config.py).

**Worth knowing:** Maia-2's value head is compressed and tactically blind.
Measured on the rapid model at 1450:

| Position | Win probability (White) |
| --- | --- |
| equal pawn endgame | 0.499 |
| White up a queen | 0.719 |
| Black up a queen | 0.109 |
| White three queens down | 0.013 |
| mate in one **against** White | 0.366 |
| mate in one **for** White | 0.996 |

So 0.03 is reachable, but only from a completely hopeless position — the bot
resigns rarely and late, and it will not see a mate in one coming. That is
faithful to the spec; raise `RESIGN_WIN_PROB` to about 0.10 if you want it to
give up more readily. Draw and resign decisions are also suppressed before move
10, because the value head is noisy in the opening (0.401 at the start position
but 0.709 after 1.e4).

## Ratings

Glicko-2 as specified by Glickman, with τ = 0.5 and the Illinois solver for
volatility. One game is one rating period, as on lichess. You start at 1450 with
RD 350; change that under Settings.

Maia is known to play above the rating it was trained on, so the conditioning
rating is **not** treated as the bot's strength. Instead each of the eleven
buckets carries its own Glicko-2 record, initialised at the bucket midpoint, and
both records update after every rated game — your rating is computed against the
bucket's *measured* rating. Over a few dozen games the pair calibrates itself.
The Ratings button shows every bucket's current record.

Rated games allow no takebacks, no hints and no analysis until the game is over,
and show your rating and RD change afterwards. Casual games change nothing and
allow takebacks and a Stockfish hint.

## Review

After a game, **Review** runs Stockfish over every position in the background
with a progress bar. Depth 18 by default, configurable in Settings (or set
seconds-per-move instead). Results are cached in the database.

Scoring follows lichess exactly, taken from the current sources rather than from
memory:

- win% = `50 + 50 * (2 / (1 + exp(-0.00368208 * cp)) - 1)` — scalachess
  `core/src/main/scala/eval.scala`
- accuracy = `103.1668 * exp(-0.04354 * drop) - 3.1669 + 1`, aggregated as the
  mean of a volatility-weighted mean and a harmonic mean — lila
  `modules/analyse/src/main/AccuracyPercent.scala`
- classification by drop in win%: inaccuracy ≥ 10, mistake ≥ 20, blunder ≥ 30

The eval graph under the board is clickable and jumps to that move; so is the
move list, and the arrow keys walk through the game. Judged moves are marked
`?!`, `?` and `??` in the move list and dotted on the graph, and the selected
position shows the engine's best move as an arrow plus its principal variation.

## Board and clock

Chessground — lichess's own board — with the brown theme and cburnett pieces.
Drag or click to move, legal-move dots, last-move and check highlighting,
premoves, a promotion picker, and coordinates.

Clocks are decided entirely by the server; the browser only animates between
snapshots. Presets are 10+5, 15+10, 10+0 and 5+3, plus a custom box. Flagging
loses, except against an opponent with insufficient mating material, which is a
draw — the lichess rule. All the draw rules are implemented: stalemate,
threefold repetition, the fifty-move rule and insufficient material. Threefold
and fifty-move are claimed automatically rather than offered as a button.

Every finished game is stored in SQLite as a PGN with full headers — players,
ratings, time control, result, termination, and per-move clock comments.

## Layout

```
app/
  main.py      FastAPI routes, websockets, the bot's turn, rating updates
  game.py      board, clock, endings, PGN
  bot.py       Maia-2: sampling, thinking time, draw and resign decisions
  glicko2.py   the rating system
  engine.py    Stockfish review and the lichess formulas
  db.py        SQLite
  config.py    every tunable constant
  state.py     the JSON the browser renders
  static/      index.html, css, js, vendored chessground, sounds
tests/
data/chess.sqlite3     games, PGNs, ratings, cached reviews
maia2_models/          the downloaded model
```

## Tests

```bash
.venv/bin/python -m pytest
```

119 tests. Glicko-2 is checked against the worked example in Glickman's paper
(1464.06 / 151.52 / 0.05999, plus every intermediate quantity); sampling is
checked for legality, for actually varying, and for responding to temperature;
the clock tests drive a fake monotonic clock so they are exact rather than
flaky; and there is a case for every way a game can end.

The tests marked `slow` load the real model and run the real engine. Skip them
with `-m "not slow"` (they take about 5 seconds).

## Requirements

Apple silicon Mac with Homebrew. Maia-2 runs on Metal (MPS) automatically —
`run.sh` reports the device it picked in the header chip. Python 3.12 is
required, not merely preferred: `maia2` pins `>=3.10,<3.13`.

See [CREDITS.md](CREDITS.md) for licences. Several bundled assets are AGPL, so
the project as a whole is AGPL-3.0-or-later.
