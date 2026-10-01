# Configuration Reference

All runtime behaviour is controlled by `config.yaml` (template: `config.example.yaml`). File-name parameters take a filename, or a path relative to the `metadata/` folder if you organize files into subfolders (e.g. `hinv/model_rigo1996.crh`) - see [Metadata Files](metadata.md).

## `database`

| Parameter | Description |
|---|---|
| `host` | PostgreSQL host. Inside Docker Compose this must be the Compose service name (`tf2asloc_db`). |
| `port` | PostgreSQL port (`5432` inside Compose). |

You only have to change these parameters if (a) you want to change the database host / port in `docker-compose.yml` or (b) you want to use your own independent PostgreSQL installation.

Credentials and the database name are **not** set here - they come from `POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB` in `.env`; the service builds the full connection URL at startup.

## `api`

| Parameter | Description |
|---|---|
| `host` | Bind address of the FastAPI app (`0.0.0.0` to accept external connections). |
| `port` | API port. |
| `max_events` | Maximum number of events `GET /events` returns in a single request. Windows matching more events are rejected with `HTTP 413`; split the request into smaller windows. |

## `global`

| Parameter | Description |
|---|---|
| `agency` | Agency code used in resource IDs and QuakeML author fields. |
| `region` | Region label used in resource IDs. |
| `smi_authority` | Authority for `smi:` resource identifiers (QuakeML publicIDs), e.g. your institution's domain (`nkua.gr`). Produces IDs like `smi:<authority>/Event/...`. |
| `inventory` | StationXML inventory filename in `metadata/`. Must cover every station whose picks are posted. |
| `fdsn_ws_clients` | FDSN-WS client URLs (or ObsPy client names) for inventory and waveform retrieval, semicolon-separated. |
| `csvfiles` | Station selection CSV filenames in `metadata/` - one per FDSN-WS client above, in the same order, semicolon-separated. |
| `max_retries` | Maximum retries for FDSN-WS requests on transient failures. |
| `retry_delay` | Delay (seconds) between FDSN-WS retries. |

## `associator`

Pick pre-filtering, geographic gating, and pick-weight mapping applied around the GaMMA run.

| Parameter | Description |
|---|---|
| `p_min_prob` / `s_min_prob` | Minimum P/S pick probability for a pick to enter association. |
| `latmin`, `latmax`, `lonmin`, `lonmax` | **Broad** bounding box. Should encompass all stations and be wider than the area of interest, so regional events can still be located preliminarily. |
| `latmin2`, `latmax2`, `lonmin2`, `lonmax2` | **Narrow** bounding box. Events whose preliminary location falls outside it are discarded before magnitude calculation and the final HypoInverse run. |
| `zmin`, `zmax` | Focal depth limits (km) for the broad box. |
| `zmin2`, `zmax2` | Focal depth limits (km) for the narrow box. |
| `pwgt0`–`pwgt3` | P pick-probability → HypoInverse weight-class thresholds: a pick with `prob >= pwgt0` is assigned weight 0 (full weight), `prob >= pwgt1` weight 1, and so on. |
| `swgt0`–`swgt3` | Same thresholds for S picks. |
| `event_match_time_tol` | Reconciliation: maximum origin-time offset (seconds) for matching a fresh preliminary association to a previously located event. |
| `event_match_coord_tol` | Reconciliation: maximum epicentre offset (degrees). |

## `gamma`

