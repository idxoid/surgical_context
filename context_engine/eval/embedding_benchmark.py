#!/usr/bin/env python3
"""Compare embedding models on the golden question set.

This benchmark intentionally runs in memory: it extracts symbols from the
fixture project, embeds each symbol's production indexing text, embeds the
questions, and ranks symbols by cosine similarity.
"""

import argparse
import json
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_PROJECT = ROOT / "context_engine" / "axis"
DEFAULT_MODELS = ("all-MiniLM-L6-v2", "microsoft/unixcoder-base")


class Embedder(Protocol):
    def encode(
        self, texts: list[str], show_progress_bar: bool = False
    ) -> Iterable[Iterable[float]]:
        """Return one vector per input text."""


@dataclass(frozen=True)
class SymbolRecord:
    uid: str
    name: str
    kind: str
    file_path: str
    start_line: int
    end_line: int
    code: str
    qualified_name: str = ""


@dataclass(frozen=True)
class QuestionRecord:
    id: str
    symbol: str
    question: str
    expected_symbols: tuple[str, ...]
    difficulty: str = "unknown"
    intent: str = "unknown"


@dataclass(frozen=True)
class RankedSymbol:
    rank: int
    score: float
    symbol: SymbolRecord


def default_encoder_factory(model_name: str) -> Embedder:
    from sentence_transformers import SentenceTransformer

    return cast(Embedder, SentenceTransformer(model_name))


def load_questions(questions_path: str | Path) -> list[QuestionRecord]:
    with Path(questions_path).open(encoding="utf-8") as handle:
        rows = yaml.safe_load(handle) or []

    questions = []
    for row in rows:
        questions.append(
            QuestionRecord(
                id=str(row["id"]),
                symbol=str(row["symbol"]),
                question=str(row["question"]),
                expected_symbols=tuple(str(name) for name in row.get("expected_symbols", [])),
                difficulty=str(row.get("difficulty", "unknown")),
                intent=str(row.get("intent", "unknown")),
            )
        )
    return questions


def collect_symbols(project_path: str | Path) -> list[SymbolRecord]:
    from context_engine.parser.extractor import SymbolExtractor

    root = Path(project_path)
    extractor = SymbolExtractor()
    records: list[SymbolRecord] = []

    for file_path in sorted(path for path in root.rglob("*") if path.is_file()):
        try:
            symbols = extractor.extract(str(file_path))
        except Exception:
            continue

        try:
            source = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        lines = source.splitlines()
        for symbol in symbols:
            code = "\n".join(lines[symbol.start_line - 1 : symbol.end_line])
            records.append(
                SymbolRecord(
                    uid=symbol.uid,
                    name=symbol.name,
                    kind=symbol.kind,
                    file_path=file_path.relative_to(root).as_posix(),
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    code=code,
                    qualified_name=symbol.qualified_name,
                )
            )

    return records


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("Vectors must have the same dimension")

    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    if left_norm == 0 or right_norm == 0:
        return 0.0

    dot = sum(a * b for a, b in zip(left, right, strict=False))
    return float(dot / (left_norm * right_norm))


def encode_texts(encoder: Embedder, texts: Iterable[str]) -> list[list[float]]:
    inputs = list(texts)
    if not inputs:
        return []

    try:
        encoded = encoder.encode(inputs, show_progress_bar=False)
    except TypeError:
        encoded = encoder.encode(inputs)

    if hasattr(encoded, "tolist"):
        encoded = encoded.tolist()

    return [[float(value) for value in row] for row in encoded]


def rank_symbols(
    query_vector: list[float],
    symbols: list[SymbolRecord],
    symbol_vectors: list[list[float]],
) -> list[RankedSymbol]:
    scored = [
        (cosine_similarity(query_vector, symbol_vector), symbol)
        for symbol, symbol_vector in zip(symbols, symbol_vectors, strict=False)
    ]
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        RankedSymbol(rank=index + 1, score=score, symbol=symbol)
        for index, (score, symbol) in enumerate(scored)
    ]


