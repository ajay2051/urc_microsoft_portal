#!/usr/bin/env python
"""Local-only helper: mint Entra-shaped access tokens for manual API testing.

    python scripts/dev_token.py init                     # create a throw-away key
    python scripts/dev_token.py token admin@contoso.com  # print a bearer token (valid 1 hour)
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm


def load_dotenv_file():
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_dotenv_file()

DIR = Path(__file__).resolve().parent.parent / ".dev"
KID = "dev-key"


def need_env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Set {name} first (same value the server uses).")
    return value


def init():
    DIR.mkdir(exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (DIR / "private.pem").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid=KID, use="sig", alg="RS256")
    (DIR / "jwks.json").write_text(json.dumps({"keys": [jwk]}))
    print(f"Created {DIR / 'jwks.json'}")


def token(email):
    tenant, api = need_env("ENTRA_TENANT_ID"), need_env("ENTRA_API_CLIENT_ID")
    now = int(time.time())
    claims = {
        "iss": f"https://login.microsoftonline.com/{tenant}/v2.0", "aud": api, "tid": tenant, "ver": "2.0",
        "oid": str(uuid.uuid5(uuid.NAMESPACE_DNS, email.lower())),
        "sub": uuid.uuid4().hex, "preferred_username": email.lower(), "scp": "access_as_user",
        "iat": now, "nbf": now - 5, "exp": now + 3600,
    }
    print(jwt.encode(claims, (DIR / "private.pem").read_bytes(), algorithm="RS256", headers={"kid": KID}))


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "init":
        init()
    elif len(sys.argv) == 3 and sys.argv[1] == "token":
        token(sys.argv[2])
    else:
        sys.exit(__doc__)
