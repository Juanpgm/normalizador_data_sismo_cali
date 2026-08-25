"""Cross-reference ("cruce") every Panel point against Firestore `evaluaciones`
(field stickers) and persist the result to `sticker_matches`, recurringly.

Extracts the matching cascade already proven in
`integracion_F1/stickers_analysis.ipynb` (`cruce_sticker()`, cell 10) — reusing
`integracion_F1/cruce_gestor.py`'s cascade functions (`nearest`,
`match_by_direccion`, `build_addr_index`, `addr_key`, `_eval_latlon`), not
reimplementing them. Panel points come from `web/data/inspections.json`
(EDE, EXIF-corrected `x`/`y`) + `puntos_israel_cali.json` (Israel delegation),
same as the notebook.

`sticker_matches/{fuente}_{registro_id}` is split into a pipeline-owned field
group (this job) and an admin-owned field group (`api/sticker-asignaciones.js`,
Phase 2). The job only ever writes the pipeline-owned subset via a
`merge:true` batched set, seeding `estado_asignacion:'pendiente'` (+
`cuadrilla_id`/`inspector_uid: null`) on a doc's first write only, and never
overwrites those fields on a doc that already exists — see `design.md` ADR-1.

    python cruce_sticker.py --check     # offline self-check, no network
    python cruce_sticker.py --dry       # real data, no Firestore write
    python cruce_sticker.py             # real data, write sticker_matches
    python cruce_sticker.py --top 50    # cap to the first N panel points (debug)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from cruce_gestor import (addr_key, build_addr_index, match_by_direccion,
                          nearest, _eval_latlon)

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[0]
INSPECTIONS_JSON = REPO_ROOT / "web" / "data" / "inspections.json"
ISRAEL_JSON = REPO_ROOT / "puntos_israel_cali.json"

# Not the integracion_F1 subproject's default Firestore project (dagma-85aad,
# see subir_cruce_firebase.py) — `evaluaciones`/`sticker_matches` live in the
# dashboard's own project.
STICKERS_PROJECT = os.environ.get("STICKERS_FIREBASE_PROJECT", "sismo-agosto-sgred")
STICKER_MATCHES_COLLECTION = "sticker_matches"
EVALUACIONES_COLLECTION = "evaluaciones"

MATCH_MAX_M = 40.0     # same proximity threshold as cruce_gestor/asignar_f3
SEM_OK = 0.90           # same "fuzzy exacto" address-ratio threshold as cruce_gestor.ADDR_MATCH_RATIO
BATCH_SIZE = 500        # Firestore batch-write / getAll chunk limit

# ADR-1 field ownership split: the job only ever writes PIPELINE_FIELDS via
# merge:true; ADMIN_DEFAULT_FIELDS is seeded ONLY on a doc's first write, never
# re-applied to a doc that already exists.
PIPELINE_FIELDS = ("fuente", "registro_id", "tiene_sticker", "tier",
                    "sticker_dist_m", "direccion", "coords", "zona_id", "matched_at",
                    "criterio_habitabilidad", "colapso")
ADMIN_DEFAULT_FIELDS = {"estado_asignacion": "pendiente", "cuadrilla_id": None,
                        "inspector_uid": None}


# ── Doc id (ADR-1) ──────────────────────────────────────────────────────────
def doc_id(fuente: str, registro_id: str) -> str:
    """Deterministic sticker_matches doc id — stable across re-runs so the
    pipeline updates the same document instead of duplicating it."""
    return f"{fuente}_{registro_id}"


# ── Panel loading (same as the notebook: EDE + Israel, EXIF-corrected coords) ─
def load_panel() -> list[dict]:
    ede = (json.loads(INSPECTIONS_JSON.read_text(encoding="utf-8"))
           if INSPECTIONS_JSON.exists() else [])
    israel = (json.loads(ISRAEL_JSON.read_text(encoding="utf-8"))
              if ISRAEL_JSON.exists() else [])
    points = []
    for row in [*ede, *israel]:
        x, y = row.get("x"), row.get("y")
        if x is None or y is None:
            continue
        registro_id = row.get("GlobalID") or row.get("id_edan")
        if not registro_id:
            continue
        fuente_raw = str(row.get("fuente") or "EDE").lower()
        fuente = "israel" if "israel" in fuente_raw else "ede"
        # Colapso: single derived tag from the two EDE booleans (Israel points
        # lack them -> "no"). Total wins over parcial when both are set.
        if str(row.get("colapso_total") or "").lower() == "si":
            colapso = "total"
        elif str(row.get("colapso_parcial") or "").lower() == "si":
            colapso = "parcial"
        else:
            colapso = "no"
        points.append({
            "fuente": fuente, "registro_id": str(registro_id),
            "lat": float(y), "lon": float(x),
            "direccion": row.get("direccion_norm") or row.get("direccion") or "",
            # Best-effort zone tag; no KML/polygon lookup in Phase 1 scope —
            # comuna is the only zone-shaped field the Panel already carries.
            "zona_id": row.get("comuna") or None,
            # Habitability + collapse from the EDE, surfaced to the assignment
            # table and the inspector's pre-form cards so field crews see the
            # criticality at a glance.
            "criterio_habitabilidad": row.get("criterio_habitabilidad") or None,
            "colapso": colapso,
        })
    return points


# ── evaluaciones (Firestore, 3-tier credential resolution) ────────────────────
def _firestore_client():
    """Order: 1. STICKERS_FIREBASE_SA path  2. FIREBASE_SERVICE_ACCOUNT_JSON env
    (whole JSON, Railway/CI)  3. ADC. Same 3-tier resolution as
    subir_cruce_firebase.py, but targeting STICKERS_PROJECT explicitly."""
    from google.cloud import firestore
    sa_path = os.environ.get("STICKERS_FIREBASE_SA", "").strip()
    if sa_path:
        return firestore.Client.from_service_account_json(sa_path, project=STICKERS_PROJECT)
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    if sa_json:
        return firestore.Client.from_service_account_info(json.loads(sa_json), project=STICKERS_PROJECT)
    return firestore.Client(project=STICKERS_PROJECT)  # ADC


def fetch_evaluaciones(db) -> list[dict]:
    """Field stickers, flattened with the SAME X/Y/DIRECCION keys cruce_gestor's
    cascade functions expect — same shape as the notebook's fetch_stickers()."""
    out = []
    for doc in db.collection(EVALUACIONES_COLLECTION).stream():
        e = doc.to_dict() or {}
        coords = e.get("coords") or {}
        desc = e.get("descripcion") or {}
        out.append({
            "CODIGO_EDIFICACION": e.get("codigo_edificacion") or doc.id,
            "Y": coords.get("lat"), "X": coords.get("lng"),
            "DIRECCION": desc.get("direccion") or "",
        })
    return out


