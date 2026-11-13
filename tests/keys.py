"""Throw-away RSA key pair used to mint Entra-like tokens in tests (never used outside tests)."""

from __future__ import annotations

import json

from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

TENANT_ID = "11111111-1111-1111-1111-111111111111"
API_CLIENT_ID = "22222222-2222-2222-2222-222222222222"
KID = "test-key-1"

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

_jwk = json.loads(RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key()))
_jwk.update(kid=KID, use="sig", alg="RS256")
JWKS_JSON = json.dumps({"keys": [_jwk]})
