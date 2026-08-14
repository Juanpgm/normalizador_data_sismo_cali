"""Central configuration: data sources, paths and tunable thresholds."""
from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path

# ── Timezone ──────────────────────────────────────────────────────────────────
# Colombia is UTC-5 year-round (no DST), so a fixed offset is exact. All
# human-facing timestamps (job banner, log lines, stats sheet, run history)
# display Bogotá time; runs.jsonl keeps the UTC twin for cross-system audits.
BOGOTA_TZ = timezone(timedelta(hours=-5), "America/Bogota")

# ── Repo layout ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent

# Local secrets (.env is gitignored). Loaded here because every entrypoint
# imports config; real environment variables always take precedence.
from .envfile import load_env_file  # noqa: E402

load_env_file(ROOT / ".env")
SERVICE_ACCOUNT_FILE = str(ROOT / "service_account.json")
DATA_DIR = ROOT / "data"
CATASTRO_DIR = DATA_DIR / "catastro"
OUTPUT_DIR = ROOT / "output"

# ── Google Sheets sources ─────────────────────────────────────────────────────
EDAN_SPREADSHEET_ID = "1QRLezOtMTZpePluDl7VVzxINYLgvACVB76z54vJNpfE"
EDAN_SHEET_NAME = "EDAN 100826 - Datos Madre"
VISITAS_SPREADSHEET_ID = "1SIzarDbjtaD6JVM7cUHWqrcLBJNgYj6ZooHVU9tRKN4"
VISITAS_SHEET_NAME = "Respuestas de formulario 1"

# ── Google Sheets destination (hourly job) ────────────────────────────────────
# The destination lives inside the EDAN document: the integrated table and its
# statistics land next to the source data the responders already look at.
# Only these two worksheets are ever written. Their sheetId is pinned so a
# renamed or recreated tab aborts the write instead of clobbering another tab.
TARGET_SPREADSHEET_ID = EDAN_SPREADSHEET_ID
TABLA_SHEET_NAME = "tabla_integrada"
TABLA_SHEET_ID = 274962096
STATS_SHEET_NAME = "integracion_stats"
STATS_SHEET_ID = 1005527582
WRITE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Deterministic-ID seeds (must match the notebook contract so IDs are stable).
EDAN_ID_SEED = 42
VISITAS_ID_SEED = 88

# ── Cali bounding box (WGS84) — used to validate parsed coordinates ───────────
CALI_BBOX = {"lat_min": 2.9, "lat_max": 4.1, "lon_min": -77.0, "lon_max": -76.0}

# ── Matching thresholds (cascade) ─────────────────────────────────────────────
VECTOR_TOL = 0.05          # tier 2: exact sub-block euclidean distance
BLOCK_PLACA_TOL = 40.0     # tier 3: same block face, placa delta
GEO_NEAR_M = 40.0          # tier 5: haversine when EDAN address is parseable
GEO_FAR_M = 90.0           # tier 5: haversine when EDAN address is unparseable
TFIDF_MIN = 0.82           # tier 6: cosine similarity floor
FUZZY_THRESHOLD = 88.0     # tier 7: token_set_ratio floor
FUZZY_MIN_LEN = 12         # tier 7: minimum address length to be specific
EMBEDDING_MIN_SIM = 0.75   # tier 8: LM cosine floor

# ── Spatial bridge (NEW tier) ─────────────────────────────────────────────────
# Two points falling in the SAME cadastral parcel polygon are the same site.
# When no parcel layer is available we fall back to a spatial clustering
# surrogate with this radius (meters) — parcel-typical urban lot span.
SPATIAL_CLUSTER_EPS_M = 22.0
SPATIAL_BRIDGE_MAX_M = 35.0   # safety cap for a parcel/cluster match

# ── EXIF coordinates from the evidence photos ─────────────────────────────────
# A visita links several photos; their GPS points cluster on the site. Measured
# on the real data: dispersion to the centroid is 14 m median, 45 m p90, 66 m
# max. The cut sits above that spread so normal scatter survives and the stray
# photo (taken elsewhere and uploaded with the rest) is dropped.
EXIF_OUTLIER_M = 75.0
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

# ── Geocoding (Google Maps) ───────────────────────────────────────────────────
# Fills `coords` ONLY where empty, from the normalized address. Precision over
# coverage: ROOFTOP is the parcel itself, RANGE_INTERPOLATED is a block-level
# interpolation; GEOMETRIC_CENTER (street/neighbourhood centre) is rejected —
# a coordinate hundreds of metres off poisons the geographic tiers.
# The values are the nominal precision (metres) recorded per accepted result.
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
GEOCODE_API_KEY_ENV = "GOOGLE_MAPS_API_KEY"
GEOCODE_ACCEPTED = {"ROOFTOP": 15.0, "RANGE_INTERPOLATED": 40.0}
# Cache location candidates, in priority order: explicit env var, the Railway
# volume (survives the container), the repo itself (committed, seeds Railway).
GEOCODE_CACHE_ENV = "GEOCODE_CACHE_DIR"
GEOCODE_VOLUME_DIR = "/data/geocode"
GEOCODE_REPO_DIR = DATA_DIR / "geocode"

# ── Geo key (last-resort tier) ────────────────────────────────────────────────
# Runs only on visitas that survived the whole cascade AND the trust cutoff with
# no match. The coordinate becomes the key by itself, so the guard is not just
# proximity: the nearest EDAN site must also be UNAMBIGUOUS — the runner-up has
# to be clearly farther away. Two sites equally close means the coordinate does
# not identify anything, and we would rather leave the visita unmatched.
#
# The radius matches GEO_FAR_M rather than being tuned upward until matches
# appear: 80 m is proximity this project already sanctions, and the two extra
# guards here (ambiguity margin, barrio) are stricter than the geo tier's.
#
# Expect little from it. Measured 2026-08-14, it adds ~3 matches, because EDAN
# only carries a coordinate for 9,4% of its sites — the ceiling is EDAN's
# coordinate coverage, not the threshold. It pays off as that coverage grows.
GEO_KEY_MAX_M = 80.0      # nearest EDAN site must be within this radius
GEO_KEY_MARGIN_M = 25.0   # ...and the 2nd nearest at least this much farther

# ── Trust ─────────────────────────────────────────────────────────────────────
TRUST_MIN = 0.70           # reliability cutoff: matches below this are rejected

# ── Trust base per method (higher = more deterministic evidence) ──────────────
METHOD_TRUST_BASE = {
    "preregistro": 0.97,      # the visita carries the EDAN consecutive id itself
    "handshake": 0.95,
    "vector": 0.93,
    "spatial_bridge": 0.90,   # cadastral parcel identity — very strong
    "vector_block": 0.82,
    "corner": 0.82,
    "geo": 0.78,
    "tfidf": 0.75,
    "embedding": 0.72,
    "fuzzy": 0.70,
    "geo_key": 0.74,          # coordinate alone, last resort — deliberately low
}
