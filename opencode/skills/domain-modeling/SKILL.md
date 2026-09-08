---
name: domain-modeling
description: Use when a project's domain language is fuzzy, terms are used inconsistently, terminology is drifting, the same concepts keep being re-explained across sessions, or you need to decide whether to record an architectural decision. Owns the CONTEXT.md glossary (a shared vocabulary of domain terms) and offers ADRs only when warranted.
---

# Domain Modeling

An active discipline for keeping a project's domain language precise. It owns
the `CONTEXT.md` glossary — a shared vocabulary of domain terms — and decides
when a decision warrants an ADR. Trigger it when terms are used inconsistently,
terminology drifts, or the same concept keeps being re-explained across
sessions (each re-explanation wastes tokens that a one-sentence entry replaces).

## Layout

- A single `CONTEXT.md` at the project root holding the glossary, plus
  `docs/adr/NNNN-*.md` for architectural decisions.
- For a codebase with several distinct contexts, use `CONTEXT-MAP.md` instead.
- Create the file lazily — only when a term actually needs pinning down. Do not
  scaffold an empty glossary up front.

## During a session

- **Challenge terms.** When a word is used two ways, name the ambiguity and
  propose the sharper meaning.
- **Sharpen fuzzy language.** Replace vague phrasing with the concrete term the
  code actually implements.
- **Stress-test relationships.** Ask whether the way two concepts relate in
  conversation matches how they relate in code.
- **Cross-reference claims against code.** A glossary entry that contradicts the
  implementation is wrong — fix the entry or the code.
- **Update `CONTEXT.md` inline**, as you discover the sharper meaning. Never
  batch glossary edits at the end of a session.
- **Pin repeated explanations.** When the same concept needs more than two
  sentences across messages or sessions, add a one-sentence `CONTEXT.md` entry
  so it never needs re-explaining.

## CONTEXT.md rules

- It is a **glossary only** — terms and their precise meanings. No
  implementation details, no how-to, no prose essays.
- One entry per term, one sentence per meaning where possible.
- If a term is genuinely contested, mark it `Flagged:` and note the competing
  meanings rather than silently picking one.

## When to offer an ADR

Offer an architectural decision record only when all three hold:

1. The decision is hard to reverse.
2. It would be surprising without context.
3. There was a real trade-off between viable options.

If any is false, record the decision as a `CONTEXT.md` glossary entry instead —
do not create an ADR for routine choices.
