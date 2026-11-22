# Design

A dark cinema wall, read on a phone. The posters carry the colour; the interface stays black and
quiet around them, and three colours mark state. Redrawn 2026-11-22 from the ruled ledger it
started as. `curator/static/app.css` is the source of truth for values.

## Palette

| Token       | Value     | Job                                                           |
| ----------- | --------- | ------------------------------------------------------------- |
| `bg`        | `#000000` | Page ground (dark only)                                       |
| `surface`   | `#0c0c0d` | Cards, lists, the summary, the phone's tab bar                |
| `surface-2` | `#181818` | Reason panels, control fill, count pills                      |
| `line`      | `#26262b` | Hairlines and card edges, 1px                                 |
| `control`   | `#666672` | Edges of buttons, fields and the IMDb pill (3:1 on a card)    |
| `text`      | `#f8fafc` | Text                                                          |
| `text-2`    | `#94a3b8` | Secondary text and labels                                     |
| `go`        | `#fb7185` | What is going: the going count, Delete instead, marked titles |
| `go-fill`   | `#e11d48` | The same red as a fill: the mark beside the name              |
| `keep`      | `#4ade80` | What is kept: Keep, "Kept" states, allow-list reasons         |
| `warn`      | `#fbbf24` | Warnings and failures: a stale list, an unreadable service    |
| `ring`      | `#ffffff` | Focus ring                                                    |

Every text colour is at least 6:1 on every ground it sits on.

## Type

Inter (bundled variable font, OFL), one family throughout. Headings at 700 with tight tracking;
labels and section heads at 600, uppercase, 0.08em; body at 16px. Tabular numerals wherever
numbers line up. Scripts the bundled subsets lack fall back to the system face.

## Components

- **Tabs**: on a phone, a bar fixed to the bottom, five icons with labels, the current one white
  with a red icon. From 760px, pills in a sticky top bar beside the name.
- **Jump strip**: on This week, Films and TV as two pills with the count going in each, stuck to
  the top while the page scrolls, because the second library sits below every card of the first.
- **Summary**: the one number a person acts on leads, large and red, with the day and the size
  beside it; later, kept, needs you and last run sit under it as small figures.
- **Card**: the poster, then the title and year, the size, plays and origin on one line, and the
  rating as a pill that opens IMDb. Under them the reason in a panel with its author, a collapsed
  synopsis, the tag state, then buttons at least 48px tall. On a phone the reason runs the full
  width under the poster; from 560px the poster takes the left edge. Cards sit two across once
  two 440px columns fit, at about 930px.
- **Poster**: 2:3, always from `/poster/…`, lazy. It opens IMDb, as the pill does, so it is kept
  out of the tab order. A title without one shows a clapperboard.
- **Sections**: Going and Needs you open; Later, Being kept for a year and Deleted collapsed
  behind a bar with a chevron.
- **Lists**: Runs, Deleted and the Allow list are one surface with hairlines between rows;
  Deleted and the Allow list carry a small poster.
- **Facts**: a grid of uppercase label over value, inside one surface.

## Rules

- Radius 12px on surfaces, 8px inside them, 10px on controls; no shadows, no gradients.
- No script, and nothing loaded from elsewhere: fonts, icons and posters come from this app.
- Red, green and amber are states, never decoration. The posters are the only other colour.
- Motion is hover and press only, 90 to 240ms, and off under reduced motion.
- Empty states are a dashed box with one sentence and the next step.
- Icons are Lucide (ISC), drawn as CSS masks so they take the text colour.
- The app icon is the mark beside the name: a red rounded square on black. The manifest lets
  a phone add the pages to its home screen and open them without browser chrome.
