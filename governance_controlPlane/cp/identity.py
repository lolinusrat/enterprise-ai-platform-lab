"""Token signing for the demo IdP and the identity broker, plus verification for the gateways.

Three token types, all RS256 JWTs:
- user token   (iss idp.corp.example)      stands in for the OIDC ID/access token from Okta or Entra ID
- JWT-SVID     (iss identity.corp.example)  the agent's workload identity, sub = spiffe://...
- OBO token    (iss identity.corp.example)  RFC 8693 token exchange result: sub = user, act.sub = agent
"""
import json
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

USER_TTL, SVID_TTL, OBO_TTL = 900, 300, 300


class Signer:
    def __init__(self, kid: str, iss: str):
        self.kid, self.iss = kid, iss
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwk(self) -> dict:
        d = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        return {**d, "kid": self.kid, "alg": "RS256", "use": "sig"}

    def sign(self, claims: dict, ttl: int) -> str:
        now = int(time.time())
        body = {"iss": self.iss, "iat": now, "exp": now + ttl, "jti": uuid.uuid4().hex, **claims}
        return jwt.encode(body, self.key, algorithm="RS256", headers={"kid": self.kid})


class Verifier:
    """Verifies tokens against the control plane's JWKS. Keys are cached, so verification is local."""

    def __init__(self, jwks_url: str):
        self.client = jwt.PyJWKClient(jwks_url, cache_keys=True)

    def verify(self, token: str, audience: str, issuer: str) -> dict:
        key = self.client.get_signing_key_from_jwt(token)
        return jwt.decode(token, key.key, algorithms=["RS256"], audience=audience, issuer=issuer)
