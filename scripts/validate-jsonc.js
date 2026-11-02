#!/usr/bin/env node
/**
 * validate-jsonc.js -- Validate OpenCode JSONC configuration files.
 *
 * Strips comments (preserving // inside strings) and trailing commas,
 * then validates as JSON. Usage: node validate-jsonc.js [files...]
 *
 * Two lint passes always run, even when explicit config paths are passed:
 * opencode/agents/*.md may only carry v2-native top-level keys (a file with
 * none of the v2-only keys, permissions/request/system/disabled, is decoded as
 * a v1 agent, so a v1 spelling silently changes the parsed shape), and
 * opencode/skills/<name>/SKILL.md must carry a name matching its directory plus
 * a known-key frontmatter block.
 *
 * Config files additionally get a model-layer lint: a key under `settings` that
 * the openai-chat protocol does not implement is dropped without a diagnostic,
 * a null in `body`/`headers` shadows the base value, and a default tier that
 * leaves the thinking toggle unset pays reasoning tokens on every step.
 *
 * Output is ASCII only (OK / FAIL) so it renders in any console encoding.
 *
 * This script is referenced by the opencode-config skill.
 * Run it before committing config changes.
 */

const fs = require('fs');
const path = require('path');

// Top-level keys that only exist in the v1 dialect. v2.2.5 normalizes most of
// them (migrated per key, then merged with the native keys) and drops the
// four in unsupportedTopLevel (logLevel, server, layout, subagent_depth) with
// a diagnostic, so the config that runs is not the config that was written.
// Hand-maintained mirror of unsupportedTopLevel plus the legacy branches in
// packages/core/src/config/normalize.ts (migrate.ts holds the converters);
// re-checked 2026-11-03.
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

// `settings` reaches the provider package as options, and the openai-chat
// protocol implements none of these: they are dropped there with no error. The
// thinking toggle is a REQUEST BODY field; effort is the camelCase provider
// setting `reasoningEffort` (snake_case is the API's spelling, not the setting).
const INERT_SETTINGS_KEYS = new Set([
  'thinking', 'temperature', 'top_p', 'presence_penalty', 'frequency_penalty',
  'reasoning_effort',
]);

const REASONING_EFFORTS = new Set(['low', 'high', 'max']);

// Top-level keys a skill file may declare. Anything else is either a v1
// spelling or does not exist, so the loader drops or migrates it silently.
const SKILL_FRONTMATTER_KEYS = new Set([
  'name', 'description', 'license', 'allowed-tools', 'metadata',
]);

// Repo root derived from __dirname, never from cwd: the script must find the
// agent files no matter which directory it is invoked from.
const REPO_ROOT = path.resolve(__dirname, '..');
const AGENTS_DIR = path.join(REPO_ROOT, 'opencode', 'agents');
const SKILLS_DIR = path.join(REPO_ROOT, 'opencode', 'skills');

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

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

// mergeRecord/mergeOverlay treat only `undefined` as "unset", so a null is a
// value: it is serialized onto the wire and shadows the base it was meant to
// clear. Walks the whole subtree, not just the first level.
function collectNullPaths(value, at, problems) {
  if (value === null) {
    problems.push(`${at} is null; mergeOverlay treats null as a value; omit the key instead`);
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((item, index) => collectNullPaths(item, `${at}[${index}]`, problems));
    return;
  }
  if (!isRecord(value)) return;
  for (const [key, item] of Object.entries(value)) {
    collectNullPaths(item, `${at}.${key}`, problems);
  }
}

