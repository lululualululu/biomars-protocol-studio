import { methodArtwork } from './visual-assets.js?v=5';
import { escapeHTML as e } from './markdown.js';
let cache;
export async function loadExamples() {
  if (!cache) {
    const response = await fetch('./examples/index.json');
    if (!response.ok) throw new Error('Examples could not be loaded.');
    cache = await response.json();
  }
  return cache;
}
export const categories = (t) => [
  ['all', t('All', '全部')], ['organoids', t('Organoids', '类器官')],
  ['cells', t('Cell culture', '细胞培养')], ['imaging', t('Imaging', '成像')],
  ['assays', t('Assays', '功能检测')], ['engineering', t('Cell engineering', '细胞工程')],
];
export function filterExamples(cases, query, category) {
  const needle = query.toLocaleLowerCase().trim();
  return cases.filter(c => (category === 'all' || c.category === category) &&
    [c.id, c.title_en, c.title_zh, c.query, c.doi].some(v => String(v || '').toLocaleLowerCase().includes(needle)));
}
export function examplesMarkup({t, icon, cases, selected = 'all'}) {
  return `<section class="examples-page"><div class="page-heading"><div><h1>${t('The collection', '案例库')}</h1><p class="collection-intro">${t('50 protocols to explore.', '50 个实验，找到你的起点。')}</p></div><a class="back" href="#">${t('New experiment', '新建实验')} ${icon('arrow',14)}</a></div><div class="collection-tools"><nav class="collection-filters" aria-label="${t('Filter examples', '筛选案例')}">${categories(t).map(([id,label])=>`<button data-category="${id}" aria-pressed="${id===selected}">${label}</button>`).join('')}</nav><label class="library-search">${icon('search',16)}<input id="example-search" type="search" placeholder="${t('Find a method', '查找方法')}" aria-label="${t('Find a method', '查找方法')}"></label></div><div class="collection-meta"><span>Qwen3.6 Flash · 2026.10</span><span id="example-count" role="status">${cases.length} ${t('drafts','份草稿')}</span></div><div class="collection-list" id="example-list"></div><p class="library-footnote">${t('Historical drafts · Review before use', '历史草稿 · 使用前请审阅')}</p></section>`;
}
export function exampleRows(cases, t) {
  const names = new Map(categories(t));
  return cases.map((c,i) => `<a class="collection-item" data-category="${e(c.category)}" style="--item-order:${i%6}" href="#example/${e(c.id)}"><div class="method-art" aria-hidden="true"><img src="${methodArtwork(c.category)}" width="140" height="140" alt="" loading="lazy" decoding="async"></div><span class="collection-id">${e(c.id)} <span>${e(names.get(c.category)||'')}</span></span><div class="method-title"><h2>${e(t(c.title_en,c.title_zh))}</h2><p>${e(t(c.title_zh,c.title_en))}</p></div><span class="collection-arrow" aria-hidden="true">↗</span></a>`).join('') || `<p class="library-empty">${t('No matching methods.', '没有匹配的方法。')}</p>`;
}
