// The whole client. The server owns the rules and the clocks; this file draws
// what the server says and sends what the player does.

import { Chessground } from "../vendor/chessground/chessground.min.js";
import * as sound from "./sound.js";
import { renderGraph } from "./graph.js";

const PIECE_CLASSES = { q: "queen", r: "rook", b: "bishop", n: "knight" };

const app = {
  boot: null,
  game: null,
  review: null,
  reviewPly: null,
  reviewStatus: null,
  mode: "lobby", // lobby | playing | over
  orientation: "white",
  ground: null,
  socket: null,
  clock: null,
  lowTimeDone: { white: false, black: false },
  pendingPromotion: null,
  seenPly: 0,
  setup: { rated: true, color: "white", initial: 600, increment: 5, temperature: 1 },
};

const $ = (id) => document.getElementById(id);
const panel = () => $("panel");

// --- small helpers --------------------------------------------------------
function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (character) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character],
  );
}

function formatClock(seconds) {
  const total = Math.max(0, seconds);
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  if (total < 20) {
    const whole = Math.floor(rest);
    const tenths = Math.floor((rest - whole) * 10);
    return `${minutes}:${String(whole).padStart(2, "0")}<span class="tenths">.${tenths}</span>`;
  }
  return `${minutes}:${String(Math.floor(rest)).padStart(2, "0")}`;
}

function formatTimeControl(initial, increment) {
  return `${Math.round(initial / 60)}+${increment}`;
}

function signed(value) {
  if (value === null || value === undefined) return "";
  const rounded = Math.round(value * 10) / 10;
  if (rounded > 0) return `+${rounded}`;
  return String(rounded);
}

function deltaClass(value) {
  if (!value) return "flat";
  return value > 0 ? "up" : "down";
}

function toast(message, milliseconds = 3200) {
  const node = $("toast");
  node.textContent = message;
  node.classList.remove("hidden");
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.add("hidden"), milliseconds);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail || detail;
    } catch {
      /* keep statusText */
    }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

// --- board ----------------------------------------------------------------
function pieceAt(fen, square) {
  const [placement] = fen.split(" ");
  const file = square.charCodeAt(0) - 97;
  const rank = Number(square[1]);
  const rows = placement.split("/");
  const row = rows[8 - rank];
  if (!row) return null;
  let index = 0;
  for (const character of row) {
    if (/\d/.test(character)) {
      index += Number(character);
    } else {
      if (index === file) return character;
      index += 1;
    }
    if (index > file) return null;
  }
  return null;
}

function isPromotion(fen, orig, dest, humanColor) {
  const piece = pieceAt(fen, orig);
  if (!piece) return false;
  const isPawn = piece.toLowerCase() === "p";
  const isOurs = humanColor === "white" ? piece === "P" : piece === "p";
  const lastRank = humanColor === "white" ? "8" : "1";
  return isPawn && isOurs && dest[1] === lastRank;
}

function buildGround() {
  app.ground = Chessground($("board"), {
    fen: "start",
    orientation: app.orientation,
    coordinates: true,
    autoCastle: true,
    highlight: { lastMove: true, check: true },
    animation: { enabled: true, duration: 200 },
    movable: { free: false, color: undefined, dests: new Map(), showDests: true, events: {} },
    premovable: { enabled: true, showDests: true, events: {} },
    draggable: { enabled: true, showGhost: true },
    selectable: { enabled: true },
    drawable: { enabled: true, visible: true },
  });
}

function destsToMap(dests) {
  const map = new Map();
  for (const [from, targets] of Object.entries(dests || {})) map.set(from, targets);
  return map;
}

function syncBoard() {
  const game = app.game;
  if (!game || !app.ground) return;

  if (app.mode === "over" && app.reviewPly !== null && app.review) {
    const position = app.review.positions[app.reviewPly];
    app.ground.set({
      fen: position.fen,
      orientation: app.orientation,
      turnColor: position.fen.split(" ")[1] === "w" ? "white" : "black",
      lastMove: reviewLastMove(app.reviewPly),
      viewOnly: true,
      movable: { color: undefined, dests: new Map() },
      premovable: { enabled: false },
      drawable: { enabled: true, visible: true },
    });
    app.ground.setAutoShapes(reviewShapes(app.reviewPly));
    return;
  }

  const playing = game.status === "playing";
  app.ground.set({
    fen: game.fen,
    orientation: app.orientation,
    turnColor: game.turn,
    lastMove: game.lastMove || undefined,
    check: game.inCheck ? game.turn : false,
    viewOnly: !playing,
    // movable.color stays set to our colour even on the bot's turn: that is
    // what lets chessground accept premoves.
    movable: {
      free: false,
      color: playing ? game.humanColor : undefined,
      dests: destsToMap(game.dests),
      showDests: true,
      events: { after: onUserMove },
    },
    premovable: {
      enabled: playing,
      showDests: true,
      events: {},
    },
  });
  app.ground.setAutoShapes([]);
}

