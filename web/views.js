import { methodArtwork } from './visual-assets.js?v=5';
import { readingControls } from "./protocol-view.js?v=5";
export const modelOptions = (models, selected, e) => models.map(m => `<option value="${e(m.id)}" ${m.id === selected ? "selected" : ""}>${e(m.name)}</option>`).join("");
// View markup contains no network requests. Actions are bound by app.js.
export function homeMarkup({
  t,
  e,
  icon,
  query,
  context,
  generationAvailable,
  models,
  selectedModel,
  featured,
}) {
  return `<div class="home"><section class="hero-grid"><div class="hero-content"><div class="eyebrow"><span class="status-point"></span> BIOMARS / STUDIO</div><h1>${t("Your next <span>experiment.</span>", "下一项<span>实验</span>")}</h1><p class="hero-subtitle">${t("Start with a question.", "从一个实验问题开始。")}</p>
  <form id="generate-form" class="composer"><label for="query" class="sr-only">${t("Describe your experiment", "描述你的实验需求")}</label><textarea id="query" required minlength="12" maxlength="6000" placeholder="${t("What would you like to do in the lab?", "想做什么实验？")}" aria-describedby="privacy-note">${e(query)}</textarea><div id="file-list" class="files"></div><div class="context-fields hidden" id="context"><label>${t("Cell line / system", "细胞系 / 生物系统")}<input id="cell-line" maxlength="120" placeholder="${t("Optional", "选填")}" value="${e(context.cell_line || "")}"></label><label>${t("Species", "物种")}<input id="species" maxlength="120" placeholder="${t("Optional", "选填")}" value="${e(context.species || "")}"></label><label class="wide">${t("Scope or constraints", "范围与约束")}<input id="scope" maxlength="500" placeholder="${t("What should the protocol focus on?", "希望聚焦哪一部分？")}" value="${e(context.scope || "")}"></label><button class="context-defaults text-button wide" id="use-defaults" type="button">${t("Use defaults", "使用默认值")}</button></div><div class="model-row"><label for="model">${t("Model", "模型")}</label><select id="model">${modelOptions(models, selectedModel, e)}</select></div><div class="composer-bottom"><div class="composer-tools"><button type="button" id="attach" class="subtle-button">${icon("paper")}${t("Attach", "附件")}</button><button type="button" id="context-toggle" class="subtle-button" aria-expanded="false" aria-controls="context">${icon("sliders")}${t("Context", "实验条件")}</button></div><button class="primary" id="generate" type="submit">${t("Generate", "生成")} ${icon("arrow")}</button></div><input class="hidden" type="file" id="file-input" multiple accept=".pdf,.md,.txt"><div id="form-error" class="error" role="alert"></div></form>
  <div class="under-composer"><span id="privacy-note">${icon("lock", 11)}${generationAvailable ? t("Ready", "服务就绪") : t("Preview", "预览模式")}</span><button class="example-link" id="open-example">${t("Browse examples", "浏览案例")} ${icon("arrow", 13)}</button></div>
  </div><figure class="biomedical-visual" aria-hidden="true"><img src="./assets/biomedical-cells-v1.jpg" alt="" fetchpriority="high"></figure></section>
  <section class="starting-points" aria-label="${t("Starting points", "实验起点")}"><span class="section-label">${t("SELECTED METHODS", "精选案例")}</span><div class="example-links">${featured.map((c,i) => `<button type="button" class="example-chip" data-example="${e(c.id)}"><img class="example-artwork" src="${methodArtwork(c.category)}" alt="" width="60" height="60" decoding="async"><span class="example-chip-title">${e(t(c.title_en,c.title_zh))}</span><span class="chip-arrow" aria-hidden="true">↗</span></button>`).join("")}</div><a class="all-protocols" href="#examples">${t("All 50", "全部 50 个")} ${icon("arrow", 13)}</a></section></div>`;
}

