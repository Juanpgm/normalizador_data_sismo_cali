"""Apply the Railway service settings that config-as-code does not.

`railway.json` is read into each deployment's manifest, but Railway's scheduler
reads `cronSchedule` from the *service instance*, not from the manifest. A
service deployed with only `railway.json` therefore builds the image and never
runs it — the symptom is deployments marked `buildOnly: true`, empty logs, and
`serviceInstance.cronSchedule = null`.

This script writes those settings through the public API so the deployment is
reproducible instead of a one-off click in the dashboard. It is idempotent: run
it after creating the service, and again whenever the schedule changes.

    python scripts/railway_setup.py            # apply and report
    python scripts/railway_setup.py --show     # report only, change nothing

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

API = "https://backboard.railway.com/graphql/v2"
PROJECT_ID = "f32efdbf-a8d5-4a43-9369-cb7b7623c4f6"
SERVICE_ID = "c4f7fdf7-88bb-42d5-9442-42ac75517bbd"
ENVIRONMENT_ID = "4418f451-bd97-4d96-ba6e-b5ecbbd49c9b"

# Must match railway.json, which stays the human-readable source of truth.
SETTINGS = {
    "cronSchedule": "0 * * * *",
    "restartPolicyType": "NEVER",
    "numReplicas": 1,
}


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


QUERY = """query($s:String!,$e:String!){
  serviceInstance(serviceId:$s, environmentId:$e){
    cronSchedule restartPolicyType numReplicas builder } }"""

MUTATION = """mutation($s:String!,$e:String,$in:ServiceInstanceUpdateInput!){
  serviceInstanceUpdate(serviceId:$s, environmentId:$e, input:$in) }"""


def current() -> dict:
    return gql(QUERY, {"s": SERVICE_ID, "e": ENVIRONMENT_ID})["serviceInstance"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--show", action="store_true",
                    help="report the current settings without changing them")
    args = ap.parse_args()

    before = current()
    print("Configuración actual del servicio:")
    for key, value in before.items():
        print(f"  {key:20s} {value!r}")

    if args.show:
        return 0

    drift = {k: v for k, v in SETTINGS.items() if before.get(k) != v}
    if not drift:
        print("\nYa está aplicada. Nada que hacer.")
        return 0

    print(f"\nAplicando: {drift}")
    gql(MUTATION, {"s": SERVICE_ID, "e": ENVIRONMENT_ID, "in": drift})

    after = current()
    for key, expected in SETTINGS.items():
        got = after.get(key)
        status = "ok" if got == expected else "NO APLICÓ"
        print(f"  {key:20s} {got!r}  [{status}]")
    return 0 if all(after.get(k) == v for k, v in SETTINGS.items()) else 1


if __name__ == "__main__":
    sys.exit(main())
