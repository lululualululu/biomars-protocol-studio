import { escapeHTML as e, safeURL, markdown } from "./markdown.js";
import {
  loadPreferences,
  savePreferences,
  normalizePreferences,
} from "./preferences.js";
import { homeMarkup, settingsMarkup, libraryMarkup } from "./views.js?v=5";
import { readerMarkup, progressMarkup, evidenceMarkup } from "./protocol-view.js?v=5";
let outlineObserver, focusMode = false;
import { loadExamples, examplesMarkup, exampleRows, filterExamples } from "./examples.js?v=5";
let exampleCatalog = null;
const $ = (s) => document.querySelector(s);
const icon = (name, size = 16) => {
  const paths = {
    focus: "M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5",
    copy: "M8 8h12v13H8zM16 8V3H3v13h5",
    print: "M6 8V3h12v5M6 17H3V8h18v9h-3M6 14h12v7H6z",
    code: "m8 5-7 7 7 7m8-14 7 7-7 7M14 3l-4 18",
    chevron: "m7 10 5 5 5-5",
    spark: "M12 3c0 6-3 9-9 9 6 0 9 3 9 9 0-6 3-9 9-9-6 0-9-3-9-9Z",
    search: "M21 21l-5-5M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14",
    plus: "M12 5v14M5 12h14",
    arrow: "M5 12h14m-5-5 5 5-5 5",
    paper: "m8 12 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l8-8",
    sliders: "M4 7h5m4 0h7M4 17h9m4 0h3M9 4v6m4 4v6",
    lock: "M6 11h12v10H6zM8 11V7a4 4 0 0 1 8 0v4",
    file: "M14 2H5v20h14V7zM14 2v5h5M8 12h8M8 16h6",
    back: "M19 12H5m5-5-5 5 5 5",
    download: "M12 3v12m-5-5 5 5 5-5M5 17v4h14v-4",
    book: "M12 5v16M3 4c4-1 6 0 9 2 3-2 5-3 9-2v15c-4-1-6 0-9 2-3-2-5-3-9-2z",
    check: "m5 12 4 4L19 6",
    trash: "M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7",
  };
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.file}"/></svg>`;
};
let prefs = loadPreferences();
let lang = "zh";
try {
  lang = localStorage.getItem("biomars-language") === "en" ? "en" : "zh";
} catch {}
let draftContextDirty = false,
  mobilePanel = "draft";
const t = (en, zh) => (lang === "zh" ? zh : en);
let accessCodeRequired = true;
let models = [{id: "qwen/qwen3.6-flash", name: "Qwen3.6 Flash"}];
let draftModel = prefs.model;
let apiAvailable = false,
  generationAvailable = false,
  currentJob = null,
  pollTimer,
  files = [],
  draftQuery = "",
  draftContext = {
    cell_line: prefs.cell_line,
    species: prefs.species,
    scope: prefs.scope,
  },
  activeTab = "sources",
  raw = false;
