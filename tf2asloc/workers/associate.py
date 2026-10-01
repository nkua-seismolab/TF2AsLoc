"""Association worker - wraps GaMMA for TF2AsLoc.

Ported from legacy/chunks/f_t314_code_input2_v1.py (GaMMA block).
"""

import logging
import time
from pathlib import Path

import pandas as pd
from pyproj import Transformer

logger = logging.getLogger(__name__)


def _read_crh(path: Path) -> tuple[list[float], list[float]]:
    """Parse a HypoInverse CRH 1-D velocity model.

    Format (skip header line)::

        vp_km_s  depth_km
        4.8      0.0
        5.2      4.0
        ...

    Returns (depths_km, vp_km_s) as plain Python lists.
    """
    depths, vps = [], []
    with open(path) as fh:
        next(fh)  # skip header
        for line in fh:
            parts = line.split()
            if len(parts) >= 2:
                vps.append(float(parts[0]))
                depths.append(float(parts[1]))
    return depths, vps


def _build_station_df(inv, transformer: Transformer) -> pd.DataFrame:
    """Build a GaMMA-compatible station DataFrame from an ObsPy Inventory."""
    rows = []
    for network in inv:
        for station in network:
            lat = station.latitude
            lon = station.longitude
            elev_m = station.elevation
            # id format: net.sta.loc - must match the pick id format
            # (location is always a string: "" or zero-padded like "00", "01")
            seed_id = f"{network.code}.{station.code}."
            x_km, y_km = transformer.transform(lat, lon)
            rows.append(
                {
                    "id": seed_id,
                    "latitude": lat,
                    "longitude": lon,
                    "elevation(m)": elev_m,
                    "x(km)": x_km / 1e3,
                    "y(km)": y_km / 1e3,
                    "z(km)": elev_m / 1e3,
                }
            )
    # A station can appear multiple times (different channels / epochs) in the
    # ObsPy Inventory.  GaMMA requires a one-to-many merge on "id", so we must
    # keep only one row per net.sta.
    df = pd.DataFrame(rows)
    return df.drop_duplicates(subset="id").reset_index(drop=True)


def _build_gamma_config(config: dict, transformer: Transformer) -> dict:
    """Construct the GaMMA config dict from the application config."""
    gc = config["gamma"]
    ac = config["associator"]

    # Broad bounding box in projected coordinates (km)
    x_min, y_min = transformer.transform(ac["latmin"], ac["lonmin"])
    x_max, y_max = transformer.transform(ac["latmax"], ac["lonmax"])
    x_min_km, x_max_km = x_min / 1e3, x_max / 1e3
    y_min_km, y_max_km = y_min / 1e3, y_max / 1e3
    z_min_km, z_max_km = float(ac["zmin"]), float(ac["zmax"])

    pvel = float(gc["pvel"])
    vpvs = float(gc["vpvs"])

    gamma_cfg: dict = {
        # "dims" must be column-name strings - GaMMA indexes the merged
        # station DataFrame with them.  Bounds go in the flat keys below.
        "dims": ["x(km)", "y(km)", "z(km)"],
        "x(km)": [x_min_km, x_max_km],
        "y(km)": [y_min_km, y_max_km],
        "z(km)": [z_min_km, z_max_km],
        # vel keys must be lowercase to match GaMMA internals.
        "vel": {"p": pvel, "s": pvel / vpvs},
        "use_dbscan": True,
        "use_amplitude": False,
        "covariance_prior": [5.0, 5.0],
        "method": gc.get("method", "BGMM"),
        "oversample_factor": int(gc.get("oversample_factor", 4)),
        "ncpu": int(gc.get("ncpu", 4)),
        "dbscan_eps": float(gc.get("dbscan_eps", 10.0)),
        "dbscan_min_samples": int(gc.get("dbscan_min_samples", 3)),
        "min_picks_per_eq": int(gc.get("min_picks_per_eq", 5)),
        "min_p_picks_per_eq": int(gc.get("min_p_picks_per_eq", 3)),
        "min_s_picks_per_eq": int(gc.get("min_s_picks_per_eq", 2)),
        "max_sigma11": float(gc.get("max_sigma11", 2.0)),
        "max_sigma22": float(gc.get("max_sigma22", 1.0)),
        "max_sigma12": float(gc.get("max_sigma12", 1.0)),
    }

    # L-BFGS-B bounds for GaMMA's hypocenter optimisation:
    # ((x_min, x_max), (y_min, y_max), (z_min, z_max), (t_min, t_max)) with a
    # 1 km margin around the bounding box (same as the legacy implementation).
    gamma_cfg["bfgs_bounds"] = (
        (x_min_km - 1, x_max_km + 1),
        (y_min_km - 1, y_max_km + 1),
        (0, z_max_km + 1),
        (None, None),  # origin time is unbounded
    )

    if gc.get("use_eikonal", False):
        velmodel_path = Path(config["_metadata_dir"]) / gc["velmodel"]
        depths, vps = _read_crh(velmodel_path)
        vss = [vp / vpvs for vp in vps]
        gamma_cfg["eikonal"] = {
            # Spatial limits required by initialize_eikonal
            "xlim": [x_min_km, x_max_km],
            "ylim": [y_min_km, y_max_km],
            "zlim": [z_min_km, z_max_km],
            "h": float(gc.get("eikonal_h", 1.0)),
            # 1-D layered velocity model parsed from the CRH file
            "vel": {"z": depths, "p": vps, "s": vss},
        }

    return gamma_cfg


