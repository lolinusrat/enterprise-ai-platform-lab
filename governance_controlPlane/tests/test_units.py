import hashlib

import pytest

from cp import pii
from cp.common import summarize, step
from cp.ledger import Ledger


def test_pii_finds_demo_entities():
    text = ("Hi, I'm Sarah Mitchell. My card 4111 1111 1111 1111 was charged twice. "
            "Call me on 0412 345 678 or email sarah.mitchell@example.com. Account 062-000 12345678.")
    kinds = [k for k, _, _ in pii.find(text)]
    assert kinds == ["person name", "card number", "phone", "email", "account number"]
    red = pii.redact(text, pii.find(text))
    assert "Sarah" not in red and "4111" not in red and "0412" not in red and "@example" not in red
    assert "[CARD_NUMBER]" in red


def test_pii_skips_non_luhn_digits_and_plain_text():
    assert pii.find("Order 1234 5678 9012 3456 shipped") == []
    assert pii.find("How many days of annual leave do I have left?") == []


def test_ledger_chain_and_tamper(tmp_path):
    led = Ledger(str(tmp_path / "l.db"))
    for i in range(5):
        led.append(kind="runtime", bu="retail", agent="retail/a", actor="u", action=f"call {i}", decision="deny" if i == 2 else "allow",
                   rule="GR-03", detail="d")
    assert led.verify()["ok"]
    led.tamper(3, "allow")
    assert led.verify() == {"ok": False, "seq": 3, "n": 5}
    led.untamper()
    assert led.verify()["ok"]


def test_ledger_is_append_only(tmp_path):
    led = Ledger(str(tmp_path / "l.db"))
    led.append(kind="k", bu="b", agent="a", actor="x", action="y", decision="allow", rule="r", detail="d")
    with pytest.raises(Exception, match="append-only"):
        led.db.execute("UPDATE events SET decision = 'deny'")
    with pytest.raises(Exception, match="append-only"):
        led.db.execute("DELETE FROM events")


def test_ledger_hash_is_sha256_of_prev_and_body(tmp_path):
    led = Ledger(str(tmp_path / "l.db"))
    e = led.append(ts="2026-10-08T09:00:00Z", kind="k", bu="b", agent="a", actor="x", action="y", decision="allow", rule="r", detail="d")
    body = '[1,"2026-10-08T09:00:00Z","k","b","a","x","y","allow","r","d",""]'
    assert e["hash"] == hashlib.sha256(("0" * 64 + "|" + body).encode()).hexdigest()


def test_summarize_precedence():
    s = [step("c", "a", "pass", "", "r1"), step("c", "b", "warn", "", "r2"), step("c", "c", "hold", "", "r3")]
    assert summarize(s)[0] == "hold"
    assert summarize(s + [step("c", "d", "deny", "", "r4")])[1]["rule"] == "r4"
    assert summarize(s[:2])[0] == "obligations"
    assert summarize(s[:1])[0] == "allow"
