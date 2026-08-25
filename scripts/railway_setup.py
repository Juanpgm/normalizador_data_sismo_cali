"""Provision and configure the Railway cron services for this project.

The `normalizador-sismo-cali` project has one environment (production) and is
deployed by CLI upload (`railway up`), not from a connected git repo. All cron
services share one Docker image but run a different entrypoint on a different
schedule:

    normalizador    python job.py               0 * * * *     tabla_integrada
    integracion-f3  python job_integrar_f3.py   0 */2 * * *   cruce F3 ↔ integrada
    asignaciones    python job_asignaciones.py  0 21 * * *    top-100 priorizado
                                                              (21:00 UTC = 16:00 Bogotá)

Railway reads `cronSchedule`/`startCommand` from the *service instance*, not from
`railway.json` — and since every service is uploaded from this same directory,
`railway.json` deliberately carries neither, so no service can override another's
command. This script owns those per-service settings and writes them through the
public API so the whole fleet is reproducible instead of dashboard clicks.

It creates a service shell and configures its schedule/command/policy. The code
itself is deployed separately, per service, from this directory:

    railway up --service integracion-f3 --path-as-root .
    railway up --service asignaciones --path-as-root .

`--path-as-root .` is NOT optional — the Railway CLI's linked-project root can
drift to the PARENT dashboard repo (its own `package.json`/`formulario/`/
`services/photo-signer/` end up in the upload instead of this subproject's
Dockerfile), independently of your shell's cwd; that's what silently broke
`cruce-sticker` (38MB upload of the wrong tree, Railpack auto-detected Node
from the leaked package.json files, every scheduled run crashed with "python:
command not found", 2026-08-25 — a stuck `builder=RAILPACK` on the service
instance looked like the cause at first but wasn't; deleting+recreating the
service made no difference until `--path-as-root .` was added). Confirm with
`--verbose` before trusting a deploy: it prints `bytes: N` — this subproject
is under 1MB gitignored, so anything in the tens of MB means the wrong tree
went up.

The script is idempotent and safe to re-run: services are matched by name (an
existing one is reused, never duplicated), and settings are written only when
they drift from the desired state.

    python scripts/railway_setup.py            # apply the whole fleet
    python scripts/railway_setup.py --show     # report only, change nothing
    python scripts/railway_setup.py --dry      # print the plan, create/update nothing
    python scripts/railway_setup.py --only asignaciones   # one service

Auth comes from the Railway CLI session (`railway login`), or RAILWAY_API_TOKEN.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# The report/help text uses non-ASCII (→ ⚠ á); the Windows console defaults to
# cp1252 and would crash on write. Railway's log viewer is UTF-8 already.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

API = "https://backboard.railway.com/graphql/v2"
PROJECT_ID = "f32efdbf-a8d5-4a43-9369-cb7b7623c4f6"
ENVIRONMENT_ID = "4418f451-bd97-4d96-ba6e-b5ecbbd49c9b"

# Desired fleet. `name` is unique within the project (the idempotency key). The
# already-provisioned hourly service is named `normalizador`; its id is pinned
# so a name change never detaches it.
# Cadencia unificada de lectura: cada 15 min PERO solo en horario diurno de
# Colombia (08:00–19:45 COT), en pausa nocturna. Railway agenda el cron en UTC
# y Colombia es UTC-5, así que la franja 08:00–19:59 COT es 13:00–00:59 UTC →
# horas 13-23,0. (Ventana ajustada a mano en la UI 2026-08-20; este script la
# adopta para que un --apply no la pise.) cruce-gestion corre desfasado a
# :10/:25/:40/:55 para no pushear a main en el mismo minuto que dashboard-refresh.
EVERY_15 = "*/15 13-23,0 * * *"
EVERY_15_OFFSET = "10,25,40,55 13-23,0 * * *"
# El normalizador (tabla_integrada → Sheet EDAN SISMO) alimenta HUMANOS, no al
# dashboard (integrar/asignar F3 leen la API atencionsismo desde 2026-08-18):
# horario alcanza y cuadruplicar escrituras a Sheets no aporta. Minuto :05 para
# no chocar con los slots :00/:15/... ni :10/:25/... de los otros jobs.
HOURLY_DAY = "5 13-23,0 * * *"
# Cruce sticker↔Panel: cada 15 min en horario diurno de Colombia (13-23,0 UTC =
# 08:00–19:59 COT). Corre desfasado a :07/:22/:37/:52 — unos minutos DESPUÉS de
# que dashboard-refresh publica inspections.json (*/15 = :00/:15/:30/:45), para
# leer Panel fresco. Escribe a Firestore (no a git), así que no hay riesgo de
# pushear a main en el mismo minuto que otro job.
STICKER_EVERY_15 = "7,22,37,52 13-23,0 * * *"

SERVICES = [
    {"name": "normalizador", "start_command": "python job.py",
     "cron": HOURLY_DAY, "service_id": "c4f7fdf7-88bb-42d5-9442-42ac75517bbd"},
    {"name": "integracion-f3", "start_command": "python job_integrar_f3.py",
     "cron": EVERY_15, "service_id": "10573e05-b476-4296-80f9-8dd10c2c55cb"},
    {"name": "asignaciones", "start_command": "python job_asignaciones.py",
     "cron": EVERY_15, "service_id": "d46b3e86-3507-4597-ba65-250b6654cbd3"},
    # Refreshes the Vercel dashboard from the F3 Sheet. Different image entirely
    # (deploy/ in the dashboard repo): its Dockerfile CMD is the entrypoint, so
    # no startCommand override — this script only owns its schedule.
    {"name": "dashboard-refresh", "start_command": None,
     "cron": EVERY_15, "service_id": "156e97a2-596b-4861-95f4-4060dab408e2"},
    # Cruce críticos↔survey + push a Firestore (gestión). Necesita en Railway:
    # GOOGLE_SERVICE_ACCOUNT_JSON (SA de dagma-85aad) e INSPECTIONS_URL
    # (inspections.json publicado en Vercel).
    {"name": "cruce-gestion", "start_command": "python job_cruce.py",
     "cron": EVERY_15_OFFSET, "service_id": "b4c8fd15-aa3b-4157-b787-2034c89a108b"},
    # Match stickers (evaluaciones, live from Firestore) against Panel points
    # every 15 min so sticker_matches.tiene_sticker stays current. Needs on
    # Railway: INSPECTIONS_URL (Blob inspections.json) + FIREBASE_SERVICE_ACCOUNT_JSON
    # (SA de sismo-agosto-sgred). Slots :07/:22/:37/:52 (lag tras el refresh).
    # service_id: None — the old shell (3b786c3f-...) never deployed
    # successfully (stuck on builder=RAILPACK, unclearable via the API — see
    # COMMON's comment) and was deleted 2026-08-25; this recreates it fresh.
    {"name": "cruce-sticker", "start_command": "python job_sticker.py",
     "cron": STICKER_EVERY_15, "service_id": None},
]

# builder: None (unset) is what lets Railway auto-detect the Dockerfile — the
# `Builder` enum only lists ALTERNATIVE builders (HEROKU/NIXPACKS/PAKETO/
# RAILPACK, confirmed via GraphQL introspection on ServiceInstanceUpdateInput),
# there is no literal "DOCKERFILE" value to force. Every service, including a
# brand-new never-deployed one, reports `builder: 'RAILPACK'` until its FIRST
# successful build resolves what it actually used — that label alone is not a
# sign of anything wrong; setting it to None here is inert (the API silently
# ignores the write) but harmless, kept as a no-op guard in case a future
# Railway API version does let it stick.
#
# The REAL cause of cruce-sticker's "python: command not found" crash-loop
# (2026-08-25) had nothing to do with this field: `railway up` from
# integracion_F1/ was uploading the DASHBOARD REPO ROOT instead (package.json,
# formulario/, services/photo-signer/ all leaked in — Railpack correctly, from
# its point of view, detected Node and built that). Deleting+recreating the
# service made no difference. The fix is `--path-as-root .` on every `railway
# up` — see the module docstring above.
COMMON = {"restartPolicyType": "NEVER", "numReplicas": 1, "builder": None}


def _token() -> str:
    token = os.environ.get("RAILWAY_API_TOKEN", "").strip()
    if token:
        return token
    # Project token del .env del subproyecto (tiene prioridad sobre el token de
    # cuenta cacheado por `railway login`, que puede estar expirado/sin scope).
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for ln in env.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln.startswith("RAILWAY_API_TOKEN") and "=" in ln:
                return ln.split("=", 1)[1].strip().strip("\"'")
    config = Path.home() / ".railway" / "config.json"
    if not config.exists():
        raise SystemExit("No Railway credentials. Run `railway login` or set "
                         "RAILWAY_API_TOKEN.")
    return json.loads(config.read_text(encoding="utf-8"))["user"]["accessToken"]


def gql(query: str, variables: dict | None = None) -> dict:
    request = urllib.request.Request(
        API,
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={
            "Content-Type": "application/json",
            # Token de PROYECTO (creado en el dashboard del proyecto): Railway lo
            # autoriza con el header Project-Access-Token, no con Authorization Bearer.
            "Project-Access-Token": _token(),
            # Cloudflare answers 403 to requests without a User-Agent.
            "User-Agent": "normalizador-sismo-cali/1.0",
        },
    )
    try:
        payload = json.load(urllib.request.urlopen(request))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Railway API {exc.code}: {exc.read().decode()[:400]}")
    if "errors" in payload:
        raise SystemExit(f"Railway API error: {payload['errors']}")
    return payload["data"]


LIST_SERVICES = """query($p:String!){
  project(id:$p){ services{ edges{ node{ id name } } } } }"""

INSTANCE = """query($s:String!,$e:String!){
  serviceInstance(serviceId:$s, environmentId:$e){
    cronSchedule startCommand restartPolicyType numReplicas builder dockerfilePath } }"""

CREATE = "mutation($in:ServiceCreateInput!){ serviceCreate(input:$in){ id } }"

UPDATE = """mutation($s:String!,$e:String,$in:ServiceInstanceUpdateInput!){
  serviceInstanceUpdate(serviceId:$s, environmentId:$e, input:$in) }"""


def project_services() -> dict[str, str]:
    """name -> service_id for every service currently in the project."""
    data = gql(LIST_SERVICES, {"p": PROJECT_ID})
    return {e["node"]["name"]: e["node"]["id"]
            for e in data["project"]["services"]["edges"]}


def instance(service_id: str) -> dict:
    return gql(INSTANCE, {"s": service_id, "e": ENVIRONMENT_ID})["serviceInstance"]


def create_service(name: str) -> str:
    """Create an empty service shell. Code is deployed later via `railway up`."""
    return gql(CREATE, {"in": {"projectId": PROJECT_ID, "name": name}})["serviceCreate"]["id"]


def desired(spec: dict) -> dict:
    d = {"cronSchedule": spec["cron"], **COMMON}
    if spec.get("start_command"):   # None -> keep the image's Dockerfile CMD
        d["startCommand"] = spec["start_command"]
    return d


def apply_service(spec: dict, by_name: dict[str, str], dry: bool) -> bool:
    name = spec["name"]
    service_id = spec["service_id"] or by_name.get(name)
    newly_created = False

    if not service_id:
        print(f"[{name}] no existe → crear")
        if dry:
            print("  (dry) serviceCreate (shell vacío; luego `railway up "
                  f"--service {name}`)")
            return True
        service_id = create_service(name)
        newly_created = True
        print(f"  creado service_id={service_id}  "
              f"→ desplegá el código: railway up --service {name}")
    else:
        print(f"[{name}] service_id={service_id}")

    want = desired(spec)
    # A just-created service has no instance settings yet, so apply everything.
    before = {} if newly_created else instance(service_id)
    drift = {k: v for k, v in want.items() if before.get(k) != v}
    if not drift:
        print("  ya aplicado; nada que hacer")
        return True

    print(f"  aplicando: {drift}")
    if dry:
        print("  (dry) sin escribir")
        return True
    try:
        gql(UPDATE, {"s": service_id, "e": ENVIRONMENT_ID, "in": drift})
    except SystemExit as exc:
        if newly_created:
            print(f"  ⚠️  '{name}' fue CREADO pero NO configurado ({exc}).\n"
                  f"     Queda sin cronSchedule (no se agenda). Re-corré este "
                  f"script para terminar de configurarlo antes de desplegarlo.")
        else:
            print(f"  ⚠️  no se pudo aplicar a '{name}': {exc}")
        return False

    after = instance(service_id)
    ok = all(after.get(k) == v for k, v in want.items())
    for k, v in want.items():
        got = after.get(k)
        print(f"    {k:20s} {got!r}  [{'ok' if got == v else 'NO APLICÓ'}]")
    if spec["service_id"] is None:
        print(f"  → fijá service_id de '{name}' en SERVICES: {service_id!r}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", action="store_true",
                    help="report current settings of the fleet, change nothing")
    ap.add_argument("--dry", action="store_true",
                    help="print the plan without creating or updating anything")
    ap.add_argument("--only", metavar="NAME", help="target a single service by name")
    args = ap.parse_args()

    by_name = project_services()
    specs = [s for s in SERVICES if not args.only or s["name"] == args.only]
    if not specs:
        raise SystemExit(f"--only {args.only!r} no coincide con ningún servicio")

    if args.show:
        for spec in specs:
            sid = spec["service_id"] or by_name.get(spec["name"])
            if not sid:
                print(f"[{spec['name']}] aún no existe")
                continue
            cur = instance(sid)
            print(f"[{spec['name']}] {sid}")
            for k in ("cronSchedule", "startCommand", "restartPolicyType", "numReplicas"):
                print(f"  {k:20s} {cur.get(k)!r}")
        return 0

    # Evaluate every service — never short-circuit, or one early failure would
    # leave the rest of the fleet unprovisioned.
    results = [apply_service(spec, by_name, args.dry) for spec in specs]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
