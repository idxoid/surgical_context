# Surgical Context Documentation

This folder contains all project documentation. Start here to understand the system.

---

## Quick Navigation

### **For Understanding the System**
- **[architectura.md](architectura.md)** — how all pieces fit together (start here)
- **[concept.md](concept.md)** — what is Surgical Context and why it exists
- **[idea_summary.md](idea_summary.md)** — elevator pitch
- **[project_gap_analysis.md](project_gap_analysis.md)** — short index to the merged project analysis

### **For Implementation**

**Code Indexing:**
- **[spec_indexer.md](spec_indexer.md)** — code extraction, call typing, AFFECTS rebuild
- **[spec_parser.md](spec_parser.md)** — language adapters, symbol extraction

**Doc Indexing:**
- **[spec_doc_indexer.md](spec_doc_indexer.md)** — chunking, embedding, DocAnchor linking
- **[spec_doc_anchor.md](spec_doc_anchor.md)** — FROM/COVERS relationships

**Context Assembly:**
- **[spec_arbitrator.md](spec_arbitrator.md)** — deprecated (use individual components instead)
- **[spec_intent_classifier.md](spec_intent_classifier.md)** — query intent → content tier ranking (Phase 6 design)
- **[spec_token_budget_bfs.md](spec_token_budget_bfs.md)** — BFS with token constraints
- **[spec_context_deduplicator.md](spec_context_deduplicator.md)** — remove redundant symbols

**APIs & Infrastructure:**
- **[spec_sidecar_api.md](spec_sidecar_api.md)** — FastAPI endpoints
- **[spec_language_adapter.md](spec_language_adapter.md)** — plugin architecture (ADR-005)
- **[spec_overlay.md](spec_overlay.md)** — in-memory dirty state

**Advanced Topics:**
- **[spec_eval_harness.md](spec_eval_harness.md)** — measuring quality (recall, precision)
- **[spec_embedding_versioning.md](spec_embedding_versioning.md)** — managing embedding model versions
- **[spec_affects_index.md](spec_affects_index.md)** — reverse dependency index

### **Planning & Review**
- **[road_map.md](road_map.md)** — phases and timelines
- **[project_gap_analysis.md](project_gap_analysis.md)** — index for current gaps and supporting specs
- **[review_findings_2026-04-17.md](review_findings_2026-04-17.md)** — external review recommendations
- **[architectural_review.md](architectural_review.md)** — technical decisions and trade-offs

---

## Current Truth

The repo currently includes the Python FastAPI sidecar, Neo4j/LanceDB storage clients, parser/indexer/context modules, tests, QA benchmark tooling, and a VS Code extension scaffold under `extension/`.

Recent hardening added request-scoped Neo4j sessions, doc retrieval inside the arbitration pipeline, typed API responses, JSON-safe SSE framing, a durable indexing job log with retry/dead-letter states, signed bearer-token enforcement behind `AUTH_REQUIRED`, and first endpoint coverage for the sidecar API.

The most important open gaps are correctness hardening rather than new feature discovery: stable symbol identity, scoped call resolution, workspace/branch isolation, production auth policy/secret management, prompt-contract observability, and backpressure for mass indexing events. See **[road_map.md](road_map.md)** for the canonical current backlog.

---

## Writing Documentation

Before writing or updating docs, read **[DOCS_STYLE_GUIDE.md](DOCS_STYLE_GUIDE.md)**.

Quick rules:
- **Be clear** — explain in 2 minutes or less
- **Show examples** — copy-paste-able code
- **Use tables** for data structures
- **Link everything** — internal links to related specs
- **List limitations** — what doesn't work today
- **Add trade-offs** — why this design over alternatives

---

## Document Status Legend

- ✅ **Implemented** — shipped, tested, in production
- 🚧 **In Progress** — active development
- 📋 **Planned** — on roadmap, not started
- ⚠️  **Known Issue** — works but with caveats
- ❌ **Not Implemented** — deferred
- 🔄 **Refactoring** — redesign underway

---

## How Docs Stay in Sync

Every pull request should include doc updates:
- **New feature?** Add a Spec or update the Concept
- **Design change?** Update architectura.md and affected Specs
- **Bug fix?** Add to Limitations if it's a known constraint
- **Phase complete?** Update road_map.md

Stale docs are worse than no docs — when in doubt, ask in code review.

---

## Questions?

- About architecture → ask in [architectura.md](architectura.md)
- About a component → find its `spec_*.md`
- About how to contribute → read [DOCS_STYLE_GUIDE.md](DOCS_STYLE_GUIDE.md)