function reviewLastMove(ply) {
  if (!ply || !app.review) return undefined;
  const move = app.review.moves[ply - 1];
  if (!move) return undefined;
  return [move.uci.slice(0, 2), move.uci.slice(2, 4)];
}

function reviewShapes(ply) {
  if (!app.review) return [];
  const position = app.review.positions[ply];
  if (!position || !position.best) return [];
  return [
    {
      orig: position.best.slice(0, 2),
      dest: position.best.slice(2, 4),
      brush: "paleGreen",
    },
  ];
}

// --- playing --------------------------------------------------------------
async function onUserMove(orig, dest) {
  const game = app.game;
  if (!game || game.status !== "playing") return;
  if (isPromotion(game.fen, orig, dest, game.humanColor)) {
    showPromotion(orig, dest);
    return;
  }
  await sendMove(orig + dest);
}

async function sendMove(uci) {
  try {
    const next = await api(`/api/live/${app.game.id}/move`, {
      method: "POST",
      body: JSON.stringify({ uci }),
    });
    applyState(next);
  } catch (error) {
    toast(`Move rejected: ${error.message}`);
    syncBoard();
  }
}

function showPromotion(orig, dest) {
  app.pendingPromotion = { orig, dest };
  const colour = app.game.humanColor;
  const overlay = $("promotion");
  const container = $("promotion-choices");
  container.replaceChildren();

  const boardBox = $("board").getBoundingClientRect();
  const squareSize = boardBox.width / 8;
  const fileIndex = dest.charCodeAt(0) - 97;
  const column = app.orientation === "white" ? fileIndex : 7 - fileIndex;
  const fromTop = app.orientation === colour;

  container.style.width = `${squareSize}px`;
  container.style.left = `${column * squareSize}px`;
  container.style.top = fromTop ? "0px" : `${boardBox.width - squareSize * 4}px`;
  container.style.flexDirection = fromTop ? "column" : "column-reverse";

  for (const [letter, name] of Object.entries(PIECE_CLASSES)) {
    const button = document.createElement("button");
    button.className = "choice";
    button.title = name;
    // Reuse chessground's cburnett sprites by borrowing its piece classes.
    const sprite = document.createElement("div");
    sprite.className = "cg-wrap";
    sprite.style.cssText = "width:100%;height:100%;position:relative;";
    const piece = document.createElement("piece");
    piece.className = `${name} ${colour}`;
    piece.style.cssText = "width:100%;height:100%;position:absolute;inset:0;";
    sprite.append(piece);
    button.append(sprite);
    button.onclick = () => {
      overlay.classList.add("hidden");
      app.pendingPromotion = null;
      sendMove(orig + dest + letter);
    };
    container.append(button);
  }

  overlay.classList.remove("hidden");
  overlay.onclick = (event) => {
    if (event.target === overlay) {
      overlay.classList.add("hidden");
      app.pendingPromotion = null;
      syncBoard();
    }
  };
}

// --- clocks ---------------------------------------------------------------
function startClockLoop() {
  if (app.clockTimer) clearInterval(app.clockTimer);
  app.clockTimer = setInterval(paintClocks, 100);
}

function clockValues() {
  if (!app.clock) return { white: 0, black: 0 };
  const { white, black, turn, at, running } = app.clock;
  const elapsed = running ? (performance.now() - at) / 1000 : 0;
  return {
    white: turn === "white" ? Math.max(0, white - elapsed) : white,
    black: turn === "black" ? Math.max(0, black - elapsed) : black,
  };
}

function paintClocks() {
  const game = app.game;
  if (!game) return;
  const values = clockValues();
  for (const colour of ["white", "black"]) {
    const node = $(`clock-${colour}`);
    if (!node) continue;
    const seconds = values[colour];
    node.innerHTML = formatClock(seconds);
    const running = game.status === "playing" && game.turn === colour;
    node.classList.toggle("running", running);
    node.classList.toggle("low", seconds < game.lowTimeSeconds);
    node.classList.toggle("flagged", seconds <= 0 && game.status === "finished");

    if (
      running &&
      colour === game.humanColor &&
      seconds < game.lowTimeSeconds &&
      !app.lowTimeDone[colour]
    ) {
      app.lowTimeDone[colour] = true;
      sound.play("lowTime");
    }
  }
}

