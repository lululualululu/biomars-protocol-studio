import { directions } from "./styles.js";
const params = new URLSearchParams(location.search);
const id = params.get("style") || "01";
const direction = directions.find((d) => d.id === id) || directions[0];
const thumb = params.has("thumb");
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const root = document.querySelector("#scene");
document.title = `BioMars · ${direction.name}`;
document.body.className = `theme-${direction.id}${thumb ? " thumbnail" : ""}`;
document.body.style.setProperty("--display", `'${direction.font}'`);
const font = document.createElement("link");
font.rel = "stylesheet";
font.href = `https://fonts.googleapis.com/css2?family=${direction.font.replaceAll(" ", "+")}&display=swap`;
document.head.append(font);
const arrow = '<span aria-hidden="true">↗</span>';
const wordmark = (name = "BioMars") =>
  `<a href="../" target="_top" class="wordmark">${name}</a>`;
const nav = (mark = "BioMars", right = "Protocol Studio") =>
  `<header class="scene-nav">${wordmark(mark)}<span>${right}</span></header>`;
const input = (button = "↗", placeholder = "Your experiment", cls = "") =>
  `<form class="experiment ${cls}"><label class="sr-only" for="experiment">Experimental request</label><input id="experiment" name="query" autocomplete="off" placeholder="${placeholder}" aria-label="Experimental request" maxlength="6000" required><button type="submit" aria-label="Open this request in Protocol Studio">${button}</button></form>`;
const img = (asset, cls = "") =>
  `<img class="art ${cls}" src="./assets/${asset}.jpg" alt="${{ pearl: "Sculptural glass cell", nocturne: "Glass specimen dish in a dark laboratory", botanical: "Ink and watercolor botanical artwork", softbody: "Inflated orange abstract sculpture" }[asset]}" draggable="false">`;
