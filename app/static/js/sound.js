// Lichess's own sound files, vendored from the lila repository.
// The standard set has no check sound of its own (Check.mp3 there is a symlink
// to Silence.mp3), so check uses the file from lichess's "sfx" set instead.

const FILES = {
  move: "/static/sound/move.mp3",
  capture: "/static/sound/capture.mp3",
  check: "/static/sound/check.mp3",
  gameEnd: "/static/sound/gameend.mp3",
  lowTime: "/static/sound/lowtime.mp3",
  confirmation: "/static/sound/confirmation.mp3",
};

const cache = new Map();
let enabled = true;

function audio(name) {
  if (!cache.has(name)) {
    const element = new Audio(FILES[name]);
    element.preload = "auto";
    element.volume = 0.6;
    cache.set(name, element);
  }
  return cache.get(name);
}

export function setEnabled(value) {
  enabled = !!value;
}

export function play(name) {
  if (!enabled || !FILES[name]) return;
  const element = audio(name);
  try {
    element.currentTime = 0;
    // Browsers reject playback before the first user gesture; that is fine.
    element.play().catch(() => {});
  } catch {
    /* ignore */
  }
}

export function preload() {
  Object.keys(FILES).forEach(audio);
}

// Pick the sound a move should make, the way lichess does.
export function playForMove({ capture, check, gameOver }) {
  if (gameOver) play("gameEnd");
  else if (check) play("check");
  else if (capture) play("capture");
  else play("move");
}
