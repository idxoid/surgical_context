import os

import ollama
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from sidecar.context.arbitrator import ContextArbitrator
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
    from sidecar.context.arbitrator import DocChunk
    db = get_db()
    try:
        arb = ContextArbitrator(db, overlay)
        ctx = arb.get_context_for_symbol(req.symbol, token_budget=req.token_budget)
        if isinstance(ctx, str):
            raise HTTPException(status_code=404, detail=ctx)

        raw_chunks = vector_db.search(f"{req.symbol} {req.question}", limit=3)
        ctx.documentation = [
            DocChunk(
                source_file=d["file_path"],
                chunk_id=f"{d['file_path']}::search",
                content=d["chunk"],
            )
            for d in raw_chunks
        ]

        response = ollama.chat(model=OLLAMA_MODEL, messages=[
            {"role": "system", "content": f"You are a Surgical Code Assistant. Use ONLY the provided context.\n\n{ctx.to_system_prompt()}"},
            {"role": "user", "content": req.question},
        ])
        return {
            "symbol": req.symbol,
            "answer": response["message"]["content"],
            "context": ctx.to_dict(),
        }
    finally:
        db.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
