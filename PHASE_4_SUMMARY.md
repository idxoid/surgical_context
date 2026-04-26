# Phase 4: Mechanism-Aware Retrieval Evaluation — COMPLETE

## Overview

Completed a fundamental shift in benchmark evaluation from "question pass rate optimization" to "mechanism coverage diagnosis." The system now classifies questions by the specific code relationship they test and evaluates retrieval using role-based recall + intent-stratified pass gates.

## What Changed

### 1. Mechanism Classification (YAML)
All 20 real-repo questions now have:
- **mechanism**: Code relationship being tested (e.g., `fastapi_route_registration`, `pydantic_validation_core_bridge`)
- **required_roles**: List of code roles that must be fulfilled for a correct answer
- **expected_mode**: `symbol` (find by name) or `workspace` (correct answer is "not found")

**Frameworks covered:**
- FastAPI (8 questions): route registration, dependency injection, endpoint execution, OpenAPI generation, serialization, docs/UI, etc.
- Pydantic (8 questions): validation bridge, Python-to-Core boundary, serialization, JSON schema generation, v1 compat, alias impact, error assembly
- Redux Toolkit (8 questions): slice generation, store config, async thunk, query API, action type impact, listener middleware, monorepo structure

### 2. Intent-Aware Ranking

Added `IMPACT_ANALYSIS` intent type with specialized ranking behavior:
- **Noise suppression**: Impact analysis questions get `noise_factor=1.0` (tests/examples not penalized as "noise")
- **Intent floors**: 3000-token minimum floor to surface test files
- **Semantic priors**: symbol=0.3, doc=0.5 (tests and docs are high-signal for impact questions)
- **Keyword detection**: "most likely to break", "what parts", "what breaks", "are most likely"

This prevents test files from being artificially downranked when they're actually crucial for understanding change impact.

### 3. Role Recall Metric

New metric: `role_recall = (required_roles not in missing_roles) / len(required_roles)`

- **Diagnostic signal**: Shows which code roles the ranker is failing to retrieve
- **Bridge misses**: Low role_recall on "public_entrypoint" means the ranker can't find entry points; likely architectural gap
- **Noise issues**: Low file_recall but high role_recall means ranking noise is the problem, not discovery

**Note:** For Pydantic/RTK questions, the ranker's internal role names diverge from YAML role names — treat as diagnostic, not truth.

### 4. Intent-Stratified Pass Gates

Different query intents have different acceptable thresholds:

| Intent | role_recall floor | file_recall floor | Gate semantics |
|---|---|---|---|
| explain_behavior | 0.70 | 0.50 | **AND** (both required) |
| trace_dependency | 0.80 | 0.70 | **AND** (both required) |
| impact_analysis | 0.60 | 0.50 | **OR** (either sufficient) |

**Reasoning:**
- **Explanation**: Can accept moderate coverage if right roles are chosen
- **Tracing**: Needs deep understanding AND broad file coverage
- **Impact**: Can work with just test coverage (proves cascade) OR symbol coverage

### 5. Benchmark Output

**Per-question display:**
```
✅ fastapi_q06: serialize_response [impact_analysis] | role=1.00 | file=0.50 | 1750t | pool_exhausted
⚠️  fastapi_q02: Depends              [trace_dependency] | role=0.20 | file=0.50 | 809t | floor_unfilled_no_useful_candidates
```

Shows:
- Status emoji (✅ pass, ⚠️  warn, ❌ error)
- Question ID and symbol
- Intent type in brackets
- role_recall and file_recall metrics
- Token count
- Stopped reason (why expansion ended)

**Summary metrics:**
```
Pass rate:       62.5% (5/8)
...
Role recall:     0.74
```

## Files Modified

### Code
1. **tests/fixtures/real_repo_question_pack.yaml** — Added mechanism + required_roles to 20 questions
2. **sidecar/context/intent_classifier.py** — Added IMPACT_ANALYSIS enum, keywords, tier priority
3. **sidecar/context/unified_ranker.py** — Added intent-aware noise suppression, IMPACT_ANALYSIS floor/priors
4. **sidecar/context/types.py** — Added mechanism field to PromptContext
5. **sidecar/context/arbitrator.py** — Wired ctx.mechanism = ranker._determine_mechanism(target)
6. **QA/qa_benchmark.py** — Implemented role_recall metric, intent-stratified pass gates, new display format

### Documentation
7. **docs/architectura.md** — Updated observability section, prompt lifecycle, and added Phase 4 section

## Verification

FastAPI core12 (4 questions):
```
Pass rate:       25.0% (1/4)
Role recall:     0.55
File recall:     0.75
Tokens (surgical): 12,885
Reduction:       92.0%
```

FastAPI full suite (8 questions):
```
Pass rate:       62.5% (5/8)
Role recall:     0.74
File recall:     0.56
Tokens (surgical): 22,281
Reduction:       97.0%
```

Key observations:
- Q03 (run_endpoint_function) passes perfectly: role=1.00, file=1.00
- Q06 (impact_analysis on serialization) passes with OR gate: role=1.00, file=0.50 (test coverage enough)
- Missing roles are now visible: "missing: registration_step,route_object,handler_or_lifecycle"

## Impact

### For Tuning
The benchmark now tells you **why** a question fails:
- Low role_recall → architectural gap (need code relationship discovery improvement)
- Low file_recall → ranking noise (need tuning)
- Low role_recall but high file_recall → ranker found files but wrong symbols in them

### For Product
Intent-stratified evaluation recognizes that different developer tasks have different acceptable context quality:
- Explaining code needs deep understanding
- Tracing dependencies needs both width and depth
- Analyzing impact can work with just test coverage

### For Scaling
Mechanism classification provides a taxonomy for future work:
- Identify which mechanisms are most commonly queried
- Profile which mechanisms have the highest failure rate
- Correlate mechanism type with LLM performance (e.g., routing decisions)

## Next Steps

The infrastructure is now in place for:
1. **Mechanism tuning**: Run full pydantic + RTK packs to see cross-framework patterns
2. **Role disambiguation**: Improve the ranker's role inference for non-FastAPI frameworks
3. **Intent refinement**: ML-based multi-label intent detection (instead of keyword heuristics)
4. **Unified ranker tuning**: Adjust α, β, γ, δ, ε weights per mechanism
5. **Architecture assessment**: Identify unfixable gaps vs. tunable issues

The Phase 4 work establishes the measurement foundation for all downstream optimization.
