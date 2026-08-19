"""Setup de Firebase para el registro del cruce (proyecto dagma-85aad).

Dos tareas de administracion, con el SA admin (FIREBASE_SA del .env):

  python firebase_setup.py rules              # despliega firestore.rules
  python firebase_setup.py user EMAIL [--pass CLAVE]
                                              # crea/asegura usuario del equipo
                                              # con custom claim pmu=true (puede editar)
  python firebase_setup.py user EMAIL --revoke  # quita el claim pmu (solo lectura)

El claim pmu==true es lo que las reglas exigen para escribir; un registro
espontaneo por el apiKey publico no lo tiene y queda en solo-lectura.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

from integracion.config import FIRESTORE_PROJECT, FIRESTORE_SA_FILE

HERE = Path(__file__).resolve().parent
RULES_FILE = HERE / "firestore.rules"


def _session():
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    creds = service_account.Credentials.from_service_account_file(
        FIRESTORE_SA_FILE, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(creds)


def deploy_rules() -> None:
    s = _session()
    src = RULES_FILE.read_text(encoding="utf-8")
    base = f"https://firebaserules.googleapis.com/v1/projects/{FIRESTORE_PROJECT}"
    r = s.post(base + "/rulesets",
               json={"source": {"files": [{"name": "firestore.rules", "content": src}]}},
               timeout=30)
    r.raise_for_status()
    name = r.json()["name"]
    rel = s.patch(base + "/releases/cloud.firestore",
                  json={"release": {"name": f"projects/{FIRESTORE_PROJECT}/releases/cloud.firestore",
                                    "rulesetName": name}}, timeout=30)
    rel.raise_for_status()
    print(f"reglas desplegadas -> ruleset {name.split('/')[-1]}")


def _admin_app():
    import firebase_admin
    from firebase_admin import credentials
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(FIRESTORE_SA_FILE))
    return firebase_admin


def ensure_user(email: str, password: str | None, revoke: bool) -> None:
    fa = _admin_app()
    from firebase_admin import auth
    try:
        user = auth.get_user_by_email(email)
        created = False
    except auth.UserNotFoundError:
        if revoke:
            print(f"{email} no existe; nada que revocar.")
            return
        password = password or secrets.token_urlsafe(9)
        user = auth.create_user(email=email, password=password, email_verified=True)
        created = True
    claims = dict(user.custom_claims or {})
    if revoke:
        claims.pop("pmu", None)
        auth.set_custom_user_claims(user.uid, claims)
        print(f"claim pmu REVOCADO a {email} (uid {user.uid}) -> solo lectura")
        return
    claims["pmu"] = True
    auth.set_custom_user_claims(user.uid, claims)
    print(f"usuario {'CREADO' if created else 'actualizado'}: {email} (uid {user.uid}) | claim pmu=true")
    if created:
        print(f"  clave temporal: {password}   (cambiala en el primer login)")
    elif password:
        auth.update_user(user.uid, password=password)
        print(f"  clave actualizada: {password}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rules")
    up = sub.add_parser("user")
    up.add_argument("email")
    up.add_argument("--pass", dest="password", default=None)
    up.add_argument("--revoke", action="store_true")
    args = ap.parse_args()
    if args.cmd == "rules":
        deploy_rules()
    elif args.cmd == "user":
        ensure_user(args.email, args.password, args.revoke)


if __name__ == "__main__":
    main()