// --- state transitions ----------------------------------------------------
function applyState(next, extra = {}) {
  const previous = app.game;
  app.game = next;
  app.clock = {
    white: next.clocks.white,
    black: next.clocks.black,
    turn: next.turn,
    at: performance.now(),
    running: next.status === "playing",
  };

  if (next.status === "playing") app.mode = "playing";
  else if (next.status === "finished") app.mode = "over";

  // Sound for whatever just happened.
  if (next.plyCount > app.seenPly) {
    const lastRow = next.moves[next.moves.length - 1];
    const lastSan = lastRow ? (lastRow.black ? lastRow.black.san : lastRow.white.san) : "";
    sound.playForMove({
      capture: lastSan.includes("x"),
      check: lastSan.includes("+") || lastSan.includes("#"),
      gameOver: next.status === "finished",
    });
    app.seenPly = next.plyCount;
  } else if (next.status === "finished" && previous && previous.status !== "finished") {
    sound.play("gameEnd");
  }

  if (extra.ratingChange || next.ratingChange) {
    app.lastRatingChange = extra.ratingChange || next.ratingChange;
    refreshRatingChip();
  }
  if (next.drawDeclined && previous && previous.status === "playing") {
    toast("Maia-2 declined the draw.");
  }

  syncBoard();
  renderPanel();
  paintClocks();

  // A premove can only be released once the new legal moves are in place.
  if (next.status === "playing" && next.isHumanTurn) {
    const played = app.ground.playPremove();
    if (played) return;
  }
}