# ── Matching cascade + quality tier ────────────────────────────────────────────
def _tier(dist_m: float | None, direccion_panel, direccion_sticker) -> str | None:
    """alta: geo AND address agree; media: only one signal backs the match;
    sospechoso: neither. Same idea as the notebook's _tier(), without the
    calle/carrera transposition detector (out of Phase 1 scope)."""
    geo_ok = dist_m is not None and dist_m <= MATCH_MAX_M
    ka, kb = addr_key(direccion_panel), addr_key(direccion_sticker)
    ratio = SequenceMatcher(None, ka, kb).ratio() if ka and kb else 0.0
    sem_ok = ratio >= SEM_OK
    if geo_ok and sem_ok:
        return "alta"
    if geo_ok or sem_ok:
        return "media"
    return "sospechoso"


def cruce_sticker_punto(lat, lon, direccion, evaluaciones: list[dict],
                        addr_index: list[tuple[str, dict]]) -> dict:
    """Panel -> evaluaciones cascade: geo (<= MATCH_MAX_M) then address
    fallback. Calls into cruce_gestor's cascade functions — the matching logic
    itself lives there, not here (Requirement: reuses the matching cascade)."""
    best, dist = nearest(lat, lon, evaluaciones, _eval_latlon, max_m=MATCH_MAX_M)
    if best is None:
        best, _via, dist = match_by_direccion(lat, lon, direccion, addr_index)
    if best is None:
        return {"tiene_sticker": False, "tier": None, "sticker_dist_m": None}
    dist_m = round(dist, 1) if dist is not None else None
    return {"tiene_sticker": True, "sticker_dist_m": dist_m,
            "tier": _tier(dist_m, direccion, best.get("DIRECCION"))}


