import { directions } from "./styles.js";
const root = document.querySelector("#gallery");
const valid = (id) => directions.some((d) => d.id === id);
let saved = new Set();
try {
  const values = JSON.parse(localStorage.getItem("biomars-directions") || "[]");
  if (Array.isArray(values)) saved = new Set(values.filter(valid));
} catch {}
let category = "All",
  current = "01",
  pair = ["02", "19"],
  noticeTimer;
const palette = (d) =>
  `<span class="card-palette" aria-label="配色 ${d.palette.join(", ")}">${d.palette.map((c) => `<i style="background:${c}"></i>`).join("")}</span>`;
const saveButton = (id) =>
  `<button class="save-button" data-save="${id}" aria-label="${saved.has(id) ? "取消收藏" : "收藏"} ${directions.find((d) => d.id === id).name}" aria-pressed="${saved.has(id)}">${saved.has(id) ? "♥" : "♡"}</button>`;
const options = (id) =>
  directions
    .map(
      (d) =>
        `<option value="${d.id}" ${d.id === id ? "selected" : ""}>${d.id} / ${d.name}</option>`,
    )
    .join("");
const footer = () =>
  `<footer class="gallery-footer"><span>BioMars Protocol Studio</span><span>25 perspectives. One beginning.</span><a href="https://github.com/lululualululu/biomars-protocol-studio" target="_blank" rel="noreferrer">Source ↗</a></footer>`;
