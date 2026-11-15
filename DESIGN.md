# DeadReckon — committed design direction

This is the design source of truth for the build. DeadReckon is a **CLI**
archetype product, so the direction is expressed in terminal geometry, a single
accent used only as a *concept*, and one monospace type ramp — not in CSS.
Every rendered surface (`legs`, `fixes`, `fixes last`, `dr`, `drift`, `status`,
`--help`) consumes the tokens below.

## Direction

| Axis | Committed value |
|---|---|
| Archetype | `cli` — a terminal tool; there is no browser surface, no CSS, no HTML |
| Style family | **calm-precise**: quiet labels, exact numbers, no decoration |
| Surface / mode | **terminal dark** — the honest CLI surface (the user's own terminal). No app-chrome colours are emitted, so the tool is correct on a dark *or* light terminal |
| Type personality | **mono / mono** — one fixed-width face (the terminal's own); no second family, no proportional text inside tables |
| Density | **compact** — no blank line between rows, two-space gutters, one blank line between blocks |
| Signature | aligned columns; one accent for primary status; **never colour-only** signalling |
| Layout skeleton | aligned terminal columns; help-first output (`--help` lists the command surface before anything else is printed) |
| Shape language | monospace, no chrome; fixed-width columns and blank-line separators; no box-drawing, no rules, no elevation |

## Tokens

Spacing scale (characters/blank lines only): `1` gutter between cells is **2
spaces**; between blocks **1 blank line**; label column width **12**
(`Store       `, `Leg table   `, `Current offset  ` uses the natural word
width). Nothing else is free-floating: every offset in the tool comes from this
scale.

Type ramp (terminal cells): body = default size; table headers = UPPERCASE
labels at the same size but left-aligned; secondary/legend lines = default size,
one blank line below the table. No bold, no underline, no ANSI at all (F7).

Palette — the accent is `oklch(0.58 0.12 193)`, hex `#00908e`. It is used as
the *concept* for "the state you are in" (the header line of a table, the
`Current offset` value, the `Saved as dr fix N` confirmation line) and it is
allowed in documentation/asset contexts. The CLI itself emits **no** colour, by
plan requirement — so the accent is never a colour-only signal:

| Token | Value | Where |
|---|---|---|
| `--accent` | `oklch(0.58 0.12 193)` / `#00908e` | documentation, examples, any future rendered surface |
| surface | terminal default background (dark assumed) | all output |
| `--text` | terminal default foreground, ≥ 4.5:1 on the surface | all body text |
| `--text-dim` | same colour, signalled by position not greyness | labels, legends |
| status signal | **words**, never colour: `obs` / `dr`, `none`, `no fix recorded yet` | every table and summary |

Semantic tokens (the words that carry state):

| Token | Meaning | Example |
|---|---|---|
| `legs` header | leg table origin line | `Leg table  origin 50.764,-1.297` |
| `fixes` header | log count line | `Fix log  4 fixes` |
| `Offset` | current snapshot on a fix | `075T/1.4`, or `-` for none |
| `Current offset` | active current | `set 075T / 1.40 kn`, or `none` |
| confirmation | write succeeded | `Saved as dr fix 4 at …` |
| refusal | destructive action without `--yes` | `refusing to clear 4 legs …` |

## Rules that follow from this direction

1. Numbers are right-aligned inside fixed-width columns; header labels are
   left-aligned so the eye finds the column start. Every row of a table lays
   its fields on the same character edges.
2. Rounding is part of the design: positions `0.000`, distances and speeds
   `0.00`, courses `090T` (three digits, whole degrees, `T` for true). `--json`
   exposes the full floats, so precision is never lost — only quietened.
3. No line exceeds 80 columns, no ANSI escape is ever emitted, and output is
   pure ASCII: it survives `pbcopy`, `tee`, `grep` and a diff.
4. A zero-row table never renders blank: it states the state and the exact
   command that fills it (`No fixes recorded yet. Record one with: …`).
5. Every failure is one line on stderr, prefixed `deadreckon: error:` or
   `deadreckon: store error:` — never a stack trace, never colour.
6. Nothing on screen is decorative: no box drawing, no themes, no progress
   animation, no ASCII art (explicit plan exclusions).
