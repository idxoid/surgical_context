import os

import lancedb
import pyarrow as pa
from sentence_transformers import SentenceTransformer

DB_PATH = os.getenv("LANCEDB_PATH", "./data/lancedb")
EMBED_MODEL = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")
DOCS_TABLE = "docs"
SYMBOLS_TABLE = "symbols"

DOCS_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("file_path", pa.string()),
    pa.field("chunk", pa.string()),
    pa.field("pending", pa.list_(pa.string())),
    pa.field("vector", pa.list_(pa.float32(), 384)),
])

SYMBOLS_SCHEMA = pa.schema([
    pa.field("uid", pa.string()),
    pa.field("name", pa.string()),
    pa.field("file_path", pa.string()),
    pa.field("code", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), 384)),
])


class LanceDBClient:
    def __init__(self):
        self._db = lancedb.connect(DB_PATH)
        self._model = SentenceTransformer(EMBED_MODEL)
        if DOCS_TABLE not in self._db.table_names():
            self._table = self._db.create_table(DOCS_TABLE, schema=DOCS_SCHEMA)
        else:
            self._table = self._db.open_table(DOCS_TABLE)
        if SYMBOLS_TABLE not in self._db.table_names():
            self._sym_table = self._db.create_table(SYMBOLS_TABLE, schema=SYMBOLS_SCHEMA)
        else:
            self._sym_table = self._db.open_table(SYMBOLS_TABLE)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(texts, show_progress_bar=False).tolist()

    def upsert_chunks(self, file_path: str, chunks: list[str]):
        vectors = self._embed(chunks)
        rows = [
            {
                "id": f"{file_path}::{i}",
                "file_path": file_path,
                "chunk": chunk,
                "pending": [],
                "vector": vec,
            }
            for i, (chunk, vec) in enumerate(zip(chunks, vectors))
        ]
        try:
            self._table.delete(f"file_path = '{file_path}'")
        except Exception:
            pass
        self._table.add(rows)

    def get_pending(self) -> dict[str, list[str]]:
        """Returns {chunk_id: [name, ...]} for all chunks with pending identifiers."""
        df = self._table.to_pandas()
        return {
            row["id"]: list(row["pending"])
            for _, row in df.iterrows()
            if len(row["pending"]) > 0
        }

    def set_pending(self, chunk_id: str, pending: list[str]):
        df = self._table.to_pandas()
        row = df[df["id"] == chunk_id]
        if row.empty:
            return
        row = row.iloc[0]
        try:
            self._table.delete(f"id = '{chunk_id}'")
        except Exception:
            pass
        self._table.add([{
            "id": chunk_id,
            "file_path": row["file_path"],
            "chunk": row["chunk"],
            "pending": pending,
            "vector": row["vector"].tolist(),
        }])

    def search(self, query: str, limit: int = 5) -> list[dict]:
        vec = self._embed([query])[0]
        results = self._table.search(vec).limit(limit).to_list()
        return [{"file_path": r["file_path"], "chunk": r["chunk"]} for r in results]

    def upsert_symbol_embeddings(self, symbols: list[dict]):
        """symbols: list of {uid, name, file_path, code}"""
        if not symbols:
            return
        codes = [s["code"] for s in symbols]
        vectors = self._embed(codes)
        rows = [
            {
                "uid": s["uid"],
                "name": s["name"],
                "file_path": s["file_path"],
                "code": s["code"],
                "vector": vec,
            }
            for s, vec in zip(symbols, vectors)
        ]
        uids = [s["uid"] for s in symbols]
        for uid in uids:
            try:
                self._sym_table.delete(f"uid = '{uid}'")
            except Exception:
                pass
        self._sym_table.add(rows)

    def search_symbols(self, query: str, limit: int = 5, threshold: float = 0.4) -> list[dict]:
        """Returns symbols semantically similar to query, with cosine distance."""
        vec = self._embed([query])[0]
        results = self._sym_table.search(vec).limit(limit).to_list()
        out = []
        for r in results:
            distance = r.get("_distance", 1.0)
            if distance <= threshold:
                out.append({
                    "uid": r["uid"],
                    "name": r["name"],
                    "file_path": r["file_path"],
                    "distance": distance,
                })
        return out
