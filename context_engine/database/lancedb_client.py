import json
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import lancedb
import pyarrow as pa

from context_engine.axis.edge_json import decode_edge_uid_map
from context_engine.axis.query_plan import render_axis_bits_predicate
from context_engine.database.embedding_cache import EmbeddingCache, EmbeddingCacheKey
from context_engine.database.embedding_registry import (
    EmbeddingMetadata,
    EmbeddingModelMismatch,
    compute_chunk_hash,
    compute_embedding_hash,
    get_model_metadata,
)
from context_engine.database.lance_workspace_tables import (
    drop_workspace_partition_table,
    workspace_partition_table_exists,
    workspace_partition_table_name,
    workspace_partitioned_enabled,
)
from context_engine.index_profile import (
    AXIS_PYTHON_V1_PROFILE,
    IndexProfile,
    active_index_profile,
    resolve_index_profile,
)
from context_engine.workspace import DEFAULT_WORKSPACE_ID

if TYPE_CHECKING:
    from context_engine.axis.query_plan import AxisQueryPlan

DB_PATH = os.getenv("LANCEDB_PATH", "./data/lancedb")
EMBED_MODEL = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")
EMBED_CACHE_ENABLED = os.getenv("EMBED_CACHE_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "32"))
EMBED_THROTTLE_MS = int(os.getenv("EMBED_THROTTLE_MS", "0"))
EMBED_LOW_PRIORITY = os.getenv("EMBED_LOW_PRIORITY", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
EMBED_LOW_PRIORITY_THROTTLE_MS = int(os.getenv("EMBED_LOW_PRIORITY_THROTTLE_MS", "25"))
LANCEDB_DELETE_BATCH_SIZE = int(os.getenv("LANCEDB_DELETE_BATCH_SIZE", "256"))
LANCEDB_AXIS_ADJACENCY_PARTIAL_RESET_MAX_RATIO = float(
    os.getenv("LANCEDB_AXIS_ADJACENCY_PARTIAL_RESET_MAX_RATIO", "0.25")
)
# Bulk-replace workspace symbols when upserting this many rows and the batch
# covers most of the workspace (cold/full reindex). Delta updates stay per-uid.
LANCEDB_SYMBOL_BULK_REPLACE_MIN = int(os.getenv("LANCEDB_SYMBOL_BULK_REPLACE_MIN", "512"))
LANCEDB_SYMBOL_BULK_REPLACE_RATIO = float(os.getenv("LANCEDB_SYMBOL_BULK_REPLACE_RATIO", "0.85"))
AXIS_SEMANTIC_CHUNK_INDEX = os.getenv("AXIS_SEMANTIC_CHUNK_INDEX", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AXIS_SEMANTIC_CHUNK_TARGET_LINES = int(os.getenv("AXIS_SEMANTIC_CHUNK_TARGET_LINES", "24"))
AXIS_SEMANTIC_CHUNK_OVERLAP_LINES = int(os.getenv("AXIS_SEMANTIC_CHUNK_OVERLAP_LINES", "4"))
AXIS_SEMANTIC_CHUNK_MIN_SYMBOL_LINES = int(os.getenv("AXIS_SEMANTIC_CHUNK_MIN_SYMBOL_LINES", "10"))
DOCS_TABLE = "docs"
SYMBOLS_TABLE = "symbols"
AXIS_ADJACENCY_TABLE = "axis_adjacency"
AXIS_ADJACENCY_EXTERNAL_TABLE = "axis_adjacency_external"
AXIS_SYMBOL_CHUNKS_SUFFIX = "_semantic_chunks_v1"

_log = logging.getLogger(__name__)
_SHARED_EMBEDDING_MODELS: dict[str, Any] = {}


def _resolve_embed_device() -> str:
    """Pick a device SentenceTransformer can actually run on.

    Recent PyTorch wheels target sm_75+; GPUs like GTX 1050 Ti (sm_61) probe as
    CUDA-available but fail at encode with cudaErrorNoKernelImageForDevice.
    """
    explicit = os.getenv("EMBED_DEVICE", "").strip().lower()
    if explicit in {"cpu", "cuda", "mps"}:
        return explicit
    if os.getenv("CUDA_VISIBLE_DEVICES", "unset") == "":
        return "cpu"
    try:
        import torch

        if not torch.cuda.is_available():
            return "cpu"
        major, _minor = torch.cuda.get_device_capability(0)
        if major < 7:
            name = torch.cuda.get_device_name(0)
            _log.warning(
                "GPU %s (CC %s.%s) is below PyTorch sm_75 support; using CPU for embeddings. "
                "Set EMBED_DEVICE=cuda to force (will fail) or CUDA_VISIBLE_DEVICES= for CPU.",
                name,
                major,
                _minor,
            )
            return "cpu"
        return "cuda"
    except Exception:
        return "cpu"


def _l2_to_score(distance: float) -> float:
    """Map LanceDB L2 distance to a [0, 1] similarity score.

    SentenceTransformer ``all-MiniLM-L6-v2`` produces L2-normalized vectors,
    so for any two unit vectors ``a`` and ``b``:
        ||a - b||² = 2 - 2·cos(a, b)
    LanceDB returns the *non-squared* L2 distance ``d = ||a - b||``, so:
        cos(a, b) = 1 - d² / 2
    Mapping cosine similarity into [0, 1] gives ``(1 + cos) / 2``.

    The previous formula ``max(0, 1 - d)`` cut off at d = 1.0, which throws
    away signal from any moderately similar pair (typical similarities give
    d in the 0.8–1.4 range). The corrected score keeps the ranking smooth.
    """
    cos = 1.0 - (distance * distance) / 2.0
    return max(0.0, min(1.0, (1.0 + cos) / 2.0))


DOCS_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string()),
        pa.field("workspace_id", pa.string()),
        pa.field("file_path", pa.string()),
        pa.field("chunk", pa.string()),
        pa.field("pending", pa.list_(pa.string())),
        pa.field("vector", pa.list_(pa.float32(), 384)),
        pa.field("embedding_metadata", pa.string()),  # JSON serialized
        # Owner symbol uid for in-code docstring anchors — fast Stage-1 seed
        # resolution without a Neo4j COVERS lookup at query time.
        pa.field("owner_uid", pa.string()),
    ]
)

SYMBOLS_SCHEMA = pa.schema(
    [
        pa.field("uid", pa.string()),
        pa.field("workspace_id", pa.string()),
        pa.field("name", pa.string()),
        pa.field("file_path", pa.string()),
        pa.field("code", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), 384)),
        pa.field("embedding_metadata", pa.string()),  # JSON serialized
    ]
)

AXIS_SYMBOLS_SCHEMA = pa.schema(
    [
        *SYMBOLS_SCHEMA,
        pa.field("symbol_kind", pa.string()),
        pa.field("qualified_name", pa.string()),
        pa.field("ast_kind_bits", pa.list_(pa.string())),
        pa.field("cfg_bits", pa.list_(pa.string())),
        pa.field("dfg_bits", pa.list_(pa.string())),
        pa.field("struct_bits", pa.list_(pa.string())),
        pa.field("container_kinds", pa.list_(pa.string())),
        pa.field("axis_evidence_json", pa.string()),
        pa.field("axis_container_kinds_json", pa.string()),
        pa.field("axis_contracts_json", pa.string()),
        pa.field("file_tier", pa.string()),
        # Signature facet: a SECOND embedding of the symbol's header
        # (def/class signature) alone. A large body dilutes the body vector
        # so a signature/API-shaped query (e.g. "the routing options in
        # apply_async") loses the symbol; the signature vector restores it.
        # Retrieval takes the min distance across the two facets, so this
        # only ever ADDS match opportunities — never displaces the body
        # match. Optional column (absent on pre-facet indexes → body-only).
        pa.field("signature_vector", pa.list_(pa.float32(), 384)),
    ]
)

AXIS_SYMBOL_REQUIRED_COLUMNS = {
    "symbol_kind",
    "qualified_name",
    "ast_kind_bits",
    "cfg_bits",
    "dfg_bits",
    "struct_bits",
    "container_kinds",
    "axis_evidence_json",
    "axis_container_kinds_json",
    "axis_contracts_json",
    "file_tier",
}
# ``signature_vector`` is intentionally NOT required: an index built before
# the facet landed keeps working (retrieval falls back to the body vector),
# and the column is added by a backfill or the next reindex — never by a
# destructive table reset on open.


def symbol_signature_text(code: str) -> str:
    """Header-only view of a symbol for the signature-facet embedding.

    For a ``def`` / ``class`` this is the lines through the one that ends the
    signature (the ``:``), decorators included; for a module-level constant
    or expression (no header colon) it is the first non-empty line. The point
    is to embed the high-signal API surface WITHOUT the body that dilutes it.
    """
    lines = code.splitlines()
    header: list[str] = []
    for line in lines:
        header.append(line)
        if line.rstrip().endswith(":"):
            return "\n".join(header)
    for line in lines:
        if line.strip():
            return line.strip()
    return code.strip()


AXIS_ADJACENCY_SCHEMA = pa.schema(
    [
        pa.field("workspace_id", pa.string()),
        pa.field("uid", pa.string()),
        pa.field("name", pa.string()),
        pa.field("file_path", pa.string()),
        pa.field("kind", pa.string()),
        pa.field("out_edges_json", pa.string()),
        pa.field("in_edges_json", pa.string()),
    ]
)

