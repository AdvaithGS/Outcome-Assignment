"""HTTP API (contract/openapi.yaml): POST /jobs, GET /jobs/{job_id}, GET /health.

    uvicorn app.service.api:factory --factory --host 0.0.0.0 --port 8080

POST /jobs only stores the upload and inserts a PENDING row, so it returns well inside 2 s; extraction runs in
the workers. With EMBEDDED_WORKERS=1 the API process also runs a worker (single-process mode for development
and tests); in docker compose the workers are a separate service sharing the data volume.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
import warnings
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.hashing import canonical_json, sha256_hex
from app.pipeline.base import SchemaError, validate_schema
from app.service.config import Settings
from app.service.filetype import detect
from app.service.logs import setup_logging
from app.service.store import JobStore, iso

log = logging.getLogger("api")
# FastAPI builds a model with a form field called "schema"; pydantic warns about the name. Harmless.
warnings.filterwarnings("ignore", message='Field name "schema"')
EXT = {"pdf": ".pdf", "docx": ".docx", "xlsx": ".xlsx", "csv": ".csv"}


def error(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status, headers=headers)


def job_status(job: dict) -> dict:
    out = {"job_id": job["id"], "status": job["status"], "stage": job["stage"],
           "created_at": iso(job["created_at"]), "updated_at": iso(job["updated_at"]), "result": None, "error": None}
    if job["status"] in ("SUCCESS", "PARTIAL") and job["result_path"]:
        out["result"] = json.loads(Path(job["result_path"]).read_text(encoding="utf-8"))
    if job["status"] == "FAILED":
        out["error"] = {"code": job["error_code"] or "failed", "message": job["error_message"] or ""}
    return out


def create_app(settings: Settings | None = None, store: JobStore | None = None,
               embedded_workers: bool | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or JobStore(settings.db_path)
    if embedded_workers is None:
        embedded_workers = os.environ.get("EMBEDDED_WORKERS", "0") == "1"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = asyncio.Event()
        task = None
        if embedded_workers:
            from app.service.worker import Worker
            task = asyncio.create_task(Worker(settings, store).run(stop))
        log.info("api started", extra={"version": settings.version, "embedded_workers": embedded_workers})
        yield
        stop.set()
        if task:
            await task

    app = FastAPI(title="Document Extraction Service", version=settings.version, lifespan=lifespan)
    app.state.settings, app.state.store = settings, store

    @app.exception_handler(RequestValidationError)
    async def _bad_request(request: Request, exc: RequestValidationError):
        return error(400, "malformed_request", str(exc.errors())[:500])

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": settings.version}

    @app.post("/jobs", status_code=202)
    async def create_job(file: UploadFile | None = File(None), schema: str | None = Form(None),
                         webhook_url: str | None = Form(None),
                         x_organization_id: str | None = Header(None)):
        t0 = time.perf_counter()
        org = x_organization_id or ""
        if file is None or schema is None:
            return error(400, "missing_field", "multipart fields 'file' and 'schema' are required")
        try:
            schema_obj = validate_schema(json.loads(schema))
        except json.JSONDecodeError as exc:
            return error(400, "invalid_schema", f"schema is not valid JSON: {exc}")
        except SchemaError as exc:
            return error(400, "invalid_schema", str(exc))

        # stream the upload to disk, hashing and enforcing the size limit as we go
        h, size = hashlib.sha256(), 0
        fd, tmp = tempfile.mkstemp(dir=settings.uploads_dir, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as f:
                while chunk := await file.read(1 << 20):
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        return error(413, "file_too_large", f"file exceeds {settings.max_upload_bytes // (1 << 20)} MB")
                    h.update(chunk)
                    f.write(chunk)
            if size == 0:
                return error(400, "empty_file", "the uploaded file is empty")
            ftype = detect(Path(tmp), file.filename or "")
            if ftype is None:
                return error(400, "unsupported_type", "file must be a PDF, DOCX, XLSX or CSV")
            file_sha = h.hexdigest()
            final = settings.uploads_dir / f"{file_sha}{EXT[ftype]}"
            if final.exists():
                os.unlink(tmp)
            else:
                os.replace(tmp, final)
            tmp = None
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

        if await asyncio.to_thread(store.pending_count) >= settings.queue_limit:
            return error(429, "queue_full", "too many queued jobs; retry later", headers={"Retry-After": "30"})

        idem_key = sha256_hex(file_sha + canonical_json(schema_obj))
        job, created = await asyncio.to_thread(
            store.create, org=org, idem_key=idem_key, file_path=str(final), file_name=file.filename or final.name,
            file_type=ftype, schema_json=json.dumps(schema_obj), webhook_url=webhook_url)
        log.info("job accepted" if created else "duplicate submission; returning existing job",
                 extra={"job_id": job["id"], "org": org, "file": file.filename, "file_type": ftype, "bytes": size,
                        "ms": int((time.perf_counter() - t0) * 1000)})
        return JSONResponse(job_status(job), status_code=202)

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str, x_organization_id: str | None = Header(None)):
        job = await asyncio.to_thread(store.get, job_id, x_organization_id or "")
        if job is None:
            return error(404, "not_found", "unknown job id")
        return job_status(job)

    return app


def factory() -> FastAPI:
    """uvicorn entry point: `uvicorn app.service.api:factory --factory --host 0.0.0.0 --port 8080`."""
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    return create_app()
