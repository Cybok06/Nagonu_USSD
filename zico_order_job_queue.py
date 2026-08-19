from __future__ import annotations

import socket
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict

from pymongo import ASCENDING, ReturnDocument

from zico_db import db


provider_jobs_col = db["provider_jobs"]

_PROCESSORS: Dict[str, Callable[[Dict[str, Any]], None]] = {}
_INDEXES_READY = False
_START_LOCK = threading.Lock()
_WORKER_STARTED = False


def _now() -> datetime:
    return datetime.utcnow()


def _worker_id() -> str:
    return f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


def _ensure_indexes() -> None:
    global _INDEXES_READY
    if _INDEXES_READY:
        return
    try:
        provider_jobs_col.create_index(
            [("status", ASCENDING), ("available_at", ASCENDING), ("created_at", ASCENDING)],
            background=True,
        )
        provider_jobs_col.create_index([("job_type", ASCENDING), ("order_id", ASCENDING)], background=True)
        provider_jobs_col.create_index([("locked_until", ASCENDING)], background=True)
        provider_jobs_col.create_index([("finished_at", ASCENDING)], background=True)
    except Exception:
        pass
    _INDEXES_READY = True


def register_job_processor(job_type: str, processor: Callable[[Dict[str, Any]], None]) -> None:
    _PROCESSORS[str(job_type or "").strip()] = processor


def enqueue_provider_jobs(order_id: str, api_jobs: list[dict], *, source: str = "checkout") -> str | None:
    if not order_id or not api_jobs:
        return None
    _ensure_indexes()
    now = _now()
    doc = {
        "job_type": "provider_dispatch",
        "order_id": str(order_id),
        "source": str(source or "checkout"),
        "payload": {
            "order_id": str(order_id),
            "api_jobs": list(api_jobs),
        },
        "status": "queued",
        "attempt_count": 0,
        "max_attempts": 4,
        "available_at": now,
        "locked_until": None,
        "last_error": "",
        "created_at": now,
        "updated_at": now,
        "history": [
            {
                "status": "queued",
                "created_at": now,
                "note": f"Queued {len(api_jobs)} provider job(s).",
            }
        ],
    }
    inserted = provider_jobs_col.insert_one(doc)
    return str(inserted.inserted_id)


def _claim_next_job(worker_name: str) -> Dict[str, Any] | None:
    now = _now()
    stale_before = now - timedelta(minutes=10)
    lock_until = now + timedelta(minutes=15)
    query = {
        "$or": [
            {"status": "queued", "available_at": {"$lte": now}},
            {"status": "processing", "locked_until": {"$lt": now}, "updated_at": {"$lt": stale_before}},
        ]
    }
    update = {
        "$set": {
            "status": "processing",
            "worker_id": worker_name,
            "locked_until": lock_until,
            "started_at": now,
            "updated_at": now,
        },
        "$inc": {"attempt_count": 1},
        "$push": {
            "history": {
                "status": "processing",
                "created_at": now,
                "worker_id": worker_name,
            }
        },
    }
    try:
        return provider_jobs_col.find_one_and_update(
            query,
            update,
            sort=[("available_at", ASCENDING), ("created_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
    except Exception:
        return None


def _complete_job(job_id: Any) -> None:
    now = _now()
    provider_jobs_col.update_one(
        {"_id": job_id},
        {
            "$set": {
                "status": "completed",
                "finished_at": now,
                "updated_at": now,
                "locked_until": None,
            },
            "$push": {"history": {"status": "completed", "created_at": now}},
        },
    )


def _fail_job(job: Dict[str, Any], exc: Exception) -> None:
    now = _now()
    attempt_count = int(job.get("attempt_count") or 0)
    max_attempts = max(1, int(job.get("max_attempts") or 1))
    final_failure = attempt_count >= max_attempts
    next_status = "failed" if final_failure else "queued"
    retry_delay_seconds = min(300, 10 * attempt_count)
    next_available = now if final_failure else now + timedelta(seconds=retry_delay_seconds)
    err_text = "".join(traceback.format_exception_only(type(exc), exc)).strip() or str(exc)
    provider_jobs_col.update_one(
        {"_id": job.get("_id")},
        {
            "$set": {
                "status": next_status,
                "updated_at": now,
                "finished_at": now if final_failure else None,
                "available_at": next_available,
                "locked_until": None,
                "last_error": err_text[:2000],
            },
            "$push": {
                "history": {
                    "status": next_status,
                    "created_at": now,
                    "error": err_text[:1000],
                    "retry_in_seconds": 0 if final_failure else retry_delay_seconds,
                }
            },
        },
    )


def _run_job(job: Dict[str, Any]) -> None:
    job_type = str(job.get("job_type") or "").strip()
    processor = _PROCESSORS.get(job_type)
    if not processor:
        raise RuntimeError(f"No processor registered for job type '{job_type}'")
    payload = job.get("payload")
    if not isinstance(payload, dict):
        raise RuntimeError("Job payload is missing or invalid")
    processor(payload)


def run_provider_job_worker_forever() -> None:
    worker_name = _worker_id()
    while True:
        try:
            _ensure_indexes()
            job = _claim_next_job(worker_name)
            if not job:
                time.sleep(1.5)
                continue
            try:
                _run_job(job)
            except Exception as exc:
                _fail_job(job, exc)
                continue
            _complete_job(job.get("_id"))
        except Exception:
            time.sleep(2.0)


def start_provider_job_worker_thread() -> None:
    global _WORKER_STARTED
    with _START_LOCK:
        if _WORKER_STARTED:
            return
        _ensure_indexes()
        thread = threading.Thread(target=run_provider_job_worker_forever, name="provider-job-worker", daemon=True)
        thread.start()
        _WORKER_STARTED = True
