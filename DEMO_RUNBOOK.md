# Global Parcel — demo runbook

Before: `podman machine start podman-wxd`, then `./scripts/start_services.sh` and `./scripts/start_data_flow.sh`. Track parcel `PCL-LIVE-000001` the whole way through.

## 1. Kafka — capture
- Topics: http://localhost:8085/ui/clusters/globalparcel/all-topics
- Live scans: http://localhost:8085/ui/clusters/globalparcel/all-topics/parcel.events/messages
- Show `parcel.events`, `fuel.surcharge` (in) → `parcel.events.enriched`, `parcel.current`, `ops.sla.alerts` (out)
- For technical audiences:
  - Flink contract: [`01-streamhouse/transform/flink/shift_left.sql`](01-streamhouse/transform/flink/shift_left.sql)
  - Tableflow (Kafka → Iceberg): [`01-streamhouse/tableflow/materialize.py`](01-streamhouse/tableflow/materialize.py)

## 2. Control tower — current view
- http://localhost:8088/tower/ (refreshes every 2 seconds)
- Show: KPI tiles → **Hubs driving the next hour** (sorted by SLA risk) → live fuel surcharge → scan ticker

## 3. watsonx.data — history + live
- https://localhost:6443 → **Query workspace**, engine `presto-01`

History (Iceberg):
```sql
SELECT origin_hub, origin_city, COUNT(*) AS scans,
       COUNT(*) FILTER (WHERE exception_code = 'WX_DELAY') AS weather_delays
FROM iceberg_data.shipping_backend.shipping_history
GROUP BY origin_hub, origin_city
ORDER BY weather_delays DESC;
```

Live (Kafka, read in place, nothing copied):
```sql
SELECT region, fuel_surcharge, updated_at
FROM shipping_ops.default.fuel_surcharge
ORDER BY updated_at DESC LIMIT 20;
```

Fuel margin drift (history joined with live):
```sql
WITH latest AS (
  SELECT region, fuel_surcharge FROM (
    SELECT region, fuel_surcharge,
           ROW_NUMBER() OVER (PARTITION BY region ORDER BY updated_at DESC) rn
    FROM shipping_ops.default.fuel_surcharge) WHERE rn = 1)
SELECT s.region,
       COUNT(*)                                           AS in_transit_scans,
       ROUND(AVG(s.fuel_surcharge), 2)                    AS surcharge_when_scanned,
       l.fuel_surcharge                                   AS surcharge_now,
       ROUND(SUM(l.fuel_surcharge - s.fuel_surcharge), 2) AS margin_drift
FROM iceberg_data.shipping_backend.shipping_history s
JOIN latest l ON s.region = l.region
WHERE s.status = 'IN_TRANSIT'
GROUP BY s.region, l.fuel_surcharge
ORDER BY margin_drift DESC;
```

## 4. Cassandra — the ledger (shown in the audit UI)
- http://localhost:8081/audit-ui/ → load `PCL-LIVE-000001`: timeline, map, driver notes, reconciliation
- Live mismatch (index lag): freeze the customer index, wait 30–60 s, load a parcel that is still moving, then unfreeze.
```bash
curl -s -XPUT localhost:9200/parcel-events-live/_settings -H 'content-type: application/json' -d '{"index":{"refresh_interval":"-1"}}'
```
```bash
grep -o 'last=[^ ]*' .run/shift_left.log | tail -1
```
```bash
curl -s -XPUT localhost:9200/parcel-events-live/_settings -H 'content-type: application/json' -d '{"index":{"refresh_interval":"1s"}}'
```
- Compare with the customer view of the same parcel: http://localhost:8081/customer-ui/

## 5. OpenSearch — search
- Customer tracking: http://localhost:8081/customer-ui/
- Full-text search over driver notes (top of the audit UI): `de-icing` · `weather delays dubai` · `"signed by recipient" chicago` · `concierge | front desk` · `delay -dubai`
- Discover: http://localhost:5601/app/data-explorer/discover (index `parcel-events-live`, time picker set to **Last 1 hour**)
```
exception_code:WX_DELAY and hub_code:"DXB-01"
sla_risk:true and region:"EU-WEST"
delivery_note:(weather and delay) and hub_city:dubai
status:OUT_FOR_DELIVERY and sla_risk:true
```
- Dashboard: http://localhost:5601/app/dashboards → **Global Parcel - Realtime Tracking Dashboard**

## 6. watsonx Orchestrate — ask the business
- Chat: http://localhost:3000/chat-lite → agent **Global Parcel Assistant**

Deep dive on one parcel:
1. `What is the latest status of PCL-LIVE-000001?`
2. `Show me its full journey, hub by hub.`
3. `What did the drivers note along the way?`
4. `The customer says it was never delivered. Is that true? Give me the evidence.`

Real time: get the parcel that hit a weather delay a few seconds ago.
```bash
curl -s 'localhost:9200/parcel-events-live/_search' -H 'content-type: application/json' -d '{"size":1,"sort":[{"event_ts":"desc"}],"query":{"term":{"exception_code":"WX_DELAY"}},"_source":["parcel_id","hub_code","event_ts"]}' | grep -o '"_source":{[^}]*}'
```
5. `Why is <PARCEL_ID> delayed, and should we warn the customer?` (quotes the driver note written seconds ago)
6. A minute later: `What's the status of <PARCEL_ID> now?` (the answer has moved on)
7. Show the same parcel in Kafka UI (search by key) and in the audit UI.