AXIS_ADJACENCY_EXTERNAL_SCHEMA = pa.schema(
    [
        pa.field("workspace_id", pa.string()),
        pa.field("sym_to_ext_json", pa.string()),
        pa.field("ext_to_sym_json", pa.string()),
    ]
)

AXIS_SYMBOL_CHUNKS_SCHEMA = pa.schema(
    [
        pa.field("chunk_uid", pa.string()),
        pa.field("workspace_id", pa.string()),
        pa.field("owner_uid", pa.string()),
        pa.field("name", pa.string()),
        pa.field("qualified_name", pa.string()),
        pa.field("file_path", pa.string()),
        pa.field("chunk_index", pa.int32()),
        pa.field("start_line", pa.int32()),
        pa.field("end_line", pa.int32()),
        pa.field("chunk_text", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), 384)),
        pa.field("embedding_metadata", pa.string()),
    ]
)


def _symbols_schema_for_profile(profile: IndexProfile) -> pa.Schema:
    if profile.name == AXIS_PYTHON_V1_PROFILE:
        return AXIS_SYMBOLS_SCHEMA
    return SYMBOLS_SCHEMA


class LanceDBClient:
    def __init__(self, index_profile: str | IndexProfile | None = None):
        if isinstance(index_profile, IndexProfile):
            self._index_profile = index_profile
        elif index_profile:
            self._index_profile = resolve_index_profile(index_profile)
        else:
            self._index_profile = active_index_profile()
        # Storage opens lazily (see the table properties below): constructing a
        # client touches no Lance connection or tables, so import and test setup
        # stay side-effect free and different profiles can coexist in one process.
        self._db_conn: Any = None
        self._model = None
        model_metadata = get_model_metadata(EMBED_MODEL)
        if model_metadata is None:
            raise ValueError(f"Unknown embedding model: {EMBED_MODEL}")
        self._model_metadata = model_metadata
        self._embedding_cache_enabled = EMBED_CACHE_ENABLED
        self._embedding_cache = EmbeddingCache() if self._embedding_cache_enabled else None
        self._embed_batch_size = max(1, EMBED_BATCH_SIZE)
        throttle_ms = max(
            EMBED_THROTTLE_MS, EMBED_LOW_PRIORITY_THROTTLE_MS if EMBED_LOW_PRIORITY else 0
        )
        self._embed_throttle_seconds = throttle_ms / 1000
        self._embedding_stats = {
            "cache_hits": 0,
            "cache_misses": 0,
            "encoded": 0,
            "cache_read_ms": 0.0,
            "cache_write_ms": 0.0,
        }
        self._symbol_axis_columns = (
            AXIS_SYMBOL_REQUIRED_COLUMNS
            if self._index_profile.name == AXIS_PYTHON_V1_PROFILE
            else set()
        )
        self._symbols_schema = _symbols_schema_for_profile(self._index_profile)
        self._symbol_required_columns = {
            "uid",
            "workspace_id",
            "name",
            "file_path",
            "code",
            "vector",
            *self._symbol_axis_columns,
        }
        # Lazily-opened table handles (backing fields for the table properties).
        self._docs_table: Any = None
        self._sym_table_obj: Any = None
        self._axis_adjacency_table_obj: Any = None
        self._axis_adjacency_external_table_obj: Any = None
        self._symbol_chunks_table_obj: Any = None
        self._workspace_sym_tables: dict[str, Any] = {}
        self._workspace_adj_tables: dict[str, Any] = {}
        self._workspace_adj_external_tables: dict[str, Any] = {}

    @property
    def index_profile_name(self) -> str:
        return self._index_profile.name

    @property
    def _db(self):
        # Lazy: open the Lance connection on first storage access, not at construction.
        if self._db_conn is None:
            self._db_conn = lancedb.connect(DB_PATH)
        return self._db_conn

    @_db.setter
    def _db(self, value):
        self._db_conn = value

    @property
    def _table(self):
        if self._docs_table is None:
            self._docs_table = self._open_or_reset_table(
                self._index_profile.docs_table,
                DOCS_SCHEMA,
                required_columns={"id", "workspace_id", "file_path", "chunk", "pending", "vector"},
            )
            self._ensure_docs_optional_columns(self._docs_table)
        return self._docs_table

    @_table.setter
    def _table(self, value):
        self._docs_table = value

    @property
    def _sym_table(self):
        if self._sym_table_obj is None:
            self._sym_table_obj = self._open_or_reset_table(
                self._index_profile.symbols_table,
                self._symbols_schema,
                required_columns=self._symbol_required_columns,
            )
        return self._sym_table_obj

    @_sym_table.setter
    def _sym_table(self, value):
        self._sym_table_obj = value

    @property
    def _axis_adjacency_table(self):
        if self._axis_adjacency_table_obj is None:
            self._axis_adjacency_table_obj = self._open_or_reset_table(
                AXIS_ADJACENCY_TABLE,
                AXIS_ADJACENCY_SCHEMA,
                required_columns={
                    "workspace_id",
                    "uid",
                    "name",
                    "file_path",
                    "kind",
                    "out_edges_json",
                    "in_edges_json",
                },
            )
        return self._axis_adjacency_table_obj

    @_axis_adjacency_table.setter
    def _axis_adjacency_table(self, value):
        self._axis_adjacency_table_obj = value

    @property
    def _axis_adjacency_external_table(self):
        if self._axis_adjacency_external_table_obj is None:
            self._axis_adjacency_external_table_obj = self._open_or_reset_table(
                AXIS_ADJACENCY_EXTERNAL_TABLE,
                AXIS_ADJACENCY_EXTERNAL_SCHEMA,
                required_columns={
                    "workspace_id",
                    "sym_to_ext_json",
                    "ext_to_sym_json",
                },
            )
        return self._axis_adjacency_external_table_obj

    @_axis_adjacency_external_table.setter
    def _axis_adjacency_external_table(self, value):
        self._axis_adjacency_external_table_obj = value

    @property
    def _symbol_chunks_table(self):
        if getattr(self, "_symbol_chunks_table_obj", None) is None:
            table_name = f"{self._index_profile.symbols_table}{AXIS_SYMBOL_CHUNKS_SUFFIX}"
            self._symbol_chunks_table_obj = self._open_or_reset_table(
                table_name,
                AXIS_SYMBOL_CHUNKS_SCHEMA,
                required_columns=set(AXIS_SYMBOL_CHUNKS_SCHEMA.names),
            )
        return self._symbol_chunks_table_obj

    @_symbol_chunks_table.setter
    def _symbol_chunks_table(self, value):
        self._symbol_chunks_table_obj = value

    def _axis_symbol_payload(self, symbol: dict) -> dict[str, object]:
        if not self._symbol_axis_columns:
            return {}
        return {
            "symbol_kind": str(symbol.get("symbol_kind") or ""),
            "ast_kind_bits": list(symbol.get("ast_kind_bits") or []),
            "cfg_bits": list(symbol.get("cfg_bits") or []),
            "dfg_bits": list(symbol.get("dfg_bits") or []),
            "struct_bits": list(symbol.get("struct_bits") or []),
            "container_kinds": list(symbol.get("container_kinds") or []),
            "axis_evidence_json": str(symbol.get("axis_evidence_json") or "[]"),
            "axis_container_kinds_json": str(symbol.get("axis_container_kinds_json") or "[]"),
            "axis_contracts_json": str(symbol.get("axis_contracts_json") or "[]"),
            "file_tier": str(symbol.get("file_tier") or "core"),
        }

    def _open_or_reset_table(self, name: str, schema: pa.Schema, *, required_columns: set[str]):
        try:
            table = self._db.open_table(name)
        except Exception:
            return self._db.create_table(name, schema=schema)
        try:
            current = set(table.schema.names)
        except Exception:
            current = set()
        if required_columns.issubset(current):
            return table
        # No in-place migration: reset table and force full reindex.
        self._db.drop_table(name)
        return self._db.create_table(name, schema=schema)

    def _ensure_docs_optional_columns(self, table) -> None:
        """Add optional doc-table columns in place (no full-table reset)."""
        try:
            current = set(table.schema.names)
        except Exception:
            return
        if "owner_uid" not in current:
            try:
                table.add_columns({"owner_uid": "cast('' as string)"})
            except Exception:
                pass

    def _open_workspace_partition_table(
        self,
        base_table: str,
        workspace_id: str,
        schema: pa.Schema,
        *,
        required_columns: set[str],
        cache: dict[str, Any],
    ):
        if not workspace_partitioned_enabled():
            if base_table == self._index_profile.symbols_table:
                return self._sym_table
            if base_table == AXIS_ADJACENCY_TABLE:
                return self._axis_adjacency_table
            if base_table == AXIS_ADJACENCY_EXTERNAL_TABLE:
                return self._axis_adjacency_external_table
            raise ValueError(f"Unknown partitioned Lance base table: {base_table}")

        cached = cache.get(workspace_id)
        if cached is not None:
            return cached

        name = workspace_partition_table_name(base_table, workspace_id)
        if workspace_partition_table_exists(self._db, base_table, workspace_id):
            table = self._db.open_table(name)
            try:
                current = set(table.schema.names)
            except Exception:
                current = set()
            if not required_columns.issubset(current):
                self._db.drop_table(name)
                table = self._db.create_table(name, schema=schema)
        else:
            table = self._db.create_table(name, schema=schema)
        cache[workspace_id] = table
        return table

    def _maybe_migrate_workspace_partition(
        self,
        workspace_id: str,
        target_table,
        legacy_table,
    ) -> None:
        """Copy rows from the monolithic table into a new workspace partition."""
        try:
            if int(target_table.count_rows()) > 0:
                return
        except Exception:
            pass
        rows = self._scan_table_by_workspace(legacy_table, workspace_id)
        if not rows:
            return
        target_table.add(rows)

    def _uses_workspace_symbol_partition(self, table) -> bool:
        return workspace_partitioned_enabled() and table is not self._sym_table

    def _uses_workspace_adjacency_partition(self, table) -> bool:
        return workspace_partitioned_enabled() and table is not self._axis_adjacency_table

    def _uses_workspace_adjacency_external_partition(self, table) -> bool:
        return workspace_partitioned_enabled() and table is not self._axis_adjacency_external_table

    def symbols_table(self, workspace_id: str):
        """Physical Lance table for one workspace's symbol rows."""
        if not workspace_partitioned_enabled() or not hasattr(self, "_workspace_sym_tables"):
            return self._sym_table
        table = self._open_workspace_partition_table(
            self._index_profile.symbols_table,
            workspace_id,
            self._symbols_schema,
            required_columns=self._symbol_required_columns,
            cache=self._workspace_sym_tables,
        )
        self._maybe_migrate_workspace_partition(workspace_id, table, self._sym_table)
        return table

    def axis_adjacency_table(self, workspace_id: str):
        """Physical Lance table for one workspace's materialized adjacency."""
        if not workspace_partitioned_enabled() or not hasattr(self, "_workspace_adj_tables"):
            return self._axis_adjacency_table
        table = self._open_workspace_partition_table(
            AXIS_ADJACENCY_TABLE,
            workspace_id,
            AXIS_ADJACENCY_SCHEMA,
            required_columns={
                "workspace_id",
                "uid",
                "name",
                "file_path",
                "kind",
                "out_edges_json",
                "in_edges_json",
            },
            cache=self._workspace_adj_tables,
        )
        self._maybe_migrate_workspace_partition(workspace_id, table, self._axis_adjacency_table)
        return table

    def axis_adjacency_external_table(self, workspace_id: str):
        """Physical Lance table for one workspace's external-bridge maps."""
        if not workspace_partitioned_enabled() or not hasattr(
            self, "_workspace_adj_external_tables"
        ):
            return self._axis_adjacency_external_table
        table = self._open_workspace_partition_table(
            AXIS_ADJACENCY_EXTERNAL_TABLE,
            workspace_id,
            AXIS_ADJACENCY_EXTERNAL_SCHEMA,
            required_columns={
                "workspace_id",
                "sym_to_ext_json",
                "ext_to_sym_json",
            },
            cache=self._workspace_adj_external_tables,
        )
        return table

    def _embedding_model(self):
        """Load the transformer lazily so delete-only paths avoid import + model init."""
        if self._model is None:
            shared = _SHARED_EMBEDDING_MODELS.get(EMBED_MODEL)
            if shared is not None:
                self._model = shared
                return self._model
            from sentence_transformers import SentenceTransformer

            device = _resolve_embed_device()
            self._model = SentenceTransformer(EMBED_MODEL, device=device)
            _SHARED_EMBEDDING_MODELS[EMBED_MODEL] = self._model
            if device == "cpu":
                _log.info("Embedding model %s on device=cpu", EMBED_MODEL)
        return self._model

    def warmup(self, *, workspace_id: str | None = None) -> None:
        """Eagerly open Lance storage and load the embedding model."""
        _ = self._db
        if workspace_id:
            try:
                _ = self.symbols_table(workspace_id)
            except Exception:
                _log.debug(
                    "Skipping Lance symbols-table warmup for workspace %s",
                    workspace_id,
                    exc_info=True,
                )
        # Always touch the transformer — ``_embed`` alone can return from a
        # disk cache hit and leave the first real /ask to pay ~6s cold start.
        _ = self._embedding_model()
        self._embed(["context_engine warmup"])

    @staticmethod
    def _quote_delete_value(value: str) -> str:
        return value.replace("'", "''")

    def _uid_batch_delete_predicate(
        self,
        batch: list[str],
        workspace_id: str,
        *,
        partitioned: bool,
    ) -> str:
        if partitioned:
            return " OR ".join(f"uid = '{self._quote_delete_value(uid)}'" for uid in batch)
        return " OR ".join(
            (
                f"(workspace_id = '{self._quote_delete_value(workspace_id)}' "
                f"AND uid = '{self._quote_delete_value(uid)}')"
            )
            for uid in batch
        )

    def _delete_uid_batch_fallback(
        self,
        table,
        batch: list[str],
        workspace_id: str,
        *,
        partitioned: bool,
    ) -> None:
        for uid in batch:
            try:
                if partitioned:
                    table.delete(f"uid = '{self._quote_delete_value(uid)}'")
                else:
                    table.delete(
                        f"(workspace_id = '{self._quote_delete_value(workspace_id)}' "
                        f"AND uid = '{self._quote_delete_value(uid)}')"
                    )
            except Exception:
                pass

    def _workspace_value_predicate(self, workspace_id: str, field: str, value: str) -> str:
        return (
            f"(workspace_id = '{self._quote_delete_value(workspace_id)}' "
            f"AND {field} = '{self._quote_delete_value(value)}')"
        )

    def _delete_workspace_value_batch_fallback(
        self,
        table,
        batch: list[str],
        workspace_id: str,
        *,
        field: str,
    ) -> None:
        for value in batch:
            try:
                table.delete(self._workspace_value_predicate(workspace_id, field, value))
            except Exception:
                pass

    def _delete_workspace_value_batches(
        self,
        table,
        values: list[str],
        workspace_id: str,
        *,
        field: str,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        if not values:
            return
        batch_size = max(1, LANCEDB_DELETE_BATCH_SIZE)
        total = len(values)
        for start in range(0, total, batch_size):
            batch = values[start : start + batch_size]
            predicate = " OR ".join(
                self._workspace_value_predicate(workspace_id, field, value) for value in batch
            )
            try:
                table.delete(predicate)
            except Exception:
                self._delete_workspace_value_batch_fallback(
                    table,
                    batch,
                    workspace_id,
                    field=field,
                )
            if progress_callback:
                progress_callback(f"delete progress: {min(start + len(batch), total)}/{total}")

    def _delete_uid_batches(
        self,
        table,
        uids: list[str],
        workspace_id: str,
        *,
        partitioned: bool,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        if not uids:
            return
        batch_size = max(1, LANCEDB_DELETE_BATCH_SIZE)
        total = len(uids)
        for start in range(0, total, batch_size):
            batch = uids[start : start + batch_size]
            predicate = self._uid_batch_delete_predicate(
                batch,
                workspace_id,
                partitioned=partitioned,
            )
            try:
                table.delete(predicate)
            except Exception:
                self._delete_uid_batch_fallback(
                    table,
                    batch,
                    workspace_id,
                    partitioned=partitioned,
                )
            if progress_callback:
                progress_callback(f"delete progress: {min(start + len(batch), total)}/{total}")

    def _scan_table_by_workspace(
        self,
        table,
        workspace_id: str,
        *,
        columns: list[str] | None = None,
        extra_predicate: str | None = None,
    ) -> list[dict]:
        """Read rows for one workspace via Lance filter (no full-table pandas)."""
        ws = self._quote_delete_value(workspace_id)
        predicate = f"workspace_id = '{ws}'"
        if extra_predicate:
            predicate = f"{predicate} AND ({extra_predicate})"
        try:
            query = table.search().where(predicate, prefilter=True).limit(0)
            if columns:
                query = query.select(columns)
            return cast(list[dict], query.to_list())
        except Exception:
            df = table.to_pandas()
            rows = [
                row.to_dict() for _, row in df.iterrows() if row.get("workspace_id") == workspace_id
            ]
            if columns:
                rows = [{key: row.get(key) for key in columns} for row in rows]
            return rows

    def scan_docs_workspace(self, workspace_id: str) -> list[dict]:
        """All doc chunks for a workspace (used by DocAnchor linking)."""
        return self._scan_table_by_workspace(self._table, workspace_id)

    def count_docs_workspace(self, workspace_id: str) -> int:
        """Row count for one workspace in the documentation table."""
        ws = self._quote_delete_value(workspace_id)
        try:
            return int(self._table.count_rows(f"workspace_id = '{ws}'"))
        except Exception:
            return len(self._scan_table_by_workspace(self._table, workspace_id, columns=["id"]))

    @staticmethod
    def storage_size_bytes() -> int:
        """Return disk usage for the local LanceDB store (all workspaces)."""
        root = Path(DB_PATH).expanduser()
        if not root.exists():
            return 0
        if root.is_file():
            return root.stat().st_size
        total = 0
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    def scan_doc_anchors_workspace(self, workspace_id: str) -> list[dict]:
        """Doc-chunk rows tied to an in-code symbol via ``owner_uid``."""
        try:
            if "owner_uid" not in set(self._table.schema.names):
                return []
        except Exception:
            return []
        rows = self.scan_docs_workspace(workspace_id)
        return [row for row in rows if str(row.get("owner_uid") or "").strip()]

    def search_doc_anchors(
        self,
        query_vector: list[float],
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        limit: int = 12,
        oversample: int = 8,
    ) -> list[dict]:
        """ANN search over in-code doc anchors for one workspace.

        Uses the Lance vector index instead of scanning every doc row — critical
        on large Python repos (django ~7k+ docstrings).
        """
        try:
            if "owner_uid" not in set(self._table.schema.names):
                return []
        except Exception:
            return []
        ws = self._quote_delete_value(workspace_id)
        k = max(limit, limit * max(1, oversample))
        try:
            results = (
                self._table.search(query_vector)
                .where(
                    f"workspace_id = '{ws}' AND owner_uid IS NOT NULL AND owner_uid != ''",
                    prefilter=True,
                )
                .limit(k)
                .to_list()
            )
        except Exception:
            return []
        return [row for row in results if str(row.get("owner_uid") or "").strip()]

    def upsert_symbol_docstring_rows(
        self,
        rows: list[dict],
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        progress_callback: Callable[[str], None] | None = None,
    ) -> int:
        """Embed and upsert in-code docstring anchor rows (one row per symbol)."""
        if not rows:
            return 0
        if progress_callback:
            progress_callback(f"doc-anchor embed: rows={len(rows)}")
        vectors = self._embed(
            [str(row.get("chunk") or "") for row in rows],
            progress_callback=progress_callback,
        )
        payload: list[dict] = []
        for row, vec in zip(rows, vectors, strict=False):
            chunk = str(row.get("chunk") or "")
            metadata = EmbeddingMetadata(
                model_name=EMBED_MODEL,
                model_version=self._model_metadata.version,
                chunk_hash=compute_chunk_hash(chunk),
                embedding_hash=compute_embedding_hash(vec),
            )
            payload.append(
                {
                    "id": str(row["id"]),
                    "workspace_id": workspace_id,
                    "file_path": str(row["file_path"]),
                    "chunk": chunk,
                    "pending": [],
                    "vector": vec,
                    "embedding_metadata": json.dumps(
                        {
                            "model_name": metadata.model_name,
                            "model_version": metadata.model_version,
                            "chunk_hash": metadata.chunk_hash,
                            "embedding_hash": metadata.embedding_hash,
                        }
                    ),
                    "owner_uid": str(row.get("owner_uid") or ""),
                }
            )
        chunk_ids = [row["id"] for row in payload]
        batch_size = max(1, LANCEDB_DELETE_BATCH_SIZE)
        for start in range(0, len(chunk_ids), batch_size):
            batch = chunk_ids[start : start + batch_size]
            predicate = " OR ".join(
                (
                    f"(workspace_id = '{self._quote_delete_value(workspace_id)}' "
                    f"AND id = '{self._quote_delete_value(chunk_id)}')"
                )
                for chunk_id in batch
            )
            try:
                self._table.delete(predicate)
            except Exception:
                pass
        add_batch_size = max(batch_size, 512)
        for start in range(0, len(payload), add_batch_size):
            self._table.add(payload[start : start + add_batch_size])
        return len(payload)

    def delete_doc_anchors_by_owner_uids(
        self,
        owner_uids: list[str],
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> None:
        """Remove doc-anchor rows whose ``owner_uid`` was tombstoned."""
        uids = [uid for uid in owner_uids if uid]
        if not uids:
            return
        try:
            if "owner_uid" not in set(self._table.schema.names):
                return
        except Exception:
            return
        self._delete_workspace_value_batches(
            self._table,
            uids,
            workspace_id,
            field="owner_uid",
        )

    def scan_symbols_workspace(
        self,
        workspace_id: str,
        *,
        columns: list[str] | None = None,
    ) -> list[dict]:
        """Symbol embedding rows for a workspace (local semantic index for doc linking)."""
        table = self.symbols_table(workspace_id)
        if self._uses_workspace_symbol_partition(table):
            try:
                query = table.search().limit(0)
                if columns:
                    query = query.select(columns)
                return cast(list[dict], query.to_list())
            except Exception:
                df = table.to_pandas()
                rows = [row.to_dict() for _, row in df.iterrows()]
                if columns:
                    rows = [{key: row.get(key) for key in columns} for row in rows]
                return rows
        return self._scan_table_by_workspace(
            table,
            workspace_id,
            columns=columns or ["uid", "name", "file_path", "vector"],
        )

    def scan_axis_adjacency_workspace(self, workspace_id: str) -> list[dict]:
        """Materialized graph-walk rows for one workspace."""
        table = self.axis_adjacency_table(workspace_id)
        columns = [
            "uid",
            "name",
            "file_path",
            "kind",
            "out_edges_json",
            "in_edges_json",
        ]
        if self._uses_workspace_adjacency_partition(table):
            try:
                return cast(
                    list[dict],
                    table.search().limit(0).select(columns).to_list(),
                )
            except Exception:
                df = table.to_pandas()
                return [{key: row.get(key) for key in columns} for _, row in df.iterrows()]
        return self._scan_table_by_workspace(
            table,
            workspace_id,
            columns=columns,
        )

    def list_symbol_uids_by_prefixes(self, workspace_id: str, prefixes: list[str]) -> set[str]:
        """Return Symbol uids whose file_path is exactly/under any prefix."""
        if not prefixes:
            return set()
        prefixes_resolved = [str(Path(prefix).resolve()) for prefix in prefixes]
        rows = self.scan_symbols_workspace(workspace_id, columns=["uid", "file_path"])
        out: set[str] = set()
        for row in rows:
            uid = str(row.get("uid") or "")
            file_path = str(row.get("file_path") or "")
            if not uid or not file_path:
                continue
            if any(
                file_path == pref or file_path.startswith(f"{pref}/") for pref in prefixes_resolved
            ):
                out.add(uid)
        return out

    def find_incident_axis_adjacency_uids(
        self,
        workspace_id: str,
        target_uids: set[str],
    ) -> set[str]:
        """Return target uids and neighbours connected in adjacency snapshot."""
        if not target_uids:
            return set()
        incident = set(target_uids)
        rows = self.scan_axis_adjacency_workspace(workspace_id)
        for row in rows:
            uid = str(row.get("uid") or "")
            if not uid:
                continue
            out_edges = decode_edge_uid_map(row.get("out_edges_json"))
            in_edges = decode_edge_uid_map(row.get("in_edges_json"))
            out_neighbours = {v for values in out_edges.values() for v in values}
            in_neighbours = {v for values in in_edges.values() for v in values}
            if (
                uid in target_uids
                or (out_neighbours & target_uids)
                or (in_neighbours & target_uids)
            ):
                incident.add(uid)
                incident.update(out_neighbours & target_uids)
                incident.update(in_neighbours & target_uids)
        return incident

    def delete_axis_adjacency_uids(self, workspace_id: str, uids: set[str]) -> None:
        """Delete axis adjacency rows for selected uids in one workspace."""
        if not uids:
            return
        self._delete_axis_adjacency_rows(sorted(uids), workspace_id)

    def _delete_axis_adjacency_rows(
        self,
        uids: list[str],
        workspace_id: str,
    ) -> None:
        table = self.axis_adjacency_table(workspace_id)
        partitioned = self._uses_workspace_adjacency_partition(table)
        self._delete_uid_batches(
            table,
            uids,
            workspace_id,
            partitioned=partitioned,
        )

    def _delete_doc_rows(
        self,
        file_paths: list[str],
        workspace_id: str,
        *,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self._delete_workspace_value_batches(
            self._table,
            file_paths,
            workspace_id,
            field="file_path",
            progress_callback=progress_callback,
        )

    def _delete_symbol_rows(
        self,
        uids: list[str],
        workspace_id: str,
        *,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        table = self.symbols_table(workspace_id)
        partitioned = self._uses_workspace_symbol_partition(table)
        self._delete_uid_batches(
            table,
            uids,
            workspace_id,
            partitioned=partitioned,
            progress_callback=progress_callback,
        )

    def _embed_cache_keys(self, content_hashes: list[str]) -> dict[str, EmbeddingCacheKey]:
        return {
            content_hash: EmbeddingCacheKey(
                model_name=EMBED_MODEL,
                model_version=self._model_metadata.version,
                content_hash=content_hash,
            )
            for content_hash in dict.fromkeys(content_hashes)
        }

    def _embed_fill_from_cache(
        self,
        texts: list[str],
        content_hashes: list[str],
        cache_keys: dict[str, EmbeddingCacheKey],
    ) -> tuple[list[list[float] | None], OrderedDict[str, str]]:
        vectors: list[list[float] | None] = [None] * len(texts)
        missing_by_hash: OrderedDict[str, str] = OrderedDict()
        t_cache = time.perf_counter()
        cached_vectors = (
            self._embedding_cache.get_many(list(cache_keys.values()))
            if self._embedding_cache
            else {}
        )
        self._embedding_stats["cache_read_ms"] = self._embedding_stats.get(
            "cache_read_ms", 0.0
        ) + (time.perf_counter() - t_cache) * 1000
        for index, (text, content_hash) in enumerate(zip(texts, content_hashes, strict=False)):
            key = cache_keys[content_hash]
            cached = cached_vectors.get(key)
            if cached is not None:
                vectors[index] = cached
                self._embedding_stats["cache_hits"] += 1
            else:
                missing_by_hash.setdefault(content_hash, text)
                self._embedding_stats["cache_misses"] += 1
        return vectors, missing_by_hash

    def _embed_encode_cache_misses(
        self,
        missing_by_hash: OrderedDict[str, str],
        cache_keys: dict[str, EmbeddingCacheKey],
        *,
        total_texts: int,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, list[float]]:
        encoded_by_hash: dict[str, list[float]] = {}
        missing_items = list(missing_by_hash.items())
        if progress_callback:
            progress_callback(
                f"cache scan: total={total_texts} missing={len(missing_items)} "
                f"batch_size={self._embed_batch_size}"
            )
        for start in range(0, len(missing_items), self._embed_batch_size):
            batch = missing_items[start : start + self._embed_batch_size]
            encoded = self._embedding_model().encode(
                [text for _, text in batch], show_progress_bar=False
            )
            cache_writes: list[tuple[EmbeddingCacheKey, list[float], str]] = []
            for (content_hash, _), row in zip(batch, encoded, strict=False):
                vector = [float(value) for value in row]
                encoded_by_hash[content_hash] = vector
                self._embedding_stats["encoded"] += 1
                if self._embedding_cache:
                    cache_writes.append(
                        (
                            cache_keys[content_hash],
                            vector,
                            compute_embedding_hash(vector),
                        )
                    )
            if self._embedding_cache and cache_writes:
                t_cache = time.perf_counter()
                self._embedding_cache.set_many(cache_writes)
                self._embedding_stats["cache_write_ms"] = self._embedding_stats.get(
                    "cache_write_ms", 0.0
                ) + (time.perf_counter() - t_cache) * 1000
            if progress_callback:
                progress_callback(
                    f"encode: {min(start + len(batch), len(missing_items))}/{len(missing_items)}"
                )
            if self._embed_throttle_seconds and start + self._embed_batch_size < len(missing_items):
                time.sleep(self._embed_throttle_seconds)
        return encoded_by_hash

    def _embed(
        self,
        texts: list[str],
        progress_callback: Callable[[str], None] | None = None,
    ) -> list[list[float]]:
        if not texts:
            return []

        content_hashes = [compute_chunk_hash(text) for text in texts]
        cache_keys = self._embed_cache_keys(content_hashes)
        vectors, missing_by_hash = self._embed_fill_from_cache(texts, content_hashes, cache_keys)
        encoded_by_hash = self._embed_encode_cache_misses(
            missing_by_hash,
            cache_keys,
            total_texts=len(texts),
            progress_callback=progress_callback,
        )

        for index, content_hash in enumerate(content_hashes):
            if vectors[index] is None:
                vectors[index] = encoded_by_hash[content_hash]

        output: list[list[float]] = []
        for maybe_vector in vectors:
            if maybe_vector is None:
                raise RuntimeError("Embedding vector was not populated")
            output.append(maybe_vector)
        return output

    def _symbol_embedding_rows(
        self,
        symbols: list[dict],
        vectors: list[list[float]],
        signature_vectors: list[list[float]] | None,
        *,
        workspace_id: str,
    ) -> list[dict]:
        rows: list[dict] = []
        for idx, (symbol, vec) in enumerate(zip(symbols, vectors, strict=False)):
            metadata = EmbeddingMetadata(
                model_name=EMBED_MODEL,
                model_version=self._model_metadata.version,
                chunk_hash=compute_chunk_hash(symbol["code"]),
                embedding_hash=compute_embedding_hash(vec),
            )
            row = {
                "uid": symbol["uid"],
                "workspace_id": str(symbol.get("workspace_id") or workspace_id),
                "name": symbol["name"],
                "qualified_name": str(symbol.get("qualified_name") or ""),
                "file_path": symbol["file_path"],
                "code": symbol["code"],
                "vector": vec,
                "embedding_metadata": json.dumps(
                    {
                        "model_name": metadata.model_name,
                        "model_version": metadata.model_version,
                        "chunk_hash": metadata.chunk_hash,
                        "embedding_hash": metadata.embedding_hash,
                    }
                ),
                **self._axis_symbol_payload(symbol),
            }
            if signature_vectors is not None:
                row["signature_vector"] = signature_vectors[idx]
            rows.append(row)
        return rows

    def _semantic_chunk_embedding_rows(
        self,
        symbols: list[dict],
        *,
        workspace_id: str,
        progress_callback: Callable[[str], None] | None = None,
    ) -> list[dict]:
        """Build and embed AST-aligned retrieval chunks for changed symbols."""
        if not getattr(self, "_symbol_axis_columns", set()) or not AXIS_SEMANTIC_CHUNK_INDEX:
            return []
        from context_engine.search.semantic_chunks import build_semantic_chunks

        prepared: list[tuple[dict, Any]] = []
        for symbol in symbols:
            chunks = build_semantic_chunks(
                symbol,
                target_lines=max(4, AXIS_SEMANTIC_CHUNK_TARGET_LINES),
                overlap_lines=max(0, AXIS_SEMANTIC_CHUNK_OVERLAP_LINES),
                min_symbol_lines=max(1, AXIS_SEMANTIC_CHUNK_MIN_SYMBOL_LINES),
            )
            prepared.extend((symbol, chunk) for chunk in chunks)
        if not prepared:
            return []
        if progress_callback:
            progress_callback(f"semantic chunks: rows={len(prepared)}")
        vectors = self._embed(
            [chunk.embedding_text for _symbol, chunk in prepared],
            progress_callback=progress_callback,
        )
        rows: list[dict] = []
        for (symbol, chunk), vector in zip(prepared, vectors, strict=False):
            metadata = EmbeddingMetadata(
                model_name=EMBED_MODEL,
                model_version=self._model_metadata.version,
                chunk_hash=compute_chunk_hash(chunk.embedding_text),
                embedding_hash=compute_embedding_hash(vector),
            )
            owner_uid = str(symbol.get("uid") or "")
            rows.append(
                {
                    "chunk_uid": f"{owner_uid}::semantic::{chunk.chunk_index}",
                    "workspace_id": str(symbol.get("workspace_id") or workspace_id),
                    "owner_uid": owner_uid,
                    "name": str(symbol.get("name") or ""),
                    "qualified_name": str(symbol.get("qualified_name") or ""),
                    "file_path": str(symbol.get("file_path") or ""),
                    "chunk_index": int(chunk.chunk_index),
                    "start_line": int(chunk.start_line),
                    "end_line": int(chunk.end_line),
                    "chunk_text": chunk.text,
                    "vector": vector,
                    "embedding_metadata": json.dumps(
                        {
                            "model_name": metadata.model_name,
                            "model_version": metadata.model_version,
                            "chunk_hash": metadata.chunk_hash,
                            "embedding_hash": metadata.embedding_hash,
                        }
                    ),
                }
            )
        return rows

    def _delete_symbol_chunk_rows(self, owner_uids: list[str], workspace_id: str) -> None:
        if not owner_uids or not getattr(self, "_symbol_axis_columns", set()):
            return
        self._delete_workspace_value_batches(
            self._symbol_chunks_table,
            owner_uids,
            workspace_id,
            field="owner_uid",
        )

    def _upsert_semantic_chunks(
        self,
        symbols: list[dict],
        *,
        workspace_id: str,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        if not getattr(self, "_symbol_axis_columns", set()) or not AXIS_SEMANTIC_CHUNK_INDEX:
            return
        owner_uids = [str(symbol.get("uid") or "") for symbol in symbols]
        rows = self._semantic_chunk_embedding_rows(
            symbols,
            workspace_id=workspace_id,
            progress_callback=progress_callback,
        )
        self._delete_symbol_chunk_rows(owner_uids, workspace_id)
        if rows:
            add_batch_size = max(512, LANCEDB_DELETE_BATCH_SIZE)
            for start in range(0, len(rows), add_batch_size):
                self._symbol_chunks_table.add(rows[start : start + add_batch_size])

    def _apply_symbol_upsert_delete_strategy(
        self,
        uids: list[str],
        workspace_id: str,
        existing: int,
        *,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        bulk_replace = (
            existing > 0
            and len(uids) >= LANCEDB_SYMBOL_BULK_REPLACE_MIN
            and (len(uids) >= int(existing * LANCEDB_SYMBOL_BULK_REPLACE_RATIO))
        )
        if existing == 0:
            if progress_callback:
                progress_callback("insert symbol vectors: skip delete (new workspace)")
            return
        if bulk_replace:
            if progress_callback:
                progress_callback(
                    f"replace symbol vectors: bulk clear {existing} existing, full reindex"
                )
            self.delete_symbols_workspace(workspace_id)
            return
        if progress_callback:
            progress_callback(
                f"patch symbol vectors: {len(uids)} uid deletes, {existing} already indexed"
            )
        self._delete_symbol_rows(uids, workspace_id, progress_callback=progress_callback)

    def embedding_cache_stats(self) -> dict:
        cache_stats = self._embedding_cache.stats() if self._embedding_cache else {"enabled": False}
        return {
            "enabled": self._embedding_cache_enabled,
            "batch_size": self._embed_batch_size,
            "throttle_ms": int(self._embed_throttle_seconds * 1000),
            "runtime": dict(self._embedding_stats),
            "cache": cache_stats,
        }

    def upsert_chunks(
        self,
        file_path: str,
        chunks: list[str],
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        self.upsert_chunk_batches([(file_path, chunks)], workspace_id=workspace_id)

    def upsert_chunk_batches(
        self,
        file_chunks: list[tuple[str, list[str]]],
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        entries: list[tuple[str, int, str]] = []
        file_paths: list[str] = []
        for file_path, chunks in file_chunks:
            file_paths.append(file_path)
            entries.extend((file_path, i, chunk) for i, chunk in enumerate(chunks))
        if not entries:
            return

        if progress_callback:
            progress_callback(f"prepare: files={len(file_paths)} chunks={len(entries)}")
        t0 = time.perf_counter()
        vectors = self._embed(
            [chunk for _, _, chunk in entries], progress_callback=progress_callback
        )
        if progress_callback:
            progress_callback(f"embed done in {time.perf_counter() - t0:.2f}s")

        rows = []
        for (file_path, i, chunk), vec in zip(entries, vectors, strict=False):
            metadata = EmbeddingMetadata(
                model_name=EMBED_MODEL,
                model_version=self._model_metadata.version,
                chunk_hash=compute_chunk_hash(chunk),
                embedding_hash=compute_embedding_hash(vec),
            )
            rows.append(
                {
                    "id": f"{file_path}::{i}",
                    "workspace_id": workspace_id,
                    "file_path": file_path,
                    "chunk": chunk,
                    "pending": [],
                    "vector": vec,
                    "embedding_metadata": json.dumps(
                        {
                            "model_name": metadata.model_name,
                            "model_version": metadata.model_version,
                            "chunk_hash": metadata.chunk_hash,
                            "embedding_hash": metadata.embedding_hash,
                        }
                    ),
                    "owner_uid": "",
                }
            )

        if progress_callback:
            progress_callback(f"delete existing rows: {len(file_paths)}")
        t0 = time.perf_counter()
        self._delete_doc_rows(file_paths, workspace_id, progress_callback=progress_callback)
        if progress_callback:
            progress_callback(f"delete done in {time.perf_counter() - t0:.2f}s")
            progress_callback(f"add rows: {len(rows)}")
        t0 = time.perf_counter()
        self._table.add(rows)
        if progress_callback:
            progress_callback(f"add done in {time.perf_counter() - t0:.2f}s")

    def get_pending(self, *, workspace_id: str = DEFAULT_WORKSPACE_ID) -> dict[str, list[str]]:
        """Returns {chunk_id: [name, ...]} for all chunks with pending identifiers.

        Uses Lance's native ``WHERE`` clause so the scan is pushed down at
        the storage layer instead of materializing the full table in
        pandas just to filter it.
        """
        rows = self._scan_pending(columns=["id", "pending"], workspace_id=workspace_id)
        return {row["id"]: list(row["pending"]) for row in rows}

    def get_pending_rows(self, *, workspace_id: str = DEFAULT_WORKSPACE_ID) -> list[dict]:
        """Return full doc rows that still have unresolved pending identifiers."""
        return self._scan_pending(columns=None, workspace_id=workspace_id)

    def _scan_pending(self, *, columns: list[str] | None, workspace_id: str) -> list[dict]:
        """Lance-native filtered scan for chunks with pending identifiers."""
        rows = self._scan_table_by_workspace(
            self._table,
            workspace_id,
            columns=columns,
            extra_predicate="array_length(pending) > 0",
        )
        return [row for row in rows if len(row.get("pending") or []) > 0]

    def _set_pending_row(self, row: dict, pending: list[str]):
        chunk_id = row["id"]
        workspace_id = row.get("workspace_id", DEFAULT_WORKSPACE_ID)
        try:
            self._table.delete(
                f"(workspace_id = '{self._quote_delete_value(workspace_id)}' "
                f"AND id = '{self._quote_delete_value(chunk_id)}')"
            )
        except Exception:
            pass
        vector = row["vector"]
        if hasattr(vector, "tolist"):
            vector = vector.tolist()
        embedding_metadata = row.get("embedding_metadata") or "{}"
        self._table.add(
            [
                {
                    "id": chunk_id,
                    "workspace_id": workspace_id,
                    "file_path": row["file_path"],
                    "chunk": row["chunk"],
                    "pending": pending,
                    "vector": vector,
                    "embedding_metadata": embedding_metadata,
                    "owner_uid": str(row.get("owner_uid") or ""),
                }
            ]
        )

    def set_pending_row(self, row: dict, pending: list[str]):
        """Update pending identifiers for an already-fetched doc row."""
        self._set_pending_row(row, pending)

    def set_pending_rows_batch(self, updates: list[tuple[dict, list[str]]]) -> int:
        """Bulk-update pending identifiers for many doc rows in one pass.

        The per-row ``_set_pending_row`` path issues a ``delete`` + ``add``
        against LanceDB for every row. On large doc sets that scales badly
        (each delete/add causes Lance to rewrite fragments). This method
        collapses N deletes + N adds into exactly 1 delete + 1 add,
        turning a ~1 s/row cost into ~tens of milliseconds amortized.

        ``updates`` is an iterable of ``(row_dict, new_pending_list)``
        tuples where ``row_dict`` is the full LanceDB row as previously
        returned by ``get_pending_rows`` / ``_prepare_doc_link_batches``.
        Returns the number of rows rewritten.
        """
        if not updates:
            return 0

        # Build the IN-list for the delete predicate. Lance does not
        # parameterize delete strings, so we escape single quotes manually.
        keys: list[tuple[str, str]] = []
        rows_to_add: list[dict] = []
        for row, pending in updates:
            chunk_id = row["id"]
            workspace_id = row.get("workspace_id", DEFAULT_WORKSPACE_ID)
            keys.append((workspace_id, chunk_id))
            vector = row["vector"]
            if hasattr(vector, "tolist"):
                vector = vector.tolist()
            rows_to_add.append(
                {
                    "id": chunk_id,
                    "workspace_id": workspace_id,
                    "file_path": row["file_path"],
                    "chunk": row["chunk"],
                    "pending": pending,
                    "vector": vector,
                    "embedding_metadata": row.get("embedding_metadata") or "{}",
                    "owner_uid": str(row.get("owner_uid") or ""),
                }
            )

        predicates = " OR ".join(
            (
                f"(workspace_id = '{self._quote_delete_value(ws)}' "
                f"AND id = '{self._quote_delete_value(cid)}')"
            )
            for ws, cid in keys
        )
        try:
            self._table.delete(predicates)
        except Exception:
            # Match the resilience of _set_pending_row — missing rows are
            # not an error; add() will insert them fresh below.
            pass
        self._table.add(rows_to_add)
        return len(rows_to_add)

    def set_pending(
        self,
        chunk_id: str,
        pending: list[str],
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        try:
            rows = (
                self._table.search()
                .where(
                    f"(workspace_id = '{self._quote_delete_value(workspace_id)}' "
                    f"AND id = '{self._quote_delete_value(chunk_id)}')",
                    prefilter=True,
                )
                .limit(1)
                .to_list()
            )
        except Exception:
            df = self._table.to_pandas()
            matched = df[(df["id"] == chunk_id) & (df["workspace_id"] == workspace_id)]
            if matched.empty:
                return
            self._set_pending_row(matched.iloc[0].to_dict(), pending)
            return
        if not rows:
            return
        self._set_pending_row(rows[0], pending)

    def search(
        self, query: str, limit: int = 5, *, workspace_id: str = DEFAULT_WORKSPACE_ID
    ) -> list[dict]:
        vec = self._embed([query])[0]
        results = (
            self._table.search(vec)
            .where(f"workspace_id = '{self._quote_delete_value(workspace_id)}'", prefilter=True)
            .limit(limit)
            .to_list()
        )

        # Guard against cross-model queries (skip check for unversioned rows)
        for r in results:
            meta_str = r.get("embedding_metadata")
            if meta_str:
                try:
                    metadata_dict = json.loads(meta_str)
                    if (
                        metadata_dict.get("model_name")
                        and metadata_dict.get("model_name") != EMBED_MODEL
                    ):
                        raise EmbeddingModelMismatch(
                            f"Query embedding uses {EMBED_MODEL} but database has {metadata_dict.get('model_name')}. "
                            "Delete ./data/lancedb (or the workspace partition) and re-index."
                        )
                except json.JSONDecodeError:
                    pass

        output = []
        for r in results:
            distance = r.get("_distance")
            score = None if distance is None else _l2_to_score(float(distance))
            output.append(
                {
                    "id": r.get("id"),
                    "file_path": r["file_path"],
                    "chunk": r["chunk"],
                    "distance": distance,
                    "score": score,
                }
            )
        return output

    def upsert_symbol_embeddings(
        self,
        symbols: list[dict],
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        progress_callback: Callable[[str], None] | None = None,
    ):
        """symbols: list of {uid, name, file_path, code}

        Body and signature facets are embedded in streaming chunks so peak
        memory does not hold both full-batch vector lists at once.
        """
        if not symbols:
            return
        if progress_callback:
            progress_callback(f"prepare: symbols={len(symbols)}")
        chunk_size = max(1, self._embed_batch_size)
        rows: list[dict] = []
        t0 = time.perf_counter()
        for start in range(0, len(symbols), chunk_size):
            batch = symbols[start : start + chunk_size]
            # Body facet — drop codes from scope after embedding this chunk.
            body_texts = [s["code"] for s in batch]
            body_vectors = self._embed(body_texts, progress_callback=progress_callback)
            del body_texts
            signature_vectors: list[list[float]] | None = None
            if self._symbol_axis_columns:
                signature_vectors = self._embed(
                    [symbol_signature_text(s["code"]) for s in batch],
                    progress_callback=progress_callback,
                )
            rows.extend(
                self._symbol_embedding_rows(
                    batch,
                    body_vectors,
                    signature_vectors,
                    workspace_id=workspace_id,
                )
            )
            del body_vectors, signature_vectors
        if progress_callback:
            progress_callback(f"embed done in {time.perf_counter() - t0:.2f}s")
        uids = [s["uid"] for s in symbols]
        existing = self.count_symbols_workspace(workspace_id)
        t0 = time.perf_counter()
        self._apply_symbol_upsert_delete_strategy(
            uids,
            workspace_id,
            existing,
            progress_callback=progress_callback,
        )
        if progress_callback and existing > 0:
            progress_callback(f"clear/delete done in {time.perf_counter() - t0:.2f}s")
        if progress_callback:
            progress_callback(f"prepare insert: symbols={len(rows)}")
        t0 = time.perf_counter()
        self.symbols_table(workspace_id).add(rows)
        if progress_callback:
            progress_callback(f"insert done in {time.perf_counter() - t0:.2f}s")
        self._upsert_semantic_chunks(
            symbols,
            workspace_id=workspace_id,
            progress_callback=progress_callback,
        )
        from context_engine.axis.role_retrieval import invalidate_workspace_scan_cache

        invalidate_workspace_scan_cache(workspace_id)

    def count_symbols_workspace(self, workspace_id: str) -> int:
        """Row count for one workspace in the symbols table."""
        table = self.symbols_table(workspace_id)
        if self._uses_workspace_symbol_partition(table):
            try:
                return int(table.count_rows())
            except Exception:
                return len(table.to_pandas())
        ws = self._quote_delete_value(workspace_id)
        try:
            return int(table.count_rows(f"workspace_id = '{ws}'"))
        except Exception:
            return len(self._scan_table_by_workspace(table, workspace_id, columns=["uid"]))

    def delete_symbols_workspace(
        self,
        workspace_id: str,
        *,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        """Drop all symbol embedding rows for a workspace (docs table untouched)."""
        if progress_callback:
            progress_callback(f"clear symbol vectors for {workspace_id}")
        if hasattr(self, "_workspace_sym_tables"):
            drop_workspace_partition_table(
                self._db, self._index_profile.symbols_table, workspace_id
            )
            self._workspace_sym_tables.pop(workspace_id, None)
            ws = self._quote_delete_value(workspace_id)
            try:
                self._sym_table.delete(f"workspace_id = '{ws}'")
            except Exception:
                pass
        else:
            ws = self._quote_delete_value(workspace_id)
            predicate = f"workspace_id = '{ws}'"
            try:
                self._sym_table.delete(predicate)
            except Exception:
                pass
        if getattr(self, "_symbol_axis_columns", set()):
            ws = self._quote_delete_value(workspace_id)
            try:
                self._symbol_chunks_table.delete(f"workspace_id = '{ws}'")
            except Exception:
                pass
        from context_engine.axis.role_retrieval import invalidate_workspace_scan_cache

        invalidate_workspace_scan_cache(workspace_id)

    def replace_axis_adjacency(
        self,
        rows: list[dict],
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> None:
        """Replace one workspace's materialized adjacency rows."""
        table = self.axis_adjacency_table(workspace_id)
        if self._uses_workspace_adjacency_partition(table):
            drop_workspace_partition_table(self._db, AXIS_ADJACENCY_TABLE, workspace_id)
            self._workspace_adj_tables.pop(workspace_id, None)
            table = self.axis_adjacency_table(workspace_id)
        else:
            ws = self._quote_delete_value(workspace_id)
            try:
                table.delete(f"workspace_id = '{ws}'")
            except Exception:
                pass
        if rows:
            table.add(rows)

    def replace_axis_adjacency_external(
        self,
        sym_to_ext: dict[str, dict[str, set[str]]],
        ext_to_sym: dict[str, dict[str, set[str]]],
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> None:
        """Replace one workspace's external-node bridge maps for in-proc walks."""
        from context_engine.axis.adjacency_bridges import serialize_external_maps

        sym_json, ext_json = serialize_external_maps(sym_to_ext, ext_to_sym)
        row = {
            "workspace_id": workspace_id,
            "sym_to_ext_json": sym_json,
            "ext_to_sym_json": ext_json,
        }
        table = self.axis_adjacency_external_table(workspace_id)
        if self._uses_workspace_adjacency_external_partition(table):
            drop_workspace_partition_table(self._db, AXIS_ADJACENCY_EXTERNAL_TABLE, workspace_id)
            self._workspace_adj_external_tables.pop(workspace_id, None)
            table = self.axis_adjacency_external_table(workspace_id)
        else:
            ws = self._quote_delete_value(workspace_id)
            try:
                table.delete(f"workspace_id = '{ws}'")
            except Exception:
                pass
        table.add([row])

    def load_axis_adjacency_external(
        self,
        workspace_id: str,
    ) -> tuple[dict[str, dict[str, set[str]]], dict[str, dict[str, set[str]]]] | None:
        """Load materialized external bridge maps, or ``None`` when absent."""
        from context_engine.axis.adjacency_bridges import deserialize_external_maps

        table = self.axis_adjacency_external_table(workspace_id)
        try:
            if self._uses_workspace_adjacency_external_partition(table):
                if int(table.count_rows()) <= 0:
                    return None
                rows = (
                    table.search().limit(1).select(["sym_to_ext_json", "ext_to_sym_json"]).to_list()
                )
            else:
                ws = self._quote_delete_value(workspace_id)
                rows = (
                    table.search()
                    .where(f"workspace_id = '{ws}'", prefilter=True)
                    .limit(1)
                    .select(["sym_to_ext_json", "ext_to_sym_json"])
                    .to_list()
                )
        except Exception:
            return None
        if not rows:
            return None
        row = rows[0]
        return deserialize_external_maps(
            str(row.get("sym_to_ext_json") or ""),
            str(row.get("ext_to_sym_json") or ""),
        )

    def upsert_axis_adjacency_rows(
        self,
        rows: list[dict],
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> None:
        """Patch selected adjacency rows without replacing the whole workspace."""
        if not rows:
            return
        uids = [str(row["uid"]) for row in rows if row.get("uid")]
        self._delete_axis_adjacency_rows(uids, workspace_id)
        self.axis_adjacency_table(workspace_id).add(rows)

    def count_axis_adjacency_workspace(self, workspace_id: str) -> int:
        """Row count for one workspace in the materialized adjacency table."""
        table = self.axis_adjacency_table(workspace_id)
        if self._uses_workspace_adjacency_partition(table):
            try:
                return int(table.count_rows())
            except Exception:
                return len(self.scan_axis_adjacency_workspace(workspace_id))
        ws = self._quote_delete_value(workspace_id)
        try:
            return int(table.count_rows(f"workspace_id = '{ws}'"))
        except Exception:
            return len(self.scan_axis_adjacency_workspace(workspace_id))

    def delete_workspace(
        self,
        workspace_id: str,
        *,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        """Drop all doc/symbol embedding rows for a workspace (fast reset path)."""
        ws = self._quote_delete_value(workspace_id)
        predicate = f"workspace_id = '{ws}'"
        if progress_callback:
            progress_callback(f"delete docs workspace={workspace_id}")
        try:
            self._table.delete(predicate)
        except Exception:
            pass
        self.delete_symbols_workspace(workspace_id, progress_callback=progress_callback)
        if hasattr(self, "_workspace_adj_tables"):
            drop_workspace_partition_table(self._db, AXIS_ADJACENCY_TABLE, workspace_id)
            self._workspace_adj_tables.pop(workspace_id, None)
        if hasattr(self, "_workspace_adj_external_tables"):
            drop_workspace_partition_table(self._db, AXIS_ADJACENCY_EXTERNAL_TABLE, workspace_id)
            self._workspace_adj_external_tables.pop(workspace_id, None)
        try:
            self._axis_adjacency_table.delete(predicate)
        except Exception:
            pass
        try:
            self._axis_adjacency_external_table.delete(predicate)
        except Exception:
            pass

    def _path_prefix_clauses(self, prefixes: list[str]) -> list[str]:
        path_clauses: list[str] = []
        for prefix in prefixes:
            escaped = self._quote_delete_value(str(Path(prefix).resolve()))
            path_clauses.append(f"file_path = '{escaped}'")
            path_clauses.append(f"file_path LIKE '{escaped}/%'")
        return path_clauses

    def _workspace_path_predicate(self, workspace_id: str, prefixes: list[str]) -> str:
        ws = self._quote_delete_value(workspace_id)
        path_predicate = " OR ".join(self._path_prefix_clauses(prefixes))
        return f"workspace_id = '{ws}' AND ({path_predicate})"

    def _reset_axis_adjacency_for_workspace(self, workspace_id: str, adj_table) -> None:
        ws = self._quote_delete_value(workspace_id)
        if self._uses_workspace_adjacency_partition(adj_table):
            drop_workspace_partition_table(self._db, AXIS_ADJACENCY_TABLE, workspace_id)
            if hasattr(self, "_workspace_adj_tables"):
                self._workspace_adj_tables.pop(workspace_id, None)
            return
        try:
            adj_table.delete(f"workspace_id = '{ws}'")
        except Exception:
            pass

    def _rematerialize_adjacency_survivors(
        self,
        db: Any,
        workspace_id: str,
        survivors: set[str],
    ) -> bool:
        if not survivors:
            return False
        try:
            from context_engine.indexer.fast.adjacency_materialization import (
                materialize_axis_adjacency_subset,
            )

            materialize_axis_adjacency_subset(db, self, workspace_id, survivors)
        except Exception:
            return False
        return True

    def _invalidate_adjacency_graph_cache(
        self,
        workspace_id: str,
        *,
        use_full_reset: bool,
        incident_uids: set[str],
    ) -> None:
        try:
            from context_engine.axis import graph_walk_inproc

            if use_full_reset:
                graph_walk_inproc.invalidate_adjacency(workspace_id)
            else:
                graph_walk_inproc.invalidate_adjacency_uids(workspace_id, incident_uids)
        except Exception:
            pass

    def delete_path_prefixes(
        self,
        workspace_id: str,
        prefixes: list[str],
        *,
        progress_callback: Callable[[str], None] | None = None,
        db: Any | None = None,
    ) -> None:
        """Delete rows whose file_path equals or lives under any prefix (no full-table scan)."""
        if not prefixes:
            return
        predicate = self._workspace_path_predicate(workspace_id, prefixes)
        sym_table = self.symbols_table(workspace_id)
        sym_predicate = (
            " OR ".join(self._path_prefix_clauses(prefixes))
            if self._uses_workspace_symbol_partition(sym_table)
            else predicate
        )

        target_uids = self.list_symbol_uids_by_prefixes(workspace_id, prefixes)
        incident_uids = self.find_incident_axis_adjacency_uids(workspace_id, target_uids)
        total_rows = self.count_axis_adjacency_workspace(workspace_id)
        use_full_reset = (
            total_rows > 0
            and len(incident_uids) / total_rows > LANCEDB_AXIS_ADJACENCY_PARTIAL_RESET_MAX_RATIO
        )

        if progress_callback:
            progress_callback(f"delete docs paths={len(prefixes)}")
        try:
            self._table.delete(predicate)
        except Exception:
            pass
        if progress_callback:
            progress_callback(f"delete symbols paths={len(prefixes)}")
        try:
            sym_table.delete(sym_predicate)
        except Exception:
            pass
        if getattr(self, "_symbol_axis_columns", set()):
            try:
                self._symbol_chunks_table.delete(predicate)
            except Exception:
                pass

        adj_table = self.axis_adjacency_table(workspace_id)
        if use_full_reset:
            self._reset_axis_adjacency_for_workspace(workspace_id, adj_table)
        else:
            self.delete_axis_adjacency_uids(workspace_id, incident_uids)

        if not use_full_reset and db is not None:
            survivors = incident_uids - target_uids
            if self._rematerialize_adjacency_survivors(db, workspace_id, survivors):
                return

        self._invalidate_adjacency_graph_cache(
            workspace_id,
            use_full_reset=use_full_reset,
            incident_uids=incident_uids,
        )

    def delete_symbol_embeddings(
        self,
        uids: list[str],
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Remove symbol embedding rows for deleted symbols."""
        self._delete_symbol_rows(uids, workspace_id)
        self._delete_symbol_chunk_rows(uids, workspace_id)

    def search_symbol_chunks_by_vector(
        self,
        vector: list[float],
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        limit: int = 24,
        threshold: float = 1.45,
    ) -> list[dict]:
        """Search semantic fragments and return source-attributed owner hits."""
        if not getattr(self, "_symbol_axis_columns", set()) or limit <= 0:
            return []
        if hasattr(vector, "tolist"):
            vector = vector.tolist()
        ws = self._quote_delete_value(workspace_id)
        try:
            rows = (
                self._symbol_chunks_table.search(vector)
                .where(f"workspace_id = '{ws}'", prefilter=True)
                .limit(limit)
                .to_list()
            )
        except Exception:
            return []
        output: list[dict] = []
        for row in rows:
            meta_str = row.get("embedding_metadata")
            if meta_str:
                try:
                    metadata_dict = json.loads(meta_str)
                    indexed_model = metadata_dict.get("model_name")
                    if indexed_model and indexed_model != EMBED_MODEL:
                        raise EmbeddingModelMismatch(
                            f"Query embedding uses {EMBED_MODEL} but semantic chunks use "
                            f"{indexed_model}. Re-index the workspace."
                        )
                except json.JSONDecodeError:
                    pass
            distance = float(row.get("_distance", 1.0))
            if distance > threshold:
                continue
            output.append(
                {
                    "chunk_uid": str(row.get("chunk_uid") or ""),
                    "owner_uid": str(row.get("owner_uid") or ""),
                    "name": str(row.get("name") or ""),
                    "qualified_name": str(row.get("qualified_name") or ""),
                    "file_path": str(row.get("file_path") or ""),
                    "start_line": int(row.get("start_line") or 0),
                    "end_line": int(row.get("end_line") or 0),
                    "distance": distance,
                    "score": _l2_to_score(distance),
                }
            )
        return output

    def search_symbols(
        self,
        query: str,
        limit: int = 5,
        threshold: float = 0.4,
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> list[dict]:
        """Returns symbols semantically similar to query, with cosine distance."""
        vec = self._embed([query])[0]
        return self.search_symbols_by_vector(
            vec, limit=limit, threshold=threshold, workspace_id=workspace_id
        )

    def search_symbols_by_vector(
        self,
        vector: list[float],
        limit: int = 5,
        threshold: float = 0.4,
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> list[dict]:
        """Returns symbols semantically similar to a precomputed embedding vector."""
        if hasattr(vector, "tolist"):
            vector = vector.tolist()
        table = self.symbols_table(workspace_id)
        query = table.search(vector).limit(limit)
        if not self._uses_workspace_symbol_partition(table):
            query = query.where(
                f"workspace_id = '{self._quote_delete_value(workspace_id)}'",
                prefilter=True,
            )
        results = query.to_list()

        # Guard against cross-model queries (skip check for unversioned rows)
        for r in results:
            meta_str = r.get("embedding_metadata")
            if meta_str:
                try:
                    metadata_dict = json.loads(meta_str)
                    if (
                        metadata_dict.get("model_name")
                        and metadata_dict.get("model_name") != EMBED_MODEL
                    ):
                        raise EmbeddingModelMismatch(
                            f"Query embedding uses {EMBED_MODEL} but database has {metadata_dict.get('model_name')}. "
                            "Delete ./data/lancedb (or the workspace partition) and re-index."
                        )
                except json.JSONDecodeError:
                    pass

        out = []
        for r in results:
            distance = r.get("_distance", 1.0)
            if distance <= threshold:
                out.append(
                    {
                        "uid": r["uid"],
                        "name": r["name"],
                        "file_path": r["file_path"],
                        "distance": distance,
                        "score": _l2_to_score(float(distance)),
                    }
                )
        return out

    def _axis_symbol_search_query(self, vector: list[float], plan: "AxisQueryPlan", table):
        query = table.search(vector).limit(plan.limit)
        if self._uses_workspace_symbol_partition(table):
            bits_predicate = render_axis_bits_predicate(
                required_bits=plan.required_bits,
                container_kinds=plan.container_kinds,
            )
            if bits_predicate != "true":
                query = query.where(bits_predicate, prefilter=True)
            return query
        return query.where(plan.lance_predicate, prefilter=True)

    @staticmethod
    def _axis_symbol_search_hit(row: dict, threshold: float) -> dict | None:
        distance = row.get("_distance", 1.0)
        if distance > threshold:
            return None
        return {
            "uid": row["uid"],
            "name": row["name"],
            "file_path": row["file_path"],
            "distance": distance,
            "score": _l2_to_score(float(distance)),
            "cfg_bits": list(row.get("cfg_bits") or []),
            "dfg_bits": list(row.get("dfg_bits") or []),
            "struct_bits": list(row.get("struct_bits") or []),
            "container_kinds": list(row.get("container_kinds") or []),
            "axis_container_kinds_json": str(row.get("axis_container_kinds_json") or "[]"),
            "axis_contracts_json": str(row.get("axis_contracts_json") or "[]"),
        }

    def search_axis_symbols_by_vector(
        self,
        vector: list[float],
        plan: "AxisQueryPlan",
        *,
        threshold: float = 0.4,
    ) -> list[dict]:
        """Vector search over axis symbol rows using a compiled axis plan."""
        if not self._symbol_axis_columns:
            raise ValueError("Axis symbol search requires an axis index profile")
        if hasattr(vector, "tolist"):
            vector = vector.tolist()
        workspace_id = plan.workspace_id or DEFAULT_WORKSPACE_ID
        table = self.symbols_table(workspace_id)
        query = self._axis_symbol_search_query(vector, plan, table)
        results = query.to_list()
        out: list[dict] = []
        for row in results:
            hit = self._axis_symbol_search_hit(row, threshold)
            if hit is not None:
                out.append(hit)
        return out

    def search_axis_symbols(
        self,
        query: str,
        plan: "AxisQueryPlan",
        *,
        threshold: float = 0.4,
    ) -> list[dict]:
        """Embed query text and search axis symbol rows with a compiled plan."""
        vec = self._embed([query])[0]
        return self.search_axis_symbols_by_vector(vec, plan, threshold=threshold)
