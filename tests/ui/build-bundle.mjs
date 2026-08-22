// jsdom cannot execute <script type="module">, so the two ES modules that make
// up the UI are flattened into one classic script before injection.
//
// This deliberately fetches from the running server rather than reading the
// files from disk, so the test exercises exactly what the API actually serves.
import { writeFileSync } from 'fs';

const BASE = process.env.GIT_SYNAPSE_URL || 'http://localhost:8080';

const [graph, app] = await Promise.all([
  fetch(BASE + '/static/graph.js').then((r) => r.text()),
  fetch(BASE + '/static/app.js').then((r) => r.text()),
]);

const flatGraph = graph.replace(/^export\s+/gm, '');
const flatApp = app
  .replace(/^import\s+\{[^}]*\}\s+from\s+'\.\/graph\.js';\s*$/m, '')
  .replace(/^export\s+(const|function|let|async function)\s/gm, '$1 ');

writeFileSync(new URL('./bundle.js', import.meta.url), `'use strict';\n${flatGraph}\n\n${flatApp}`);
console.log('bundle.js written');