export function settingsMarkup({
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
}) {
  let content = "";
  if (section === "general")
    content = `<div class="settings-heading"><h2>${t("Appearance", "外观")}</h2></div><div class="setting-row"><label for="pref-language">${t("Language", "语言")}</label><select id="pref-language"><option value="zh" ${lang === "zh" ? "selected" : ""}>简体中文</option><option value="en" ${lang === "en" ? "selected" : ""}>English</option></select></div><div class="settings-reading">${readingControls(prefs,t)}<div class="reading-sample"><span>PROTOCOL</span><h3>Cell culture</h3><p>${t("Prepare the culture medium before starting.", "开始实验前，准备好所需培养基。")}</p></div></div><div class="setting-row"><label for="pref-motion">${t("Reduce motion", "减少动态效果")}</label><input type="checkbox" role="switch" class="switch" id="pref-motion" ${prefs.reduceMotion ? "checked" : ""}></div><p class="setting-footnote">${t("Saved automatically", "自动保存")}</p>`;
  if (section === "experiment")
    content = `<div class="settings-heading"><h2>${t("Defaults", "默认设置")}</h2></div><form id="defaults-form" class="settings-form"><label for="default-model">${t("Default model", "默认模型")}</label><select id="default-model">${modelOptions(models, prefs.model, e)}</select><label for="default-system">${t("Cell line / biological system", "细胞系 / 生物系统")}</label><input id="default-system" maxlength="120" placeholder="${t("e.g. adherent mammalian cells", "例如：哺乳动物贴壁细胞")}" value="${e(prefs.cell_line)}"><label for="default-species">${t("Species", "物种")}</label><input id="default-species" maxlength="120" placeholder="${t("Optional", "选填")}" value="${e(prefs.species)}"><label for="default-scope">${t("Scope or constraints", "实验范围或约束")}</label><textarea id="default-scope" rows="3" maxlength="500" placeholder="${t("Your usual experimental context", "常用的实验背景或限制条件")}" >${e(prefs.scope)}</textarea><div class="settings-form-actions"><button type="submit" class="primary">${t("Save", "保存")} ${icon("check", 14)}</button><button type="button" class="text-button" id="reset-defaults">${t("Clear", "清空")}</button><span id="defaults-status" role="status"></span></div></form>`;
  if (section === "service")
    content = `<div class="settings-heading"><h2>${t("Connection", "连接")}</h2></div><div class="service-status"><div class="service-symbol">${icon("sliders", 22)}</div><div><h3>${generationAvailable ? t("Connected", "已连接") : apiAvailable ? t("Setup required", "待配置") : t("Preview mode", "预览模式")}</h3><p>${generationAvailable ? (accessCodeRequired ? t("Access code required", "需要访问码") : t("Generation service", "生成服务")) : apiAvailable ? t("Add credentials on the server.", "请在服务端配置凭证。") : t("Browse the example collection.", "可浏览案例")}</p></div><button class="secondary" id="recheck-service" type="button">${t("Refresh", "刷新")}</button></div><div class="setting-row service-address"><span>${t("Current workspace", "当前工作区")}</span><code>${e(location.host)}</code></div>${apiAvailable && accessCodeRequired ? `<form id="access-form" class="settings-form"><label for="access-code">${t("Service access code", "服务访问码")}</label><div class="password-field"><input id="access-code" type="password" autocomplete="off" value="${e(token)}" placeholder="${t("Provided by your service operator", "由服务部署者提供")}"><button type="button" id="reveal-code" class="text-button" aria-pressed="false">${t("Show", "显示")}</button></div><p class="field-help">${t("Stored only in this tab’s session. Validated when you submit a request.", "仅保存在当前标签页会话中，提交任务时验证。")}</p><div class="settings-form-actions"><button type="submit" class="primary">${t("Save access code", "保存访问码")}</button><button type="button" id="clear-access" class="text-button">${t("Clear", "清除")}</button></div></form>` : ""}<div class="service-links"><button class="example-link" id="settings-example">${t("View example protocol", "查看示例 Protocol")} ${icon("arrow", 13)}</button><details><summary>${t("Service setup", "服务部署说明")}</summary><p>${t("Model and retrieval credentials are configured on the server. This page never asks for a model API key.", "模型与检索凭证在服务端配置。这里无需填写模型 API Key。")}</p><a href="https://github.com/lululualululu/biomars-protocol-studio#start-locally" target="_blank" rel="noopener">${t("Open setup guide", "打开部署指南")} ↗</a></details></div><div id="connection-feedback" class="field-help" role="status"></div>`;
  return `<section class="settings-page"><div class="page-heading"><div><h1>${t("Settings", "设置")}</h1></div><a class="back" href="#">${t("Back", "返回")} ${icon("arrow", 14)}</a></div><div class="settings-layout"><nav class="settings-nav" aria-label="${t("Settings sections", "设置分类")}">${[
    ["general", t("Appearance", "外观"), "sliders"],
    ["experiment", t("Defaults", "默认设置"), "file"],
    ["service", t("Connection", "连接"), "lock"],
  ]
    .map(
      ([key, label, symbol]) =>
        `<a href="#settings/${key}" ${section === key ? 'aria-current="page"' : ""}>${icon(symbol, 16)}${label}</a>`,
    )
    .join(
      "",
    )}</nav><section class="settings-content">${content}</section></div></section>`;
}

export function libraryMarkup({ t, icon }) {
  return `<section class="library-page"><div class="page-heading"><div><h1>${t("My protocols", "我的 Protocol")}</h1></div><a class="primary" href="#">${icon("plus", 14)}${t("New experiment", "新建实验")}</a></div><div class="library-toolbar"><label class="library-search">${icon("search", 16)}<input type="search" id="library-search" placeholder="${t("Find a protocol", "查找 Protocol")}" aria-label="${t("Find a protocol", "查找 Protocol")}"></label><span id="library-count" class="side-note"></span></div><div id="history-list" class="history-list" aria-live="polite"><div class="library-empty"><span class="empty-orbit">${icon("file", 26)}</span><p>${t("Loading protocols…", "正在载入…")}</p></div></div><p class="library-footnote">${t("Saved in this browser", "当前浏览器的记录")}</p></section>`;
}
