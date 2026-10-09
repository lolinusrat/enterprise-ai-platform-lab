"""Permission model: a reader needs a matching group AND enough clearance for the classification."""
import json
from dataclasses import dataclass
from pathlib import Path

LEVELS = ["public", "internal", "confidential", "restricted"]
RANK = {level: i for i, level in enumerate(LEVELS)}
EVERYONE = "everyone"
USERS_FILE = Path(__file__).resolve().parent.parent / "data" / "users.json"


@dataclass(frozen=True)
class Principal:
    id: str
    name: str
    role: str
    groups: tuple[str, ...]
    clearance: str

    @property
    def effective_groups(self) -> frozenset[str]:
        return frozenset(self.groups) | {EVERYONE}


def can_read(principal: Principal, classification: str, groups) -> bool:
    return RANK[classification] <= RANK[principal.clearance] and bool(principal.effective_groups & set(groups))


def deny_reason(principal: Principal, classification: str, groups) -> str | None:
    if RANK[classification] > RANK[principal.clearance]:
        return f"clearance {principal.clearance} < {classification}"
    if not principal.effective_groups & set(groups):
        return "no matching group"
    return None


def load_users(path: Path = USERS_FILE) -> dict[str, Principal]:
    return {
        u["id"]: Principal(u["id"], u["name"], u["role"], tuple(u["groups"]), u["clearance"])
        for u in json.loads(path.read_text())
    }