const scaleObserver = new ResizeObserver((entries) =>
  entries.forEach(({ target, contentRect }) => {
    const frame = target.querySelector("iframe");
    if (frame) frame.style.transform = `scale(${contentRect.width / 1280})`;
  }),
);
function scaleFrames() {
  root
    .querySelectorAll("[data-scaled]")
    .forEach((el) => scaleObserver.observe(el));
}
function announce(message) {
  const el = document.querySelector("#notice");
  el.textContent = message;
  el.classList.add("visible");
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => el.classList.remove("visible"), 1800);
}
function updateSaved() {
  document.querySelector("#saved-count").textContent = saved.size;
  root.querySelectorAll("[data-save]").forEach((button) => {
    const active = saved.has(button.dataset.save);
    button.textContent = active ? "♥" : "♡";
    button.setAttribute("aria-pressed", String(active));
    button.setAttribute(
      "aria-label",
      `${active ? "取消收藏" : "收藏"} ${directions.find((d) => d.id === button.dataset.save).name}`,
    );
  });
}
function toggleSaved(id) {
  saved.has(id) ? saved.delete(id) : saved.add(id);
  try {
    localStorage.setItem("biomars-directions", JSON.stringify([...saved]));
  } catch {
    announce("已收藏于本次浏览");
  }
  updateSaved();
  if (location.hash === "#saved") renderOverview(true);
}
function renderOverview(onlySaved = false) {
  scaleObserver.disconnect();
  document.body.classList.remove("immersive");
  const items = directions.filter(
    (d) =>
      (!onlySaved || saved.has(d.id)) &&
      (category === "All" || d.family === category),
  );
  root.innerHTML = `<section class="overview"><div class="gallery-intro"><h1>${onlySaved ? "Your <em>selection.</em>" : "One studio.<br>25 <em>perspectives.</em>"}</h1><p>${onlySaved ? "留住喜欢的方向。" : "选一种感觉。"}</p></div><div class="filter-bar" role="group" aria-label="风格分类">${[
    ["All", "全部"],
    ["Minimal", "极简"],
    ["Editorial", "编排"],
    ["Graphic", "图形"],
    ["Digital", "数字"],
    ["Material", "材质"],
  ]
    .map(
      ([key, label]) =>
        `<button data-filter="${key}" aria-pressed="${category === key}">${label}</button>`,
    )
    .join(
      "",
    )}<span class="filter-total">${String(items.length).padStart(2, "0")} DIRECTIONS</span></div>${items.length ? `<div class="direction-grid">${items.map((d) => `<article class="direction-card"><div class="thumbnail" data-scaled style="--card-bg:${d.palette[0]}"><iframe src="scene.html?style=${d.id}&thumb=1" title="${d.name} 缩略图" loading="eager" tabindex="-1" aria-hidden="true"></iframe><button class="open-direction" data-open="${d.id}" aria-label="探索 ${d.id} ${d.name}"></button></div><div class="card-meta"><span class="card-number">${d.id}</span><h2 class="card-name">${d.name}<span class="card-chinese">${d.zh}</span></h2>${palette(d)}${saveButton(d.id)}</div></article>`).join("")}</div>` : `<div class="empty"><span>♡</span>${onlySaved ? "点一下喜欢的设计旁的 ♡" : "这个分类暂时没有收藏"}<br><br><a href="#all">返回总览 ↗</a></div>`}${footer()}</section>`;
  scaleFrames();
}
function renderSingle(id) {
  scaleObserver.disconnect();
  current = id;
  const d = directions.find((d) => d.id === id);
  root.innerHTML = `<section class="viewer"><div class="viewer-toolbar"><div class="viewer-heading"><a class="back" href="#all" aria-label="返回总览">↖</a><select id="direction-select" aria-label="选择设计方向">${options(id)}</select></div><div class="viewer-actions">${saveButton(id)}<button class="plain-action compare-action" data-compare="${id}">加入对比 ◫</button><button class="plain-action" data-fullscreen>全屏 ⛶</button></div></div><div class="scene-stage"><iframe src="scene.html?style=${id}" title="${d.name} 交互设计"></iframe></div><div class="viewer-footer"><div class="design-note">${palette(d)}<span class="font-note">${d.font}</span><span class="motion-note">${d.motion}</span></div><div class="page-controls"><button data-step="-1" aria-label="上一款">←</button><span>${id} / 25</span><button data-step="1" aria-label="下一款">→</button></div></div></section><button class="exit-immersive" data-fullscreen>退出全屏 ↙</button>`;
  document
    .querySelector("#direction-select")
    .addEventListener(
      "change",
      (event) => (location.hash = event.target.value),
    );
}
function comparePane(id, index) {
  const d = directions.find((d) => d.id === id);
  return `<section class="compare-pane"><div class="compare-bar"><select data-compare-select="${index}" aria-label="${index ? "右" : "左"}侧设计">${options(id)}</select>${saveButton(id)}</div><div class="compare-frame" data-scaled><iframe src="scene.html?style=${id}" title="对比 ${d.name}"></iframe></div><div class="compare-note"><span>${d.zh} · ${d.font}</span><button data-open="${id}">展开 ↗</button></div></section>`;
}
function renderCompare() {
  scaleObserver.disconnect();
  document.body.classList.remove("immersive");
  root.innerHTML = `<section class="comparison"><div class="comparison-intro"><h1>Side by side.</h1><span>同一个产品，两种表达。</span></div><div class="comparison-grid">${pair.map(comparePane).join("")}</div>${footer()}</section>`;
  scaleFrames();
  root.querySelectorAll("[data-compare-select]").forEach((select) =>
    select.addEventListener("change", (event) => {
      pair[Number(event.target.dataset.compareSelect)] = event.target.value;
      renderCompare();
    }),
  );
}
function route() {
  const hash = location.hash.slice(1) || "all";
  document.body.classList.remove("immersive");
  document.querySelectorAll("[data-nav]").forEach((a) => {
    if (a.dataset.nav === hash) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  if (hash === "compare") renderCompare();
  else if (valid(hash)) renderSingle(hash);
  else renderOverview(hash === "saved");
  updateSaved();
  scrollTo(0, 0);
}
function step(n) {
  const index = directions.findIndex((d) => d.id === current);
  location.hash = directions[(index + n + 25) % 25].id;
}
function handleKey(key) {
  if (key === "Escape") {
    if (document.body.classList.contains("immersive"))
      document.body.classList.remove("immersive");
    else location.hash = "all";
  } else if (valid(location.hash.slice(1))) step(key === "ArrowLeft" ? -1 : 1);
}
root.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.dataset.open) location.hash = button.dataset.open;
  else if (button.dataset.save) toggleSaved(button.dataset.save);
  else if (button.dataset.filter) {
    category = button.dataset.filter;
    renderOverview(location.hash === "#saved");
  } else if (button.dataset.step) step(Number(button.dataset.step));
  else if (button.hasAttribute("data-fullscreen"))
    document.body.classList.toggle("immersive");
  else if (button.dataset.compare) {
    pair = [
      button.dataset.compare,
      pair.find((id) => id !== button.dataset.compare) || "19",
    ];
    location.hash = "compare";
  }
});
addEventListener("keydown", (event) => {
  if (event.target.matches("input,textarea,select")) return;
  if (["ArrowLeft", "ArrowRight", "Escape"].includes(event.key)) {
    event.preventDefault();
    handleKey(event.key);
  }
});
addEventListener("message", (event) => {
  if (
    event.origin !== location.origin ||
    ![...root.querySelectorAll("iframe")].some(
      (frame) => frame.contentWindow === event.source,
    )
  )
    return;
  if (
    event.data?.type === "biomars:key" &&
    ["ArrowLeft", "ArrowRight", "Escape"].includes(event.data.key)
  )
    handleKey(event.data.key);
  if (
    event.data?.type === "biomars:open-studio" &&
    typeof event.data.query === "string"
  ) {
    try {
      sessionStorage.setItem(
        "biomars-design-draft",
        event.data.query.slice(0, 6000),
      );
    } catch {}
    location.href = "../";
  }
});
addEventListener("hashchange", route);
route();