function connectSocket(gameId) {
  if (app.socket) {
    app.socket.onclose = null;
    app.socket.close();
  }
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/ws/${gameId}`);
  app.socket = socket;
  socket.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "reviewProgress") {
      app.reviewStatus = { status: "running", done: data.done, total: data.total };
      renderPanel();
      return;
    }
    if (data.type === "reviewDone") {
      app.review = data.review;
      app.reviewStatus = { status: "done" };
      app.reviewPly = app.review.positions.length - 1;
      renderPanel();
      syncBoard();
      return;
    }
    if (data.type === "reviewError") {
      app.reviewStatus = { status: "error", message: data.message };
      renderPanel();
      return;
    }
    if (data.type === "error") {
      toast(data.message);
      return;
    }
    if (data.id) applyState(data, data);
  };
  socket.onclose = () => {
    if (app.game && app.game.status === "playing") setTimeout(() => connectSocket(gameId), 1000);
  };
}

// --- panel rendering ------------------------------------------------------
function renderPanel() {
  if (app.mode === "lobby") renderLobby();
  else renderGame();
  $("graph-wrap").classList.toggle("hidden", !(app.mode === "over" && app.review));
  if (app.mode === "over" && app.review) {
    renderGraph($("eval-graph"), app.review, app.reviewPly, (ply) => {
      app.reviewPly = ply;
      syncBoard();
      renderPanel();
    });
  }
}

function renderLobby() {
  const boot = app.boot;
  const presets = boot ? boot.presets : [];
  const setup = app.setup;
  panel().innerHTML = `
    <div class="panel-section">
      <h2>New game</h2>
      <div class="field">
        <label>Mode</label>
        <div class="segmented" id="seg-rated">
          <button data-rated="true" class="${setup.rated ? "active" : ""}">Rated</button>
          <button data-rated="false" class="${setup.rated ? "" : "active"}">Casual</button>
        </div>
        <div class="inline-note">${
          setup.rated
            ? "No takebacks, no hints. Your rating and RD change afterwards."
            : "Takebacks and hints allowed. Nothing is rated."
        }</div>
      </div>
      <div class="field">
        <label>Play as</label>
        <div class="segmented" id="seg-color">
          ${["white", "random", "black"]
            .map(
              (choice) =>
                `<button data-color="${choice}" class="${
                  setup.color === choice ? "active" : ""
                }">${choice[0].toUpperCase()}${choice.slice(1)}</button>`,
            )
            .join("")}
        </div>
      </div>
      <div class="field">
        <label>Time control</label>
        <div class="grid-4" id="seg-time">
          ${presets
            .map(
              (preset) =>
                `<button data-initial="${preset.initial}" data-increment="${preset.increment}"
                  class="${
                    setup.initial === preset.initial && setup.increment === preset.increment
                      ? "active"
                      : ""
                  }">${preset.name}</button>`,
            )
            .join("")}
        </div>
        <div class="grid-2" style="margin-top:0.5rem">
          <div>
            <label>Minutes</label>
            <input type="number" id="custom-minutes" min="1" max="180"
              value="${Math.round(setup.initial / 60)}" />
          </div>
          <div>
            <label>Increment (s)</label>
            <input type="number" id="custom-increment" min="0" max="180"
              value="${setup.increment}" />
          </div>
        </div>
      </div>
      <div class="field">
        <label>Temperature — how much the bot wanders from its favourite move</label>
        <div class="range-row">
          <input type="range" id="temp-range" min="0" max="2" step="0.05"
            value="${setup.temperature}" />
          <output id="temp-out">${setup.temperature.toFixed(2)}</output>
        </div>
        <div class="inline-note">
          1.00 samples Maia-2's own distribution. Lower plays its top move more
          often; higher blunders more.
        </div>
      </div>
    </div>
    <div class="button-row">
      <button class="primary" id="btn-start" ${boot && boot.bot.ready ? "" : "disabled"}>
        ${boot && boot.bot.ready ? "Start game" : "Loading Maia-2…"}
      </button>
    </div>
  `;

  $("seg-rated").onclick = (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    app.setup.rated = button.dataset.rated === "true";
    renderLobby();
  };
  $("seg-color").onclick = (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    app.setup.color = button.dataset.color;
    renderLobby();
  };
  $("seg-time").onclick = (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    app.setup.initial = Number(button.dataset.initial);
    app.setup.increment = Number(button.dataset.increment);
    renderLobby();
  };
  $("custom-minutes").onchange = (event) => {
    app.setup.initial = Math.max(1, Number(event.target.value)) * 60;
    renderLobby();
  };
  $("custom-increment").onchange = (event) => {
    app.setup.increment = Math.max(0, Number(event.target.value));
    renderLobby();
  };
  $("temp-range").oninput = (event) => {
    app.setup.temperature = Number(event.target.value);
    $("temp-out").textContent = app.setup.temperature.toFixed(2);
  };
  $("btn-start").onclick = startGame;
}

function playerRow(game, colour, id) {
  const player = game[colour];
  const thinking = game.isBotTurn && player.isBot && game.status === "playing";
  const sub = player.isBot
    ? `Maia-2 rapid · bucket ${escapeHtml(player.bucket)} · fed ${player.conditioningElo}`
    : `RD ${player.rd}`;
  return `
    <div class="player-row">
      <div class="who">
        <div class="name">${escapeHtml(player.name)} <span class="sub">${player.rating}</span></div>
        <div class="sub ${thinking ? "thinking" : ""}">${thinking ? "thinking…" : sub}</div>
      </div>
      <div class="clock" id="${id}">0:00</div>
    </div>
  `;
}

function moveListHtml(game) {
  if (!game.moves.length) return `<div class="moves-empty">No moves yet</div>`;
  const judgments = new Map();
  if (app.review) {
    for (const move of app.review.moves) judgments.set(move.ply, move);
  }
  const cell = (entry) => {
    if (!entry) return `<div class="san empty"></div>`;
    const judged = judgments.get(entry.ply);
    const classes = ["san"];
    if (judged && judged.judgment) classes.push(judged.judgment);
    if (app.reviewPly === entry.ply) classes.push("active");
    const glyph = judged && judged.glyph ? `<span class="glyph">${judged.glyph}</span>` : "";
    return `<div class="${classes.join(" ")}" data-ply="${entry.ply}">${escapeHtml(
      entry.san,
    )}${glyph}</div>`;
  };
  const rows = game.moves
    .map(
      (row) =>
        `<div class="index">${row.number}</div>${cell(row.white)}${cell(row.black)}`,
    )
    .join("");
  return `<div class="moves" id="move-list">${rows}</div>`;
}

function renderGame() {
  const game = app.game;
  const top = app.orientation === "white" ? "black" : "white";
  const bottom = app.orientation === "white" ? "white" : "black";
  const over = game.status === "finished";

  const banner = over ? resultBannerHtml() : "";
  const controls = over ? overControlsHtml() : playControlsHtml(game);

  panel().innerHTML = `
    ${banner}
    ${playerRow(game, top, `clock-${top}`)}
    ${moveListHtml(game)}
    ${playerRow(game, bottom, `clock-${bottom}`)}
    ${controls}
    ${over && app.review ? reviewDetailHtml() : ""}
  `;

  wireGameControls();
  paintClocks();
  scrollMoveListToEnd();
}

function scrollMoveListToEnd() {
  const list = $("move-list");
  if (!list) return;
  const active = list.querySelector(".san.active");
  if (active) active.scrollIntoView({ block: "nearest" });
  else list.scrollTop = list.scrollHeight;
}

function playControlsHtml(game) {
  return `
    <div class="button-row">
      <button id="btn-resign" class="danger">Resign</button>
      <button id="btn-draw">Offer draw</button>
      <button id="btn-flip">Flip</button>
    </div>
    ${
      game.rated
        ? `<div class="panel-section"><div class="inline-note">
             Rated game: no takebacks, no hints, no analysis until it is over.
           </div></div>`
        : `<div class="button-row">
             <button id="btn-takeback" ${game.canTakeback ? "" : "disabled"}>Takeback</button>
             <button id="btn-hint">Hint</button>
           </div>`
    }
  `;
}

function resultBannerHtml() {
  const game = app.game;
  const change = app.lastRatingChange;
  let ratingHtml = "";
  if (change && change.rated) {
    ratingHtml = `
      <div class="rating-change">
        <div class="block">
          <div class="label">Your rating</div>
          <div class="value">${Math.round(change.humanRatingAfter)}
            <span class="delta ${deltaClass(change.humanRatingDelta)}">${signed(
              change.humanRatingDelta,
            )}</span>
          </div>
        </div>
        <div class="block">
          <div class="label">Your RD</div>
          <div class="value">${Math.round(change.humanRdAfter)}
            <span class="delta ${deltaClass(-change.humanRdDelta)}">${signed(
              change.humanRdDelta,
            )}</span>
          </div>
        </div>
        <div class="block">
          <div class="label">Bucket ${escapeHtml(change.bucket)}</div>
          <div class="value">${Math.round(change.botRatingAfter)}
            <span class="delta ${deltaClass(change.botRatingDelta)}">${signed(
              change.botRatingDelta,
            )}</span>
          </div>
        </div>
      </div>`;
  } else {
    ratingHtml = `<div class="sub">Casual game — nothing rated.</div>`;
  }
  return `
    <div class="result-banner">
      <div class="headline">${escapeHtml(game.statusText)}</div>
      <div class="sub">${escapeHtml(game.result || "")} ·
        ${formatTimeControl(game.initialSeconds, game.incrementSeconds)} ·
        temperature ${game.temperature}</div>
      ${ratingHtml}
    </div>
  `;
}

function overControlsHtml() {
  const status = app.reviewStatus;
  let reviewBlock = "";
  if (app.review) {
    const accuracy = app.review.accuracy;
    const counts = app.review.judgmentCounts;
    const mine = app.review.humanColor;
    const theirs = mine === "white" ? "black" : "white";
    const line = (kind, glyph) => `
      <div class="line ${kind}">
        <span><span class="swatch">${glyph}</span>${kind[0].toUpperCase()}${kind.slice(1)}</span>
        <span>you ${counts[mine][kind]} · bot ${counts[theirs][kind]}</span>
      </div>`;
    reviewBlock = `
      <div class="panel-section">
        <h2>Review — depth ${app.review.depth}</h2>
        <div class="accuracy-row">
          <div class="accuracy-card">
            <div class="label">Your accuracy</div>
            <div class="value">${accuracy[mine] ?? "—"}%</div>
          </div>
          <div class="accuracy-card">
            <div class="label">Maia-2</div>
            <div class="value">${accuracy[theirs] ?? "—"}%</div>
          </div>
        </div>
        <div class="judgments">
          ${line("inaccuracy", "?!")}
          ${line("mistake", "?")}
          ${line("blunder", "??")}
        </div>
      </div>
      <div class="nav-row">
        <button id="nav-first">|&lt;</button>
        <button id="nav-prev">&lt;</button>
        <button id="nav-next">&gt;</button>
        <button id="nav-last">&gt;|</button>
      </div>`;
  } else if (status && status.status === "running") {
    const percent = status.total ? Math.round((status.done / status.total) * 100) : 0;
    reviewBlock = `
      <div class="panel-section">
        <h2>Analysing</h2>
        <div class="inline-note">${status.done} / ${status.total} positions · ${percent}%</div>
        <div class="progress"><div class="bar" style="width:${percent}%"></div></div>
      </div>`;
  } else if (status && status.status === "error") {
    reviewBlock = `<div class="panel-section"><div class="inline-note">
        Review failed: ${escapeHtml(status.message)}</div></div>`;
  }

  return `
    <div class="button-row">
      <button class="primary" id="btn-review" ${
        app.review || (status && status.status === "running") ? "disabled" : ""
      }>Review</button>
      <button id="btn-rematch">New game</button>
      <button id="btn-flip">Flip</button>
    </div>
    <div class="button-row">
      <button id="btn-pgn">Download PGN</button>
    </div>
    ${reviewBlock}
  `;
}

function reviewDetailHtml() {
  const ply = app.reviewPly;
  if (ply === null || ply === undefined) return "";
  const move = app.review.moves[ply - 1];
  const position = app.review.positions[ply];
  if (!position) return "";

  const evalText = position.mate
    ? `#${position.mate}`
    : `${position.cpWhite > 0 ? "+" : ""}${(position.cpWhite / 100).toFixed(2)}`;

  let played = "";
  if (move) {
    const tag = move.judgment
      ? `<span class="tag ${move.judgment}">${move.glyph} ${move.judgment}</span>`
      : "";
    const better =
      move.judgment && move.bestSan
        ? ` Best was <strong>${escapeHtml(move.bestSan)}</strong>
            <button id="btn-show-better" style="padding:0.1em 0.5em;font-size:0.8rem">show</button>`
        : "";
    played = `
      <div class="headline">${move.moveNumber}${move.color === "white" ? "." : "..."}
        ${escapeHtml(move.san)} ${tag}</div>
      <div>Win chance ${move.winBefore}% → ${move.winAfter}%
        (${move.drop > 0 ? `−${move.drop}` : "0"}) · accuracy ${move.accuracy}%${better}</div>`;
  } else {
    played = `<div class="headline">Starting position</div>`;
  }

  return `
    <div class="move-detail">
      ${played}
      <div>Engine eval <strong>${evalText}</strong>${
        position.bestSan ? ` · best ${escapeHtml(position.bestSan)}` : ""
      }</div>
      ${
        position.pvSan && position.pvSan.length
          ? `<div class="pv">${escapeHtml(position.pvSan.join(" "))}</div>`
          : ""
      }
    </div>
  `;
}

