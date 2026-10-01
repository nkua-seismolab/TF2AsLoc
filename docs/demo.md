# Demo Walkthrough

A complete, self-contained example is archived on [Zenodo](https://doi.org/10.5281/zenodo.23082786).

The archive contains 24 hours of raw [PhaseNet](https://doi.org/10.1093/gji/ggy423) (trained on [INSTANCE](https://doi.org/10.5194/essd-13-5509-2021)) picks (model tag `pnet-instance`, run via SeisBench) from the Corinth Rift Laboratory Near-Fault Observatory (CRL-NFO) - **2021-01-06 00:00:00 to 2021-01-07 00:00:00 UTC** - plus every input file needed to process them:

- `picks_seisbench_crltest_pnet-instance_2021-01-06-00-00-00_2021-01-07-00-00-00.json` - ~46k P and S picks in the TF2AsLoc format, from 56 stations of the CL, HA, HL, HP, and HT networks;
- `config.yaml` - the demo configuration;
- `metadata/inventory/eida_greece_stations_response.xml` - StationXML inventory;
- `metadata/stations/eida_noa_stations_list.csv`, `metadata/stations/eida_eposfr_stations_list.csv` - station selection CSVs (one per FDSN client);
- `metadata/hinv/model_rigo1996.crh` - velocity model (HypoInverse CRH format);
- `metadata/hinv/h2000_params.txt` - HypoInverse parameter file;
- `tf2asloc-catalog_crltest_pnet-instance_2021-01-06-00-00-00_2021-01-07-00-00-00.xml` - the **expected QuakeML** output for comparison;
- `README.md`, `LICENSE` (CC BY 4.0).

## 1. Download the demo files

```bash
wget -r https://zenodo.org/records/23082787
```

Then you need to unzip `tf2asloc_demo_dataset.zip`.

## 2. Install the demo files

From the TF2AsLoc project root, copy the provided input files into `metadata/` and the demo config over the project root:

```bash
cp -r tf2asloc_demo_dataset/metadata/* metadata/
cp tf2asloc_demo_dataset/config.yaml config.yaml
```

!!! important "Review the demo config"
    The provided `config.yaml` matches the default Docker Compose setup, but review:

    - `database.host` / `database.port` - must match your deployment (`tf2asloc_db:5432` for the default Compose stack);
    - `global.fdsn_ws_clients` - the `NOA` and `EPOSFR` FDSN services must be reachable from your machine for waveform retrieval during magnitude calculation;
    - `orchestrator.replay` - ships as `true`, so the pick window is driven by the pick timestamps rather than the wall clock. This is required for the historical demo picks.
    - `orchestrator.window_sec` - ships as `86000` (~24 h), so a single association run covers the whole demo day and all picks are associated immediately after posting. If you build your config from `config.example.yaml` instead, its real-time default (`window_sec: 30`) will still process everything in replay mode, but gradually, window by window.

## 3. Start the stack

Create `.env` as described in [Installation](installation.md), then:

```bash
docker compose build
docker compose up -d
```

In replay mode the orchestrator waits for the first pick to arrive, so it is safe to start the stack before posting.

## 4. Post the demo picks

Post everything at once:

```bash
python scripts/post_picks_to_api.py tf2asloc_demo_dataset/picks_seisbench_crltest_pnet-instance_2021-01-06-00-00-00_2021-01-07-00-00-00.json --api-key <your-api-key>
```

or post in timed batches with `--interval <seconds>` (note that `--interval 60` replays the full day at true speed, i.e. ~24 h).

Watch progress:

```bash
docker compose logs -f orchestrator
```

!!! note "Re-running the demo"
    The orchestrator only consumes picks inserted *after* it started. If you post the picks while the orchestrator is down (or want to re-process picks already in the database), set `orchestrator.session_epoch` in `config.yaml` to an ISO-8601 UTC instant at or before the picks were inserted, and restart the orchestrator.

## 5. Retrieve and compare the results

Fetch the QuakeML for the demo time window (the latitude/longitude bounds are required; the values below are the demo's broad association box):

```bash
curl -o result.xml "http://localhost:8000/events?start=2021-01-06T00:00:00&end=2021-01-07T00:00:00&minlatitude=37.80&maxlatitude=38.84&minlongitude=21.47&maxlongitude=22.73"
```

or use the helper script (prints a catalog summary; add `--save` to also write the QuakeML to `tf2asloc_catalog_output.xml` in the current directory):

```bash
python scripts/get_quakeml_from_api.py http://localhost:8000/events \
    2021-01-06T00:00:00 2021-01-07T00:00:00 \
    --minlatitude 37.80 --maxlatitude 38.84 \
    --minlongitude 21.47 --maxlongitude 22.73 \
    --save
```

Compare against the expected catalog shipped in the archive, e.g. with ObsPy:

```python
from obspy import read_events

got = read_events("result.xml")
expected = read_events(
    "tf2asloc_demo_dataset/tf2asloc-catalog_crltest_pnet-instance_2021-01-06-00-00-00_2021-01-07-00-00-00.xml"
)
print(got)
print(expected)
```

Small differences in magnitudes and location uncertainties are normal (they depend on waveform availability from the FDSN services at run time); origin times and epicentres should closely match the expected catalog.

## 6. Clean up

```bash
docker compose down
```

If you set `orchestrator.session_epoch` for the demo, remove/comment it afterwards so a later restart does not re-process the backlog.

Additionally, you should purge the database before running in real-time:

```bash
sudo rm -rf database/
```
