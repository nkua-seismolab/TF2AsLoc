# API Usage

The API is served at `http://<host>:<api.port>` (default `http://localhost:8000`). Interactive OpenAPI documentation is available at `/docs`.

## Authentication

`POST` endpoints require the API key from `.env` (`TF2ASLOC_API_KEY`) in the `X-API-Key` header; requests without a valid key are rejected with `401`. `GET` requests need no key.

## `POST /picks`

Ingest a JSON array of picks:

```bash
curl -i -X POST http://localhost:8000/picks \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your TF2ASLOC_API_KEY>" \
  -d '[
    {
      "network": "XX",
      "station": "STATION",
      "location": "",
      "channel": "HHZ",
      "phase": "P",
      "time": "2026-09-03T19:23:49.320Z",
      "prob": 0.93,
      "model": "DL Model",
      "author": "Agency"
    }
  ]'
```

A successful request returns `HTTP 201 Created` with the IDs assigned to the newly stored picks plus dedup counters:

```json
{"ids": ["<uuid>", "..."], "deduplicated": 0, "updated": 0}
```

!!! note "Pick deduplication"
    Picks are deduplicated on ingestion, both within the batch and against picks already stored: two picks with the same network, station and phase whose times differ by at most 1 s are duplicates, and the one with the higher `prob` wins. A duplicate with lower (or equal) probability is dropped and counted in `deduplicated`; a higher-probability repost updates the stored pick in place (keeping its ID and any event linkage) and is counted in `updated`. Re-posting an identical batch therefore returns empty `ids` - this is expected, not an error.

| Field | Description |
|---|---|
| `network` / `station` / `location` / `channel` | SEED stream identifiers of the pick. |
| `phase` | `P` or `S`. |
| `time` | Pick time (ISO-8601 UTC). |
| `prob` | Pick probability/confidence in `[0, 1]`, used for filtering and weighting. |
| `model` | Name of the picker model (free text). |
| `author` | Producing agency/author (free text). |

## `GET /events`

Request located events within a UTC time window and a geographic bounding box, returned as QuakeML:

```bash
curl -i "http://localhost:8000/events?start=2026-01-01T00:00:00Z&end=2026-01-02T00:00:00Z&minlatitude=37.5&maxlatitude=38.8&minlongitude=21.0&maxlongitude=23.0&minmagnitude=2.0&maxdepth=30"
```

| Parameter | Required | Description |
|---|---|---|
| `start` / `end` | yes | UTC time window (`start` inclusive, `end` exclusive). |
| `minlatitude` / `maxlatitude` | yes | Latitude range in degrees (inclusive, `[-90, 90]`). |
| `minlongitude` / `maxlongitude` | yes | Longitude range in degrees (inclusive, `[-180, 180]`). |
| `mindepth` / `maxdepth` | no | Depth range in km (inclusive). |
| `minmagnitude` / `maxmagnitude` | no | Preferred-magnitude range (inclusive); events without a magnitude are excluded when either bound is set. |

- Returns `HTTP 200 OK` with a QuakeML XML document; the document can be empty when no events match the filters.
- The window is half-open: `start` inclusive, `end` exclusive.
- All timestamps must be UTC (naive, `Z`, or `+00:00`).
- A request matching more than `api.max_events` events returns `HTTP 413` - split the request into smaller windows.

!!! note "publicID stability"
    QuakeML publicIDs follow the scheme `smi:<global.smi_authority>/<Type>/...` (e.g. `smi:local/Event/nkua/crl/20260101120000000_38.112_21.659`). An event's publicID is minted from its **preliminary** origin time and epicentre (rounded to ~110 m, so simultaneous events in different locations get distinct IDs) and is stable thereafter: refined solutions are matched to the existing event (within `orchestrator.dedup_time_sec` / `orchestrator.dedup_dist_km`) and stored as additional origins/magnitudes under the same publicID. The origin publicID changes with each refinement - track the event's `preferredOriginID`/`preferredMagnitudeID` to follow the latest solution.

!!! note "Picks and arrivals reflect the location"
    Each event is served with its **preferred origin only** (the full refinement history stays in the database). The QuakeML contains exactly the picks that origin used: those referenced by an arrival with a positive time weight (arrivals stored without a weight are treated as used). Picks that were associated but weighted out or unused by the locator are not emitted, and zero-weight arrivals are dropped, so an event's picks match its arrivals. `originQuality` counts follow the same rule: `usedPhaseCount` is the number of emitted arrivals (origins stored without arrivals fall back to the locator-reported phase count) and `usedStationCount` is the number of distinct stations among them. Magnitudes computed for superseded origins are still listed but carry no `originID`.

## `GET /health`

Liveness/readiness probe; verifies the app and its database connection.

```bash
curl http://localhost:8000/health
# {"status": "ok"}
```

## `POST /system/reload-inventory`

Signal the orchestrator to reload the StationXML inventory from the configured FDSN-WS clients (e.g. after adding a station). Requires the API key.

```bash
curl -i -X POST http://localhost:8000/system/reload-inventory \
  -H "X-API-Key: <your TF2ASLOC_API_KEY>"
```

The orchestrator polls the reload request each tick and reloads the inventory on the next one. Scheduled refreshes also happen every `orchestrator.inventory_refresh_hours`.