// --- control wiring -------------------------------------------------------
function wireGameControls() {
  const bind = (id, handler) => {
    const node = $(id);
    if (node) node.onclick = handler;
  };

  bind("btn-flip", () => {
    app.orientation = app.orientation === "white" ? "black" : "white";
    app.ground.set({ orientation: app.orientation });
    renderPanel();
  });
  bind("btn-resign", async () => {
    if (!confirm("Resign this game?")) return;
    applyState(await api(`/api/live/${app.game.id}/resign`, { method: "POST" }));
  });
  bind("btn-draw", async () => {
    const response = await api(`/api/live/${app.game.id}/draw`, { method: "POST" });
    applyState(response);
    if (!response.drawAccepted) {
      toast(
        `Maia-2 declined. Its win chance is ${
          response.botWinProb === null ? "unknown" : Math.round(response.botWinProb * 100) + "%"
        } and it accepts only between ${app.boot.thresholds.drawAcceptMin * 100}% and ${
          app.boot.thresholds.drawAcceptMax * 100
        }% in a simplified position.`,
        5000,
      );
    }
  });
  bind("btn-takeback", async () => {
    try {
      applyState(await api(`/api/live/${app.game.id}/takeback`, { method: "POST" }));
    } catch (error) {
      toast(error.message);
    }
  });
  bind("btn-hint", async () => {
    try {
      const hint = await api(`/api/live/${app.game.id}/hint`);
      if (!hint.uci) return toast("No hint available.");
      app.ground.setAutoShapes([{ orig: hint.from, dest: hint.to, brush: "paleBlue" }]);
      toast(`Stockfish likes ${hint.san}`);
    } catch (error) {
      toast(error.message);
    }
  });
  bind("btn-rematch", () => {
    app.mode = "lobby";
    app.review = null;
    app.reviewPly = null;
    app.reviewStatus = null;
    app.game = null;
    app.clock = null;
    $("graph-wrap").classList.add("hidden");
    renderPanel();
  });
  bind("btn-pgn", () => {
    window.open(`/api/games/${app.game.id}/pgn`, "_blank");
  });
  bind("btn-review", startReview);
  bind("nav-first", () => gotoPly(0));
  bind("nav-prev", () => gotoPly((app.reviewPly ?? 0) - 1));
  bind("nav-next", () => gotoPly((app.reviewPly ?? 0) + 1));
  bind("nav-last", () => gotoPly(app.review ? app.review.positions.length - 1 : 0));
  bind("btn-show-better", () => gotoPly((app.reviewPly ?? 1) - 1));

  const list = $("move-list");
  if (list) {
    list.onclick = (event) => {
      const cell = event.target.closest(".san");
      if (!cell || !cell.dataset.ply || !app.review) return;
      gotoPly(Number(cell.dataset.ply));
    };
  }
}

