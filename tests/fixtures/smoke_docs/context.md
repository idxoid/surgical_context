# Context Arbitrator Smoke Doc

`ContextArbitrator` assembles the prompt context for a local ask request. It combines the primary symbol, graph neighbors, documentation chunks, token budget metadata, and dirty overlay state.

This file is intentionally small so `scripts/local_dev.py smoke` can exercise documentation indexing without embedding the full project documentation set.