const star = `<svg viewBox="0 0 100 100" class="asterisk" aria-hidden="true"><path d="M50 0v100M0 50h100M15 15l70 70M85 15 15 85" fill="none" stroke="currentColor" stroke-width="18"/></svg>`;
const circleDiagram = `<svg viewBox="0 0 400 400" class="cell-diagram" aria-hidden="true"><circle cx="200" cy="200" r="170"/><circle cx="200" cy="200" r="154" stroke-dasharray="2 8"/><ellipse cx="200" cy="200" rx="165" ry="75" transform="rotate(-32 200 200)"/><ellipse cx="200" cy="200" rx="65" ry="165" transform="rotate(-32 200 200)"/><circle cx="200" cy="200" r="37"/><path d="M0 200h400M200 0v400" stroke-dasharray="4 6"/><circle cx="200" cy="200" r="9" fill="currentColor"/></svg>`;
const views = {
  "01": () =>
    `${nav("BioMars", "Protocol Studio")}<section class="precision"><div class="precision-title"><p class="eyebrow">A NEW EXPERIMENT</p><h1>Less friction.<br>More discovery.</h1>${input("Start " + arrow)}</div><div class="pearl-wrap">${img("pearl")}<span class="specimen-tag">B / 01</span></div></section><footer class="scene-foot"><span>Research, refined.</span><span>Open source ${arrow}</span></footer>`,
  "02": () =>
    `${nav("BioMars®", "Protocol Studio / 02")}<section class="swiss-grid"><h1>PRO<br>TO<br>COL.</h1><div class="swiss-right"><div class="swiss-symbol">${star}</div><div class="swiss-form"><span class="micro">01 — NEW EXPERIMENT</span>${input(arrow, "Begin here")}</div></div><span class="swiss-bottom">SCIENCE.<br>IN ORDER.</span></section>`,
  "03": () =>
    `${nav("BioMars", "プロトコル")}<section class="ma-layout"><div class="vertical-word">探究の、その先へ。</div><div class="ma-text"><span class="red-seal">生</span><h1>余白から、<br><i>はじめる。</i></h1><p>Protocol Studio</p>${input("→", "Your experiment")}</div>${img("botanical")}<span class="ma-line"></span></section><footer class="scene-foot"><span>間 — MA</span><span>01 / 生成</span></footer>`,
  "04": () =>
    `<header class="journal-mast">${wordmark()}<span>THE PROTOCOL ISSUE</span><span>Nº 04</span></header><section class="journal-grid"><div class="journal-head"><h1>The art<br>of <i>asking.</i></h1><div class="journal-rule"></div><p>Protocol Studio</p>${input(arrow, "A question worth exploring")}</div><figure>${img("nocturne")}<figcaption>METHOD / MATERIAL / DISCOVERY</figcaption></figure><div class="journal-edge">B—M</div></section>`,
  "05": () =>
    `${nav("BIOMARS_", "PROTOCOL STUDIO")}<section class="acid-layout"><h1>MAKE<br>IT<span class="acid-star">✳</span><br>HAPPEN.</h1><div class="acid-panel"><span>INPUT_01</span>${input("GO!", "YOUR EXPERIMENT")}<div class="acid-checker"></div></div></section><div class="acid-bottom">LESS TALK. MORE LAB. ↗</div>`,
  "06": () =>
    `${nav("bio/mars", "Protocol Studio")}<section class="bauhaus-layout"><div class="bauhaus-type"><h1>Ideas<br>take<br><em>shape.</em></h1>${input(arrow, "Your experiment")}</div><div class="bauhaus-objects"><div class="bauhaus-circle"></div><div class="bauhaus-square"></div><div class="bauhaus-arch"></div><div class="bauhaus-dot"></div></div></section><footer class="scene-foot"><span>FORM → METHOD</span><span>06 / 25</span></footer>`,
  "07": () =>
    `<div class="terminal-frame"><header><span>biomars@studio:~</span><i></i></header><div class="terminal-main"><pre class="terminal-logo">BIO<span>_</span>MARS</pre><p class="terminal-sub">Protocol Studio</p><div class="terminal-prompt"><span>~/new_experiment</span>${input("↵", "Type your question_")}</div><span class="terminal-cursor">▊</span></div><footer><span>LOCAL INPUT</span><span>ENTER ↵</span></footer></div><div class="scanlines"></div>`,
  "08": () =>
    `${nav("BIOMARS", "PROTOCOL / STUDIO")}<div class="blueprint-grid"></div><section class="blueprint-layout"><div class="blueprint-heading"><span>FIG. 08</span><h1>Design<br>the experiment.</h1>${input("+", "Define your objective")}</div><div class="blueprint-drawing">${circleDiagram}<span class="dimension top">Ø 400</span><span class="dimension side">SPECIMEN / CONCEPT</span></div></section><div class="crosshair"></div><footer class="scene-foot"><span>BIOLOGICAL SYSTEMS</span><output id="coordinates">X 000 / Y 000</output></footer>`,
  "09": () =>
    `<div class="deco-frame"><div class="deco-corner a"></div><div class="deco-corner b"></div>${nav("B I O M A R S", "")}<section class="deco-layout"><div class="deco-emblem">✦</div><p>THE PROTOCOL STUDIO</p><h1>Where inquiry<br>becomes <i>craft.</i></h1>${input("ENTER", "Your next experiment")}<div class="deco-fan"><i></i><i></i><i></i><i></i><i></i></div></section></div>`,
  10: () =>
    `<div class="glass-backdrop"><div class="glass-ball one"></div><div class="glass-ball two"></div>${img("pearl")}</div>${nav("BioMars", "Protocol Studio")}<section class="glass-panel"><span class="glass-icon">◈</span><h1>A clear<br>starting point.</h1>${input(arrow, "Your experiment")}<div class="glass-hint"><span>01</span><span>NEW PROTOCOL</span></div></section>`,
  11: () =>
    `${nav("BIOMARS", "PRINT / 11")}<section class="riso-layout"><div class="riso-art"><span class="riso-circle"></span><span class="riso-burst">✳</span><span class="riso-caption">PROTOCOL STUDIO</span></div><div class="riso-text"><h1>GOOD<br>QUESTIONS.<br><span>GREAT<br>SCIENCE.</span></h1>${input(arrow, "Put it on paper")}</div></section><div class="grain"></div>`,
  12: () =>
    `${nav("BioMars", "Est. in curiosity")}<section class="midcentury-layout"><div class="midcentury-art"><div class="mid-sun"></div><div class="mid-stem"></div><div class="mid-leaf first"></div><div class="mid-leaf second"></div><div class="mid-base"></div></div><div class="midcentury-text"><span>PROTOCOL STUDIO</span><h1>Make room<br>for <i>discovery.</i></h1>${input(arrow, "Your experiment")}</div></section>`,
  13: () =>
    `<div class="retro-desktop"><div class="desktop-icon"><span>▧</span><p>BioMars</p></div><div class="desktop-icon second"><span>▤</span><p>Protocols</p></div><section class="retro-window" data-draggable><div class="window-title" data-handle><span>BioMars Protocol Studio</span><span class="window-buttons">_ □ ×</span></div><div class="retro-menu">File &nbsp; Edit &nbsp; Experiment</div><div class="retro-content"><span class="retro-molecule">⚛</span><h1>What’s next?</h1>${input("Start ▶", "New experiment...")}</div><div class="retro-status">Ready.</div></section><footer class="retro-taskbar"><strong>✳ Start</strong><span>BioMars Protocol Studio</span><time>1998</time></footer></div>`,
  14: () =>
    `${nav("B I O M A R S", "MISSION / PROTOCOL")}<section class="orbital-layout"><div class="orbital-text"><span class="orange-dot"></span><p>EXPLORATION BEGINS HERE</p><h1>Beyond<br>the known.</h1>${input("LAUNCH ↗", "Mission objective")}</div><div class="orbital-map"><span class="orbit o1"></span><span class="orbit o2"></span><span class="orbit o3"></span><span class="planet"></span><span class="satellite"></span></div></section><footer class="scene-foot"><span>PROTOCOL STUDIO</span><span>∞ / POSSIBILITIES</span></footer>`,
  15: () =>
    `<div class="industrial-shell"><div class="rivet tl"></div><div class="rivet tr"></div><div class="rivet bl"></div><div class="rivet br"></div><header><span class="industrial-logo">BIO<br>MARS</span><div>PROTOCOL STUDIO<br><span>FIELD EQUIPMENT / 15</span></div></header><section class="industrial-layout"><div class="industrial-screen"><span>NEW EXPERIMENT</span><h1>READY<br>WHEN<br>YOU ARE.</h1>${input("→", "Enter objective")}</div><div class="industrial-control"><div class="dial"><i></i></div><label for="focus">FOCUS</label><input id="focus" type="range" min="0" max="100" value="45" aria-label="Visual focus dial"><div class="industrial-bars">▌▌▌ ▌▌ ▌▌▌▌ ▌</div></div></section></div>`,
  16: () =>
    `${nav("biomars.", "Protocol Studio")}<section class="soft-layout"><h1>Big<br><i>little</i><br>ideas.</h1><div class="soft-art">${img("softbody")}</div><div class="soft-form">${input("Let’s go ↗", "What are you exploring?")}</div><span class="soft-dot">✽</span></section>`,
  17: () =>
    `<div class="notebook"><div class="notebook-holes"><i></i><i></i><i></i></div><header>${wordmark()}<span>Lab notes / 17</span></header><section><span class="handwritten-small">a place to begin.</span><h1>Every discovery<br>starts with a<br><em>question.</em></h1><div class="notebook-sketch">${circleDiagram}</div>${input("↗", "What if...")}<p class="notebook-caption">Protocol Studio</p></section></div>`,
  18: () =>
    `${img("nocturne", "fullbleed")}<div class="nocturne-shade"></div>${nav("BioMars", "Protocol Studio")}<section class="nocturne-layout"><span class="small-line"></span><h1>Find<br>your <i>next.</i></h1>${input(arrow, "Your experiment")}</section><div class="spotlight"></div><footer class="scene-foot"><span>INQUIRY, IN FOCUS.</span><span>18</span></footer>`,
  19: () =>
    `<div class="herbarium-frame">${nav("BioMars", "THE PROTOCOL STUDIO")}<section class="herbarium-layout"><div class="herbarium-text"><span class="herbarium-no">PLATE XIX</span><h1>Cultivate<br><i>the unknown.</i></h1>${input("Begin →", "A seed of an idea")}</div><figure>${img("botanical")}<span class="specimen-label">B · M<br>HERBARIUM</span></figure></section><footer class="scene-foot"><span>OBSERVE. QUESTION. DISCOVER.</span><span>✳</span></footer></div>`,
  20: () =>
    `${nav("BIOMARS", "PROTOCOL STUDIO")}<section class="type-layout"><div class="type-ribbon"><span>WHAT IF?&nbsp;</span><span aria-hidden="true">WHAT IF?&nbsp;</span></div><div class="type-ribbon outline"><span>WHY NOT?&nbsp;</span><span aria-hidden="true">WHY NOT?&nbsp;</span></div><div class="type-input">${input("→", "ASK SOMETHING.")}</div><div class="type-circle">${arrow}</div></section>`,
  21: () =>
    `<section class="stijl-grid"><header>${wordmark("BIO<br>MARS")}</header><div class="stijl-blue"></div><div class="stijl-yellow"><span>21</span></div><div class="stijl-title"><span>PROTOCOL STUDIO</span><h1>Think.<br>Make.<br>Discover.</h1></div><div class="stijl-red">${arrow}</div><div class="stijl-input">${input("→", "Your experiment")}</div><div class="stijl-white"></div></section>`,
  22: () =>
    `${nav("B I O M A R S", "PROTOCOL STUDIO")}<section class="atelier-layout"><div class="atelier-word">P<span>r</span>otocol</div><figure>${img("pearl")}</figure><div class="atelier-caption">THE SCIENCE OF POSSIBILITY.</div><div class="atelier-form">${input("↗", "Begin a new study")}</div><span class="atelier-index">COLLECTION / 22</span></section>`,
  23: () =>
    `<div class="memphis-pattern"></div>${nav("BIOMARS", "PROTOCOL STUDIO")}<section class="memphis-layout"><div class="memphis-zigzag"></div><div class="memphis-triangle"></div><span class="memphis-sticker" data-draggable>✳</span><div class="memphis-card"><h1>Stay<br><span>curious.</span></h1>${input("GO ↗", "Your next experiment")}</div><div class="memphis-dots">•••<br>•••<br>•••</div></section>`,
  24: () =>
    `<canvas id="network" aria-hidden="true"></canvas>${nav("BioMars", "Protocol Studio")}<section class="network-layout"><span class="network-cross">+</span><h1>Connect<br>the <i>possibilities.</i></h1>${input(arrow, "Your experiment")}<p>FROM QUESTION TO CONNECTION.</p></section><footer class="scene-foot"><span>◉ LIVING SYSTEMS</span><span>24</span></footer>`,
  25: () =>
    `<div class="chrome-surface"></div>${nav("BIOMARS", "PROTOCOL STUDIO")}<section class="chrome-layout"><h1>SCIENCE<br>TAKES<br><span>FORM.</span></h1><div class="chrome-object">${img("pearl")}</div><div class="chrome-input">${input("↗", "Your experiment")}</div><span class="chrome-label">OBJECT / 025</span></section>`,
};
root.innerHTML = views[direction.id]();
root.querySelector(".experiment")?.addEventListener("submit", (event) => {
  event.preventDefault();
  const query = new FormData(event.currentTarget).get("query").trim();
  if (!query) return;
  try {
    sessionStorage.setItem("biomars-design-draft", query);
  } catch {}
  if (parent !== window)
    parent.postMessage({ type: "biomars:open-studio", query }, location.origin);
  else location.href = "../";
});
if (thumb) {
  root
    .querySelectorAll("input,button,a")
    .forEach((el) => el.setAttribute("tabindex", "-1"));
}
const move = (event) => {
  if (thumb || reduced) return;
  const x = event.clientX / innerWidth - 0.5,
    y = event.clientY / innerHeight - 0.5;
  root.style.setProperty("--mx", x);
  root.style.setProperty("--my", y);
  root.style.setProperty("--px", `${event.clientX}px`);
  root.style.setProperty("--py", `${event.clientY}px`);
  const coords = document.querySelector("#coordinates");
  if (coords)
    coords.textContent = `X ${Math.round(event.clientX).toString().padStart(3, "0")} / Y ${Math.round(event.clientY).toString().padStart(3, "0")}`;
};
document.addEventListener("pointermove", move, { passive: true });
document
  .querySelector("#focus")
  ?.addEventListener("input", (event) =>
    root.style.setProperty(
      "--dial",
      `${Number(event.target.value) * 2.7 - 135}deg`,
    ),
  );
