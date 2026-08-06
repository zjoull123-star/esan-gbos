# ADR-0007: 中文 UI 与原文多语言

- Status: Gate 0/1 decision; implementation evidence is pending
- Date: 2026-08-06

## Context

The first operators work in Chinese while external messages may be Arabic,
English, Spanish, or other languages. Replacing source text with a translation
would destroy evidence and make review difficult.

## Decision

- Chinese is the primary application UI language for Gate 0/1. Navigation,
  permissions, review states, errors, and safety notices have Chinese labels;
  stable identifiers and contract field names remain machine-readable.
- Every observed item keeps the original bytes/text, `original_language`, and
  source/evidence references. A translation is a separately labeled view or
  draft and never overwrites the original.
- User-facing multilingual content must preserve semantic parity across the
  supported locales. Missing or uncertain translation is shown as such and
  goes to human review; legal, financial, and outbound-message text is never
  silently auto-sent.
- Locale and direction are explicit presentation metadata. Arabic/RTL views
  must be tested without changing stored evidence order or identifiers.
- AI translation/extraction remains subject to ADR-0004 and is disabled with
  real model traffic in Gate 0/1.

## Consequences

- Storage and export payloads are multilingual, while Chinese UI copy is the
  default operator affordance.
- QA must compare meaning, placeholders, numbers, dates, and direction—not
  just whether a string exists.
- A locale may be added without changing the evidence contract, but a change
  to original-content semantics requires an ADR and migration review.

## Verification gate

UI smoke covers Chinese labels, source/original display, locale switching,
Arabic direction, and mobile layout. A test must prove the original text is
unchanged after translation or correction.
