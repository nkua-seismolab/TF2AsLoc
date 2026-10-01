# Metadata Files

TF2AsLoc depends on **four** external input files placed in the `metadata/` folder, which is mounted read-only into the API and orchestrator containers. The service will not run without them.

Each `config.yaml` key below takes a filename, or a path relative to `metadata/` if you prefer subfolders (e.g. `hinv/model_rigo1996.crh`). No other configuration (including `docker-compose.yml`) needs to change for subfolders, since the whole `metadata/` folder is mounted.

| File | `config.yaml` key | Purpose |
|---|---|---|
| StationXML inventory | `global.inventory` | Station coordinates for association/location, instrument responses for magnitude calculation. |
| Station selection CSVs | `global.csvfiles` | Which stations' waveforms are requested from each FDSN-WS client. |
| 1D velocity model (CRH) | `gamma.velmodel`, `locator.model` | Travel times for GaMMA (Eikonal mode) and HypoInverse. |
| HypoInverse parameter file | `locator.params` | HypoInverse control-file commands. |

## StationXML inventory

A [FDSN StationXML](https://docs.fdsn.org/projects/stationxml/) file (e.g. `stations.xml`) providing:

- station coordinates, used during association and location, and
- instrument responses, used during magnitude calculation.

It must cover **every** station whose picks are posted; picks from stations missing from the inventory are skipped.

The inventory is reloaded automatically every `orchestrator.inventory_refresh_hours`, or on demand via `POST /system/reload-inventory` (e.g. after adding a new station to the file) - see [API Usage](api.md).

## Station selection CSVs

One CSV per FDSN-WS client in `global.fdsn_ws_clients` (same order, semicolon-separated in `global.csvfiles`). They define which stations' waveforms are requested from each client during magnitude calculation.

The CSVs are headerless, one station per line:

```csv
network,station,channel_prefix
```

Example:

```csv
CL,AGRP,HH
HA,ATHU,HH
```

## 1D velocity model (CRH format)

Used by both GaMMA (preliminary association/location, only when `gamma.use_eikonal: true`) and HypoInverse (final location, always). `gamma.velmodel` and `locator.model` can point at the same physical file (typical) or two different ones.

HypoInverse CRH format:

- line 1 - free-text label;
- each following line - `Vp(km/s) Depth(km)` for one layer top, ordered by increasing depth.

Example (`model_rigo1996.crh`):

```text
Rigo et al. (1996) model
 5.60  0.00
 5.80  4.00
 6.20  7.20
 ...
```

## HypoInverse parameter file

Raw HypoInverse control-file lines (`ZTR`, `POS`, `DIS`, `ERR`, `DUR`, ...) inserted verbatim into the generated `.inp` control file before each location run. Always required.

See the [HypoInverse documentation](https://www.usgs.gov/software/hypoinverse-earthquake-location) for the full command reference.
