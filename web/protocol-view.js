import { escapeHTML as e, markdown, safeURL } from './markdown.js';

const titleKey = value => value.replace(/^protocol\s*:\s*/i, '').replace(/[.。]$/, '').trim().toLowerCase();
export function protocolSections(source, title = '') {
  const lines = String(source || '').split('\n');
  let first = lines.findIndex(line => line.trim());
  const heading = lines[first]?.match(/^#{1,2}\s+(.+)$/);
  if (heading && (titleKey(heading[1]) === titleKey(title) || /^protocol\s*:/i.test(heading[1]) || lines[first].startsWith('# '))) lines.splice(first, 1);
  const sections = [];
  let current = {title: '', body: []}, major = false, fenced = false;
  const finish = () => { if (current.title || current.body.some(l => l.trim())) sections.push({...current, body: current.body.join('\n')}); };
  for (const line of lines) {
    if (/^```/.test(line)) fenced = !fenced;
    const h = !fenced && line.match(/^(#{1,3})\s+(.+)$/);
    if (h && (h[1].length <= 2 || !major)) {
      finish(); current = {title: h[2], body: []};
      if (h[1].length <= 2) major = true;
    } else current.body.push(line);
  }
  finish();
  return sections.map((section, i) => ({...section, id: `protocol-section-${i + 1}`}));
}

export function documentMarkup(job, t) {
  const sections = protocolSections(job.markdown, job.title);
  let offset = 0;
  const body = sections.map((section, index) => {
    const stepCount = (section.body.match(/<!--\s*protocol-step:/g) || []).length;
    const html = markdown(section.body, {protocol: true, sourceCount: job.sources?.length || 0, stepOffset: offset});
    offset += stepCount;
    return `<section class="protocol-section ${stepCount ? 'procedure-section' : ''}" id="${section.id}"><div class="section-heading"><span>${String(index+1).padStart(2,'0')}</span><h2>${e(section.title || t('Protocol', '实验方案'))}</h2>${stepCount ? `<small>${stepCount} ${t('steps', '步')}</small>` : ''}</div><div class="section-body">${html}</div></section>`;
  }).join('');
  return {body, sections, stepCount: offset};
}

export function readingControls(prefs, t) {
  return `<div class="reading-controls"><span class="popover-label">${t('Typeface','字体')}</span><div class="segmented">${[['sans',t('Sans','无衬线')],['serif',t('Serif','衬线')]].map(([id,label])=>`<button type="button" data-reader-font="${id}" aria-pressed="${prefs.font === id}" class="font-${id}">${label}</button>`).join('')}</div><span class="popover-label">${t('Text size','字号')}</span><div class="segmented size-options">${[['compact','A−'],['standard','A'],['large','A+']].map(([id,label])=>`<button type="button" data-reader-size="${id}" aria-pressed="${prefs.reading===id}" aria-label="${t('Text size','字号')} ${id}">${label}</button>`).join('')}</div><span class="popover-label">${t('Paper','纸张')}</span><div class="paper-options">${[['white',t('White','白纸')],['warm',t('Ivory','米白')]].map(([id,label])=>`<button type="button" data-reader-paper="${id}" aria-pressed="${prefs.paper===id}" aria-label="${label}"><i class="paper-swatch ${id}"></i>${label}</button>`).join('')}</div></div>`;
}

export function readerMarkup({job, t, icon, modelName, prefs, activeTab, mobilePanel, raw, focusMode}) {
  const doc = documentMarkup(job,t);
  const historical = Boolean(job.historical);
  const date = job.provenance?.date || (job.created ? new Date(job.created*1000).toLocaleDateString('en-CA') : '');
  return `<section class="workspace reader" data-focus="${focusMode}"><div class="reader-toolbar"><a class="back" href="#${historical?'examples':'library'}">${icon('back',15)}${t(historical?'Examples':'My protocols',historical?'案例':'我的 Protocol')}</a><div class="workspace-actions">${historical?`<button class="secondary reuse-button" id="reuse-example">${icon('plus',14)}${t('Use request','使用此需求')}</button>`:''}<button id="focus-reader" class="icon-button" aria-label="${t('Focus reading','专注阅读')}" aria-pressed="${focusMode}" title="${t('Focus reading','专注阅读')}">${icon('focus',17)}</button><details class="toolbar-menu type-menu"><summary aria-label="${t('Reading settings','阅读设置')}" title="${t('Reading settings','阅读设置')}">Aa</summary><div class="toolbar-popover">${readingControls(prefs,t)}</div></details><button class="icon-button" id="copy" aria-label="${t('Copy protocol','复制全文')}" title="${t('Copy protocol','复制全文')}">${icon('copy',17)}</button><details class="toolbar-menu export-menu"><summary class="primary">${icon('download',14)}${t('Export','导出')}${icon('chevron',12)}</summary><div class="toolbar-popover export-options"><button id="download">${icon('file',15)}Markdown <span>.md</span></button><button id="print">${icon('print',15)}${t('Print / PDF','打印 / PDF')}</button><button id="raw-toggle">${icon('code',15)}${raw?t('Reading view','返回阅读'):t('Markdown source','查看 Markdown')}</button></div></details></div></div>
  <div class="mobile-panels" aria-label="${t('Document panels','文档面板')}">${[['draft',t('Document','正文')],['outline',t('Contents','目录')],['sources',t('Sources','文献')],['review',t('Checks','核对')]].map(([id,label])=>`<button data-panel="${id}" aria-pressed="${mobilePanel===id}">${label}</button>`).join('')}</div>
  <div class="reader-layout" data-mobile-panel="${mobilePanel}"><aside class="reader-outline" aria-label="${t('Contents','目录')}"><div class="outline-heading">${t('CONTENTS','目录')}<span>${String(doc.sections.length).padStart(2,'0')}</span></div><nav>${doc.sections.map((s,i)=>`<button data-section="${s.id}" ${i===0?'aria-current="true"':''}><span>${String(i+1).padStart(2,'0')}</span>${e(s.title || t('Protocol','实验方案'))}</button>`).join('')}</nav><div class="outline-end"><img src="./assets/brand-glass-v5.png" width="25" height="25" alt=""><span>BioMars<br>Protocol Studio</span></div></aside>
  <div class="document-column"><article class="protocol-paper"><header class="paper-header"><div class="paper-kicker"><span>PROTOCOL</span><span class="draft-tag">${t(historical?'EXAMPLE':'DRAFT',historical?'案例':'草稿')}</span></div><h1>${e(job.title)}</h1><div class="paper-meta"><span>${e(modelName)}</span>${doc.stepCount?`<span>${doc.stepCount} ${t('steps','个步骤')}</span>`:''}<span>${job.sources?.length || 0} ${t('source(s)','篇文献')}</span>${date?`<span>${e(date)}</span>`:''}</div>${job.query?`<details class="original-request"><summary>${t('Original request','查看需求')}${icon('chevron',12)}</summary><p>${e(job.query)}</p></details>`:''}</header><div id="document-body">${raw?`<textarea class="raw-editor" readonly aria-label="Protocol Markdown">${e(job.markdown)}</textarea>`:`<div class="draft">${doc.body}</div>`}</div><div class="paper-end"><img src="./assets/brand-glass-v5.png" width="20" height="20" alt=""><span>${t('Draft · Review before use','草稿 · 使用前请审阅')}</span></div></article></div>
  <aside class="evidence" aria-label="${t('Sources and checks','文献与核对')}"><div class="evidence-tabs" role="tablist">${['sources','review'].map(id=>`<button id="${id}-tab" class="tab ${activeTab===id?'active':''}" role="tab" aria-selected="${activeTab===id}" aria-controls="evidence-content">${id==='sources'?t('Sources','文献'):t('Checks','核对')}${id==='sources'?`<span>${job.sources?.length||0}</span>`:''}</button>`).join('')}</div><div id="evidence-content" role="tabpanel" aria-labelledby="${activeTab}-tab">${evidenceMarkup(job,activeTab,t,icon)}</div></aside></div></section>`;
}

export function evidenceMarkup(job, tab, t, icon) {
  if (tab === 'review') {
    const notes = (job.review_notes || []).filter(n => !['Review the draft, source applicability, and unresolved parameters before use.','Historical model-generated draft. Review the source and applicability before use.'].includes(n));
    const result = job.integrity_check;
    return `<div class="review-status ${result?.passed?'passed':''}"><span class="review-icon">${icon(result?.passed?'check':'file',20)}</span><strong>${result ? (result.passed?t('Integrity checked','完整性检查通过'):t('Needs review','需要核对')):t('Review draft','待审阅')}</strong><p>${result?.step_count?`${result.step_count} ${t('steps checked','个步骤已检查')}`:t('Historical draft','历史生成草稿')}</p></div>${notes.length?`<ul class="note-list">${notes.map(n=>`<li>${e(n)}</li>`).join('')}</ul>`:''}<p class="rail-footnote">${t('Check the source and experimental conditions before use.','使用前请核对原文与实验条件。')}</p>`;
  }
  if (!job.sources?.length) return `<div class="rail-empty">${icon('book',24)}<p>${t('Sources will appear here.','文献将在此显示')}</p></div>`;
  return job.sources.map((s,i)=>`<div class="source" id="source-${i+1}"><div class="source-meta"><span>${String(i+1).padStart(2,'0')}</span>${s.year?`<span>${e(s.year)}</span>`:''}${icon('book',14)}</div><h3>${e(s.title || s.doi || t('Source','参考文献'))}</h3>${s.doi?`<p class="source-doi">${e(s.doi)}</p>`:''}${safeURL(s.url)?`<a class="source-open" href="${e(safeURL(s.url))}" target="_blank" rel="noopener noreferrer">${t('Read paper','查看原文')}${icon('arrow',13)}</a>`:`<span class="side-note">${t('Link unavailable','暂无链接')}</span>`}</div>`).join('');
}

export const stages = ['protocol_domain_pack','protocol_research','protocol_pdf_ingest','protocol_planning','protocol_grounded_writer','protocol_critic'];
export function progressMarkup(job,t,icon,modelName) {
  const running = ['queued','running'].includes(job.state);
  const index = stages.indexOf(job.stage);
  const labels = [t('Request','需求'),t('Sources','文献'),t('Full text','全文'),t('Structure','规划'),t('Draft','撰写'),t('Review','检查')];
  const titles = [t('Understanding your experiment','理解实验需求'),t('Finding the right papers','正在查找文献'),t('Reading the full text','正在阅读原文'),t('Organizing the steps','正在组织步骤'),t('Writing your protocol','正在撰写 Protocol'),t('Checking the details','正在核对细节')];
  const title = job.state==='queued'?t('Waiting to begin','等待开始'):running?(titles[index] || t('Starting your protocol','正在准备')):job.state==='awaiting_papers'?t('Add the full text','补充全文'):t('Generation stopped','生成已停止');
  return `<section class="generation-page" data-state="${e(job.state)}"><div class="generation-top"><a class="back" href="#library">${icon('back',15)}${t('My protocols','我的 Protocol')}</a><span class="model-badge">${e(modelName)}</span></div><div class="generation-stage"><div class="generation-main"><div class="orbit ${running?'is-running':''}" aria-hidden="true"><div class="orbit-ring ring-one"></div><div class="orbit-ring ring-two"></div><div class="orbit-ring ring-three"></div><div class="orbit-core">${running?'<img src="./assets/brand-glass-v5.png" width="34" height="34" alt="">':icon(job.state==='awaiting_papers'?'paper':'file',26)}</div><i></i></div><div class="generation-label">${t('PROTOCOL IN PROGRESS','PROTOCOL')}</div><h1 id="generation-status" role="status" aria-live="polite">${e(title)}</h1><p class="generation-query">${e(job.title)}</p>${running?`<p class="generation-note">${t('You can return to this task later.','可稍后在「我的 Protocol」中查看。')}</p><div class="generation-actions"><a href="#library" class="secondary">${t('Back to protocols','返回列表')}${icon('arrow',14)}</a><button id="cancel-job" class="text-button">${t('Cancel','取消')}</button></div>`:job.state==='awaiting_papers'?`<div class="paper-requests">${(job.missing_papers||[]).map(p=>`<div class="paper-request">${icon('file',21)}<div><strong>${e(p.title||p.evidence_id)}</strong>${safeURL(p.url)?`<a href="${e(safeURL(p.url))}" target="_blank" rel="noopener">${t('Find paper','查看论文')} ↗</a>`:''}<details><summary>${t('File name','文件名要求')}</summary><code>${e(p.expected_pdf_filename)}</code></details></div></div>`).join('')}<form id="resume-form"><label class="upload-picker" for="papers">${icon('plus',16)}<span id="paper-selection">${t('Choose PDF','选择 PDF')}</span><input type="file" id="papers" accept=".pdf" multiple required></label><button class="primary" id="resume-button">${t('Continue','继续生成')}${icon('arrow',15)}</button><small>PDF · 10 MB · ${t('up to 30 pages','最多 30 页')}</small><div class="error" role="alert" id="resume-error"></div></form></div>`:`<p class="generation-note">${e(job.message || t('Start a new request when ready.','请重新提交实验需求。'))}</p><a href="#" class="primary">${t('New protocol','重新开始')}${icon('arrow',14)}</a>`}</div><aside class="generation-preview" aria-hidden="true"><div class="preview-sheet"><span>BIOMARS <b>PROTOCOL</b></span><div class="skeleton title-line"></div><div class="skeleton short-line"></div><hr><div class="preview-section"><i>01</i><div><div class="skeleton medium-line"></div><div class="skeleton"></div><div class="skeleton short-line"></div></div></div><div class="preview-section"><i>02</i><div><div class="skeleton medium-line"></div><div class="skeleton"></div><div class="skeleton"></div></div></div><div class="preview-section"><i>03</i><div><div class="skeleton short-line"></div><div class="skeleton"></div></div></div><span class="preview-seal">B / STUDIO</span></div><div class="preview-shadow"></div></aside></div>${running?`<ol class="generation-steps">${labels.map((label,i)=>`<li class="${job.state==='running'?(i===index?'current':i<index?'done':''):''}" ${job.state==='running'&&i===index?'aria-current="step"':''}><span>${i<index&&job.state==='running'?icon('check',13):String(i+1).padStart(2,'0')}</span><strong>${label}</strong><div></div></li>`).join('')}</ol>`:''}</section>`;
}
