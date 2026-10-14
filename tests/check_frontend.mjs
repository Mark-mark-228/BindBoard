// Static checks for app.html: JS syntax, inline-handler functions, icons, i18n keys, API contract.
// Usage: node check_html.mjs <app.html> <app.py>
import fs from 'node:fs';
import vm from 'node:vm';

const [, , htmlPath, pyPath] = process.argv;
const html = fs.readFileSync(htmlPath, 'utf8').replace(/^﻿/, '');
const py = fs.readFileSync(pyPath, 'utf8');
let problems = 0;
const bad = (msg) => { problems++; console.log('PROBLEM:', msg); };

// 1. syntax of every inline <script>
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
scripts.forEach((src, i) => {
  try { new vm.Script(src, { filename: `script${i}.js` }); }
  catch (e) { bad(`script #${i} syntax: ${e.message}`); }
});
const js = scripts.join('\n');

// 2. functions referenced from inline handlers (static markup and template strings)
const defined = new Set([...js.matchAll(/(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/g)].map(m => m[1]));
for (const m of js.matchAll(/(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>/g)) defined.add(m[1]);
for (const m of js.matchAll(/window\.([A-Za-z_$][\w$]*)\s*=/g)) defined.add(m[1]);
const builtins = new Set(['if', 'var', 'document', 'event', 'this', 'window', 'setTimeout', 'parseInt', 'parseFloat', 'return', 'pywebview']);
const handlerRe = /\bon(?:click|input|change|keydown|mousedown|focus|blur|mouseover|mouseout)="([^"]*)"/g;
const called = new Set();
for (const m of html.matchAll(handlerRe)) {
  for (const c of m[1].matchAll(/(?:^|[^.\w$])([A-Za-z_$][\w$]*)\s*\(/g)) {
    if (!builtins.has(c[1])) called.add(c[1]);
  }
}
for (const f of called) if (!defined.has(f)) bad(`handler calls undefined function ${f}()`);

// 3. icons used vs defined
const iconsBlock = js.slice(js.indexOf('const ICONS = {'), js.indexOf('};', js.indexOf('const ICONS = {')));
const iconNames = new Set([...iconsBlock.matchAll(/^\s+'?([a-z0-9-]+)'?\s*:/gm)].map(m => m[1]));
const usedIcons = new Set([...html.matchAll(/data-icon="([a-z0-9-]+)"/g)].map(m => m[1]).concat(
  [...js.matchAll(/icon\('([a-z0-9-]+)'/g)].map(m => m[1])));
for (const n of usedIcons) if (!iconNames.has(n)) bad(`icon "${n}" used but not defined`);

// 4. i18n keys used vs defined (en and ru)
const ctx = {};
vm.runInNewContext(js.slice(js.indexOf('const STRINGS = {'), js.indexOf('\n};', js.indexOf('const STRINGS = {')) + 3).replace('const STRINGS', 'STRINGS'), ctx);
const S = ctx.STRINGS;
const usedKeys = new Set([...html.matchAll(/data-i18n(?:-html|-title)?="([\w]+)"/g)].map(m => m[1]).concat(
  [...js.matchAll(/\bt\('([\w]+)'\)/g)].map(m => m[1])));
for (const k of usedKeys) {
  if (!(k in S.en)) bad(`i18n key "${k}" missing in en`);
  if (!(k in S.ru)) bad(`i18n key "${k}" missing in ru`);
}
for (const k of Object.keys(S.en)) if (!(k in S.ru)) bad(`i18n key "${k}" has no ru translation`);

// 5. JS -> Python API contract
const pyMethods = new Set();
const apiBody = py.slice(py.indexOf('class API:'), py.indexOf('\n# ──', py.indexOf('class API:')));
for (const m of apiBody.matchAll(/^    def ([a-z][\w]*)\(/gm)) pyMethods.add(m[1]);
const jsCalls = new Set([...html.matchAll(/pywebview\??\.api\??\.([A-Za-z_]\w*)/g)].map(m => m[1]));
for (const c of jsCalls) if (!pyMethods.has(c)) bad(`JS calls api.${c}() but API has no such method`);

console.log(`scripts=${scripts.length} handlers-called=${called.size} icons=${usedIcons.size} i18n-keys=${usedKeys.size} api-calls=${jsCalls.size} -> ${problems} problem(s)`);
process.exit(problems ? 1 : 0);
