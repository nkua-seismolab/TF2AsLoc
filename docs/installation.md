# Installation & Requirements

## Software requirements

- **Docker** and **Docker Compose** - the service runs as three containers (API, orchestrator, PostgreSQL). No host Python installation is needed; HypoInverse and GaMMA are built into the image.
- Optionally, **Git** to clone the repository. Otherwise you may download the repository manually (git is recommended).

## Hardware requirements

!!! note
    The following specifications are estimates. Performance heavily depends on your expected workload and configuration (e.g., number of stations and picks, length of windows, GaMMA configuration).

| Resource | Minimum | Recommended |
|---|---|---|
| CPU | 2 cores | 4 cores |
| RAM | 8 GB | 16 GB |
| Disk | 4 GB | >4 GB (grows with pick/event volume and database retention) |

!!! note
    Waveform download and amplitude measurement during magnitude calculation are the most resource-intensive steps; `magnitude.workers` and the GaMMA settings can be tuned to the available cores (see the [Configuration Reference](configuration.md)).

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/nkua-seismolab/TF2AsLoc
cd TF2AsLoc
```

### 2. Create the environment file

Create `.env` in the project root:

```env
POSTGRES_USER=tf2asloc
POSTGRES_PASSWORD=changeme
POSTGRES_DB=tf2asloc
TF2ASLOC_API_KEY=<random secret>
```

`TF2ASLOC_API_KEY` is required by the `POST` endpoints to be able to accept picks; generate one with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 3. Provide the configuration

Copy the template and edit it for your deployment:

```bash
cp config.example.yaml config.yaml
```

Inside Docker Compose, `database.host`/`database.port` must point at the Compose Postgres service (`tf2asloc_db:5432` by default). If you change database host / port in `docker-compose.yml` or you want to use your own PostgreSQL installation, make sure to change this setting. Every parameter is documented in the [Configuration Reference](configuration.md).

### 4. Provide the metadata files

The service will not run without four external input files placed under `metadata/` - a StationXML inventory, station selection CSVs, a 1D velocity model, and a HypoInverse parameter file. See [Metadata Files](metadata.md).

### 5. Build and start

```bash
docker compose build
docker compose up -d  # -d flag sends docker to background
docker compose ps
```

You may watch real-time logs of all three services (databse, API and orchestrator) with:

```bash
docker compose logs -f
```

The API listens by default on `http://localhost:8000`. Verify with its health status with:

```bash
curl http://localhost:8000/health
```

## Operating the stack

```bash
docker compose down             # stop the service
docker compose down -v          # stop the service and remove volumes - useful when rebuilding
docker compose logs             # all logs
docker compose logs --tail=50   # last 50 lines
docker compose logs -f          # follow live
docker compose logs <service>   # one container (tf2asloc-api, tf2asloc-orchestrator, tf2asloc-db)
```