def evaluate_model(
    model_name: str,
    symbols: list[SymbolRecord],
    questions: list[QuestionRecord],
    top_k: int,
    encoder_factory: Callable[[str], Embedder] = default_encoder_factory,
) -> dict:
    started = time.perf_counter()

    try:
        encoder = encoder_factory(model_name)
        symbol_vectors = encode_texts(encoder, (symbol.code for symbol in symbols))
        question_vectors = encode_texts(encoder, (question.question for question in questions))
    except Exception as exc:
        return {
            "model": model_name,
            "status": "unavailable",
            "error": str(exc),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    question_results = []
    target_hits = 0
    reciprocal_rank_total = 0.0
    expected_recall_total = 0.0
    expected_precision_total = 0.0

    for question, question_vector in zip(questions, question_vectors, strict=False):
        ranking = rank_symbols(question_vector, symbols, symbol_vectors)
        top = ranking[:top_k]
        top_names = [candidate.symbol.name for candidate in top]
        expected = set(question.expected_symbols)
        hits = set(top_names) & expected
        target_rank = next(
            (candidate.rank for candidate in ranking if candidate.symbol.name == question.symbol),
            None,
        )
        target_hit = target_rank is not None and target_rank <= top_k

        target_hits += int(target_hit)
        reciprocal_rank_total += 0.0 if target_rank is None else 1.0 / target_rank
        expected_recall_total += len(hits) / len(expected) if expected else 0.0
        expected_precision_total += len(hits) / len(top) if top else 0.0

        question_results.append(
            {
                "id": question.id,
                "symbol": question.symbol,
                "question": question.question,
                "difficulty": question.difficulty,
                "intent": question.intent,
                "target_rank": target_rank,
                "target_hit_at_k": target_hit,
                "expected_recall_at_k": len(hits) / len(expected) if expected else 0.0,
                "expected_precision_at_k": len(hits) / len(top) if top else 0.0,
                "expected_symbols": sorted(expected),
                "top_symbols": [
                    {
                        "rank": candidate.rank,
                        "name": candidate.symbol.name,
                        "uid": candidate.symbol.uid,
                        "kind": candidate.symbol.kind,
                        "file_path": candidate.symbol.file_path,
                        "score": candidate.score,
                    }
                    for candidate in top
                ],
            }
        )

    question_count = len(question_results)
    return {
        "model": model_name,
        "status": "ok",
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "summary": {
            "question_count": question_count,
            "symbol_count": len(symbols),
            "top_k": top_k,
            "target_hit_rate_at_k": target_hits / question_count if question_count else 0.0,
            "mrr": reciprocal_rank_total / question_count if question_count else 0.0,
            "expected_recall_at_k": expected_recall_total / question_count
            if question_count
            else 0.0,
            "expected_precision_at_k": expected_precision_total / question_count
            if question_count
            else 0.0,
        },
        "questions": question_results,
    }


def run_benchmark(
    questions_path: str | Path,
    project_path: str | Path = DEFAULT_PROJECT,
    models: Iterable[str] = DEFAULT_MODELS,
    top_k: int = 5,
    encoder_factory: Callable[[str], Embedder] = default_encoder_factory,
) -> dict:
    symbols = collect_symbols(project_path)
    questions = load_questions(questions_path)
    model_names = [model for model in models if model]

    return {
        "timestamp": time.time(),
        "project_path": str(project_path),
        "questions_path": str(questions_path),
        "top_k": top_k,
        "symbol_count": len(symbols),
        "question_count": len(questions),
        "models": [
            evaluate_model(model, symbols, questions, top_k, encoder_factory)
            for model in model_names
        ],
    }


def parse_models(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def print_report(metrics: dict) -> None:
    print("Embedding Benchmark")
    print(f"Project:   {metrics['project_path']}")
    print(f"Questions: {metrics['question_count']}")
    print(f"Symbols:   {metrics['symbol_count']}")
    print(f"Top K:     {metrics['top_k']}")
    print()

    for model_result in metrics["models"]:
        model = model_result["model"]
        status = model_result["status"]
        if status != "ok":
            print(f"{model}: {status}")
            print(f"  {model_result.get('error', 'unknown error')}")
            continue

        summary = model_result["summary"]
        print(f"{model}:")
        print(f"  target_hit@{summary['top_k']}: {summary['target_hit_rate_at_k']:.2f}")
        print(f"  mrr:           {summary['mrr']:.2f}")
        print(f"  expected_recall@{summary['top_k']}:    {summary['expected_recall_at_k']:.2f}")
        print(f"  expected_precision@{summary['top_k']}: {summary['expected_precision_at_k']:.2f}")
        print(f"  elapsed_ms:    {model_result['elapsed_ms']:.1f}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare embedding models on golden questions")
    parser.add_argument("--project", default=str(DEFAULT_PROJECT), help="Project path to scan")
    parser.add_argument("--questions", required=True, help="Path to questions YAML (list format)")
    parser.add_argument(
        "--models",
        default=",".join(DEFAULT_MODELS),
        help="Comma-separated SentenceTransformer model names",
    )
    parser.add_argument(
        "--top-k", type=int, default=5, help="Number of symbols to rank per question"
    )
    parser.add_argument("--report", default=None, help="Write JSON metrics to this path")
    parser.add_argument("--json", action="store_true", help="Print JSON metrics only")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any requested model is unavailable",
    )
    args = parser.parse_args()

    metrics = run_benchmark(
        questions_path=args.questions,
        project_path=args.project,
        models=parse_models(args.models),
        top_k=max(1, args.top_k),
    )

    if args.report:
        Path(args.report).write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(metrics, indent=2))
    else:
        print_report(metrics)

    ok_count = sum(1 for result in metrics["models"] if result["status"] == "ok")
    if args.strict and ok_count != len(metrics["models"]):
        return 1
    return 0 if ok_count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
