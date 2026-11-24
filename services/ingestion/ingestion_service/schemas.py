"""Pydantic response models for documents and jobs (uploads are multipart, so there is no request model)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from ingestion_service.models import DocumentStatus, JobStatus


class DocumentOut(BaseModel):
    """Public view of a document (no tenant_id or content hash)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    file_type: str
    size_bytes: int
    pages: int | None
    chunk_count: int
    status: DocumentStatus
    error: str | None
    uploaded_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class JobOut(BaseModel):
    """Public view of an ingestion job."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    status: JobStatus
    error: str | None
    total_chunks: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None


class UploadedJob(BaseModel):
    """One accepted file and the job that processes it."""

    job_id: uuid.UUID
    document_id: uuid.UUID
    filename: str


class UploadResponse(BaseModel):
    """Response of POST /documents (202)."""

    jobs: list[UploadedJob]
