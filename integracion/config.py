"""Central configuration: data sources, paths and tunable thresholds."""
from __future__ import annotations

from pathlib import Path

# ── Repo layout ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
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

# ── Trust ─────────────────────────────────────────────────────────────────────
TRUST_MIN = 0.70           # reliability cutoff: matches below this are rejected

# ── Trust base per method (higher = more deterministic evidence) ──────────────
METHOD_TRUST_BASE = {
    "handshake": 0.95,
    "vector": 0.93,
    "spatial_bridge": 0.90,   # cadastral parcel identity — very strong
    "vector_block": 0.82,
    "corner": 0.82,
    "geo": 0.78,
    "tfidf": 0.75,
    "embedding": 0.72,
    "fuzzy": 0.70,
}