Settings passed to the [GaMMA](https://github.com/AI4EPS/GaMMA) associator.

| Parameter | Description |
|---|---|
| `projection` | EPSG code of the local projected CRS used internally by GaMMA (e.g. [`2100`](https://epsg.io/2100) = Greek Grid GGRS87). |
| `use_eikonal` | `true` to compute travel times through the 1D `velmodel` with an Eikonal solver; `false` to use the homogeneous `pvel`/`vpvs` model. |
| `velmodel` | 1D velocity model filename in `metadata/`, in HypoInverse `.crh` format. May point at the same file as `locator.model`. |
| `eikonal_h` | Grid spacing (km) for the Eikonal travel-time interpolation. |
| `pvel` | Homogeneous P velocity (km/s), used when `use_eikonal: false`. |
| `vpvs` | Vp/Vs ratio (also used for S travel times in Eikonal mode). |
| `method` | Clustering method: `BGMM` (Bayesian) or `GMM`. |
| `oversample_factor` | GaMMA oversampling factor for initial cluster seeds. |
| `ncpu` | Number of CPUs for GaMMA's internal multiprocessing pool. |
| `dbscan_eps` | DBSCAN: maximum time (s) between picks in the same cluster. |
| `dbscan_min_samples` | DBSCAN: minimum picks to form a core cluster point. |
| `min_picks_per_eq` | Minimum total picks for a candidate event. |
| `min_p_picks_per_eq` / `min_s_picks_per_eq` | Minimum P/S picks for a candidate event. |
| `max_sigma11` | Maximum phase-time residual (s). |
| `max_sigma22` | Maximum phase-amplitude residual (log scale). |
| `max_sigma12` | Maximum covariance term. |

!!! note "GaMMA multiprocessing (`ncpu`)"
    By default GaMMA hard-codes the `fork` multiprocessing start method on Linux, and forking a pool from TF2AsLoc's multi-threaded orchestrator could inherit a held lock and deadlock the pipeline. The TF2AsLoc Docker image patches GaMMA to use `spawn` (see [Customizations](customizations.md)), so `ncpu > 1` is considered safe. Note however that with the few pick clusters present in a typical real-time window, a larger pool (i.e. `ncpu > 1`) may yield little to no speedup.

## `magnitude`

Waveform retrieval and magnitude (ML and duration) calculation.

| Parameter | Description |
|---|---|
| `workers` | Parallel worker processes for amplitude measurement. |
| `par_chunks` | Bulk-download chunks processed in parallel per worker. |
| `chunksize` | Stations per bulk-download chunk. |
| `tbef` / `taft` | Waveform extraction window: seconds before the earliest pick / after the latest pick of the event. |
| `preP` / `postP` | Signal window around the P arrival (seconds before/after). |
| `preS` / `postS` | Signal window around the S arrival (seconds before/after). |
| `prefilter` | Pre-filter corner frequencies (Hz) applied during instrument response removal (`[f1, f2, f3, f4]`). |
| `water_level` | Water level (dB) for response removal. |
| `max_waveform_wait_sec` | Maximum wait (seconds) for FDSN waveform availability before skipping magnitude calculation for an event - accounts for near-real-time data latency. |
| `mag_n` | ML geometrical spreading factor. |
| `mag_K` | ML anelastic attenuation coefficient. |
| `mag_c` | ML station correction / constant term. |
| `mag_dist` | ML reference distance (km). |
| `test7`, `test8`, `test9` | Duration magnitude coefficients (equivalents of Hypo71 `RESET TEST` values). |

The local magnitude formula (defaults from [Scordilis et al., 2013](https://ejournals.epublishing.ekt.gr/index.php/geosociety/article/view/10980), for Greece) is:

$$
M_L = \log_{10}A + n \cdot \log_{10}\!\left(\frac{R}{d_{ref}}\right) + K \cdot (R - d_{ref}) + c
$$

where $A$ is the Wood-Anderson amplitude (mm), $R$ the hypocentral distance (km), and $n$, $K$, $c$, $d_{ref}$ map to `mag_n`, `mag_K`, `mag_c`, `mag_dist`.

## `locator`

Final location with USGS HypoInverse (hyp1.40).

| Parameter | Description |
|---|---|
| `model` | 1D velocity model filename in `metadata/` (HypoInverse CRH format). Typically the same file as `gamma.velmodel`. |
| `params` | HypoInverse parameter file in `metadata/`: raw control-file lines (`ZTR`, `POS`, `DIS`, `ERR`, `DUR`, ...) inserted verbatim into the generated `.inp` control file before each run. |
| `use_trial` | If `true`, use the GaMMA preliminary origin as the trial hypocenter for HypoInverse. |

## `orchestrator`

The scheduling loop that drives association runs.

| Parameter | Description |
|---|---|
| `window_sec` | Width of the sliding pick window (seconds) - each run looks back this far. |
| `overlap_sec` | Extra safety buffer (seconds) added on top of `window_sec` for the look-back start. The real overlap between consecutive runs comes from `interval_sec << window_sec`, not from this value. |
| `interval_sec` | How often a new association run starts (seconds). Consecutive runs overlap by `window_sec - interval_sec`; keep this well below `window_sec` (e.g. 1/5th) so a pick posted late is still captured by a later overlapping run. |
| `inventory_refresh_hours` | Reload the StationXML inventory from FDSN-WS every N hours (also on demand via `POST /system/reload-inventory`). |
| `dedup_time_sec` / `dedup_dist_km` | Event deduplication: a freshly associated event whose origin time is within `dedup_time_sec` seconds **and** whose epicentre is within `dedup_dist_km` km of an already-persisted event is treated as a duplicate - its picks are linked to the existing event and the duplicate is dropped before location. Defaults: 5 s / 10 km. |
| `replay` | `true` drives the sliding window from pick timestamps instead of wall-clock time. Use when posting historical/backfill picks so the orchestrator does not anchor its window to "now". Once the replay cursor catches up with the wall clock, the orchestrator switches to real-time operation automatically. |
| `session_epoch` | Backfill anchor. Normally only picks inserted after orchestrator startup are processed; set this to an ISO-8601 UTC datetime to also consume picks already stored in the database (implies replay mode until caught up). Remove or comment it out once the backfill is done, so a future restart does not re-process the backlog. |