let token = "";
try {
  const saved = JSON.parse(sessionStorage.getItem("biomars-draft") || "null");
  if (saved && typeof saved.query === "string") {
    draftQuery = saved.query.slice(0, 6000);
    const clean = normalizePreferences(saved.context);
    draftContext = {
      cell_line: clean.cell_line,
      species: clean.species,
      scope: clean.scope,
    };
    draftContextDirty = Boolean(saved.dirty);
    draftModel = saved.model || draftModel;
  }
  draftQuery = sessionStorage.getItem("biomars-design-draft") || draftQuery;
  sessionStorage.removeItem("biomars-design-draft");
  token = sessionStorage.getItem("biomars-access") || "";
} catch {}
function saveDraft() {
  try {
    sessionStorage.setItem(
      "biomars-draft",
      JSON.stringify({
        query: draftQuery,
        context: draftContext,
        dirty: draftContextDirty,
        model: draftModel,
      }),
    );
  } catch {}
}
function applyPreferences() {
  document.documentElement.dataset.reading = prefs.reading;
  document.documentElement.dataset.font = prefs.font;
  document.documentElement.dataset.paper = prefs.paper;
  document.documentElement.dataset.motion = prefs.reduceMotion
    ? "reduced"
    : "system";
}
function persistPreferences() {
  applyPreferences();
  const saved = savePreferences(prefs);
  if (!saved)
    toast(
      t(
        "Browser storage is unavailable. Changes apply for this visit.",
        "浏览器未允许保存，设置仅本次生效。",
      ),
    );
  return saved;
}
function changeLanguage(value) {
  lang = value === "en" ? "en" : "zh";
  try {
    localStorage.setItem("biomars-language", lang);
  } catch {}
  updateChrome();
  route();
}
const stateLabel = (state) =>
  ({
    queued: t("In queue", "排队中"),
    running: t("In progress", "生成中"),
    review_required: t("Draft ready", "草稿已就绪"),
    awaiting_papers: t("Needs full text", "待补充全文"),
    failed: t("Stopped", "未完成"),
    interrupted: t("Interrupted", "已中断"),
    cancelled: t("Cancelled", "已取消"),
    example: t("Example", "案例"),
  })[state] || state;
