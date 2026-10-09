# Demo data fixtures

The CSV files in this directory are the versioned, synthetic internal business records for the local demo. Docker mounts this directory into both database containers at `/data`; the initialization SQL creates the tables and imports each file with `\copy`.

`catalog/` holds reference data. `analytics/` holds time-series business events. The fixtures intentionally model an inventory decline for supplier `S17` products in physical northern-Italy stores, while online availability and traffic remain stable.

They also contain two Dubai benchmarks. April 16–19, 2024 has sharply lower physical-store sales and traffic with weather-related operational restrictions while online sales continue. March 2026 has lower Dubai physical-store traffic and sales plus security-related operational restrictions, while inventory and online trade remain available. `store_operations.csv` is a generic daily operations signal used to distinguish access restrictions from pricing, inventory, or demand explanations.

External weather, FX, and historic-event facts are deliberately **not** stored here. Those must be retrieved independently by the appropriate external-research client at investigation time.

To reload changed fixtures, remove only the named demo volumes and start again:

```bash
docker compose down -v
docker compose up -d --wait
```
