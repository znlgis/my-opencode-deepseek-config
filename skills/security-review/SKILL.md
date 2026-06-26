---
name: security-review
description: Audit code changes for security vulnerabilities before merging. Use when reviewing a diff/PR, hardening code, or the task mentions security, injection, XSS, SSRF, secrets, auth, deserialization, path traversal, or "is this safe?". Provides a concrete checklist and reporting format; never auto-fixes silently.
license: MIT
compatibility: opencode
metadata:
  audience: reviewers
  workflow: security
---

# Security Review

A focused checklist for catching real vulnerabilities in a change set. Apply it
to the **diff** first (the lines that actually changed and the code they call),
then widen only if a finding points elsewhere. Report findings; do not rewrite
code unless explicitly asked.

## Threat checklist

- **Injection** — untrusted input flowing into SQL, shell, OS commands, eval,
  template engines, or LDAP. Look for string concatenation instead of
  parameterized queries / argument arrays.
- **XSS** — user data rendered into HTML/JS/DOM without escaping. Check
  `innerHTML`, `dangerouslySetInnerHTML`, unescaped template interpolation.
- **AuthN / AuthZ** — missing authentication, missing ownership/permission
  checks, IDOR (acting on an object by id without verifying the caller owns it),
  privilege escalation, trusting client-supplied roles.
- **Secrets** — hardcoded API keys, tokens, passwords, private keys, or
  connection strings; secrets logged or committed. Should come from env/secret
  store, never source.
- **Path traversal / file access** — user-controlled paths joined without
  normalization/allow-listing (`../` escapes, symlink following).
- **SSRF** — server-side requests to a user-supplied URL/host without
  allow-listing; can reach internal metadata endpoints.
- **Deserialization / parsing** — untrusted data fed to unsafe deserializers
  (pickle, native YAML loaders, Java/PHP object deserialization).
- **Crypto** — weak/auto algorithms (MD5/SHA1 for passwords), hardcoded IVs,
  ECB mode, missing TLS verification, predictable randomness for tokens.
- **Sensitive data exposure** — PII/secrets in logs, error messages, or API
  responses; verbose stack traces leaked to clients.
- **Dependencies** — newly added packages: are they reputable, pinned, and free
  of known CVEs? Avoid typosquats. For supply-chain verification, use
  `gh attestation` (see the `gh-cli` skill).
- **Resource & DoS** — unbounded loops, unbounded request/body sizes, missing
  timeouts, regex catastrophic backtracking (ReDoS).
- **Race conditions / TOCTOU** — check-then-act on files, balances, or auth
  state without locking.

## Method

1. Identify the **trust boundary**: where does untrusted input enter, and where
   does it reach a sink (DB, shell, filesystem, network, HTML)?
2. Trace each tainted value from source to sink; a vuln exists when it reaches a
   dangerous sink without validation/encoding/parameterization.
3. Prefer **allow-lists** over deny-lists; prefer library-provided escaping and
   parameterization over manual sanitization.
4. Only flag issues you can justify with a concrete exploit path — no
   speculative or style-only noise.

## Report format

For each finding:

```
[severity: high|medium|low] <title>
location: path/to/file.ext:LINE
issue: <what is wrong and the input that triggers it>
impact: <what an attacker gains>
fix: <the minimal concrete remediation>
```

Order findings by severity. If you reviewed the change and found nothing
actionable, say so explicitly rather than padding with low-value notes.