def run_association(
    picks_df: pd.DataFrame,
    inv,
    config: dict,
    transformer: Transformer,
    rev_transformer: Transformer,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run GaMMA association on *picks_df*.

    Parameters
    ----------
    picks_df:
        DataFrame with columns: id, timestamp, type (p/s), prob, resource_id.
    inv:
        ObsPy Inventory (used to build the station DataFrame).
    config:
        Application config dict.
    transformer:
        Forward pyproj Transformer - geographic -> projected (e.g. GGRS87).
    rev_transformer:
        Inverse Transformer - projected -> geographic.

    Returns
    -------
    cat_df : pd.DataFrame
        GaMMA event catalog (latitude/longitude converted back from UTM).
    assign_df : pd.DataFrame
        Pick-to-event assignments with columns pick_idx, event_idx, prob_gamma.
    """
    from gamma.utils import association  # imported lazily to keep startup fast

    ac = config["associator"]
    p_min = float(ac.get("p_min_prob", 0.30))
    s_min = float(ac.get("s_min_prob", 0.30))

    df = picks_df.copy()

    # Filter low-probability picks
    low_p = df[(df["type"] == "p") & (df["prob"] < p_min)].index
    low_s = df[(df["type"] == "s") & (df["prob"] < s_min)].index
    df.drop(low_p.union(low_s), inplace=True)
    df.reset_index(drop=True, inplace=True)

    if df.empty:
        logger.info("No picks remaining after probability filter.")
        return pd.DataFrame(), pd.DataFrame()

    # GaMMA's convert_picks_csv overwrites 'timestamp' with datetime objects.
    # Pandas 2+ uses StringDtype for string columns and rejects non-string
    # values, so cast to object dtype before handing off to GaMMA.
    df["timestamp"] = df["timestamp"].astype(object)

    station_df = _build_station_df(inv, transformer)
    gamma_config = _build_gamma_config(config, transformer)

    logger.info("Running GaMMA on %d picks...", len(df))
    tic = time.perf_counter()
    sb_catalog, assignments = association(
        df, station_df, gamma_config, method=gamma_config["method"]
    )
    elapsed = time.perf_counter() - tic
    logger.info("GaMMA finished in %.2f s - %d events.", elapsed, len(sb_catalog))

    if not sb_catalog:
        return pd.DataFrame(), pd.DataFrame()

    # Convert UTM back to geographic
    for ev in sb_catalog:
        lat, lon = rev_transformer.transform(ev["x(km)"] * 1e3, ev["y(km)"] * 1e3)
        ev["latitude"] = lat
        ev["longitude"] = lon

    cat_df = pd.DataFrame(sb_catalog)
    cat_df["magnitude"] = cat_df["magnitude"].astype(float)

    assign_df = pd.DataFrame(assignments, columns=["pick_idx", "event_idx", "prob_gamma"])

    # --- Narrow spatial / depth filter ---
    latmin2 = float(ac["latmin2"])
    latmax2 = float(ac["latmax2"])
    lonmin2 = float(ac["lonmin2"])
    lonmax2 = float(ac["lonmax2"])
    zmin2 = float(ac["zmin2"])
    zmax2 = float(ac["zmax2"])

    cat_df = cat_df[
        (cat_df["latitude"] >= latmin2)
        & (cat_df["latitude"] <= latmax2)
        & (cat_df["longitude"] >= lonmin2)
        & (cat_df["longitude"] <= lonmax2)
        & (cat_df["z(km)"] >= zmin2)
        & (cat_df["z(km)"] <= zmax2)
    ].copy()

    logger.info("After narrow spatial filter: %d events kept.", len(cat_df))

    if cat_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    assign_df = assign_df[assign_df["event_idx"].isin(cat_df["event_index"])].copy()

    return cat_df, assign_df
