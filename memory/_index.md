# Memory Index

This is ATLAS's persistent memory. Each file here is a markdown document the
brain reads and writes. The structure:

- `digest.md` — rolling "what I keep getting wrong" summary, rebuilt after every
  closed-trade autopsy. The brain receives this on every cycle.
- `autopsies/` — one markdown file per closed trade. Frontmatter for query;
  prose for the post-mortem.
- `setups/` — taxonomy of known setups. The brain pulls relevant entries based
  on the current pattern.
- `regimes/` — descriptions of each market regime (accumulation, markup,
  distribution, markdown, reaccumulation, redistribution).
- `journal/` — daily session log. One file per UTC day.

Wikilinks (`[[setups/london-reversal]]`) connect autopsies to setups and
regimes. Tags (`#won`, `#lost`, `#kill-thesis-clean`, `#london`) drive the
digest aggregation.

The brain reads files; the operator can edit them. Both audiences matter.