function gotoPly(ply) {
  if (!app.review) return;
  const total = app.review.positions.length;
  app.reviewPly = Math.max(0, Math.min(total - 1, ply));
  syncBoard();
  renderPanel();
}

async function startReview() {
  const depth = app.boot.settings.reviewDepth;
  app.reviewStatus = { status: "running", done: 0, total: (app.game.plyCount || 0) + 1 };
  renderPanel();
  try {
    const response = await api(`/api/games/${app.game.id}/review`, {
      method: "POST",
      body: JSON.stringify({ depth }),
    });
    if (response.status === "done") {
      app.review = response.review;
      app.reviewStatus = { status: "done" };
      app.reviewPly = app.review.positions.length - 1;
      renderPanel();
      syncBoard();
    }
  } catch (error) {
    app.reviewStatus = { status: "error", message: error.message };
    renderPanel();
  }
}

async function startGame() {
  try {
    const game = await api("/api/games", {
      method: "POST",
      body: JSON.stringify({
        rated: app.setup.rated,
        color: app.setup.color,
        initial: app.setup.initial,
        increment: app.setup.increment,
        temperature: app.setup.temperature,
      }),
    });
    app.review = null;
    app.reviewPly = null;
    app.reviewStatus = null;
    app.lastRatingChange = null;
    app.seenPly = 0;
    app.lowTimeDone = { white: false, black: false };
    app.orientation = game.humanColor;
    connectSocket(game.id);
    applyState(game);
    sound.play("confirmation");
  } catch (error) {
    toast(`Could not start: ${error.message}`);
  }
}

