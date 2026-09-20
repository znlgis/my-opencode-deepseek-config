---
name: grilling
description: Use when requirements are ambiguous or a message doesn't land, before planning or implementing — ask ONE question at a time, prefer multiple choice, until intent is clear. Triggers include "grill", "wait what", interview, unclear or ambiguous requirements, clarifying questions.
---

# Grilling

Discipline for aligning on requirements when intent is unclear. Ask until the
ambiguity resolves, then stop.

## Rules

- Ask ONE question at a time.
- Prefer multiple choice (A/B/C); each option carries its implication.

## Format

> **Understood**: [your interpretation]
> **Unsure about**: [the specific ambiguity]
> **Options**: 1. [A] — [implications]  2. [B] — [implications]
> **Recommendation**: [choice + reasoning]

## Stop conditions

- Stop the moment intent is clear. Do not ask more to look thorough.
- If a default is obvious, proceed and state the assumption instead of asking.

## When the message doesn't land at all

Re-pitch it in one sentence before acting:

> Wait — you're asking me to [X], so I'd [Y]. Correct, or did you mean something else?

## When the domain language is fuzzy

If the same concept is called different things (or a term's meaning shifts), also
load the `domain-modeling` skill: as each answer sharpens a term, record it in
`CONTEXT.md` inline. Alternating a grilling question with a terminology fix is
one flow, not two.

This format matches the "When to Ask vs. Proceed" rule in `AGENTS.md`.
