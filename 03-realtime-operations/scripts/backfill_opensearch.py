"""Re-index the customer search index from Kafka history.

Run from `03-realtime-operations/` (workshop venv active):

    PYTHONPATH=../01-streamhouse python scripts/backfill_opensearch.py

Kafka keeps every enriched scan, so a fresh consumer group can replay
`parcel.events.enriched` from the beginning and rebuild OpenSearch. Document
ids match the live transform, so existing scans are overwritten (now with the
full-text `delivery_note` and `hub_city`) instead of duplicated. Stops once
the replay has caught up.
"""

from __future__ import annotations

import time

from opensearchpy import helpers

from shared.config import OPENSEARCH_INDEX, TOPIC_ENRICHED
from shared.ibm_sinks import connect_opensearch, opensearch_doc
from shared.kafka_io import consumer


def main() -> None:
    client = connect_opensearch()
    replay = consumer(TOPIC_ENRICHED, f"opensearch-backfill-{int(time.time())}")
    # The transform keeps appending, so stop at the end offsets seen at start;
    # anything newer is already written by the live transform.
    while not replay.assignment():
        replay.poll(timeout_ms=500)
    replay.seek_to_beginning()
    end = replay.end_offsets(list(replay.assignment()))
    total = 0
    idle = 0
    try:
        while idle < 3 and any(replay.position(tp) < off for tp, off in end.items()):
            batches = replay.poll(timeout_ms=2000, max_records=2000)
            actions = [
                {"_index": OPENSEARCH_INDEX, "_id": doc_id, "_source": doc}
                for recs in batches.values()
                for rec in recs
                if (rec.value or {}).get("parcel_id")
                for doc_id, doc in [opensearch_doc(rec.value)]
            ]
            if not actions:
                idle += 1
                continue
            idle = 0
            helpers.bulk(client, actions)
            total += len(actions)
            print(f"re-indexed {total}", flush=True)
    finally:
        replay.close()
        client.indices.refresh(index=OPENSEARCH_INDEX)
        client.close()
    print(f"Done: {total} scans replayed from {TOPIC_ENRICHED} into {OPENSEARCH_INDEX}.")


if __name__ == "__main__":
    main()