# ── Write path (ADR-1: pipeline-owned fields only, merge:true, batched) ───────
def build_write_ops(points: list[dict], existing_ids: set[str]) -> list[tuple[str, dict]]:
    """(doc_id, write_fields) per point. write_fields ONLY ever contains
    PIPELINE_FIELDS, so a merge:true set can never touch an admin-owned field —
    plus ADMIN_DEFAULT_FIELDS, but ONLY when the doc has no prior write
    (first-write pending seed). Pure — no Firestore access, testable offline."""
    ops = []
    for p in points:
        did = doc_id(p["fuente"], p["registro_id"])
        # .get so a point missing an optional pipeline field (e.g. Israel points
        # have no habitability/colapso) writes None instead of crashing.
        fields = {k: p.get(k) for k in PIPELINE_FIELDS}
        if did not in existing_ids:
            fields.update(ADMIN_DEFAULT_FIELDS)
        ops.append((did, fields))
    return ops


def write_sticker_matches(db, points: list[dict]) -> int:
    col = db.collection(STICKER_MATCHES_COLLECTION)
    refs = [col.document(doc_id(p["fuente"], p["registro_id"])) for p in points]

    existing_ids: set[str] = set()
    for start in range(0, len(refs), BATCH_SIZE):
        chunk = refs[start:start + BATCH_SIZE]
        if not chunk:
            continue
        for snap in db.get_all(chunk):
            if snap.exists:
                existing_ids.add(snap.id)

    ops = build_write_ops(points, existing_ids)
    n = 0
    for start in range(0, len(ops), BATCH_SIZE):
        batch = db.batch()
        for did, fields in ops[start:start + BATCH_SIZE]:
            batch.set(col.document(did), fields, merge=True)
            n += 1
        batch.commit()
    return n


