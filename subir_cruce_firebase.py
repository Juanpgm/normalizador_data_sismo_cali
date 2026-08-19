"""Sube web/data/cruce_criticos_survey.json a una coleccion de Firestore.

Mantiene el registro del cruce critico<->survey para gestionarlo despues. Cada
critico es un documento idempotente (doc id = registro_id, upsert con merge), asi
que re-correr sobre-escribe sin duplicar. La `clave_integracion` y el
`survey_globalid` quedan como campos (la llave que engancha ambos lados).

Credenciales: mismo `service_account.json` del subproyecto (o
$GOOGLE_SERVICE_ACCOUNT_JSON con el JSON entero). El SA necesita rol
`roles/datastore.user` en el proyecto y la Firestore API habilitada.

Uso:
    python subir_cruce_firebase.py --dry     # no conecta; muestra que subiria
    python subir_cruce_firebase.py           # sube a Firestore
    python subir_cruce_firebase.py --project OTRO_ID --coll otra_coleccion
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

# Importar config carga el .env (FIREBASE_SA / FIREBASE_PROJECT) al entorno.
from integracion.config import (FIRESTORE_COLLECTION, FIRESTORE_PROJECT,
                                FIRESTORE_SA_FILE)

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[0]
CRUCE_JSON = REPO_ROOT / "web" / "data" / "cruce_criticos_survey.json"
COLLECTION = FIRESTORE_COLLECTION
META_DOC = "_meta/resumen"  # coleccion _meta, doc resumen


def load_cruce() -> dict:
    return json.loads(CRUCE_JSON.read_text(encoding="utf-8"))


def _client(project: str | None, sa: str | None):
    """Firestore client. Orden de credenciales:
      1. --sa RUTA (service account key JSON explicito)
      2. $GOOGLE_SERVICE_ACCOUNT_JSON (el JSON entero, para CI/containers)
      3. ADC: `gcloud auth application-default login` o $GOOGLE_APPLICATION_CREDENTIALS
    Nota: service_account.json (SA de pmegeocode) NO alcanza dagma-85aad, por eso
    no es el default aca."""
    from google.cloud import firestore
    sa = sa or FIRESTORE_SA_FILE
    if sa:
        return firestore.Client.from_service_account_json(sa, project=project)
    env = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if env:
        return firestore.Client.from_service_account_info(json.loads(env), project=project)
    return firestore.Client(project=project)  # ADC (login por CLI o GOOGLE_APPLICATION_CREDENTIALS)


def upload(data: dict, project: str | None, collection: str, sa: str | None) -> int:
    db = _client(project, sa)
    records = data["records"]
    col = db.collection(collection)
    # Batches de 500 (limite de Firestore por commit).
    n = 0
    for start in range(0, len(records), 400):
        batch = db.batch()
        for rec in records[start:start + 400]:
            doc_id = str(rec["registro_id"]) or rec["clave_integracion"]
            batch.set(col.document(doc_id), rec, merge=True)
            n += 1
        batch.commit()
    # Doc de resumen + metadatos de la corrida.
    meta_col, meta_id = META_DOC.split("/")
    db.collection(meta_col).document(meta_id).set({
        "generated_at": data["generated_at"],
        "match_radio_m": data["match_radio_m"],
        "resumen": data["resumen"],
        "zonas": data["zonas"],
    }, merge=True)
    print(f"subidos {n} docs -> {db.project}/{collection} (+ {META_DOC})")
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="no conecta; solo valida el JSON")
    ap.add_argument("--project", default=FIRESTORE_PROJECT, help="project_id destino")
    ap.add_argument("--coll", default=COLLECTION, help="nombre de la coleccion")
    ap.add_argument("--sa", default=None, help="override: ruta a un service account key JSON")
    args = ap.parse_args()

    data = load_cruce()
    r = data["resumen"]
    print(f"cruce a subir: {r['total_criticos']} criticos "
          f"({r['levantados']} levantados / {r['pendientes']} pendientes)")
    if args.dry:
        ej = data["records"][0]
        print("ejemplo de doc (id = registro_id):")
        print(json.dumps(ej, ensure_ascii=False, indent=1))
        print(f"[dry] subiria {len(data['records'])} docs a "
              f"{args.project}/{args.coll}")
        return
    upload(data, args.project, args.coll, args.sa)


if __name__ == "__main__":
    main()
