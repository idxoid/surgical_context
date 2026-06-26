#!/usr/bin/env python3
"""
Surgical Context — interactive demo.

Modes
-----
  Full pipeline (clean → index → ask):
      python run_demo.py

  Skip re-indexing (DBs already populated):
      python run_demo.py --no-index

  Non-interactive, symbol + question supplied:
      python run_demo.py --symbol run_axis_retrieval --question "How does the axis retrieval pipeline assemble context?"

  Symbol only — question asked interactively:
      python run_demo.py --symbol run_axis_retrieval

  Question only — symbol asked interactively:
      python run_demo.py --question "How does the axis retrieval pipeline assemble context?"

  Print assembled prompt, skip LLM:
      python run_demo.py --no-llm
Lets
  Loop: ask multiple questions without re-indexing:
      python run_demo.py --no-index --loop
"""

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from context_engine.database.neo4j_env import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER
from context_engine.silence import install as _silence

_silence()

ROOT = os.path.dirname(os.path.abspath(__file__))
LANCEDB_PATH = os.path.join(ROOT, "data", "lancedb")

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")
WORKSPACE_ID = os.getenv("DEFAULT_WORKSPACE_ID", "local/surgical_context@main")
DEFAULT_SYMBOL = "run_axis_retrieval"
DEFAULT_QUESTION = "How does the axis retrieval pipeline assemble context?"

os.environ.setdefault("LANCEDB_PATH", LANCEDB_PATH)
os.environ.setdefault("INDEX_PROFILE", "axis_python_v1")

SEP = "=" * 70
SEP2 = "-" * 70


# ── helpers ───────────────────────────────────────────────────────────────


def _prompt(label: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        val = input(f"  {label}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val or default


# ── pipeline steps ────────────────────────────────────────────────────────


def clean_dbs():
    if os.path.exists(LANCEDB_PATH):
        shutil.rmtree(LANCEDB_PATH)
    os.makedirs(LANCEDB_PATH, exist_ok=True)
    print("  LanceDB cleared.")

    from context_engine.database.neo4j_client import Neo4jClient

    db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    with db.driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
    db.close()
    print("  Neo4j graph cleared.")


def index_code():
    from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE
    from context_engine.indexer.fast import run_fast_indexing

    for rel in ["context_engine"]:
        abs_p = os.path.join(ROOT, rel)
        if os.path.isdir(abs_p):
            print(f"  Indexing code: {abs_p}")
            run_fast_indexing(
                abs_p,
                workspace_id=WORKSPACE_ID,
                index_profile=AXIS_PYTHON_V1_PROFILE,
            )
        else:
            print(f"  Skipping (not found): {abs_p}")


def index_docs():
    from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE, resolve_index_profile
    from context_engine.indexer.docs import index_docs as _index_docs

    abs_p = os.path.join(ROOT, "docs")
    workspace_id = resolve_index_profile(AXIS_PYTHON_V1_PROFILE).workspace_id(WORKSPACE_ID)
    print(f"  Indexing docs: {abs_p}")
    _index_docs(abs_p, workspace_id=workspace_id)


def assemble_and_ask(symbol: str, question: str, no_llm: bool, fmt: str = "json"):
    from context_engine.axis.pipeline import AxisRetrievalConfig, run_axis_retrieval
    from context_engine.axis.prompt_provider import axis_bundles_to_prompt_context
    from context_engine.context_types import DocChunk
    from context_engine.database.lancedb_client import LanceDBClient
    from context_engine.database.neo4j_client import Neo4jClient
    from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE, resolve_index_profile

    db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    lance = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)
    workspace_id = resolve_index_profile(AXIS_PYTHON_V1_PROFILE).workspace_id(WORKSPACE_ID)
    retrieval_question = f"{symbol}: {question}" if symbol else question

    try:
        result = run_axis_retrieval(
            retrieval_question,
            workspace_id=workspace_id,
            db=db,
            lance=lance,
            config=AxisRetrievalConfig(with_context=True, intent_budget=True),
        )
        intent = ", ".join(match.role for match in result.intent)
        ctx = axis_bundles_to_prompt_context(
            result.bundles,
            workspace_id=workspace_id,
            intent=intent,
            render_mode=result.render_mode,
        )
        if ctx is None:
            print("\n  ERROR: axis retrieval returned no renderable context.")
            return

        raw_chunks = lance.search(retrieval_question, limit=3, workspace_id=workspace_id)
        ctx.documentation = [
            DocChunk(
                source_file=d["file_path"],
                chunk_id=f"{d['file_path']}::search",
                content=d["chunk"],
            )
            for d in raw_chunks
        ]
    finally:
        db.close()

    import json

    if fmt == "json":
        context_body = json.dumps(ctx.to_dict(), indent=2)
        label = "CONTEXT  (JSON)"
    else:
        context_body = ctx.to_system_prompt()
        label = "CONTEXT  (text)"

    system_msg = (
        "You are a Surgical Code Assistant. Use ONLY the provided context.\n\n" + context_body
    )

    print(f"\n{SEP}")
    print(label)
    print(SEP)
    print(context_body)

    print(f"\n{SEP}")
    print("QUESTION")
    print(SEP)
    print(question)

    if no_llm:
        return

    import ollama

    print(f"\n{SEP}")
    print(f"LLM ANSWER  ({OLLAMA_MODEL})")
    print(SEP)
    response = ollama.chat(
        model=OLLAMA_MODEL,
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": question},
        ],
    )
    print(response["message"]["content"])
    print(f"\n{SEP}")


# ── main ──────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Surgical Context demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--symbol", default="", help="Target symbol name")
    parser.add_argument("--question", default="", help="Question to ask")
    parser.add_argument(
        "--no-index", action="store_true", help="Skip clean + index (DBs already populated)"
    )
    parser.add_argument(
        "--no-llm", action="store_true", help="Print assembled prompt only, skip LLM call"
    )
    parser.add_argument(
        "--loop", action="store_true", help="Keep asking questions without re-indexing"
    )
    parser.add_argument(
        "--fmt",
        choices=["json", "text"],
        default="json",
        help="Prompt format sent to LLM (default: json)",
    )
    args = parser.parse_args()

    print(SEP)
    print("SURGICAL CONTEXT DEMO")
    print(SEP)

    if not args.no_index:
        print("\n[1/2] Setting up databases...")
        clean_dbs()
        print("\n[2/2] Indexing...")
        index_code()
        index_docs()
        print(f"\n{SEP2}")

    first = True
    while True:
        if first:
            symbol = args.symbol or _prompt("Symbol", DEFAULT_SYMBOL)
            question = args.question or _prompt("Question", DEFAULT_QUESTION)
            first = False
        else:
            print(f"\n{SEP2}")
            symbol = _prompt("Symbol   (Enter to keep previous)", symbol)
            question = _prompt("Question")
            if not question:
                break

        print()
        assemble_and_ask(symbol, question, args.no_llm, args.fmt)

        if not args.loop:
            break


if __name__ == "__main__":
    main()
