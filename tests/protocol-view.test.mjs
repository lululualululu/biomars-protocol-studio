import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { documentMarkup, protocolSections } from '../web/protocol-view.js';
import { markdown } from '../web/markdown.js';
const t = (en) => en;

test('all 50 historical drafts preserve canonical step order and count in the reader', async () => {
  const catalog = JSON.parse(await readFile(new URL('../web/examples/index.json', import.meta.url),'utf8'));
  for (const item of catalog.cases) {
    const job = JSON.parse(await readFile(new URL(`../web/examples/${item.id}.json`, import.meta.url),'utf8'));
    const before = job.markdown;
    const expected = [...before.matchAll(/<!--\s*protocol-step:([A-Za-z0-9_-]+)\s*-->/g)].map(m=>m[1]);
    const doc = documentMarkup(job,t);
    const actual = [...doc.body.matchAll(/data-step-id="([^"]+)"/g)].map(m=>m[1]);
    assert.deepEqual(actual, expected, item.id);
    assert.equal(doc.stepCount, expected.length, item.id);
    assert.equal(job.markdown, before, 'export content is unchanged');
    assert.equal(new Set(doc.sections.map(s=>s.id)).size, doc.sections.length);
  }
});

test('headings inside code are preserved and untrusted content remains escaped', () => {
  const source = '# Draft\n\n### Overview\nText\n```\n## Not a section\n```\n## Procedure\n- <img src=x onerror=alert(1)> [1] [9] <!-- protocol-step:B01-S01 -->';
  const sections = protocolSections(source,'Draft');
  assert.deepEqual(sections.map(s=>s.title),['Overview','Procedure']);
  assert.ok(sections[0].body.includes('## Not a section'));
  const html = markdown(sections[1].body,{protocol:true,sourceCount:1});
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('data-cite="1"'));
  assert.ok(!html.includes('data-cite="9"'));
  assert.ok(html.includes('[9]'));
});
