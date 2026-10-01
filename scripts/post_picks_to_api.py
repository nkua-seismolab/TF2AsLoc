"""
Reads a JSON file containing picks in the TF2AsLoc format and POSTs them
to the TF2AsLoc API endpoint POST /picks.

With --interval, picks are divided into successive time windows and posted
sequentially, sleeping --interval seconds between each batch to simulate
real-time ingestion.  Window boundaries are aligned to midnight of the
first pick's day.  Empty windows are skipped silently.
"""

# --------------------------------------------------------------
# Imports
# --------------------------------------------------------------
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# --------------------------------------------------------------
# CLI
# --------------------------------------------------------------
parser = argparse.ArgumentParser(description="POST a JSON picks file to the TF2AsLoc API.")
parser.add_argument(
    "picks_file",
    type=Path,
    help="Path to the JSON file containing picks.",
)
parser.add_argument(
    "--host",
    default="localhost",
    help="API host (default: %(default)s).",
)
parser.add_argument(
    "--port",
    type=int,
    default=8000,
    help="API port (default: %(default)s).",
)
parser.add_argument(
    "--base-path",
    default="",
    help="Optional base path prefix, e.g. '/api/v1' (default: none).",
)
parser.add_argument(
    "--api-key",
    default=os.environ.get("TF2ASLOC_API_KEY"),
    help="API key for POST /picks (default: TF2ASLOC_API_KEY env var).",
)
parser.add_argument(
    "--interval",
    type=float,
    default=None,
    metavar="SECONDS",
    help=(
        "Divide picks into time windows of this width (in seconds) and post "
        "them one by one, sleeping SECONDS between each batch to simulate "
        "real-time ingestion.  Windows are aligned to midnight of the first "
        "pick's day.  Empty windows are skipped.  "
        "If omitted, all picks are posted in a single request."
    ),
)
args = parser.parse_args()

# --------------------------------------------------------------
# Read the picks file
# --------------------------------------------------------------
picks_file: Path = args.picks_file
if not picks_file.is_file():
    print(f"ERROR: file not found: '{picks_file}'", file=sys.stderr)
    sys.exit(1)

with picks_file.open("r", encoding="utf-8") as fh:
    payload = json.load(fh)

if not isinstance(payload, list):
    print("ERROR: JSON file must contain a top-level array of picks.", file=sys.stderr)
    sys.exit(1)

print(f"Read {len(payload):,} picks from '{picks_file}'")

# --------------------------------------------------------------
# Helper: POST a single batch and exit on error
# --------------------------------------------------------------
base_path = args.base_path.rstrip("/")
url = f"http://{args.host}:{args.port}{base_path}/picks"


def post_batch(batch: list, label: str) -> None:
    print(f"  POSTing {len(batch):,} picks ({label}) to {url} ...")
    body = json.dumps(batch).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if args.api_key:
        headers["X-API-Key"] = args.api_key
    req = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            status = resp.status
            response_body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status = exc.code
        response_body = exc.read().decode("utf-8")
    except urllib.error.URLError as exc:
        print(f"ERROR: could not reach API at {url}: {exc.reason}", file=sys.stderr)
        sys.exit(1)

    print(f"  Response {status}: {response_body}")
    if status not in (200, 201):
        sys.exit(1)


# --------------------------------------------------------------
# Post: single batch or windowed simulation
# --------------------------------------------------------------
if args.interval is None:
    # Original behaviour - post everything at once.
    print(f"POSTing to {url} ...")
    post_batch(payload, "all")

else:
    interval = args.interval
    if interval <= 0:
        print("ERROR: --interval must be a positive number of seconds.", file=sys.stderr)
        sys.exit(1)

    # Parse the "time" field of every pick.
    # datetime.fromisoformat does not handle the trailing 'Z' before Python 3.11.
    def _parse_time(t: str) -> datetime:
        return datetime.fromisoformat(t.replace("Z", "+00:00"))

    try:
        picks_with_times = [(p, _parse_time(p["time"])) for p in payload]
    except (KeyError, ValueError) as exc:
        print(f"ERROR: could not parse pick times: {exc}", file=sys.stderr)
        sys.exit(1)

    # Sort chronologically.
    picks_with_times.sort(key=lambda x: x[1])

    first_time = picks_with_times[0][1]
    last_time = picks_with_times[-1][1]

    # Align window boundaries to midnight of the first pick's day.
    origin = first_time.replace(hour=0, minute=0, second=0, microsecond=0)
    delta = timedelta(seconds=interval)

    # Jump directly to the window that covers first_time (skip empty early windows).
    steps = int((first_time - origin).total_seconds() / interval)
    window_start = origin + steps * delta

    print(
        f"Interval mode: {interval:.1f}s windows | "
        f"{first_time.isoformat()} -> {last_time.isoformat()}"
    )

    batch_index = 0
    pick_cursor = 0
    n_picks = len(picks_with_times)

    while pick_cursor < n_picks:
        window_end = window_start + delta

        # Collect picks in [window_start, window_end).
        batch = []
        while pick_cursor < n_picks and picks_with_times[pick_cursor][1] < window_end:
            batch.append(picks_with_times[pick_cursor][0])
            pick_cursor += 1

        if batch:
            batch_index += 1
            label = f"batch {batch_index} | {window_start.isoformat()} - {window_end.isoformat()}"
            post_batch(batch, label)

            # Sleep to simulate real-time gap, but not after the very last batch.
            if pick_cursor < n_picks:
                print(f"  Sleeping {interval:.1f}s ...")
                time.sleep(interval)

        window_start = window_end

    print(f"Done. Posted {batch_index} batch(es).")
