"""Provision and configure the Railway cron services for this project.

One Docker image, three cron services in the `normalizador-sismo-cali` project,
each with its own schedule and start command:

    hourly          python job.py               0 * * * *     tabla_integrada
    integracion-f3  python job_integrar_f3.py   0 */2 * * *   cruce F3 ↔ integrada
    asignaciones    python job_asignaciones.py  0 21 * * *    top-100 priorizado
                                                              (21:00 UTC = 16:00 Bogotá)

Railway reads `cronSchedule`/`startCommand` from the *service instance*, not from
`railway.json`, so a service configured only by manifest builds the image and
never runs it. This script writes those settings through the public API so the
whole fleet is reproducible instead of a set of dashboard clicks.

It is idempotent and safe to re-run:
  * services are matched by name inside the project — an existing one is
    reused, never duplicated;
  * a missing service is created, cloning the source repo / branch / root
    directory of the existing `hourly` service so the new one builds the same
    image;
  * settings are only written when they drift from the desired state.

    python scripts/railway_setup.py            # apply the whole fleet
    python scripts/railway_setup.py --show     # report only, change nothing
    python scripts/railway_setup.py --dry      # print the plan, create/update nothing
    python scripts/railway_setup.py --only asignaciones   # one service

A created service clones the template's source repo and root directory but NOT
its branch (the API shape for that is unverified here) — a new service builds
from the repo's default branch. If the template deploys from a non-default
branch, set the branch on the new service in the Railway dashboard once.

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

# The already-provisioned hourly service. Its source repo / branch / root
# directory are the template every new service clones, so all three build the
# same Dockerfile.
TEMPLATE_SERVICE_ID = "c4f7fdf7-88bb-42d5-9442-42ac75517bbd"

# Desired fleet. `name` must be unique within the project (idempotency key).
SERVICES = [
    {"name": "hourly", "start_command": "python job.py",
     "cron": "0 * * * *", "service_id": TEMPLATE_SERVICE_ID},
    {"name": "integracion-f3", "start_command": "python job_integrar_f3.py",
     "cron": "0 */2 * * *", "service_id": None},
    {"name": "asignaciones", "start_command": "python job_asignaciones.py",
     "cron": "0 21 * * *", "service_id": None},   # 16:00 America/Bogota (UTC-5)
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
    cronSchedule startCommand rootDirectory restartPolicyType numReplicas
    source{ repo } } }"""

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


def create_service(name: str, template: dict) -> str:
    """Create a service cloning the template's source repo / branch."""
    source = template.get("source") or {}
    src_input = {}
    if source.get("repo"):
        src_input["repo"] = source["repo"]
    payload = {"projectId": PROJECT_ID, "name": name}
    if src_input:
        payload["source"] = src_input
    return gql(CREATE, {"in": payload})["serviceCreate"]["id"]


def desired(spec: dict, template: dict) -> dict:
    """Full desired serviceInstance settings for one service."""
    return {
        "cronSchedule": spec["cron"],
        "startCommand": spec["start_command"],
        "rootDirectory": template.get("rootDirectory"),
        **COMMON,
    }


def apply_service(spec: dict, template: dict, by_name: dict[str, str],
                  dry: bool) -> bool:
    name = spec["name"]
    service_id = spec["service_id"] or by_name.get(name)
    newly_created = False

    if not service_id:
        print(f"[{name}] no existe → crear")
        if dry:
            print(f"  (dry) serviceCreate name={name} "
                  f"repo={template.get('source', {}).get('repo')}")
            return True
        service_id = create_service(name, template)
        newly_created = True
        print(f"  creado service_id={service_id}")
    else:
        print(f"[{name}] service_id={service_id}")

    want = desired(spec, template)
    want = {k: v for k, v in want.items() if v is not None}
    # A just-created service inherits the image default (CMD = job.py, the
    # hourly pipeline) until configured, so apply the full desired state.
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
                  f"     Sin startCommand corre `python job.py` (pipeline horario "
                  f"que escribe tabla_integrada). PAUSÁ el servicio en Railway y "
                  f"re-corré este script para terminar de configurarlo.")
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
    ap.add_argument("--only", metavar="NAME",
                    help="target a single service by name")
    args = ap.parse_args()

    template = instance(TEMPLATE_SERVICE_ID)
    print(f"Template (hourly): repo={template.get('source', {}).get('repo')} "
          f"rootDir={template.get('rootDirectory')!r}\n")

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
            for k in ("cronSchedule", "startCommand", "rootDirectory",
                      "restartPolicyType", "numReplicas"):
                print(f"  {k:20s} {cur.get(k)!r}")
        return 0

    # Evaluate every service — never short-circuit, or one early failure would
    # leave the rest of the fleet unprovisioned.
    results = [apply_service(spec, template, by_name, args.dry) for spec in specs]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
