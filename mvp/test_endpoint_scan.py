"""Endpoint discovery scan (pod/endpoints.py) — the 'scan for a live instance' half of easy
verified target mode. Injectable transport, no network: two ports serve, the rest are dead;
asserts the scan finds the live ones, dedups a server fronted on two ports, caps hosts, and
never raises on an unreachable port."""
import os
import sys

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

from pod import endpoints as ep  # noqa: E402

PASSED = 0


def check(cond, msg):
    global PASSED
    assert cond, "FAIL: " + msg
    PASSED += 1
    print("PASS:", msg)


# a fake LAN: :8000 serves the prod aliases, :8001 an ASR model, everything else is dead. A
# second host mirrors :8000 (same model list) to prove per-URL results, not per-server dedup.
LIVE = {
    "http://127.0.0.1:8000/v1/models": {"data": [{"id": "aeon-ultimate"}, {"id": "gemma4-26b"}]},
    "http://127.0.0.1:8001/v1/models": {"data": [{"id": "qwen3-asr"}]},
    "http://10.0.0.9:8000/v1/models": {"data": [{"id": "aeon-ultimate"}]},
}


def transport(url):
    if url in LIVE:
        return LIVE[url]
    raise ConnectionError("refused")            # dead port


# ---- localhost scan ----
r = ep.scan(transport=transport)
urls = {e["url"] for e in r["endpoints"]}
check(urls == {"http://127.0.0.1:8000/v1", "http://127.0.0.1:8001/v1"},
      "scan finds exactly the live localhost servers, ignores dead ports")
e8000 = next(e for e in r["endpoints"] if e["url"].endswith(":8000/v1"))
check(e8000["models"] == ["aeon-ultimate", "gemma4-26b"] and e8000["reachable"],
      "a found endpoint lists the model ids it actually serves")
check(r["scanned"] == len(ep.COMMON_PORTS) and set(r["hosts"]) == {"127.0.0.1"},
      "scan reports how many (host×port) targets it probed")

# ---- multi-host (declared cluster / LAN) ----
r2 = ep.scan(hosts=["127.0.0.1", "10.0.0.9"], transport=transport)
urls2 = {e["url"] for e in r2["endpoints"]}
check("http://10.0.0.9:8000/v1" in urls2, "a declared remote host is scanned too")

# ---- dedup + robustness ----
r3 = ep.scan(hosts=["127.0.0.1"], ports=[8000, 8000, 8001], transport=transport)
check(len([e for e in r3["endpoints"] if e["url"].endswith(":8000/v1")]) == 1,
      "a duplicated port is scanned once (no double-listing)")
check(ep._probe("http://127.0.0.1:9999", transport=transport) is None,
      "an unreachable endpoint returns None, never raises")
check(ep.scan(hosts=["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"], ports=[8000],
              transport=lambda u: (_ for _ in ()).throw(ConnectionError()))["hosts"].__len__()
      == ep.MAX_HOSTS, "host list is capped at MAX_HOSTS")
check(ep.scan(ports=[7777], transport=transport)["endpoints"] == [],
      "nothing serving on the probed ports -> empty, honest result")

print(f"\nOK  endpoint scan: {PASSED} checks passed")
