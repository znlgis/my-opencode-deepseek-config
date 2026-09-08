#!/usr/bin/env node
/**
 * estimate-cost.js — Estimate a session's DeepSeek API cost from token counts.
 *
 * Reads the USD-per-1M-token cost table from opencode/opencode.jsonc
 * (provider.deepseek.models) and prints the estimated cost for each model.
 * Plain Node, zero dependencies — mirrors validate-jsonc.js.
 *
 * Usage:
 *   node estimate-cost.js [--input N] [--output N] [--cache-read N] [--cache-write N]
 *
 * Defaults match the README worked example: 200K input, 150K cache hits, 30K output.
 * Token counts are in raw tokens (not per-1M); the script divides internally.
 */

const fs = require('fs');
const path = require('path');

function stripJsonc(source) {
  const out = [];
  let inString = false;
  let inBlockComment = false;
  let escape = false;
  let i = 0;

  while (i < source.length) {
    const ch = source[i];
    const next = source[i + 1];

    if (escape) {
      out.push(ch);
      escape = false;
      i++;
      continue;
    }
    if (ch === '\\') {
      out.push(ch);
      escape = true;
      i++;
      continue;
    }
    if (inBlockComment) {
      if (ch === '*' && next === '/') {
        inBlockComment = false;
        i += 2;
        continue;
      }
      i++;
      continue;
    }
    if (inString) {
      out.push(ch);
      if (ch === '"') inString = false;
      i++;
      continue;
    }
    if (ch === '"') {
      out.push(ch);
      inString = true;
      i++;
      continue;
    }
    if (ch === '/' && next === '/') {
      while (i < source.length && source[i] !== '\n') i++;
      continue;
    }
    if (ch === '/' && next === '*') {
      inBlockComment = true;
      i += 2;
      continue;
    }
    out.push(ch);
    i++;
  }

  return stripTrailingCommas(out.join(''));
}

// Remove trailing commas before ] or } without touching commas inside string
// literals. Scans char-by-char so a `, }` or `, ]` sequence inside a quoted
// string (e.g. "a, }") is never mangled, and honors backslash escapes.
function stripTrailingCommas(source) {
  let out = '';
  let inString = false;
  let escape = false;

  for (let i = 0; i < source.length; i++) {
    const ch = source[i];

    if (inString) {
      out += ch;
      if (escape) escape = false;
      else if (ch === '\\') escape = true;
      else if (ch === '"') inString = false;
      continue;
    }

    if (ch === '"') {
      out += ch;
      inString = true;
      continue;
    }

    if (ch === ',') {
      let j = i + 1;
      while (j < source.length && /\s/.test(source[j])) j++;
      if (source[j] === '}' || source[j] === ']') continue; // trailing comma
    }

    out += ch;
  }

  return out;
}

function parseArgs(argv) {
  const args = { input: 200000, output: 30000, cacheRead: 150000, cacheWrite: 0 };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    const val = Number(argv[i + 1]);
    if (Number.isNaN(val)) continue;
    if (flag === '--input') args.input = val;
    else if (flag === '--output') args.output = val;
    else if (flag === '--cache-read') args.cacheRead = val;
    else if (flag === '--cache-write') args.cacheWrite = val;
  }
  return args;
}

function loadCostTable(configPath) {
  const abs = path.resolve(configPath);
  const raw = fs.readFileSync(abs, 'utf-8');
  const parsed = JSON.parse(stripJsonc(raw));
  const models = parsed.provider && parsed.provider.deepseek
    ? parsed.provider.deepseek.models
    : {};
  return Object.entries(models).map(([id, cfg]) => ({
    id,
    cost: cfg.cost || {},
  }));
}

function main() {
  const configPath = process.argv.includes('--config')
    ? process.argv[process.argv.indexOf('--config') + 1]
    : 'opencode/opencode.jsonc';
  const { input, output, cacheRead, cacheWrite } = parseArgs(process.argv.slice(2));

  const table = loadCostTable(configPath);
  if (table.length === 0) {
    console.error('No cost table found in ' + configPath);
    process.exit(1);
  }

  const rows = table.map(({ id, cost }) => {
    const inputCost = Math.max(0, input - cacheRead) * (cost.input || 0) / 1e6;
    const cacheCost = cacheRead * (cost.cache_read || 0) / 1e6;
    const writeCost = cacheWrite * (cost.cache_write || 0) / 1e6;
    const outputCost = output * (cost.output || 0) / 1e6;
    const total = inputCost + cacheCost + writeCost + outputCost;
    return { id, inputCost, cacheCost, writeCost, outputCost, total };
  });

  const cheapest = rows.reduce((a, b) => (b.total < a.total ? b : a));

  console.log(`Cost estimate (tokens: ${input} input, ${cacheRead} cache-read, ${cacheWrite} cache-write, ${output} output)\n`);
  console.log('Model'.padEnd(32) + 'input-miss'.padEnd(12) + 'cache-read'.padEnd(12) + 'cache-write'.padEnd(13) + 'output'.padEnd(10) + 'total');
  for (const r of rows) {
    const usd = (n) => '$' + n.toFixed(4);
    console.log(
      r.id.padEnd(32) +
      usd(r.inputCost).padEnd(12) +
      usd(r.cacheCost).padEnd(12) +
      usd(r.writeCost).padEnd(13) +
      usd(r.outputCost).padEnd(10) +
      usd(r.total)
    );
  }
  console.log(`\nCheapest: ${cheapest.id} (${'$' + cheapest.total.toFixed(4)})`);
}

main();