// --- modals ---------------------------------------------------------------
function openModal(title, bodyHtml, footHtml = "") {
  $("modal-title").textContent = title;
  $("modal-body").innerHTML = bodyHtml;
  $("modal-foot").innerHTML = footHtml;
  $("modal-backdrop").classList.remove("hidden");
}

function closeModal() {
  $("modal-backdrop").classList.add("hidden");
}

async function openRatings() {
  const boot = await api("/api/bootstrap");
  app.boot = boot;
  const rows = boot.ratings
    .map((row) => {
      const isHuman = row.id === "human";
      return `<tr class="${isHuman ? "current" : ""}">
        <td>${isHuman ? "You" : escapeHtml(row.label)}</td>
        <td class="num">${Math.round(row.rating)}</td>
        <td class="num">${Math.round(row.rd)}</td>
        <td class="num">${row.volatility.toFixed(4)}</td>
        <td class="num">${row.games}</td>
      </tr>`;
    })
    .join("");
  openModal(
    "Glicko-2 ratings",
    `<table class="ratings">
       <thead><tr><th>Who</th><th>Rating</th><th>RD</th><th>Vol</th><th>Games</th></tr></thead>
       <tbody>${rows}</tbody>
     </table>
     <div class="inline-note">
       Each Maia-2 conditioning bucket keeps its own Glicko-2 record, started at
       the bucket's midpoint. Maia plays above its training label, so your rating
       is computed against the bucket's measured rating rather than the number it
       was trained on. Both sides update after every rated game, so the pair
       calibrates itself as you play.
     </div>`,
  );
}

async function openHistory() {
  const { games } = await api("/api/games?limit=50");
  if (!games.length) {
    openModal("Games", `<div class="inline-note">No games saved yet.</div>`);
    return;
  }
  const items = games
    .map((row) => {
      const mine = row.human_color;
      let outcome = "draw";
      let text = "Draw";
      if (row.result === "1-0") {
        outcome = mine === "white" ? "win" : "loss";
      } else if (row.result === "0-1") {
        outcome = mine === "black" ? "win" : "loss";
      }
      if (outcome !== "draw") text = outcome === "win" ? "Win" : "Loss";
      const delta =
        row.rated && row.human_rating_after !== null
          ? ` · ${signed(row.human_rating_after - row.human_rating_before)}`
          : "";
      return `<div class="history-item">
        <div>
          <div class="outcome ${outcome}">${text} as ${mine}${delta}</div>
          <div class="meta">${escapeHtml(row.status_text || "")} ·
            ${formatTimeControl(row.time_initial, row.time_increment)} ·
            ${row.rated ? "rated" : "casual"} · ${row.ply_count} plies</div>
        </div>
        <div><button data-pgn="${row.id}">PGN</button></div>
      </div>`;
    })
    .join("");
  openModal("Games", `<div class="history-list">${items}</div>`);
  $("modal-body").onclick = (event) => {
    const button = event.target.closest("button[data-pgn]");
    if (button) window.open(`/api/games/${button.dataset.pgn}/pgn`, "_blank");
  };
}

