#!/usr/bin/env node
/**
 * validate-jsonc.js -- Validate OpenCode JSONC configuration files.
 *
 * Strips comments (preserving // inside strings) and trailing commas,
 * then validates as JSON. Usage: node validate-jsonc.js [files...]
 *
 * An agent-frontmatter lint pass always runs, even when explicit config paths
 * are passed: opencode/agents/*.md may only carry v2-native top-level keys.
 * Any v1 spelling reroutes that agent through the V1->V2 migration path.
 *
 * Output is ASCII only (OK / FAIL) so it renders in any console encoding.
 *
 * This script is referenced by the opencode-config skill.
 * Run it before committing config changes.
 */

const fs = require('fs');
const path = require('path');

// Top-level keys that only exist in the v1 dialect. Any one of them makes v2
// take the V1->V2 migration branch instead of native decode.
// Hand-maintained mirror of isV1 in packages/core/src/v1/config/migrate.ts
// (re-checked 2026-10-27).
const V1_TOP_LEVEL_KEYS = new Set([
  'logLevel', 'server', 'command', 'reference', 'snapshot', 'plugin',
  'autoupdate', 'autoshare', 'disabled_providers', 'enabled_providers',
  'small_model', 'mode', 'agent', 'provider', 'permission', 'tools',
  'attachment', 'layout', 'subagent_depth',
]);

// Top-level keys a v2-native agent file may declare. Anything else either
// belongs to the v1 agent dialect (name, options, permission, temperature,
// tools, reasoningEffort) or does not exist at all; both flip the file onto
// the migration path or get silently dropped, so both fail the lint.
const AGENT_FRONTMATTER_KEYS = new Set([
  'model', 'request', 'system', 'description', 'mode', 'hidden', 'color',
  'steps', 'disabled', 'permissions', 'variant',
]);

const LEGACY_AGENT_KEYS = new Set([
  'name', 'options', 'permission', 'temperature', 'top_p', 'prompt', 'tools',
  'disable', 'maxSteps', 'reasoningEffort',
]);

// Repo root derived from __dirname, never from cwd: the script must find the
// agent files no matter which directory it is invoked from.
const REPO_ROOT = path.resolve(__dirname, '..');
const AGENTS_DIR = path.join(REPO_ROOT, 'opencode', 'agents');

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
    console.error(`  FAIL: not found: ${absPath}`);
    return false;
  }

  try {
    const raw = fs.readFileSync(absPath, 'utf-8');
    const stripped = stripJsonc(raw.replace(/^\uFEFF/, ''));
    const parsed = JSON.parse(stripped);

    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      console.error(`  FAIL: invalid: ${absPath}`);
      console.error('    config root must be a JSON object');
      return false;
    }

    // The repo's central invariant: never contain a top-level v1 key, because
    // any one of them silently reroutes the whole file through the V1->V2
    // migration path. Nested keys are not checked here -- `mode` and `tools`
    // are legal inside v2 agent/provider objects.
    const legacy = Object.keys(parsed).filter((key) => V1_TOP_LEVEL_KEYS.has(key));
    if (legacy.length > 0) {
      console.error(`  FAIL: invalid: ${absPath}`);
      console.error(`    v1 top-level key(s) trigger the V1->V2 migration path: ${legacy.join(', ')}`);
      return false;
    }

    console.log(`  OK: ${absPath}`);
    return true;
  } catch (e) {
    console.error(`  FAIL: invalid: ${absPath}`);
    console.error(`    ${e.message}`);
    return false;
  }
}

// Returns the list of problems found in one agent file; empty means valid.
function checkAgentFrontmatter(absPath) {
  const blocks = fs.readFileSync(absPath, 'utf-8').replace(/^\uFEFF/, '').split(/\r?\n/);

  if (blocks[0].trim() !== '---') {
    return ['missing opening --- frontmatter block'];
  }

  let end = -1;
  for (let i = 1; i < blocks.length; i++) {
    if (blocks[i].trim() === '---') {
      end = i;
      break;
    }
  }
  if (end === -1) {
    return ['unterminated frontmatter: no closing ---'];
  }

  const problems = [];
  const allowed = [...AGENT_FRONTMATTER_KEYS].join(', ');
  for (let i = 1; i < end; i++) {
    // Top-level keys only: the pattern is anchored at column 0, so nested keys
    // (pushed under `permissions:` and indented) are never collected.
    const match = /^([A-Za-z_][\w-]*):/.exec(blocks[i]);
    if (!match) continue;

    const key = match[1];
    if (AGENT_FRONTMATTER_KEYS.has(key)) continue;

    problems.push(
      LEGACY_AGENT_KEYS.has(key)
        ? `v1 frontmatter key "${key}" at line ${i + 1} routes the agent through the V1->V2 migration path; allowed keys: ${allowed}`
        : `frontmatter key "${key}" at line ${i + 1} is not a v2-native agent key (v1 spelling or unknown); allowed keys: ${allowed}`
    );
  }
  return problems;
}

function lintAgentFrontmatter() {
  if (!fs.existsSync(AGENTS_DIR)) {
    console.error(`  FAIL: agent directory not found: ${AGENTS_DIR}`);
    return false;
  }

  const files = fs.readdirSync(AGENTS_DIR).filter((name) => name.endsWith('.md')).sort();
  if (files.length === 0) {
    console.error(`  FAIL: no agent files found in ${AGENTS_DIR}`);
    return false;
  }

  let ok = true;
  for (const name of files) {
    const absPath = path.join(AGENTS_DIR, name);
    const problems = checkAgentFrontmatter(absPath);
    if (problems.length === 0) {
      console.log(`  OK: ${absPath}`);
      continue;
    }
    console.error(`  FAIL: ${absPath}`);
    for (const problem of problems) console.error(`    ${problem}`);
    ok = false;
  }
  return ok;
}

const targets = process.argv.slice(2).length > 0
  ? process.argv.slice(2)
  : ['opencode/opencode.jsonc'];

console.log('Validating OpenCode JSONC configs...\n');

let allValid = true;
for (const target of targets) {
  if (!validate(target)) allValid = false;
}

// Always runs: the agent files carry the same v1-routing hazard as the config
// files, and an explicit config path argument must not silence that check.
console.log('\nLinting agent frontmatter...\n');
if (!lintAgentFrontmatter()) allValid = false;

console.log(allValid ? '\nAll checks passed.' : '\nSome checks failed.');
process.exit(allValid ? 0 : 1);
