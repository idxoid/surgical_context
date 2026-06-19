"""Indexing and overlay request/response models."""

from typing import Any

from pydantic import BaseModel


class IndexRequest(BaseModel):
    project_path: str
    queue: bool = True


class IndexFileRequest(BaseModel):
    file_path: str
    queue: bool = True


class IndexFilesRequest(BaseModel):
    file_paths: list[str]
    queue: bool = True


class IndexGitDeltaRequest(BaseModel):
    project_path: str | None = None
    queue: bool = True


class IndexDocsRequest(BaseModel):
    docs_path: str


class StatusPathResponse(BaseModel):
    status: str
    path: str
    queued: int = 0
    coalesced: int = 0
    rejected: int = 0
    queue_depth: int = 0


class IndexFileResponse(BaseModel):
    status: str
    file_path: str
    job_id: int = 0
    workspace_id: str
    queue_depth: int = 0
    reason: str = ""


class IndexFilesResponse(BaseModel):
    status: str
    workspace_id: str
    results: list[dict[str, Any]]
    queued: int
    coalesced: int
    rejected: int
    queue_depth: int


class IndexQueueStatusResponse(BaseModel):
    status: str
    queue: dict[str, Any]


class OverlayRequest(BaseModel):
    file_path: str
    content: str
    dirty: bool = True


class OverlayResponse(BaseModel):
    file_path: str
    symbols: list[str]


class ClearOverlayResponse(BaseModel):
    cleared: str
