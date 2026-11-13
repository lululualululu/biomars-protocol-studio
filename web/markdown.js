// Small, deliberately HTML-free renderer. Untrusted raw HTML is always escaped.
export const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function safeURL(value) {
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : ''; } catch { return ''; }
}
function inline(value) {
  const chunks = String(value).split(/(\[[^\]\n]+\]\([^\s)]+\))/g);
  return chunks.map(chunk => {
    const match = chunk.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
    if (match) { const url = safeURL(match[2]); return url ? `<a href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">${escapeHTML(match[1])} ↗</a>` : escapeHTML(match[1]); }
    return escapeHTML(chunk).replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>');
  }).join('');
}
export function markdown(source) {
  const lines = String(source ?? '').replace(/<!--\s*protocol-step:[\s\S]*?-->/g, '').split('\n');
  let html = '', list = '', code = false, codeLines = [];
  const closeList = () => { if (list) { html += `</${list}>`; list = ''; } };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^```/.test(line)) { closeList(); if (code) { html += `<pre><code>${escapeHTML(codeLines.join('\n'))}</code></pre>`; codeLines = []; } code = !code; continue; }
    if (code) { codeLines.push(line); continue; }
    if (line.includes('|') && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i+1])) {
      closeList(); const cells = l => l.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
      html += `<table><thead><tr>${cells(line).map(c=>`<th>${inline(c)}</th>`).join('')}</tr></thead><tbody>`; i += 2;
      while (i < lines.length && lines[i].trim() && lines[i].includes('|')) { html += `<tr>${cells(lines[i]).map(c=>`<td>${inline(c)}</td>`).join('')}</tr>`; i++; }
      i--; html += '</tbody></table>'; continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)/);
    const item = line.match(/^\s*(?:([-*])|(\d+)\.)\s+(.+)/);
    if (item) { const type = item[2] ? 'ol' : 'ul'; if (list !== type) { closeList(); list = type; html += `<${type}>`; } html += `<li>${inline(item[3])}</li>`; continue; }
    closeList();
    if (heading) html += `<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`;
    else if (line.startsWith('>')) html += `<blockquote>${inline(line.replace(/^>\s?/, ''))}</blockquote>`;
    else if (/^\s*---+\s*$/.test(line)) html += '<hr>';
    else if (line.trim()) html += `<p>${inline(line)}</p>`;
  }
  closeList(); if (codeLines.length) html += `<pre>${escapeHTML(codeLines.join('\n'))}</pre>`;
  return html;
}
