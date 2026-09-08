#!/usr/bin/env node
/**
 * validate-jsonc.js — Validate OpenCode JSONC configuration files.
 *
 * Strips comments (preserving // inside strings) and trailing commas,
 * then validates as JSON. Usage: node validate-jsonc.js [files...]
 *
 * This script is referenced by the opencode-config skill.
 * Run it before committing config changes.
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

    // Block comment: /* ... */
    if (inBlockComment) {
      if (ch === '*' && next === '/') {
        inBlockComment = false;
        i += 2;
        continue;
      }
      i++;
      continue;
    }

    // String: preserve everything inside
    if (inString) {
      out.push(ch);
      if (ch === '"') inString = false;
      i++;
      continue;
    }

    // Start of string
    if (ch === '"') {
      out.push(ch);
      inString = true;
      i++;
      continue;
    }

    // Line comment: // ... (only outside strings)
    if (ch === '/' && next === '/') {
      // Skip until end of line
      while (i < source.length && source[i] !== '\n') i++;
      continue;
    }

    // Block comment start
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

function validate(filePath) {
  const absPath = path.resolve(filePath);
  if (!fs.existsSync(absPath)) {
    console.error(`  ✗ NOT FOUND: ${absPath}`);
    return false;
  }

  try {
    const raw = fs.readFileSync(absPath, 'utf-8');
    const stripped = stripJsonc(raw);
    JSON.parse(stripped);
    console.log(`  ✓ VALID: ${absPath}`);
    return true;
  } catch (e) {
    console.error(`  ✗ INVALID: ${absPath}`);
    console.error(`    ${e.message}`);
    return false;
  }
}

const targets = process.argv.slice(2).length > 0
  ? process.argv.slice(2)
  : ['opencode/opencode.jsonc', 'opencode/dcp.jsonc'];

console.log('Validating OpenCode JSONC configs...\n');

let allValid = true;
for (const target of targets) {
  if (!validate(target)) allValid = false;
}

console.log(allValid ? '\nAll configs valid.' : '\nSome configs have errors.');
process.exit(allValid ? 0 : 1);
