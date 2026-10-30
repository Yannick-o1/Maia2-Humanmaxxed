// The eval graph under the board: White's win percentage over the game,
// clickable to jump to a move, with a marker on every judged move.

const WIDTH = 1000;
const HEIGHT = 100;
const SVG_NS = "http://www.w3.org/2000/svg";

const JUDGMENT_COLOURS = {
  inaccuracy: "#56b4e9",
  mistake: "#e69f00",
  blunder: "#df5353",
};

function element(name, attributes) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) {
    node.setAttribute(key, String(value));
  }
  return node;
}

function x(ply, total) {
  return total <= 1 ? 0 : (ply / (total - 1)) * WIDTH;
}

function y(winPercent) {
  // 100% for White at the top, 0% at the bottom.
  return HEIGHT - (winPercent / 100) * HEIGHT;
}

export function renderGraph(svg, review, activePly, onSelect) {
  svg.setAttribute("viewBox", `0 0 ${WIDTH} ${HEIGHT}`);
  svg.replaceChildren();

  const positions = review.positions || [];
  const total = positions.length;
  if (total < 2) return;

  // Black's half of the graph, so the eye reads advantage by area.
  svg.append(element("rect", { x: 0, y: 0, width: WIDTH, height: HEIGHT / 2, fill: "#1b1a17" }));
  svg.append(
    element("rect", { x: 0, y: HEIGHT / 2, width: WIDTH, height: HEIGHT / 2, fill: "#111" }),
  );

  const points = positions.map((position, ply) => `${x(ply, total)},${y(position.winWhite)}`);

  svg.append(
    element("polygon", {
      points: `0,${HEIGHT / 2} ${points.join(" ")} ${WIDTH},${HEIGHT / 2}`,
      fill: "#dedede",
      "fill-opacity": "0.22",
    }),
  );
  svg.append(
    element("polyline", {
      points: points.join(" "),
      fill: "none",
      stroke: "#dedede",
      "stroke-width": "1.6",
      "stroke-linejoin": "round",
    }),
  );
  svg.append(
    element("line", {
      x1: 0,
      y1: HEIGHT / 2,
      x2: WIDTH,
      y2: HEIGHT / 2,
      stroke: "#5a5a5a",
      "stroke-width": "0.8",
      "stroke-dasharray": "4 4",
    }),
  );

  for (const move of review.moves || []) {
    if (!move.judgment) continue;
    const position = positions[move.ply];
    if (!position) continue;
    svg.append(
      element("circle", {
        cx: x(move.ply, total),
        cy: y(position.winWhite),
        r: 3.2,
        fill: JUDGMENT_COLOURS[move.judgment] || "#fff",
        stroke: "#161512",
        "stroke-width": "0.8",
      }),
    );
  }

  if (activePly !== null && activePly !== undefined && positions[activePly]) {
    svg.append(
      element("line", {
        x1: x(activePly, total),
        y1: 0,
        x2: x(activePly, total),
        y2: HEIGHT,
        stroke: "#3692e7",
        "stroke-width": "1.4",
      }),
    );
  }

  if (onSelect) {
    svg.onclick = (event) => {
      const box = svg.getBoundingClientRect();
      const ratio = (event.clientX - box.left) / box.width;
      const ply = Math.round(ratio * (total - 1));
      onSelect(Math.max(0, Math.min(total - 1, ply)));
    };
  }
}
