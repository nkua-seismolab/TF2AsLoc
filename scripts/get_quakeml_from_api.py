"""
Retrieve QuakeML from the TF2AsLoc API GET /events endpoint and read it
into an ObsPy catalog.

Latitude/longitude bounds are mandatory for the API; when omitted here
they default to the whole Earth.  Depth and magnitude bounds are only
sent when provided.
"""

# --------------------------------------------------------------
# Imports
# --------------------------------------------------------------
import argparse
import random
import statistics
import sys
from io import BytesIO

import requests
from obspy import read_events

# --------------------------------------------------------------
# CLI
# --------------------------------------------------------------
parser = argparse.ArgumentParser(
    description="GET events as QuakeML from the TF2AsLoc API and print a catalog summary."
)
parser.add_argument(
    "api_url",
    help="Full URL of the events endpoint, e.g. http://localhost:8000/events",
)
parser.add_argument(
    "start",
    help="Start of the time window (UTC, inclusive), e.g. 2026-01-01T00:00:00Z",
)
parser.add_argument(
    "end",
    help="End of the time window (UTC, exclusive), e.g. 2026-02-01T00:00:00Z",
)
parser.add_argument(
    "--minlatitude",
    type=float,
    default=-90.0,
    help="Minimum latitude in degrees (default: %(default)s).",
)
parser.add_argument(
    "--maxlatitude",
    type=float,
    default=90.0,
    help="Maximum latitude in degrees (default: %(default)s).",
)
parser.add_argument(
    "--minlongitude",
    type=float,
    default=-180.0,
    help="Minimum longitude in degrees (default: %(default)s).",
)
parser.add_argument(
    "--maxlongitude",
    type=float,
    default=180.0,
    help="Maximum longitude in degrees (default: %(default)s).",
)
parser.add_argument(
    "--mindepth",
    type=float,
    default=None,
    help="Minimum depth in km (default: no lower bound).",
)
parser.add_argument(
    "--maxdepth",
    type=float,
    default=None,
    help="Maximum depth in km (default: no upper bound).",
)
parser.add_argument(
    "--minmagnitude",
    type=float,
    default=None,
    help="Minimum preferred magnitude (default: no lower bound).",
)
parser.add_argument(
    "--maxmagnitude",
    type=float,
    default=None,
    help="Maximum preferred magnitude (default: no upper bound).",
)
parser.add_argument(
    "--save",
    action="store_true",
    help="Save the retrieved QuakeML to 'tf2asloc_catalog_output.xml' "
    "in the current working directory.",
)
args = parser.parse_args()

# --------------------------------------------------------------
# Build the query
# --------------------------------------------------------------
params = {
    "start": args.start,
    "end": args.end,
    "minlatitude": args.minlatitude,
    "maxlatitude": args.maxlatitude,
    "minlongitude": args.minlongitude,
    "maxlongitude": args.maxlongitude,
}
for name in ("mindepth", "maxdepth", "minmagnitude", "maxmagnitude"):
    value = getattr(args, name)
    if value is not None:
        params[name] = value

# --------------------------------------------------------------
# Fetch and parse
# --------------------------------------------------------------
try:
    response = requests.get(args.api_url, params=params, timeout=30)
except requests.exceptions.RequestException as exc:
    print(f"ERROR: could not reach API at {args.api_url}: {exc}", file=sys.stderr)
    sys.exit(1)

if response.status_code != 200:
    print(f"ERROR: API returned {response.status_code}: {response.text}", file=sys.stderr)
    sys.exit(1)

# Read the QuakeML bytes directly from memory (no file on disk)
catalog = read_events(BytesIO(response.content), format="QUAKEML")

# --------------------------------------------------------------
# Optionally save the raw QuakeML locally
# --------------------------------------------------------------
if args.save:
    output_name = "tf2asloc_catalog_output.xml"
    with open(output_name, "wb") as fid:
        fid.write(response.content)
    print(f"QuakeML saved to '{output_name}'")


# --------------------------------------------------------------
# Summarize what we got
# --------------------------------------------------------------
def _summarize(counts):
    """Return a min/max/mean/median/std summary string for a list of counts."""
    if not counts:
        return "n/a"
    return (
        f"min={min(counts)} max={max(counts)} "
        f"mean={statistics.mean(counts):.2f} "
        f"median={statistics.median(counts):.2f} "
        f"std={statistics.pstdev(counts):.2f}"
    )


p_counts = []
s_counts = []
for event in catalog:
    phases = [(pick.phase_hint or "").upper() for pick in event.picks]
    p_counts.append(sum(phase.startswith("P") for phase in phases))
    s_counts.append(sum(phase.startswith("S") for phase in phases))

print(f"Number of events: {len(catalog)}")
print(f"P picks per event: {_summarize(p_counts)}")
print(f"S picks per event: {_summarize(s_counts)}")

if len(catalog):
    print()
    print("Randomly selected event (seed=42):")
    print(random.Random(42).choice(catalog.events))
