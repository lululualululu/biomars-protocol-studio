import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../web/markdown.js',import.meta.url),'utf8');
const {markdown,safeURL}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
test('raw HTML and scripts are rendered as text',()=>{
  const output=markdown('<img src=x onerror=alert(1)>\n<script>alert(1)</script>');
  assert.ok(!output.includes('<img'));
  assert.ok(!output.includes('<script>'));
});
test('unsafe source URLs cannot become executable links',()=>{
  assert.equal(safeURL('javascript:alert(1)'), '');
  assert.equal(safeURL('data:text/html,test'), '');
  assert.ok(!markdown('[click](javascript:alert)').includes('href='));
});
test('headings, lists, source links and tables render',()=>{
  const output=markdown('# Draft\n\n- Check context\n\n| Item | Status |\n| --- | --- |\n| Medium | Unknown |\n\n[Source](https://example.org)');
  for(const tag of ['<h1>','<ul>','<table>','<td>','rel="noopener noreferrer"'])assert.ok(output.includes(tag),tag);
});
test('canonical step markers remain in exports but are hidden in reading view',()=>{
  assert.ok(!markdown('1. Check context. <!-- protocol-step:B01-S01 -->').includes('protocol-step'));
});
