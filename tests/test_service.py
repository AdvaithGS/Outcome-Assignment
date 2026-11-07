"""Service guarantees the brief singles out: async job model, terminal states never change, no lost or
duplicated jobs, crash recovery, idempotency, tenant isolation, sane error handling, valid output."""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.pipeline import base as pipeline_base
from app.pipeline.base import DocumentError, PipelineOutput
from app.service import worker as worker_mod
from app.service.api import create_app
from app.service.config import Settings
from app.service.store import JobStore
from app.service.worker import Worker

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("validate", ROOT / "tools" / "validate.py")
validate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate)

SCHEMA = {"name": "t", "fields": {"code": {"name": "Code", "description": "Unit code", "primary": True},
                                  "rent": {"name": "Rent", "description": "Rent as a number"}}}
CSV = b"Code,Rent\nA1,1000\nB2,1200\n"


def settings(tmp_path, **kw) -> Settings:
    s = Settings(data_dir=tmp_path, **{"poll_s": 0.02, "heartbeat_s": 0.05, "lease_s": 5.0, **kw})
    for d in (s.data_dir, s.uploads_dir, s.results_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s


def tiny_pdf() -> bytes:
    import pymupdf
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Base Rent: $4,250.00 per month")
    return doc.tobytes()


def new_job(store: JobStore, key: str = "k", org: str = "") -> dict:
    return store.create(org=org, idem_key=key, file_path="/dev/null", file_name="x.csv", file_type="csv",
                        schema_json=json.dumps(SCHEMA), webhook_url=None)[0]


# ------------------------------------------------------------------ store

def test_concurrent_claims_never_hand_out_a_job_twice(tmp_path):
    store = JobStore(tmp_path / "j.sqlite")
    ids = {new_job(store, key=f"k{i}")["id"] for i in range(40)}
    claimed: list[str] = []
    lock = threading.Lock()

    def claimer(n):
        while (job := store.claim(f"w{n}", 60, 3)) is not None:
            with lock:
                claimed.append(job["id"])

    ts = [threading.Thread(target=claimer, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(claimed) == sorted(ids)            # every job claimed exactly once


def test_expired_lease_is_reclaimed_and_stale_worker_is_fenced(tmp_path):
    store = JobStore(tmp_path / "j.sqlite")
    job = new_job(store)
    assert store.claim("w1", 0.05, 3)["id"] == job["id"]
    time.sleep(0.1)                                   # w1 "crashes": lease expires
    j2 = store.claim("w2", 60, 3)
    assert j2["id"] == job["id"] and j2["attempts"] == 2
    assert not store.finish(job["id"], "w1", "SUCCESS")   # stale worker cannot finish
    assert not store.heartbeat(job["id"], "w1", 60)
    assert store.finish(job["id"], "w2", "SUCCESS", result_path="r.json")


def test_terminal_states_never_change(tmp_path):
    store = JobStore(tmp_path / "j.sqlite")
    job = new_job(store)
    store.claim("w", 60, 3)
    assert store.finish(job["id"], "w", "PARTIAL", result_path="r.json")
    for status in ("SUCCESS", "FAILED", "PARTIAL"):
        assert not store.finish(job["id"], "w", status)
    assert not store.release(job["id"], "w", "x") and not store.set_stage(job["id"], "w", "X")
    assert store.claim("w2", 60, 3) is None
    assert store.get(job["id"], "")["status"] == "PARTIAL"


def test_poison_job_is_failed_after_max_attempts(tmp_path):
    store = JobStore(tmp_path / "j.sqlite")
    job = new_job(store)
    for w in ("w1", "w2"):
        assert store.claim(w, 0.01, 2)["id"] == job["id"]
        time.sleep(0.03)
    assert store.claim("w3", 60, 2) is None
    got = store.get(job["id"], "")
    assert got["status"] == "FAILED" and got["error_code"] == "worker_lost"


def test_idempotency_and_tenant_isolation(tmp_path):
    store = JobStore(tmp_path / "j.sqlite")
    a = new_job(store, key="same", org="acme")
    assert new_job(store, key="same", org="acme")["id"] == a["id"]          # duplicate → same job
    assert new_job(store, key="same", org="other")["id"] != a["id"]         # other tenant → own job
    assert store.get(a["id"], "other") is None and store.get(a["id"], "acme") is not None
    store.claim("w", 60, 3)
    store.finish(a["id"], "w", "FAILED", error_code="x")
    assert new_job(store, key="same", org="acme")["id"] != a["id"]          # a failed job may be retried


# ------------------------------------------------------------------ API end to end (embedded worker)

def poll(client, job_id, headers=None, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = client.get(f"/jobs/{job_id}", headers=headers or {})
        if r.json()["status"] in ("SUCCESS", "PARTIAL", "FAILED"):
            return r.json()
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_api_end_to_end_contract(tmp_path):
    app = create_app(settings(tmp_path), embedded_workers=True)
    with TestClient(app) as c:
        assert c.get("/health").json()["status"] == "ok"
        t0 = time.perf_counter()
        r = c.post("/jobs", files={"file": ("box.csv", CSV, "text/csv")}, data={"schema": json.dumps(SCHEMA)},
                   headers={"X-Organization-Id": "acme"})
        assert r.status_code == 202 and time.perf_counter() - t0 < 2.0
        body = r.json()
        assert body["status"] in ("PENDING", "RUNNING") and body["created_at"].endswith("Z")
        done = poll(c, body["job_id"], {"X-Organization-Id": "acme"})
        assert done["status"] == "SUCCESS" and done["error"] is None
        assert validate.validate_contract(done["result"], SCHEMA) == []       # the official validator's checks
        # duplicate submission returns the same job; other tenants and unknown ids get 404
        again = c.post("/jobs", files={"file": ("other-name.csv", CSV, "text/csv")},
                       data={"schema": json.dumps(SCHEMA)}, headers={"X-Organization-Id": "acme"})
        assert again.json()["job_id"] == body["job_id"]
        assert c.get(f"/jobs/{body['job_id']}", headers={"X-Organization-Id": "evil"}).status_code == 404
        assert c.get(f"/jobs/{body['job_id']}").status_code == 404
        assert c.get("/jobs/nope").status_code == 404
        # a PDF goes through the same endpoint
        rp = c.post("/jobs", files={"file": ("doc.bin", tiny_pdf(), "application/octet-stream")},
                    data={"schema": json.dumps(SCHEMA)})
        assert rp.status_code == 202 and poll(c, rp.json()["job_id"])["status"] == "SUCCESS"


@pytest.mark.parametrize("files,data,status,code", [
    ({}, {"schema": json.dumps(SCHEMA)}, 400, "missing_field"),
    ({"file": ("a.csv", CSV, "text/csv")}, {}, 400, "missing_field"),
    ({"file": ("a.csv", CSV, "text/csv")}, {"schema": "{not json"}, 400, "invalid_schema"),
    ({"file": ("a.csv", CSV, "text/csv")}, {"schema": json.dumps({"fields": {}})}, 400, "invalid_schema"),
    ({"file": ("a.doc", b"\xd0\xcf\x11\xe0\x00\x00binary", "application/msword")}, {"schema": json.dumps(SCHEMA)},
     400, "unsupported_type"),
    ({"file": ("a.csv", b"", "text/csv")}, {"schema": json.dumps(SCHEMA)}, 400, "empty_file"),
])
def test_api_rejects_malformed_requests(tmp_path, files, data, status, code):
    with TestClient(create_app(settings(tmp_path), embedded_workers=False)) as c:
        r = c.post("/jobs", files=files, data=data)
        assert r.status_code == status and r.json()["code"] == code


def test_api_413_on_large_file(tmp_path):
    s = replace(settings(tmp_path), max_upload_bytes=1024)
    with TestClient(create_app(s, embedded_workers=False)) as c:
        r = c.post("/jobs", files={"file": ("a.csv", b"a,b\n" * 1000, "text/csv")}, data={"schema": json.dumps(SCHEMA)})
        assert r.status_code == 413
        assert not [p for p in s.uploads_dir.iterdir()]          # no partial upload left behind


# ------------------------------------------------------------------ worker outcome policy

class Raises:
    name = "raises"

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    async def run(self, path, schema, ctx):
        self.calls += 1
        raise self.exc


class Slow:
    name = "slow"

    async def run(self, path, schema, ctx):
        await asyncio.sleep(5)
        return PipelineOutput(rows=[{"code": {"value": "late"}}])


def run_one_job(tmp_path, pipeline, monkeypatch, **kw):
    s = settings(tmp_path, **kw)
    store = JobStore(s.db_path)
    job = new_job(store)
    monkeypatch.setitem(pipeline_base.PIPELINES, "null", lambda: pipeline)
    w = Worker(s, store, worker_id="w")

    async def go():
        stop = asyncio.Event()
        t = asyncio.create_task(w.run(stop))
        for _ in range(400):
            if store.get(job["id"], "")["status"] in ("SUCCESS", "PARTIAL", "FAILED"):
                break
            await asyncio.sleep(0.02)
        stop.set()
        await t

    asyncio.run(go())
    return store.get(job["id"], ""), s


def test_unreadable_document_fails_with_reason(tmp_path, monkeypatch):
    job, _ = run_one_job(tmp_path, Raises(DocumentError("encrypted PDF")), monkeypatch)
    assert job["status"] == "FAILED" and job["error_code"] == "unreadable_document"
    assert "encrypted" in job["error_message"]


def test_pipeline_bug_is_retried_then_returns_valid_nulls(tmp_path, monkeypatch):
    p = Raises(RuntimeError("boom"))
    job, _ = run_one_job(tmp_path, p, monkeypatch, max_attempts=3)
    assert p.calls == 3 and job["status"] == "PARTIAL"
    result = json.loads(Path(job["result_path"]).read_text())
    assert validate.validate_contract(result, SCHEMA) == []
    assert all(f["value"] is None for f in result["entries"][0].values())


def test_internal_deadline_finishes_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_mod, "MIN_RUN_S", 0.2)
    job, _ = run_one_job(tmp_path, Slow(), monkeypatch, job_deadline_s=0.2)
    assert job["status"] == "PARTIAL"
    assert validate.validate_contract(json.loads(Path(job["result_path"]).read_text()), SCHEMA) == []


def test_job_of_a_crashed_worker_is_finished_by_another(tmp_path, monkeypatch):
    s = settings(tmp_path, lease_s=0.05)
    store = JobStore(s.db_path)
    job = new_job(store)
    assert store.claim("dead-worker", 0.05, 3)["id"] == job["id"]     # claimed, then the worker dies
    time.sleep(0.1)
    w = Worker(s, store, worker_id="healthy")

    async def go():
        stop = asyncio.Event()
        t = asyncio.create_task(w.run(stop))
        for _ in range(200):
            if store.get(job["id"], "")["status"] == "SUCCESS":
                break
            await asyncio.sleep(0.02)
        stop.set()
        await t

    asyncio.run(go())
    got = store.get(job["id"], "")
    assert got["status"] == "SUCCESS" and got["attempts"] == 2
