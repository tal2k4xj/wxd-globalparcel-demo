"""Serve realtime operations: Cassandra ledger + OpenSearch customer tracking."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from cassandra.cluster import Cluster
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from opensearchpy import OpenSearch

CASSANDRA_HOST = os.environ.get("CASSANDRA_HOST", "127.0.0.1")
CASSANDRA_PORT = int(os.environ.get("CASSANDRA_PORT", "9042"))
CASSANDRA_KEYSPACE = os.environ.get("CASSANDRA_KEYSPACE", "globalparcel_ops")
OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
OPENSEARCH_INDEX = os.environ.get("OPENSEARCH_INDEX", "parcel-events-live")

cluster: Cluster | None = None
session = None
os_client: OpenSearch | None = None


def ensure_cassandra() -> bool:
    """Connect (or reconnect) to the ledger. Keyspace appears only after shift_left runs."""
    global cluster, session
    if session is not None:
        return True
    try:
        if cluster is not None:
            try:
                cluster.shutdown()
            except Exception:
                pass
            cluster = None
        cluster = Cluster([CASSANDRA_HOST], port=CASSANDRA_PORT)
        session = cluster.connect(CASSANDRA_KEYSPACE)
        return True
    except Exception:
        cluster = None
        session = None
        return False


def ensure_opensearch() -> bool:
    global os_client
    if os_client is not None:
        return True
    try:
        os_client = OpenSearch(hosts=[OPENSEARCH_URL])
        os_client.info()
        return True
    except Exception:
        os_client = None
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global cluster, session, os_client
    ensure_cassandra()
    ensure_opensearch()
    try:
        yield
    finally:
        if os_client is not None:
            os_client.close()
            os_client = None
        if cluster is not None:
            cluster.shutdown()
            cluster = None
            session = None


app = FastAPI(title="Global Parcel Realtime Ops", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

frontend = Path(__file__).resolve().parent / "frontend"
app.mount(
    "/audit-ui",
    StaticFiles(directory=frontend / "audit-reconciliation", html=True),
    name="audit-ui",
)
app.mount(
    "/customer-ui",
    StaticFiles(directory=frontend / "customer-tracking", html=True),
    name="customer-ui",
)


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/customer-ui/")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "cassandra": "ok" if ensure_cassandra() else "down",
        "opensearch": "ok" if ensure_opensearch() else "down",
    }


@app.get("/api/audit/{parcel_id}")
def get_audit(parcel_id: str) -> dict[str, Any]:
    if not ensure_cassandra():
        raise HTTPException(status_code=503, detail="Cassandra session not ready")

    rows = session.execute(
        """
        SELECT parcel_id, event_ts, status, hub_code, region, latitude, longitude,
               exception_code, customer_eta, delivery_note
        FROM parcel_events_by_parcel
        WHERE parcel_id = %s
        """,
        [parcel_id.strip().upper()],
    )
    events = list(rows)
    if not events:
        raise HTTPException(status_code=404, detail=f"No Cassandra events found for parcel {parcel_id}")

    timeline = [
        {
            "event_ts": r.event_ts.isoformat() if r.event_ts else None,
            "status": r.status,
            "hub_code": r.hub_code,
            "region": r.region,
            "geo_position": {
                "latitude": r.latitude,
                "longitude": r.longitude,
            },
            "exception_code": r.exception_code or "",
            "customer_eta": r.customer_eta.isoformat() if r.customer_eta else None,
            "delivery_note": getattr(r, "delivery_note", None) or "",
        }
        for r in events
    ]

    latest = timeline[0]
    # What the customer sees right now: the newest searchable scan in OpenSearch.
    customer = latest_customer_scan(parcel_id.strip().upper())
    if customer is None:
        app_status = "UNAVAILABLE"
        result = "Customer index unavailable"
        mismatch = False
    else:
        app_status = customer.get("status") or "NOT_INDEXED"
        mismatch = app_status != latest["status"]
        result = "Mismatch detected" if mismatch else "No mismatch"

    return {
        "parcel_id": parcel_id.strip().upper(),
        "summary": {
            "customer_claim": 'Customer says "Not delivered"',
            "customer_app_status": app_status,
            "customer_app_event_ts": (customer or {}).get("event_ts"),
            "cassandra_latest_status": latest["status"],
            "cassandra_latest_event_ts": latest["event_ts"],
            "reconciliation_result": result,
            "index_lag_suspected": mismatch,
        },
        "timeline": timeline,
    }


def latest_customer_scan(parcel_id: str) -> dict[str, Any] | None:
    """Newest scan OpenSearch serves for this parcel; {} if none indexed, None if unreachable."""
    if not ensure_opensearch():
        return None
    try:
        result = os_client.search(
            index=OPENSEARCH_INDEX,
            body={
                "size": 1,
                "sort": [{"event_ts": {"order": "desc"}}],
                "query": {"term": {"parcel_id": parcel_id}},
                "_source": ["status", "event_ts"],
            },
        )
    except Exception:
        return None
    hits = result.get("hits", {}).get("hits", [])
    return hits[0]["_source"] if hits else {}


@app.get("/api/search")
def search_notes(q: str, size: int = 20) -> dict[str, Any]:
    """Ops full-text search over driver notes and hub cities (OpenSearch).

    simple_query_string keeps the syntax forgiving: "exact phrase", -exclude,
    a|b, and a trailing * for prefixes. Results collapse to one hit per parcel
    (its best-matching scan); aggregations show where the matches cluster.
    """
    if not ensure_opensearch():
        raise HTTPException(status_code=503, detail="OpenSearch client not ready")
    q = q.strip()
    if not q:
        raise HTTPException(status_code=400, detail="Empty query")

    body = {
        "size": max(1, min(size, 50)),
        "query": {
            "simple_query_string": {
                "query": q,
                "fields": ["delivery_note", "hub_city^2"],
                "default_operator": "and",
            }
        },
        "collapse": {"field": "parcel_id"},
        "sort": ["_score", {"event_ts": {"order": "desc"}}],
        "highlight": {
            "encoder": "html",
            "pre_tags": ["<mark>"],
            "post_tags": ["</mark>"],
            "fields": {"delivery_note": {"number_of_fragments": 0}},
        },
        "aggs": {
            "parcels": {"cardinality": {"field": "parcel_id"}},
            "by_hub": {"terms": {"field": "hub_code", "size": 12}},
            "by_status": {"terms": {"field": "status", "size": 8}},
        },
        "track_total_hits": True,
    }
    result = os_client.search(index=OPENSEARCH_INDEX, body=body)
    hits = result.get("hits", {})
    aggs = result.get("aggregations", {})
    return {
        "query": q,
        "matching_scans": hits.get("total", {}).get("value", 0),
        "matching_parcels": aggs.get("parcels", {}).get("value", 0),
        "took_ms": result.get("took"),
        "by_hub": [{"key": b["key"], "count": b["doc_count"]} for b in aggs.get("by_hub", {}).get("buckets", [])],
        "by_status": [{"key": b["key"], "count": b["doc_count"]} for b in aggs.get("by_status", {}).get("buckets", [])],
        "results": [
            {
                "parcel_id": h["_source"].get("parcel_id"),
                "event_ts": h["_source"].get("event_ts"),
                "status": h["_source"].get("status"),
                "hub_code": h["_source"].get("hub_code"),
                "hub_city": h["_source"].get("hub_city"),
                "sla_risk": h["_source"].get("sla_risk"),
                "delivery_note": h["_source"].get("delivery_note") or "",
                "highlight": (h.get("highlight", {}).get("delivery_note") or [""])[0],
            }
            for h in hits.get("hits", [])
        ],
    }


@app.get("/api/customer/{parcel_id}")
def get_customer_view(parcel_id: str) -> dict[str, Any]:
    if not ensure_opensearch():
        raise HTTPException(status_code=503, detail="OpenSearch client not ready")

    query = {
        "size": 100,
        "sort": [{"event_ts": {"order": "desc"}}],
        "query": {"term": {"parcel_id": parcel_id.strip().upper()}},
    }
    result = os_client.search(index=OPENSEARCH_INDEX, body=query)
    hits = result.get("hits", {}).get("hits", [])
    if not hits:
        raise HTTPException(status_code=404, detail=f"No customer events found for {parcel_id}")

    timeline = []
    for hit in hits:
        source = hit.get("_source", {})
        geo = source.get("geo_position") or {}
        timeline.append(
            {
                "event_ts": source.get("event_ts"),
                "status": source.get("status"),
                "hub_code": source.get("hub_code"),
                "region": source.get("region"),
                "exception_code": source.get("exception_code") or "",
                "customer_eta": source.get("customer_eta"),
                "geo_position": {
                    "latitude": geo.get("lat"),
                    "longitude": geo.get("lon"),
                },
            }
        )

    latest = timeline[0]
    return {
        "parcel_id": parcel_id.strip().upper(),
        "latest_status": latest.get("status") or "UNKNOWN",
        "latest_hub": latest.get("hub_code") or "-",
        "customer_eta": latest.get("customer_eta"),
        "latest_event_ts": latest.get("event_ts"),
        "timeline": timeline,
    }