# ── Self-check ────────────────────────────────────────────────────────────────
def _selfcheck_cruce_sticker():
    # Doc id is stable / deterministic.
    assert doc_id("ede", "1234") == "ede_1234"
    assert doc_id("israel", "45") == "israel_45"

    # Matching cascade reuse: geo hit, address-fallback hit, and a clean miss —
    # same idiom as the notebook's own _selfcheck_cruce_sticker().
    evaluaciones = [
        {"CODIGO_EDIFICACION": "76001-1-0010001", "Y": 3.4200, "X": -76.5300,
         "DIRECCION": "Calle 1 # 2-3"},
        {"CODIGO_EDIFICACION": "76001-1-0020001", "Y": 3.4500, "X": -76.5600,
         "DIRECCION": "Carrera 9 # 8-7"},
    ]
    addr_index = build_addr_index(evaluaciones)

    r = cruce_sticker_punto(3.42001, -76.53001, "Calle 1 # 2-3", evaluaciones, addr_index)  # ~1 m, address agrees too
    assert r["tiene_sticker"] and r["sticker_dist_m"] < 2.0 and r["tier"] == "alta", r

    r = cruce_sticker_punto(3.9, -76.9, "CL 1 No. 2-3, Cali", evaluaciones, addr_index)  # far, same address
    assert r["tiene_sticker"] and r["tier"] == "media", r  # address agrees, geo doesn't

    r = cruce_sticker_punto(3.9, -76.9, "DG 99 # 1-1", evaluaciones, addr_index)  # neither signal
    assert not r["tiene_sticker"] and r["tier"] is None, r

    # (a) Re-run on an existing doc: the write dict never carries an
    # admin-owned key, so a merge:true set leaves estado_asignacion/
    # cuadrilla_id/inspector_uid/asignado_en/reasignado_de untouched.
    points = [
        {"fuente": "ede", "registro_id": "1234", "tiene_sticker": True, "tier": "alta",
         "sticker_dist_m": 5.0, "direccion": "CL 1 # 2-3", "coords": {"lat": 3.42, "lon": -76.53},
         "zona_id": "Comuna 3", "matched_at": "2026-08-25T00:00:00"},
        {"fuente": "israel", "registro_id": "45", "tiene_sticker": False, "tier": None,
         "sticker_dist_m": None, "direccion": "", "coords": {"lat": 3.50, "lon": -76.40},
         "zona_id": None, "matched_at": "2026-08-25T00:00:00"},
    ]
    existing = {doc_id("ede", "1234")}
    ops = build_write_ops(points, existing)
    by_id = dict(ops)

    ede_fields = by_id[doc_id("ede", "1234")]
    for admin_field in ("estado_asignacion", "cuadrilla_id", "inspector_uid",
                        "asignado_en", "reasignado_de"):
        assert admin_field not in ede_fields, ede_fields
    assert ede_fields["tiene_sticker"] is True and ede_fields["tier"] == "alta"
    assert set(ede_fields) == set(PIPELINE_FIELDS)

    # (b) First write (no prior doc): pending assignment state is seeded
    # alongside the pipeline fields, in the same merge:true set.
    israel_fields = by_id[doc_id("israel", "45")]
    assert israel_fields["estado_asignacion"] == "pendiente"
    assert israel_fields["cuadrilla_id"] is None and israel_fields["inspector_uid"] is None
    assert israel_fields["tiene_sticker"] is False and israel_fields["tier"] is None
    assert set(israel_fields) == set(PIPELINE_FIELDS) | set(ADMIN_DEFAULT_FIELDS)

    print("cruce_sticker self-check OK")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> dict:
    if "--check" in sys.argv:
        _selfcheck_cruce_sticker()
        return {}
    top = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else None

    panel = load_panel()
    if top is not None:
        panel = panel[:top]

    db = _firestore_client()
    evaluaciones = fetch_evaluaciones(db)
    addr_index = build_addr_index(evaluaciones)
    print(f"Panel: {len(panel)} puntos | evaluaciones (stickers) en Firestore: {len(evaluaciones)}")

    now = datetime.now(timezone.utc)
    points = []
    for p in panel:
        r = cruce_sticker_punto(p["lat"], p["lon"], p["direccion"], evaluaciones, addr_index)
        points.append({
            "fuente": p["fuente"], "registro_id": p["registro_id"],
            "tiene_sticker": r["tiene_sticker"], "tier": r["tier"],
            "sticker_dist_m": r["sticker_dist_m"], "direccion": p["direccion"],
            "coords": {"lat": p["lat"], "lon": p["lon"]}, "zona_id": p["zona_id"],
            "criterio_habitabilidad": p["criterio_habitabilidad"], "colapso": p["colapso"],
            "matched_at": now,
        })

    n_con = sum(1 for x in points if x["tiene_sticker"])
    print(f"con sticker: {n_con} ({n_con / len(points):.1%}) | faltantes: {len(points) - n_con}"
          if points else "sin puntos de panel con coords")
    summary = {"total": len(points), "con_sticker": n_con, "faltantes": len(points) - n_con}

    if "--dry" in sys.argv:
        print(f"[dry] no Firestore write; {len(points)} docs listos para {STICKER_MATCHES_COLLECTION}")
        return summary

    n = write_sticker_matches(db, points)
    print(f"escritos {n} docs -> {db.project}/{STICKER_MATCHES_COLLECTION}")
    summary["escritos"] = n
    return summary


if __name__ == "__main__":
    main()
