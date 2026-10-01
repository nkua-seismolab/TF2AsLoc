# Quick Start

A condensed end-to-end run. See [Installation & Requirements](installation.md) for details on each step.

## 1. Configure

```bash
git clone https://github.com/nkua-seismolab/TF2AsLoc
cd TF2AsLoc
cp config.example.yaml config.yaml   # then edit for your region/stations
# create .env with POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB, TF2ASLOC_API_KEY
```

Place your [metadata files](metadata.md) under `metadata/` and reference them in `config.yaml`. if the folder does not exist:

```bash
mkdir -p metadata/
```

## 2. Start

```bash
docker compose build
docker compose up -d
```

## 3. Post a pick

`POST /picks` accepts a JSON array of picks and requires the API key in the `X-API-Key` header:

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

A successful request returns `HTTP 201 Created` with the assigned pick IDs.

You can `POST` sample picks obtained from our [demo package on Zenodo](https://doi.org/10.5281/zenodo.23082786) using the helper script `scripts/post_picks_to_api.py` (see [Helper Scripts](scripts.md)).

## 4. Retrieve located events

Once enough picks have been associated and located, request QuakeML for a UTC time window:

```bash
curl -i "http://localhost:8000/events?start=2026-01-01T00:00:00Z&end=2026-01-02T00:00:00Z"
```

Returns `HTTP 200 OK` with a QuakeML document (possibly empty). See [API Usage](api.md) for window semantics and limits.

## 5. Explore

Interactive OpenAPI documentation is served at `http://localhost:8000/docs`.

To run a full example with provided input data, follow the [Demo Walkthrough](demo.md).