// Returns the list of model-layer problems; empty means valid. Covers every
// provider, model and variant, not just the DeepSeek block, so a second model
// added later inherits the same guards.
function checkModelLayers(parsed) {
  const problems = [];
  const providers = isRecord(parsed.providers) ? parsed.providers : {};

  for (const [providerID, provider] of Object.entries(providers)) {
    const models = isRecord(provider) && isRecord(provider.models) ? provider.models : {};
    for (const [modelID, model] of Object.entries(models)) {
      if (!isRecord(model)) continue;

      const variants = Array.isArray(model.variants) ? model.variants : [];
      const base = `providers.${providerID}.models.${modelID}`;
      const layers = [
        { at: base, cfg: model },
        ...variants.map((variant, index) => ({
          at: `${base}.variants[${isRecord(variant) && typeof variant.id === 'string' ? variant.id : index}]`,
          cfg: variant,
        })),
      ];

      for (const { at, cfg } of layers) {
        if (!isRecord(cfg)) continue;

        const settings = isRecord(cfg.settings) ? cfg.settings : {};
        const inert = Object.keys(settings).filter((key) => INERT_SETTINGS_KEYS.has(key));
        if (inert.length > 0) {
          problems.push(
            `${at}.settings has inert key(s) ${inert.join(', ')}: ` +
            'inert under settings: move toggle to body, effort to settings.reasoningEffort (camelCase)'
          );
        }

        for (const key of ['body', 'headers']) {
          if (key in cfg) collectNullPaths(cfg[key], `${at}.${key}`, problems);
        }
      }

      for (const variant of variants) {
        if (!isRecord(variant) || !isRecord(variant.settings)) continue;
        const effort = variant.settings.reasoningEffort;
        if (effort === undefined || REASONING_EFFORTS.has(effort)) continue;
        problems.push(
          `${base}.variants[${typeof variant.id === 'string' ? variant.id : '?'}].settings.reasoningEffort ` +
          `is ${JSON.stringify(effort)}; expected one of ${[...REASONING_EFFORTS].join('/')}`
        );
      }
    }
  }

  // The upstream default is thinking ON at effort `high`: a default tier that
  // omits the toggle silently pays reasoning tokens on every step. Variants are
  // exempt -- they layer on top of this tier and restate `enabled` themselves.
  const flash = isRecord(providers.deepseek) && isRecord(providers.deepseek.models)
    ? providers.deepseek.models['deepseek-flash']
    : undefined;
  if (isRecord(flash)) {
    const thinking = isRecord(flash.body) && isRecord(flash.body.thinking) ? flash.body.thinking : {};
    if (thinking.type !== 'disabled') {
      problems.push(
        'providers.deepseek.models["deepseek-flash"].body.thinking.type must be "disabled": ' +
        'default tier must send thinking disabled'
      );
    }
  }

  return problems;
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
    // v2 silently migrates or drops them per key, so the config that runs
    // stops matching this file. Nested keys are not checked here -- `mode` and
    // `tools` are legal inside v2 agent/provider objects.
    const legacy = Object.keys(parsed).filter((key) => V1_TOP_LEVEL_KEYS.has(key));
    if (legacy.length > 0) {
      console.error(`  FAIL: invalid: ${absPath}`);
      console.error(`    v1 top-level key(s) are silently migrated or dropped by v2: ${legacy.join(', ')}`);
      return false;
    }

    const problems = checkModelLayers(parsed);
    if (problems.length > 0) {
      console.error(`  FAIL: invalid: ${absPath}`);
      for (const problem of problems) console.error(`    ${problem}`);
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
        ? `v1 frontmatter key "${key}" at line ${i + 1} is not a v2-native agent key; allowed keys: ${allowed}`
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

// Returns the list of problems found in one skill file; empty means valid. The
// files carry a deliberately small frontmatter (name, description, at most a
// license or metadata map), so a line-based parser covers them and the repo
// stays dependency-free.
function checkSkillFrontmatter(absPath, dirName) {
  const lines = fs.readFileSync(absPath, 'utf-8').replace(/^\uFEFF/, '').split(/\r?\n/);

  if (lines[0].trim() !== '---') {
    return ['missing opening --- frontmatter block: line 1 is not "---"'];
  }

  let end = -1;
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim() === '---') {
      end = i;
      break;
    }
  }
  if (end === -1) {
    return ['unterminated frontmatter: no closing ---'];
  }

  const problems = [];
  const values = {};
  const allowed = [...SKILL_FRONTMATTER_KEYS].join(', ');

  for (let i = 1; i < end; i++) {
    const line = lines[i];
    if (line.trim() === '' || line.startsWith('#')) continue;
    // Indented lines are nested values (allowed-tools entries, metadata maps)
    // or block-scalar continuations; only column-0 keys are linted.
    if (/^\s/.test(line)) continue;

    const match = /^([A-Za-z_][\w-]*):(.*)$/.exec(line);
    if (!match) {
      problems.push(`malformed frontmatter at line ${i + 1}: expected "key: value"`);
      continue;
    }

    const key = match[1];
    if (!SKILL_FRONTMATTER_KEYS.has(key)) {
      problems.push(`frontmatter key "${key}" at line ${i + 1} is not allowed; allowed keys: ${allowed}`);
      continue;
    }

    const value = match[2].trim().replace(/^(['"])(.*)\1$/, '$2').trim();
    if (value !== '') values[key] = value;
  }

  // The directory name is the skill's identity in the roster and in every
  // agent's allowlist; a mismatch registers the file under a name no allow
  // rule mentions, so the skill silently disappears.
  if (!values.name) {
    problems.push('missing non-empty "name:" in frontmatter');
  } else if (values.name !== dirName) {
    problems.push(`frontmatter name "${values.name}" must match the skill directory "${dirName}"`);
  }
  if (!values.description) {
    problems.push('missing non-empty "description:" in frontmatter');
  }
  return problems;
}

function lintSkillFrontmatter() {
  if (!fs.existsSync(SKILLS_DIR)) {
    console.error(`  FAIL: skill directory not found: ${SKILLS_DIR}`);
    return false;
  }

  const names = fs.readdirSync(SKILLS_DIR, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort();
  if (names.length === 0) {
    console.error(`  FAIL: no skill directories found in ${SKILLS_DIR}`);
    return false;
  }

  let ok = true;
  for (const name of names) {
    const absPath = path.join(SKILLS_DIR, name, 'SKILL.md');
    const problems = fs.existsSync(absPath)
      ? checkSkillFrontmatter(absPath, name)
      : ['missing SKILL.md'];
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
  : [path.join(REPO_ROOT, 'opencode', 'opencode.jsonc')];

console.log('Validating OpenCode JSONC configs...\n');

let allValid = true;
for (const target of targets) {
  if (!validate(target)) allValid = false;
}

// Always runs: the agent files carry the same v1-routing hazard as the config
// files, and an explicit config path argument must not silence that check.
console.log('\nLinting agent frontmatter...\n');
if (!lintAgentFrontmatter()) allValid = false;

console.log('\nLinting skill frontmatter...\n');
if (!lintSkillFrontmatter()) allValid = false;

console.log(allValid ? '\nAll checks passed.' : '\nSome checks failed.');
process.exit(allValid ? 0 : 1);
