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

    railway up --service integracion-f3
    railway up --service asignaciones

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
SERVICES = [
    {"name": "normalizador", "start_command": "python job.py",
     "cron": "0 * * * *", "service_id": "c4f7fdf7-88bb-42d5-9442-42ac75517bbd"},
    {"name": "integracion-f3", "start_command": "python job_integrar_f3.py",
     "cron": "0 */2 * * *", "service_id": "10573e05-b476-4296-80f9-8dd10c2c55cb"},
    {"name": "asignaciones", "start_command": "python job_asignaciones.py",
     "cron": "0 21 * * *",   # 16:00 America/Bogota (UTC-5)
     "service_id": "d46b3e86-3507-4597-ba65-250b6654cbd3"},
    # Refreshes the Vercel dashboard from the F3 Sheet. Different image entirely
    # (deploy/ in the dashboard repo): its Dockerfile CMD is the entrypoint, so
    # no startCommand override — this script only owns its schedule.
    {"name": "dashboard-refresh", "start_command": None,
     "cron": "0 * * * *", "service_id": "156e97a2-596b-4861-95f4-4060dab408e2"},
]

COMMON = {"restartPolicyType": "NEVER", "numReplicas": 1}


def _token() -> str:
    token = os.environ.get("RAILWAY_API_TOKEN", "").strip()
    if token:
        return token
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
            "Authorization": f"Bearer {_token()}",
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
    cronSchedule startCommand restartPolicyType numReplicas } }"""

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
