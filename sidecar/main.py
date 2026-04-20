import os

import ollama
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from sidecar.context.arbitrator import ContextArbitrator
from sidecar.context.doc_resolver import DocResolver
from sidecar.context.overlay import InMemoryOverlay
from sidecar.database.lancedb_client import LanceDBClient
from sidecar.database.neo4j_client import Neo4jClient

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")

app = FastAPI(title="Surgical Context Sidecar")
overlay = InMemoryOverlay()
vector_db = LanceDBClient()


def get_db() -> Neo4jClient:
    return Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)


class IndexRequest(BaseModel):
    project_path: str


class IndexFileRequest(BaseModel):
    file_path: str


class IndexDocsRequest(BaseModel):
    docs_path: str


class AskRequest(BaseModel):
    symbol: str
    question: str = "What does this code do?"
    token_budget: int = 4000


class OverlayRequest(BaseModel):
    file_path: str
    content: str


class SearchRequest(BaseModel):
    query: str
    limit: int = 5


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/index")
def index(req: IndexRequest):
    if not os.path.isdir(req.project_path):
        raise HTTPException(status_code=400, detail=f"Path not found: {req.project_path}")

    from sidecar.indexer.code import run_indexing

    run_indexing(req.project_path)
    return {"status": "indexed", "path": req.project_path}


@app.post("/index/file")
def index_file_endpoint(req: IndexFileRequest):
    if not os.path.isfile(req.file_path):
        raise HTTPException(status_code=400, detail=f"File not found: {req.file_path}")

    from sidecar.indexer.anchor import resolve_pending_anchors
    from sidecar.indexer.code import index_file
    from sidecar.parser.extractor import SymbolExtractor

    db = get_db()
    try:
        db.delete_symbols_for_file(req.file_path)
        index_file(req.file_path, db, vector_db, SymbolExtractor())
        resolve_pending_anchors(db, vector_db)
    finally:
        db.close()
    return {"status": "indexed", "file_path": req.file_path}


@app.post("/index/docs")
def index_docs_endpoint(req: IndexDocsRequest):
    if not os.path.isdir(req.docs_path):
        raise HTTPException(status_code=400, detail=f"Path not found: {req.docs_path}")

    from sidecar.indexer.docs import index_docs

    index_docs(req.docs_path)
    return {"status": "indexed", "path": req.docs_path}


@app.post("/search")
def search(req: SearchRequest):
    return {"results": vector_db.search(req.query, req.limit)}


@app.post("/overlay")
def update_overlay(req: OverlayRequest):
    overlay.update(req.file_path, req.content)
    symbols = overlay.get_symbols(req.file_path)
    return {"file_path": req.file_path, "symbols": list(symbols.keys())}


@app.delete("/overlay")
def clear_overlay(file_path: str):
    overlay.clear(file_path)
    return {"cleared": file_path}


@app.post("/ask")
def ask(req: AskRequest):
    db = get_db()
    try:
        arb = ContextArbitrator(db, overlay)
        ctx = arb.get_context_for_symbol(req.symbol, token_budget=req.token_budget)
        if isinstance(ctx, str):
            raise HTTPException(status_code=404, detail=ctx)

        doc_resolver = DocResolver(vector_db)
        ctx.documentation = doc_resolver.search(f"{req.symbol} {req.question}", limit=3)

        response = ollama.chat(
            model=OLLAMA_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": f"You are a Surgical Code Assistant. Use ONLY the provided context.\n\n{ctx.to_system_prompt()}",
                },
                {"role": "user", "content": req.question},
            ],
        )
        return {
            "symbol": req.symbol,
            "answer": response["message"]["content"],
            "context": ctx.to_dict(),
        }
    finally:
        db.close()


@app.get("/impact")
def impact(symbol: str):
    """Return downstream dependents affected by a change to the given symbol."""
    db = get_db()
    try:
        from sidecar.indexer.affects import AFFECTSIndexer

        # Look up symbol UID by name
        query = "MATCH (s:Symbol {name: $name}) RETURN s.uid AS uid LIMIT 1"
        with db.driver.session() as session:
            result = session.run(query, name=symbol).single()

        if not result:
            raise HTTPException(status_code=404, detail=f"Symbol '{symbol}' not found")

        symbol_uid = result["uid"]

        # Get affected symbols
        indexer = AFFECTSIndexer(db)
        affected_symbols = indexer.get_affected_symbols(symbol_uid)

        # Get file containing the symbol
        query = """
        MATCH (s:Symbol {uid: $uid})
        OPTIONAL MATCH (f:File)-[:CONTAINS]->(s)
        RETURN coalesce(f.path, '<unknown>') AS file_path
        """
        with db.driver.session() as session:
            result = session.run(query, uid=symbol_uid).single()

        symbol_file = result["file_path"] if result else "<unknown>"

        # Get affected files
        if symbol_file != "<unknown>":
            affected_files = indexer.get_affected_files(symbol_file)
        else:
            affected_files = []

        return {
            "symbol": symbol,
            "symbol_uid": symbol_uid,
            "file_path": symbol_file,
            "affected_symbols": affected_symbols,
            "affected_files": affected_files,
            "affected_count": len(affected_symbols),
            "affected_file_count": len(affected_files),
            "max_depth": AFFECTSIndexer.MAX_AFFECTS_DEPTH,
        }
    finally:
        db.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