for (const element of root.querySelectorAll("[data-draggable]")) {
  if (thumb) continue;
  const handle = element.querySelector("[data-handle]") || element;
  let dragging = false,
    originX = 0,
    originY = 0,
    startX = 0,
    startY = 0;
  handle.addEventListener("pointerdown", (event) => {
    if (event.target.closest("input,button")) return;
    dragging = true;
    handle.setPointerCapture(event.pointerId);
    startX = event.clientX - originX;
    startY = event.clientY - originY;
  });
  handle.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    originX = Math.max(
      -innerWidth * 0.2,
      Math.min(innerWidth * 0.2, event.clientX - startX),
    );
    originY = Math.max(
      -innerHeight * 0.2,
      Math.min(innerHeight * 0.2, event.clientY - startY),
    );
    element.style.translate = `${originX}px ${originY}px`;
  });
  handle.addEventListener("pointerup", () => (dragging = false));
  handle.addEventListener("pointercancel", () => (dragging = false));
}
if (direction.id === "24") {
  const canvas = document.querySelector("#network"),
    ctx = canvas.getContext("2d");
  let w = 0,
    h = 0,
    tick = 0,
    mouse = { x: -1000, y: -1000 },
    frame;
  const points = Array.from({ length: thumb ? 45 : 75 }, (_, i) => ({
    x: ((i * 193) % 997) / 997,
    y: ((i * 157) % 863) / 863,
    phase: i * 0.7,
  }));
  const resize = () => {
    w = innerWidth;
    h = innerHeight;
    canvas.width = w * devicePixelRatio;
    canvas.height = h * devicePixelRatio;
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;
    ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
  };
  const draw = () => {
    ctx.clearRect(0, 0, w, h);
    tick += 0.003;
    const ps = points.map((p) => {
      let x = p.x * w + Math.sin(tick + p.phase) * 12,
        y = p.y * h + Math.cos(tick + p.phase) * 12;
      const dx = x - mouse.x,
        dy = y - mouse.y,
        d = Math.hypot(dx, dy);
      if (d < 130 && d > 0) {
        x += (dx / d) * (130 - d) * 0.35;
        y += (dy / d) * (130 - d) * 0.35;
      }
      return { x, y };
    });
    for (let i = 0; i < ps.length; i++) {
      for (let j = i + 1; j < ps.length; j++) {
        const d = Math.hypot(ps[i].x - ps[j].x, ps[i].y - ps[j].y);
        if (d < 150) {
          ctx.strokeStyle = `rgba(104,217,177,${(1 - d / 150) * 0.18})`;
          ctx.beginPath();
          ctx.moveTo(ps[i].x, ps[i].y);
          ctx.lineTo(ps[j].x, ps[j].y);
          ctx.stroke();
        }
      }
      ctx.fillStyle = "#76d9b5";
      ctx.beginPath();
      ctx.arc(ps[i].x, ps[i].y, 1.5, 0, Math.PI * 2);
      ctx.fill();
    }
    if (!thumb && !reduced && !document.hidden)
      frame = requestAnimationFrame(draw);
  };
  resize();
  draw();
  addEventListener("resize", () => {
    resize();
    if (thumb || reduced) draw();
  });
  addEventListener(
    "pointermove",
    (e) => {
      mouse = { x: e.clientX, y: e.clientY };
    },
    { passive: true },
  );
  document.addEventListener("visibilitychange", () => {
    cancelAnimationFrame(frame);
    if (!document.hidden) draw();
  });
}
addEventListener("keydown", (event) => {
  if (event.target.matches("input,textarea")) return;
  if (
    ["ArrowLeft", "ArrowRight", "Escape"].includes(event.key) &&
    parent !== window
  ) {
    event.preventDefault();
    parent.postMessage(
      { type: "biomars:key", key: event.key },
      location.origin,
    );
  }
});
