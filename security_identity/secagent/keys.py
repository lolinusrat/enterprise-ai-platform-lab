"""The authorization server's RS256 signing key. Generated per process; only the public JWKS goes to OPA."""
import json

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm


class SigningKey:
    def __init__(self, kid: str = "authz-1") -> None:
        self.kid = kid
        self._private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = json.loads(RSAAlgorithm.to_jwk(self._private.public_key()))
        self.jwks = {"keys": [{**public, "kid": kid, "alg": "RS256", "use": "sig"}]}
        self.private_jwk = {**json.loads(RSAAlgorithm.to_jwk(self._private)), "kid": kid, "alg": "RS256"}

    def sign(self, claims: dict) -> str:
        return jwt.encode(claims, self._private, algorithm="RS256", headers={"kid": self.kid})
