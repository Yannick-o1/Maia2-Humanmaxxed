# Credits and licences

This project bundles third-party work. Everything here is free software, but
several pieces are copyleft, so the project as a whole is distributed under the
**GNU Affero General Public License v3.0 or later** to stay compatible.

| What | Where it came from | Licence |
| --- | --- | --- |
| Chessground (board widget, `app/static/vendor/chessground/`) | [lichess-org/chessground](https://github.com/lichess-org/chessground) v10.5.0 | GPL-3.0-or-later |
| Brown board theme and the board SVG inside `chessground.brown.css` | lichess | AGPL-3.0-or-later |
| cburnett piece set, embedded in `chessground.cburnett.css` | [Colin M.L. Burnett](https://en.wikipedia.org/wiki/User:Cburnett) | GPL-2.0-or-later |
| `move.mp3`, `capture.mp3`, `lowtime.mp3`, `confirmation.mp3`, `gameend.mp3` | [lichess-org/lila](https://github.com/lichess-org/lila), `public/sound/standard/` | AGPL-3.0-or-later |
| `check.mp3` | lila, `public/sound/sfx/` (by [Enigmahack](https://github.com/Enigmahack)) | AGPL-3.0-or-later |
| Win%, accuracy and move-classification formulas | lila `modules/analyse` and [scalachess](https://github.com/lichess-org/scalachess) `core/src/main/scala/eval.scala` | AGPL-3.0-or-later |
| Maia-2 model and `maia2` package | [CSSLab/maia2](https://github.com/CSSLab/maia2) (NeurIPS 2024) | MIT |
| Stockfish | [official-stockfish/Stockfish](https://github.com/official-stockfish/Stockfish), installed via Homebrew | GPL-3.0-or-later |
| python-chess | [niklasf/python-chess](https://github.com/niklasf/python-chess) | GPL-3.0-or-later |
| Glicko-2 | Mark Glickman's [specification](http://www.glicko.net/glicko/glicko2.pdf) | public specification |

## Notes on the sounds

lichess's standard sound set has no check sound of its own: `Check.mp3` there is
a symlink to `Silence.mp3`, and `Victory.mp3`, `Defeat.mp3` and `Draw.mp3` all
symlink to `GenericNotify.mp3`. This app therefore uses `GenericNotify` for every
game ending, and borrows the check sound from lichess's `sfx` set so that checks
are actually audible.
