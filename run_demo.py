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
      python run_demo.py --symbol ContextArbitrator --question "How does dirty state work?"

  Symbol only — question asked interactively:
      python run_demo.py --symbol ContextArbitrator

  Question only — symbol asked interactively:
      python run_demo.py --question "How does dirty state work?"

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
from sidecar.silence import install as _silence; _silence()

ROOT          = os.path.dirname(os.path.abspath(__file__))
LANCEDB_PATH  = os.path.join(ROOT, "data", "lancedb")
NEO4J_URI     = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
NEO4J_USER    = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")
OLLAMA_MODEL  = os.getenv("OLLAMA_MODEL",   "llama3")

SEP  = "=" * 70
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

    from sidecar.database.neo4j_client import Neo4jClient
    db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    with db.driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
    db.close()
    print("  Neo4j graph cleared.")


def index_code():
    from sidecar.indexer.code import run_indexing
    for rel in ["sidecar"]:
        abs_p = os.path.join(ROOT, rel)
        if os.path.isdir(abs_p):
            print(f"  Indexing code: {abs_p}")
            run_indexing(abs_p)
        else:
            print(f"  Skipping (not found): {abs_p}")


def index_docs():
    from sidecar.indexer.docs import index_docs as _index_docs
    abs_p = os.path.join(ROOT, "docs")
    print(f"  Indexing docs: {abs_p}")
    _index_docs(abs_p)


def assemble_and_ask(symbol: str, question: str, no_llm: bool, fmt: str = "json"):
    from sidecar.context.arbitrator import ContextArbitrator, DocChunk
    from sidecar.database.lancedb_client import LanceDBClient
    from sidecar.database.neo4j_client import Neo4jClient

    db    = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    lance = LanceDBClient()

    try:
        arb = ContextArbitrator(db)
        ctx = arb.get_context_for_symbol(symbol)
        if isinstance(ctx, str):
            print(f"\n  ERROR: {ctx}")
            return

        raw_chunks = lance.search(f"{symbol} {question}", limit=3)
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
        "You are a Surgical Code Assistant. Use ONLY the provided context.\n\n"
        + context_body
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
    response = ollama.chat(model=OLLAMA_MODEL, messages=[
        {"role": "system", "content": system_msg},
        {"role": "user",   "content": question},
    ])
    print(response["message"]["content"])
    print(f"\n{SEP}")


# ── main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Surgical Context demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--symbol",   default="", help="Target symbol name")
    parser.add_argument("--question", default="", help="Question to ask")
    parser.add_argument("--no-index", action="store_true", help="Skip clean + index (DBs already populated)")
    parser.add_argument("--no-llm",   action="store_true", help="Print assembled prompt only, skip LLM call")
    parser.add_argument("--loop",     action="store_true", help="Keep asking questions without re-indexing")
    parser.add_argument("--fmt",      choices=["json", "text"], default="json", help="Prompt format sent to LLM (default: json)")
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
            symbol   = args.symbol   or _prompt("Symbol",   "ContextArbitrator")
            question = args.question or _prompt("Question", "How does dirty state work?")
            first = False
        else:
            print(f"\n{SEP2}")
            symbol   = _prompt("Symbol   (Enter to keep previous)", symbol)
            question = _prompt("Question")
            if not question:
                break

        print()
        assemble_and_ask(symbol, question, args.no_llm, args.fmt)

        if not args.loop:
            break


if __name__ == "__main__":
    main()
