import logging
import os

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from sidecar.ai.engine import AIEngine
from sidecar.auth import AuditLog, UserAuth
from sidecar.context.arbitrator import ContextArbitrator
from sidecar.context.overlay import InMemoryOverlay
from sidecar.database.lancedb_client import LanceDBClient
from sidecar.database.session import db_session

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL_PREFERENCE = os.getenv("MODEL_PREFERENCE", "auto")  # "claude" | "ollama" | "auto"

app = FastAPI(title="Surgical Context Sidecar")
overlay = InMemoryOverlay()
vector_db = LanceDBClient()
ai_engine = AIEngine(model_preference=MODEL_PREFERENCE)
user_auth = UserAuth()
audit_log = AuditLog()


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

    with db_session() as db:
        db.delete_symbols_for_file(req.file_path)
        index_file(req.file_path, db, vector_db, SymbolExtractor())
        resolve_pending_anchors(db, vector_db)
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
def ask(req: AskRequest, x_user_id: str = Header(None)):
    """Ask about a symbol (with multi-user audit logging)."""
    user_id = user_auth.identify_user(x_user_id)
    with db_session(user_id=user_id) as db:
        arb = ContextArbitrator(db, overlay, vector_db)
        ctx = arb.get_context_for_symbol(req.symbol, question=req.question, token_budget=req.token_budget)
        if isinstance(ctx, str):
            audit_log.log_error(user_id, "query", ctx)
            raise HTTPException(status_code=404, detail=ctx)

        system_prompt = f"You are a Surgical Code Assistant. Use ONLY the provided context.\n\n{ctx.to_system_prompt()}"

        # Use AIEngine to route between Claude and Ollama based on context size and intent
        answer = ai_engine.chat(
            system_prompt=system_prompt,
            user_message=req.question,
            token_count=ctx.token_count(),
            intent=ctx.intent,
        )

        # Log query action
        audit_log.log_query(user_id, req.symbol, req.question, ctx.intent, ctx.mode)

        return {
            "symbol": req.symbol,
            "answer": answer,
            "context": ctx.to_dict(),
            "user": user_id,
            "cloud": db.is_cloud(),
        }


@app.post("/ask/stream")
def ask_stream(req: AskRequest, x_user_id: str = Header(None)):
    """Streaming version of /ask endpoint (SSE)."""
    user_id = user_auth.identify_user(x_user_id)

    def response_generator():
        with db_session(user_id=user_id) as db:
            arb = ContextArbitrator(db, overlay, vector_db)
            ctx = arb.get_context_for_symbol(req.symbol, question=req.question, token_budget=req.token_budget)
            if isinstance(ctx, str):
                yield f"data: {{'error': '{ctx}'}}\n\n"
                return

            system_prompt = f"You are a Surgical Code Assistant. Use ONLY the provided context.\n\n{ctx.to_system_prompt()}"

            # Stream response chunks
            for chunk in ai_engine.stream_chat(
                system_prompt=system_prompt,
                user_message=req.question,
                token_count=ctx.token_count(),
                intent=ctx.intent,
            ):
                # SSE format: "data: {content}\n\n"
                yield f"data: {chunk}\n\n"

            # Send context metadata at end
            yield f"data: [CONTEXT]\n{ctx.to_dict()}\n\n"

    return StreamingResponse(response_generator(), media_type="text/event-stream")


@app.get("/impact")
def impact(symbol: str):
    """Return downstream dependents affected by a change to the given symbol."""
    with db_session() as db:
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


@app.post("/auth/token")
def auth_token(user_id: str = None):
    """Generate JWT token for multi-user mode."""
    user_id = user_auth.identify_user(user_id)
    token = user_auth.generate_token(user_id)
    logger.info(f"✅ Token issued for user: {user_id}")
    return {"token": token, "user_id": user_id, "expires_in_hours": 24}


@app.get("/auth/users")
def list_users():
    """List all active users."""
    return {"users": user_auth.list_users()}


@app.get("/status/cloud")
def cloud_status():
    """Get cloud (Aura) connection status."""
    with db_session() as db:
        health = db.health_check()
        return {
            "cloud_enabled": True,
            "using_aura": db.is_cloud(),
            "using_fallback": db.is_fallback(),
            "health": health,
        }


@app.get("/audit/actions")
def audit_actions(user_id: str = None, limit: int = 100):
    """Get recent audit log entries."""
    actions = audit_log.get_recent_actions(user_id=user_id, limit=limit)
    return {"actions": actions, "total": len(actions)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
