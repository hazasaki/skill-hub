import { build } from 'esbuild';
import { readFile, readdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

const { metafile } = await build({
  entryPoints: ['main.tsx'],
  bundle: true,
  format: 'iife',
  target: 'es2022',
  outfile: '../static/sidebar.bundle.js',
  minify: true,
  define: { 'process.env.NODE_ENV': '"production"' },
  banner: { js: '/*! Third-party notices: sidebar.bundle.licenses.txt */' },
  legalComments: 'inline',
  metafile: true,
});

const packages = new Set(['node_modules/tailwindcss']);
for (const input of Object.keys(metafile.inputs)) {
  const match = input.match(/^(.*node_modules\/(?:@[^/]+\/)?[^/]+)\//);
  if (match) packages.add(match[1]);
}

const notices = [];
for (const directory of [...packages].sort()) {
  const files = (await readdir(directory)).filter(name => /^(licen[cs]e|notice)(\.|$)/i.test(name)).sort();
  const manifest = JSON.parse(await readFile(join(directory, 'package.json'), 'utf8'));
  if (files.length === 0) throw new Error('Missing license for ' + manifest.name);
  const texts = await Promise.all(files.map(name => readFile(join(directory, name), 'utf8')));
  notices.push([manifest.name + ' ' + manifest.version, ...texts.map(text => text.trim().replace(/\r\n/g, '\n'))].join('\n\n'));
}
await writeFile('../static/sidebar.bundle.licenses.txt', notices.join('\n\n---\n\n') + '\n');