function toast(message) {
  $("#toast").textContent = message;
  $("#toast").classList.add("show");
  setTimeout(() => $("#toast").classList.remove("show"), 3500);
}
function dialog(content) {
  $("#dialog-content").innerHTML = content;
  if (!$("#dialog").open) $("#dialog").showModal();
}
$(".dialog-close").onclick = () => $("#dialog").close();
$("#dialog").onclick = (event) => {
  if (event.target === $("#dialog")) {
    const r = $("#dialog").getBoundingClientRect();
    if (
      event.clientX < r.left ||
      event.clientX > r.right ||
      event.clientY < r.top ||
      event.clientY > r.bottom
    )
      $("#dialog").close();
  }
};
function settings() {
  if (location.hash === "#settings/service") settingsPage("service");
  else location.hash = "settings/service";
}
function how() {
  dialog(
    `<h2>${t("How it works", "使用说明")}</h2><ol class="help-steps"><li>${t("Describe your experiment.", "写下实验需求")}</li><li>${t("Add a paper if full text is needed.", "按提示补充论文原文")}</li><li>${t("Review the draft and export.", "核对草稿，导出使用")}</li></ol><details class="help-details"><summary>${t("About your data", "数据与使用说明")}</summary><p>${t("Requests and reference text are sent to the configured model and retrieval services. Drafts require human review.", "生成时，需求与参考文本会发送至配置的模型和检索服务。生成草稿需人工审阅。")}</p></details><button id="how-example" class="primary">${t("Browse examples", "浏览案例")} ${icon("arrow")}</button>`,
  );
  $("#how-example").onclick = () => {
    $("#dialog").close();
    location.hash = "examples";
  };
}
$("#how-button").onclick = how;
$("#language").onclick = () => changeLanguage(lang === "en" ? "zh" : "en");
document.addEventListener("pointerdown", event => {
  document.querySelectorAll(".toolbar-menu[open]").forEach(menu => { if (!menu.contains(event.target)) menu.open = false; });
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") document.querySelectorAll(".toolbar-menu[open]").forEach(menu => { menu.open = false; menu.querySelector("summary").focus(); });
});
function updateChrome() {
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  $("#language").textContent = lang === "en" ? "中文" : "EN";
  $("#language").setAttribute(
    "aria-label",
    t("切换为中文", "Switch to English"),
  );
  $("#how-button").textContent = t("Help", "帮助");
  $("#nav-examples").textContent = t("Examples", "案例");
  $("#nav-new").textContent = t("Create", "新建");
  $("#nav-library").textContent = t("My protocols", "我的 Protocol");
  $("#nav-settings").setAttribute("aria-label", t("Settings", "设置"));
  const route = location.hash.slice(1);
  for (const id of ["nav-new", "nav-examples", "nav-library", "nav-settings"]) {
    const active =
      id === "nav-new"
        ? !route
        : id === "nav-library"
          ? route === "library"
          : id === "nav-examples" ? route.startsWith("example") : route.startsWith("settings/");
    if (active) $("#" + id).setAttribute("aria-current", "page");
    else $("#" + id).removeAttribute("aria-current");
  }
  applyPreferences();
}
async function api(path, options = {}) {
  const response = await fetch(`./api${path}`, {
    ...options,
    credentials: "same-origin",
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    let message = t(
      "The request could not be completed. Please try again.",
      "请求未完成，请稍后重试。",
    );
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
    } catch {}
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}
// A small light follows the pointer; the work surface itself stays still.
function bindGlassLight(surface) {
  if (!matchMedia("(hover: hover) and (pointer: fine)").matches ||
      matchMedia("(prefers-reduced-motion: reduce)").matches || prefs.reduceMotion) return;
  let frame = 0;
  let x = 50, y = 0;
  surface.addEventListener("pointermove", event => {
    const bounds = surface.getBoundingClientRect();
    x = (event.clientX - bounds.left) / bounds.width * 100;
    y = (event.clientY - bounds.top) / bounds.height * 100;
    if (!frame) frame = requestAnimationFrame(() => {
      frame = 0;
      surface.style.setProperty("--glass-x", `${x}%`);
      surface.style.setProperty("--glass-y", `${y}%`);
    });
  }, { passive: true });
  surface.addEventListener("pointerleave", () => {
    cancelAnimationFrame(frame);
    frame = 0;
    surface.style.removeProperty("--glass-x");
    surface.style.removeProperty("--glass-y");
  });
}
function home() {
  currentJob = null;
  raw = false;
  $("#main").innerHTML = homeMarkup({
    t,
    e,
    icon,
    query: draftQuery,
    context: draftContext,
    generationAvailable,
    models,
    selectedModel: draftModel,
    featured: exampleCatalog ? exampleCatalog.featured.map(id => exampleCatalog.cases.find(c => c.id === id)) : [],
  });
  bindGlassLight($("#generate-form"));
  $("#model").onchange = (event) => { draftModel = event.target.value; saveDraft(); };
  $("#query").oninput = (event) => {
    draftQuery = event.target.value;
    saveDraft();
  };
  $("#query").onkeydown = (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      $("#generate-form").requestSubmit();
    }
  };
  $("#context-toggle").onclick = () => {
    const isHidden = $("#context").classList.toggle("hidden");
    $("#context-toggle").setAttribute("aria-expanded", String(!isHidden));
  };
  $("#use-defaults").onclick = () => {
    draftContext = {
      cell_line: prefs.cell_line,
      species: prefs.species,
      scope: prefs.scope,
    };
    draftContextDirty = false;
    saveDraft();
    ["cell-line", "species", "scope"].forEach((id) => {
      $("#" + id).value = draftContext[id.replace("-", "_")] || "";
    });
    toast(t("Default context applied.", "已使用默认实验背景。"));
  };
  ["cell-line", "species", "scope"].forEach((id) => {
    $(`#${id}`).oninput = (event) => {
      draftContext[id.replace("-", "_")] = event.target.value;
      draftContextDirty = true;
      saveDraft();
    };
  });
  $("#attach").onclick = () => $("#file-input").click();
  $("#file-input").onchange = (event) => addFiles([...event.target.files]);
  $("#generate-form").ondragover = (event) => event.preventDefault();
  $("#generate-form").ondrop = (event) => {
    event.preventDefault();
    addFiles([...event.dataTransfer.files]);
  };
  $("#generate-form").onsubmit = submit;
  $("#open-example").onclick = () => (location.hash = "examples");
  document.querySelectorAll("[data-example]").forEach(button => {
    button.onclick = () => { location.hash = `example/${button.dataset.example}`; };
  });
  renderFiles();
}
function addFiles(selected) {
  $("#form-error").textContent = "";
  for (const file of selected) {
    if (files.length >= 5) {
      $("#form-error").textContent = t(
        "Add up to 5 references.",
        "最多添加 5 份资料。",
      );
      break;
    }
    if (
      !/\.(pdf|txt|md)$/i.test(file.name) ||
      file.size > 10 * 1024 * 1024 ||
      file.size === 0
    ) {
      $("#form-error").textContent = t(
        "Use a non-empty PDF, Markdown, or text file, up to 10 MB.",
        "请使用非空的 PDF、Markdown 或文本文件，每份不超过 10 MB。",
      );
      continue;
    }
    files.push(file);
  }
  renderFiles();
  $("#file-input").value = "";
}
function renderFiles() {
  $("#file-list").innerHTML = files
    .map(
      (f, i) =>
        `<span class="file-chip">${icon("file", 12)}<span>${e(f.name)}</span><button type="button" data-remove="${i}" aria-label="${t("Remove", "移除")} ${e(f.name)}">×</button></span>`,
    )
    .join("");
  document.querySelectorAll("[data-remove]").forEach(
    (button) =>
      (button.onclick = () => {
        files.splice(Number(button.dataset.remove), 1);
        renderFiles();
      }),
  );
}
async function submit(event) {
  event.preventDefault();
  if (!generationAvailable) {
    settings();
    return;
  }
  if (accessCodeRequired && !token) {
    settings();
    return;
  }
  const button = $("#generate");
  button.disabled = true;
  $("#form-error").textContent = "";
  try {
    const body = new FormData();
    body.append(
      "payload",
      JSON.stringify({
        query: $("#query").value.trim(),
        model: draftModel,
        cell_line: $("#cell-line").value.trim() || null,
        species: $("#species").value.trim() || null,
        scope: $("#scope").value.trim() || "full_protocol",
      }),
    );
    files.forEach((file) => body.append("files", file));
    const job = await api("/jobs", { method: "POST", body });
    files = [];
    draftQuery = "";
    draftContextDirty = false;
    draftContext = {
      cell_line: prefs.cell_line,
      species: prefs.species,
      scope: prefs.scope,
    };
    saveDraft();
    location.hash = `job/${job.id}`;
  } catch (error) {
    $("#form-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}
async function library() {
  currentJob = null;
  $("#main").innerHTML = libraryMarkup({ t, icon });
  let jobs = [];
  const draw = () => {
    if (location.hash !== "#library") return;
    const query = $("#library-search").value.toLowerCase();
    const shown = jobs.filter((job) => job.title.toLowerCase().includes(query));
    $("#library-count").textContent =
      `${shown.length} ${t("protocols", "份 Protocol")}`;
    $("#history-list").innerHTML = shown.length
      ? shown
          .map(
            (job) =>
              `<div class="history-item"><span class="history-file">${icon("file", 18)}</span><button class="history-open" data-job="${job.id}">${e(job.title)}</button><span class="pill">${e(stateLabel(job.state))}</span><time>${new Date(job.updated * 1000).toLocaleDateString(lang === "zh" ? "zh-CN" : "en")}</time><button class="history-delete" data-delete="${job.id}" aria-label="${t("Delete task", "删除任务")}">${icon("trash", 14)}</button></div>`,
          )
          .join("")
      : `<div class="library-empty"><span class="empty-orbit">${icon("file", 26)}</span><h2>${query ? t("No matching protocols", "没有找到") : t("No protocols yet.", "还没有记录")}</h2><p>${query ? t("Try a different search.", "试试其他关键词。") : apiAvailable ? t("Your saved tasks will appear here.", "从一项实验开始。") : t("Connect a service to keep your protocol tasks.", "连接服务后可创建任务。")}</p><a class="${query ? "text-button" : "primary"}" href="#">${t("New experiment", "新建实验")} ${icon("arrow", 14)}</a>${!query ? `<a class="empty-example" href="#examples">${t("View an example", "先查看示例")} ↗</a>` : ""}</div>`;
    document
      .querySelectorAll("[data-job]")
      .forEach(
        (b) => (b.onclick = () => (location.hash = `job/${b.dataset.job}`)),
      );
    document
      .querySelectorAll("[data-delete]")
      .forEach((b) => (b.onclick = () => confirmDelete(b.dataset.delete)));
  };
  if (apiAvailable) {
    try {
      jobs = await api("/jobs");
    } catch {
      if (location.hash !== "#library") return;
      $("#history-list").innerHTML =
        `<div class="library-empty"><h2>${t("Could not load protocols", "暂时无法载入记录")}</h2><button id="retry-history" class="secondary">${t("Try again", "重试")}</button></div>`;
      $("#retry-history").onclick = library;
      return;
    }
  }
  if (location.hash !== "#library") return;
  $("#library-search").oninput = draw;
  draw();
}
async function checkService() {
  apiAvailable = false;
  generationAvailable = false;
  try {
    const response = await fetch("./site-config.json", {
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    const site = await response.json();
    if (site.api) {
      const config = await api("/config", {
        signal: AbortSignal.timeout(5000),
      });
      apiAvailable = true;
      generationAvailable = Boolean(config.generation_available);
      accessCodeRequired = Boolean(config.access_code_required);
      models = config.models || models;
      if (!models.some(m => m.id === draftModel)) draftModel = config.default_model;
      if (!models.some(m => m.id === prefs.model)) prefs.model = config.default_model;
    }
  } catch {}
}
function settingsPage(section = "general") {
  currentJob = null;
  $("#main").innerHTML = settingsMarkup({
    t,
    e,
    icon,
    section,
    prefs,
    lang,
    apiAvailable,
    generationAvailable,
    token,
    accessCodeRequired,
    models,
  });
  if (section === "general") {
    $("#pref-language").onchange = (event) =>
      changeLanguage(event.target.value);
    bindReadingControls();
    $("#pref-motion").onchange = (event) => {
      prefs.reduceMotion = event.target.checked;
      persistPreferences();
    };
  }
  if (section === "experiment") {
    $("#defaults-form").onsubmit = (event) => {
      event.preventDefault();
      prefs = {
        ...prefs,
        model: $("#default-model").value,
        cell_line: $("#default-system").value.trim(),
        species: $("#default-species").value.trim(),
        scope: $("#default-scope").value.trim(),
      };
      draftModel = prefs.model;
      saveDraft();
      const saved = persistPreferences();
      if (!draftContextDirty) {
        draftContext = {
          cell_line: prefs.cell_line,
          species: prefs.species,
          scope: prefs.scope,
        };
        saveDraft();
      }
      $("#defaults-status").textContent = saved
        ? t("Saved", "已保存")
        : t("Applied for this visit", "本次访问已应用");
    };
    $("#defaults-form").oninput = () =>
      ($("#defaults-status").textContent = "");
    $("#reset-defaults").onclick = () => {
      prefs = { ...prefs, cell_line: "", species: "", scope: "" };
      persistPreferences();
      if (!draftContextDirty) {
        draftContext = {};
        saveDraft();
      }
      settingsPage("experiment");
      toast(t("Experiment defaults cleared.", "已清空实验默认值。"));
    };
  }
  if (section === "service") {
    $("#recheck-service").onclick = async () => {
      const pending = $("#access-code")?.value;
      const button = $("#recheck-service");
      button.disabled = true;
      button.textContent = t("Checking…", "检查中…");
      await checkService();
      if (location.hash !== "#settings/service") return;
      settingsPage("service");
      if ($("#access-code") && pending !== undefined)
        $("#access-code").value = pending;
      $("#connection-feedback").textContent = apiAvailable
        ? t("Connection checked.", "已检查当前连接。")
        : t(
            "No generation service is available in this preview.",
            "当前预览没有可用的生成服务。",
          );
    };
    if ($("#access-form")) {
      $("#access-form").onsubmit = (event) => {
        event.preventDefault();
        token = $("#access-code").value.trim();
        try {
          sessionStorage.setItem("biomars-access", token);
          toast(
            t(
              "Saved for this tab. Verified on submission.",
              "已保存至当前标签页，提交时验证。",
            ),
          );
        } catch {
          toast(t("Saved for this visit only.", "仅本次访问生效。"));
        }
      };
      $("#clear-access").onclick = () => {
        token = "";
        try {
          sessionStorage.removeItem("biomars-access");
        } catch {}
        $("#access-code").value = "";
        toast(t("Access code cleared.", "访问码已清除。"));
      };
      $("#reveal-code").onclick = () => {
        const reveal = $("#access-code").type === "password";
        $("#access-code").type = reveal ? "text" : "password";
        $("#reveal-code").textContent = reveal
          ? t("Hide", "隐藏")
          : t("Show", "显示");
        $("#reveal-code").setAttribute("aria-pressed", String(reveal));
      };
    }
    $("#settings-example").onclick = () => (location.hash = "examples");
  }
}
function confirmDelete(id) {
  dialog(
    `<h2>${t("Delete this task?", "删除这个任务？")}</h2><p>${t("This removes its uploaded files and generated results from this service. Running tasks must be cancelled first.", "这会从本服务删除该任务的上传文件和生成结果。正在运行的任务需要先取消。")}</p><button class="primary" id="confirm-delete">${t("Delete task and files", "删除任务和文件")}</button><p id="delete-error" role="alert"></p>`,
  );
  $("#confirm-delete").onclick = async () => {
    try {
      await api(`/jobs/${id}`, { method: "DELETE" });
      $("#dialog").close();
      route();
    } catch (error) {
      $("#delete-error").textContent = error.message;
    }
  };
}
function workspace(job) {
  currentJob = job;
  const isExample = Boolean(job.historical) || job.id === "example";
  const hasDraft = Boolean(job.markdown);
  outlineObserver?.disconnect();
  const modelName = models.find(m => m.id === job.model)?.name || job.model || "Qwen3.6 Flash";
  $("#main").innerHTML = hasDraft
    ? readerMarkup({job,t,icon,modelName,prefs,activeTab,mobilePanel,raw,focusMode})
    : progressMarkup(job,t,icon,modelName);
  if (hasDraft) bindReader(job);
  if ($("#papers")) $("#papers").onchange = event => {
    $("#paper-selection").textContent = event.target.files.length ? `${event.target.files.length} PDF` : t("Choose PDF", "选择 PDF");
  };
  if ($("#reuse-example")) $("#reuse-example").onclick = () => {
    draftQuery = job.query;
    draftContext = {cell_line: "", species: "", scope: ""};
    draftContextDirty = true;
    files = [];
    saveDraft();
    location.hash = "";
  };
  if ($("#raw-toggle"))
    $("#raw-toggle").onclick = () => {
      raw = !raw;
      workspace(job);
    };
  if ($("#download"))
    $("#download").onclick = () => {
      const blob = new Blob([job.markdown], {
        type: "text/markdown;charset=utf-8",
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = isExample
        ? "biomars-example-draft.md"
        : "biomars-protocol.md";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      document.querySelectorAll(".toolbar-menu[open]").forEach(menu => { menu.open = false; });
    };
  if ($("#copy"))
    $("#copy").onclick = async () => {
      try {
        await navigator.clipboard.writeText(job.markdown);
        toast(t("Markdown copied.", "已复制 Markdown。"));
      } catch {
        raw = true;
        workspace(job);
        toast(
          t(
            "Select and copy the Markdown text.",
            "请选中 Markdown 文本后复制。",
          ),
        );
      }
    };
  if ($("#print"))
    $("#print").onclick = () => {
      if (raw) {
        raw = false;
        workspace(job);
      }
      window.print();
    };
  if ($("#cancel-job"))
    $("#cancel-job").onclick = async () => {
      try {
        await api(`/jobs/${job.id}/cancel`, { method: "POST" });
        await refreshJob(job.id);
      } catch (err) {
        toast(err.message);
      }
    };
  if ($("#resume-form"))
    $("#resume-form").onsubmit = async (event) => {
      event.preventDefault();
      const button = $("#resume-button");
      button.disabled = true;
      const body = new FormData();
      [...$("#papers").files].forEach((f) => body.append("files", f));
      try {
        await api(`/jobs/${job.id}/resume`, { method: "POST", body });
        await refreshJob(job.id);
      } catch (error) {
        $("#resume-error").textContent = error.message;
        button.disabled = false;
      }
    };
}
function bindReadingControls() {
  const options = [["font", "readerFont"], ["reading", "readerSize"], ["paper", "readerPaper"]];
  for (const [key, dataKey] of options) {
    const attribute = dataKey.replace(/[A-Z]/g, letter => "-" + letter.toLowerCase());
    document.querySelectorAll(`[data-${attribute}]`).forEach(button => {
      button.onclick = () => {
        prefs[key] = button.dataset[dataKey];
        persistPreferences();
        document.querySelectorAll(`[data-${attribute}]`).forEach(b => b.setAttribute("aria-pressed", String(b.dataset[dataKey] === prefs[key])));
      };
    });
  }
}
function bindReader(job) {
  bindReadingControls();
  const selectPanel = panel => {
    mobilePanel = panel;
    if (["sources", "review"].includes(panel)) {
      activeTab = panel;
      $("#evidence-content").innerHTML = evidenceMarkup(job, panel, t, icon);
      $("#evidence-content").setAttribute("aria-labelledby", `${panel}-tab`);
      ["sources", "review"].forEach(id => {
        $(`#${id}-tab`).classList.toggle("active", id === panel);
        $(`#${id}-tab`).setAttribute("aria-selected", String(id === panel));
      });
    }
    $(".reader-layout").dataset.mobilePanel = panel;
    document.querySelectorAll("[data-panel]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.panel === panel)));
  };
  document.querySelectorAll("[data-panel]").forEach(button => button.onclick = () => selectPanel(button.dataset.panel));
  ["sources", "review"].forEach(id => {
    $(`#${id}-tab`).onclick = () => selectPanel(id);
    $(`#${id}-tab`).onkeydown = event => {
      if (["ArrowLeft", "ArrowRight"].includes(event.key)) {
        event.preventDefault(); const next = id === "sources" ? "review" : "sources";
        selectPanel(next); $(`#${next}-tab`).focus();
      }
    };
  });
  $("#focus-reader").onclick = () => {
    focusMode = !focusMode;
    $(".reader").dataset.focus = String(focusMode);
    $("#focus-reader").setAttribute("aria-pressed", String(focusMode));
    if (focusMode) selectPanel("draft");
  };
  document.querySelectorAll("[data-section]").forEach(button => button.onclick = () => {
    selectPanel("draft");
    document.getElementById(button.dataset.section)?.scrollIntoView({block:"start", behavior: prefs.reduceMotion || matchMedia('(prefers-reduced-motion: reduce)').matches ? "instant" : "smooth"});
  });
  document.querySelectorAll("[data-cite]").forEach(button => button.onclick = () => {
    focusMode = false;
    $(".reader").dataset.focus = "false";
    $("#focus-reader").setAttribute("aria-pressed", "false");
    selectPanel("sources");
    const source = document.getElementById(`source-${button.dataset.cite}`);
    source?.classList.add("source-highlight");
    source?.scrollIntoView({block:"nearest",behavior:"instant"});
    source?.querySelector("a")?.focus({preventScroll:true});
  });
  const sections = [...document.querySelectorAll(".protocol-section")];
  if (sections.length) {
    outlineObserver = new IntersectionObserver(entries => {
      const entry = entries.find(e => e.isIntersecting);
      if (entry) document.querySelectorAll("[data-section]").forEach(b => {
        if (b.dataset.section === entry.target.id) b.setAttribute("aria-current", "true");
        else b.removeAttribute("aria-current");
      });
    }, {rootMargin:"-100px 0px -65% 0px", threshold:0});
    sections.forEach(section => outlineObserver.observe(section));
  }
  document.querySelectorAll(".toolbar-menu").forEach(menu => menu.addEventListener("toggle", () => {
    if (menu.open) document.querySelectorAll(".toolbar-menu").forEach(other => { if (other !== menu) other.open = false; });
  }));
}
async function refreshJob(id) {
  clearTimeout(pollTimer);
  try {
    const job = await api(`/jobs/${id}`);
    if (location.hash !== `#job/${id}`) return;
    // Keep the spinner DOM alive between unchanged polling responses.
    if (!currentJob || currentJob.id !== job.id || currentJob.state !== job.state || currentJob.stage !== job.stage || currentJob.markdown !== job.markdown) workspace(job);
    else currentJob = job;
    if (["queued", "running"].includes(job.state))
      pollTimer = setTimeout(() => refreshJob(id), 2500);
  } catch (error) {
    if (location.hash !== `#job/${id}`) return;
    dialog(
      `<h2>${t("Unable to load this task.", "暂时无法打开任务。")}</h2><p>${e(error.message)}</p><button id="retry-job" class="primary">${t("Try again", "重试")}</button>`,
    );
    $("#retry-job").onclick = () => {
      $("#dialog").close();
      refreshJob(id);
    };
  }
}
async function examplesPage() {
  currentJob = null;
  try {
    const catalog = await loadExamples();
    if (location.hash !== "#examples") return;
    $("#main").innerHTML = examplesMarkup({ t, icon, cases: catalog.cases });
    let category = "all";
    const draw = () => {
      const matches = filterExamples(catalog.cases, $("#example-search").value, category);
      $("#example-list").innerHTML = exampleRows(matches, t);
      $("#example-count").textContent = `${matches.length} / ${catalog.count}`;
    };
    $("#example-search").oninput = draw;
    document.querySelectorAll("[data-category]").forEach(button => {
      button.onclick = () => {
        category = button.dataset.category;
        document.querySelectorAll("[data-category]").forEach(b => b.setAttribute("aria-pressed", String(b === button)));
        draw();
      };
    });
    draw();
  } catch {
    $("#main").innerHTML = `<div class="library-empty"><h2>${t("Collection unavailable", "案例库暂时不可用")}</h2><button class="secondary" id="retry-examples">${t("Retry", "重试")}</button></div>`;
    $("#retry-examples").onclick = examplesPage;
  }
}
async function route() {
  clearTimeout(pollTimer);
  outlineObserver?.disconnect();
  currentJob = null;
  const path = location.hash.slice(1);
  updateChrome();
  document.body.dataset.page = path.startsWith("settings/")
    ? "settings"
    : path === "library" || path === "examples"
      ? "library"
      : path === "example" || path.startsWith("example/") || path.startsWith("job/")
        ? "workspace"
        : "home";
  if (path === "examples") {
    await examplesPage();
  } else if (path === "example" || /^example\/(O\d{2}|V2-\d{2})$/.test(path)) {
    const id = path === "example" ? "O07" : path.split("/")[1];
    try {
      const response = await fetch(`./examples/${id}.json`);
      if (!response.ok) throw new Error("Missing example");
      const example = await response.json();
      if (location.hash.slice(1) === path) workspace(example);
    } catch {
      $("#main").innerHTML = `<div class="library-empty"><h2>${t("Example unavailable", "案例暂时不可用")}</h2><a href="#examples">${t("Back to collection", "返回案例库")}</a></div>`;
    }
  } else if (/^job\/[a-f0-9]{32}$/.test(path) && apiAvailable) {
    await refreshJob(path.slice(4));
  } else if (path === "library") {
    await library();
  } else if (path.startsWith("settings/")) {
    const section = path.split("/")[1];
    settingsPage(
      ["general", "experiment", "service"].includes(section)
        ? section
        : "general",
    );
  } else {
    home();
  }
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", () => {
  if (location.hash === "#main") {
    $("#main").focus();
    return;
  }
  route();
});
updateChrome();
await Promise.all([checkService(), loadExamples().then(data => { exampleCatalog = data; }).catch(() => {})]);
await route();
