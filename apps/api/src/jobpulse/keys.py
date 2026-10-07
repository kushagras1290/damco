"""Generate an Ed25519 signing key pair for web → API access tokens.

    python -m jobpulse.keys [--kid KID]

Prints two env lines:
  API_JWT_PRIVATE_JWK  -> web app only (signs tokens; keep secret)
  API_JWT_JWKS         -> API (public key set; verifies tokens)

Rotation: generate a new pair with a new kid, ADD its public key to API_JWT_JWKS
(keep the old one), deploy the API, switch the web app's private key, then remove the
old public key once outstanding tokens (max 5 min) have expired.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jwt.algorithms import OKPAlgorithm


def generate(kid: str) -> tuple[dict[str, str], dict[str, list[dict[str, str]]]]:
    private_key = Ed25519PrivateKey.generate()
    private_jwk: dict[str, str] = json.loads(OKPAlgorithm.to_jwk(private_key))
    public_jwk: dict[str, str] = json.loads(OKPAlgorithm.to_jwk(private_key.public_key()))
    for jwk in (private_jwk, public_jwk):
        jwk.update({"kid": kid, "alg": "EdDSA", "use": "sig"})
    return private_jwk, {"keys": [public_jwk]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    default_kid = f"{datetime.now(tz=UTC):%Y%m%d}-{secrets.token_hex(3)}"
    parser.add_argument("--kid", default=default_kid, help="key id (default: date + random suffix)")
    args = parser.parse_args()
    private_jwk, jwks = generate(args.kid)
    compact = {"separators": (",", ":")}
    sys.stdout.write(f"API_JWT_PRIVATE_JWK={json.dumps(private_jwk, **compact)}\n")  # type: ignore[arg-type]
    sys.stdout.write(f"API_JWT_JWKS={json.dumps(jwks, **compact)}\n")  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
