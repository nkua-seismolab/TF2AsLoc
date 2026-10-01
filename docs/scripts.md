# Helper Scripts

Standalone utilities under `scripts/`. They run on the host (not inside the containers) and require Python with `requests` and `obspy` available.

## `post_picks_to_api.py`

Reads a JSON picks file (TF2AsLoc format) and POSTs it to `POST /picks`.

```bash
export TF2ASLOC_API_KEY=<your key>
python scripts/post_picks_to_api.py picks.json --host localhost --port 8000
```

| Argument | Description |
|---|---|
| `picks_file` | JSON picks file (positional). |
| `--host` / `--port` | API endpoint (default: `localhost:8000`). |
| `--base-path` | Optional URL prefix, e.g. `/api/v1`. |
| `--api-key` | API key (default: `TF2ASLOC_API_KEY` environment variable). |
| `--interval SECONDS` | Split picks into successive time windows of this width and post them one by one, sleeping `SECONDS` between batches to simulate real-time ingestion. Windows align to midnight of the first pick's day; empty windows are skipped. Omit to post everything in a single request. |

!!! tip
    When posting historical picks, combine this script with the orchestrator's `replay` / `session_epoch` settings so the sliding window follows the pick timestamps instead of the wall clock - see the [Configuration Reference](configuration.md#orchestrator) and the [Demo Walkthrough](demo.md).

## `get_quakeml_from_api.py`

Retrieves QuakeML from `GET /events`, reads it into an ObsPy catalog and prints a summary: the number of events, per-event P- and S-pick statistics (min/max/mean/median/std), and one randomly selected event (fixed seed, `42`).

```bash
python scripts/get_quakeml_from_api.py http://localhost:8000/events \
    2026-01-01T00:00:00Z 2026-02-01T00:00:00Z --minmagnitude 2.0
```

| Argument | Description |
|---|---|
| `api_url` | Full URL of the events endpoint (positional). |
| `start` / `end` | UTC time window (positional; `start` inclusive, `end` exclusive). |
| `--minlatitude` / `--maxlatitude` | Latitude range in degrees (default: `-90` / `90`). |
| `--minlongitude` / `--maxlongitude` | Longitude range in degrees (default: `-180` / `180`). |
| `--mindepth` / `--maxdepth` | Depth range in km (omitted from the query unless set). |
| `--minmagnitude` / `--maxmagnitude` | Preferred-magnitude range (omitted from the query unless set). |
| `--save` | Save the retrieved QuakeML as `tf2asloc_catalog_output.xml` in the current working directory. |
