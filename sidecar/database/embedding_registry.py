"""Model registry and metadata tracking for LanceDB embeddings."""

import hashlib
from dataclasses import dataclass


@dataclass
class EmbeddingModel:
    """Known embedding model metadata."""

    name: str  # HuggingFace model ID or identifier
    version: str  # Model version (e.g. "1.0", "2.2")
    dimensions: int  # Vector dimension (e.g. 384, 768)


# Registry of known embedding models
KNOWN_MODELS = {
    "all-MiniLM-L6-v2": EmbeddingModel(
        name="sentence-transformers/all-MiniLM-L6-v2",
        version="2.2",
        dimensions=384,
    ),
    "bge-code": EmbeddingModel(
        name="BAAI/bge-code-v1.5",
        version="1.5",
        dimensions=768,
    ),
    "unixcoder": EmbeddingModel(
        name="microsoft/unixcoder-base",
        version="1.0",
        dimensions=768,
    ),
}


class EmbeddingModelMismatch(Exception):
    """Raised when querying across embeddings from different models."""

    pass


@dataclass
class EmbeddingMetadata:
    """Metadata about an embedding for tracking and validation."""

    model_name: str  # Key into KNOWN_MODELS (e.g. "all-MiniLM-L6-v2")
    model_version: str  # Model version string
    chunk_hash: str  # SHA256(content)
    embedding_hash: str  # SHA256(embedding bytes)


def compute_chunk_hash(content: str) -> str:
    """Compute SHA256 hash of chunk content."""
    return hashlib.sha256(content.encode()).hexdigest()


def compute_embedding_hash(embedding: list[float]) -> str:
    """Compute SHA256 hash of embedding vector."""
    # Convert to bytes for hashing
    import struct

    embedding_bytes = b"".join(struct.pack("f", x) for x in embedding)
    return hashlib.sha256(embedding_bytes).hexdigest()


def get_model_metadata(model_name: str) -> EmbeddingModel | None:
    """Look up model in registry. Returns None if not found."""
    return KNOWN_MODELS.get(model_name)
