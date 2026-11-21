# Design

A ruled ledger on graphite, read on a phone. Built 2026-11-21 on the visual language of the owner's
cel catalogue: one line weight, no radius, state marked in a single colour. `curator/static/app.css`
is the source of truth for values.

## Palette

| Token      | Value     | Job                                                     |
| ---------- | --------- | ------------------------------------------------------- |
| `ground`   | `#141414` | Page ground (dark only)                                 |
| `ground-2` | `#1d1d1d` | Reason panels and control fill                          |
| `rule`     | `#333333` | Every rule and border, 1px                              |
| `shell`    | `#2a2a2a` | Current tab, Keep button fill                           |
| `ink`      | `#ece8e1` | Text                                                    |
| `ink-2`    | `#b5afa5` | Secondary text and labels                               |
| `ink-3`    | `#8a857d` | Borders of plain controls                               |
| `biro`     | `#f5a524` | What is going: the going count, Delete instead, marks   |
| `keep`     | `#7fc28a` | What is kept: Keep, "Kept" states                       |
| `bad`      | `#e8705a` | Failures only: stale list, unreadable service, refusals |
| `gold`     | `#c9ad7a` | Counts in section heads, focus ring, reason chips       |

## Type

- Archivo (bundled variable font, OFL) at 75% width for labels, counts and section heads, in
  uppercase with 0.1em tracking. Tabular numerals wherever numbers line up.
- System sans for titles, reasons and body text.

## Components

- **Tabs**: full-width ruled segments, sticky; the current page filled `shell` with an amber underline.
- **Totals**: ruled grid of counts; the going count in amber.
- **Card**: title and year, a facts line, the reason in a panel with its author, a collapsed
  synopsis, the tag state, then full-width buttons at least 48px tall.
- **Sections**: Going and Needs you open; Later, Being kept for a year and Deleted collapsed.
- **Facts**: ruled definition grid (uppercase narrow label over value).

## Rules

- No radius, no shadows, no gradients, no script, nothing loaded from elsewhere.
- Amber and green are states, never decoration. Red only for failures.
- Empty states are a ruled box with one sentence and the next step.