function openSettings() {
  const settings = app.boot.settings;
  openModal(
    "Settings",
    `<div class="field">
       <label>Default temperature</label>
       <div class="range-row">
         <input type="range" id="set-temp" min="0" max="2" step="0.05" value="${settings.temperature}" />
         <output id="set-temp-out">${Number(settings.temperature).toFixed(2)}</output>
       </div>
       <div class="inline-note">Applied to new games. 0 makes the bot deterministic.</div>
     </div>
     <div class="field">
       <label>Review depth</label>
       <input type="number" id="set-depth" min="6" max="30" value="${settings.reviewDepth}" />
       <div class="inline-note">Stockfish depth per position. Set seconds below to use time instead.</div>
     </div>
     <div class="field">
       <label>Review seconds per move (0 = use depth)</label>
       <input type="number" id="set-movetime" min="0" max="10" step="0.1" value="${settings.reviewMovetime}" />
     </div>
     <div class="field">
       <label>Starting rating / RD for a reset</label>
       <div class="grid-2">
         <input type="number" id="set-start-rating" min="400" max="2800" value="${settings.startRating}" />
         <input type="number" id="set-start-rd" min="30" max="500" value="${settings.startRd}" />
       </div>
       <div class="inline-note">Tick below to wipe your current rating back to these.</div>
       <label style="display:flex;gap:0.5rem;align-items:center;margin-top:0.4rem">
         <input type="checkbox" id="set-reset" style="width:auto" /> Reset my rating now
       </label>
     </div>
     <div class="field">
       <label style="display:flex;gap:0.5rem;align-items:center">
         <input type="checkbox" id="set-sound" style="width:auto" ${settings.sound ? "checked" : ""} /> Sound
       </label>
     </div>`,
    `<button id="set-cancel">Cancel</button><button class="primary" id="set-save">Save</button>`,
  );
  $("set-temp").oninput = (event) => {
    $("set-temp-out").textContent = Number(event.target.value).toFixed(2);
  };
  $("set-cancel").onclick = closeModal;
  $("set-save").onclick = async () => {
    const payload = {
      temperature: Number($("set-temp").value),
      reviewDepth: Number($("set-depth").value),
      reviewMovetime: Number($("set-movetime").value),
      startRating: Number($("set-start-rating").value),
      startRd: Number($("set-start-rd").value),
      resetRating: $("set-reset").checked,
      sound: $("set-sound").checked,
    };
    app.boot = await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
    sound.setEnabled(app.boot.settings.sound);
    app.setup.temperature = app.boot.settings.temperature;
    refreshRatingChip();
    closeModal();
    if (app.mode === "lobby") renderLobby();
    toast("Settings saved.");
  };
}

function refreshRatingChip() {
  const player = app.boot && app.boot.player;
  if (!player) return;
  $("rating-chip").innerHTML = `${Math.round(player.rating)} <span>RD ${Math.round(
    player.rd,
  )}</span>`;
}

function refreshBotChip() {
  const bot = app.boot.bot;
  const chip = $("bot-chip");
  chip.classList.toggle("ok", bot.ready);
  chip.classList.toggle("bad", !!bot.error);
  if (bot.error) chip.textContent = `model failed: ${bot.error}`;
  else if (bot.ready) chip.textContent = `${bot.model} on ${bot.device}`;
  else chip.textContent = "loading model…";
}

// --- boot -----------------------------------------------------------------
async function refreshBoot() {
  app.boot = await api("/api/bootstrap");
  sound.setEnabled(app.boot.settings.sound);
  app.setup.temperature = Number(app.boot.settings.temperature);
  const preset = app.boot.presets.find((item) => item.name === app.boot.defaultPreset);
  if (preset && app.mode === "lobby") {
    app.setup.initial = preset.initial;
    app.setup.increment = preset.increment;
  }
  refreshRatingChip();
  refreshBotChip();
}

async function waitForBot() {
  while (!app.boot.bot.ready && !app.boot.bot.error) {
    await new Promise((resolve) => setTimeout(resolve, 1200));
    await refreshBoot();
    if (app.mode === "lobby") renderLobby();
  }
  if (app.mode === "lobby") renderLobby();
}

async function main() {
  buildGround();
  sound.preload();
  await refreshBoot();
  renderPanel();
  startClockLoop();
  waitForBot();

  $("btn-settings").onclick = openSettings;
  $("btn-ratings").onclick = openRatings;
  $("btn-history").onclick = openHistory;
  $("modal-close").onclick = closeModal;
  $("modal-backdrop").onclick = (event) => {
    if (event.target === $("modal-backdrop")) closeModal();
  };

  document.addEventListener("keydown", (event) => {
    if (app.mode !== "over" || !app.review) return;
    if (event.key === "ArrowLeft") gotoPly((app.reviewPly ?? 0) - 1);
    if (event.key === "ArrowRight") gotoPly((app.reviewPly ?? 0) + 1);
    if (event.key === "Home") gotoPly(0);
    if (event.key === "End") gotoPly(app.review.positions.length - 1);
  });
}

main();
