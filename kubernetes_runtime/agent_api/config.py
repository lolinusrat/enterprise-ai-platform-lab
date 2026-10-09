"""Runtime configuration.

Plain settings come from environment variables (a ConfigMap in the cluster). Secrets come from
files mounted from a Kubernetes Secret, re-read when the file changes, so a rotated secret is
picked up without restarting the process. Locally, the same values can come from env vars.
"""
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", "/var/run/secrets/agent"))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    service_name: str = os.environ.get("OTEL_SERVICE_NAME", "agent-api")
    version: str = os.environ.get("APP_VERSION", "dev")
    default_backend: str = os.environ.get("DEFAULT_BACKEND", "scripted")
    groq_model: str = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")
    max_agent_steps: int = _env_int("MAX_AGENT_STEPS", 4)
    # Simulated warm-up (loading tool registry, warming clients). Drives the startup probe.
    startup_delay_s: float = float(os.environ.get("STARTUP_DELAY_S", "4"))
    # PBKDF2 iterations for the risk_score tool: the CPU-bound work that makes the HPA react.
    risk_iterations: int = _env_int("RISK_ITERATIONS", 60000)
    pod_name: str = os.environ.get("POD_NAME", os.uname().nodename)
    node_name: str = os.environ.get("NODE_NAME", "local")
    namespace: str = os.environ.get("POD_NAMESPACE", "local")


settings = Settings()


class SecretFile:
    """A secret value backed by a mounted file (preferred) or an env var (local runs).

    Kubernetes updates mounted Secret volumes in place (atomic symlink swap), so caching on
    mtime lets the process pick up a rotated value on the next request.
    """

    def __init__(self, filename: str, env: str):
        self.path = SECRETS_DIR / filename
        self.env = env
        self._mtime: float | None = None
        self._value: str | None = None

    def get(self) -> str | None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return os.environ.get(self.env) or None
        if mtime != self._mtime:
            self._value = self.path.read_text().strip() or None
            self._mtime = mtime
        return self._value


API_TOKEN = SecretFile("api-token", "API_TOKEN")
# During a rotation the outgoing token stays valid, so clients can switch over without 401s
# while the kubelet syncs the new Secret into each pod at its own pace.
API_TOKEN_PREVIOUS = SecretFile("api-token-previous", "API_TOKEN_PREVIOUS")
GROQ_API_KEY = SecretFile("groq-api-key", "GROQ_API_KEY")


def fingerprint(value: str | None) -> str | None:
    """Short, non-reversible id of a secret, so rotation progress can be observed without exposing it."""
    return hashlib.sha256(value.encode()).hexdigest()[:8] if value else None
