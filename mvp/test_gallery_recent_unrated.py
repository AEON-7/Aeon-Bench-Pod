"""Self-test: mature Code Gallery prompts still show fresh unrated artifacts.

Runs fully offline (temp SQLite). From the mvp dir:
    python test_gallery_recent_unrated.py
"""
import os
import sys
import tempfile
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# Point the DB at a throwaway SQLite file BEFORE importing aeon (db.py reads the
# env at import time; AEON_DB_URL would select the prod Postgres backend).
_TMP = tempfile.mkdtemp(prefix="aeon-gallery-selftest-")
os.environ["AEON_DB"] = os.path.join(_TMP, "test.db")
os.environ.pop("AEON_DB_URL", None)

from aeon import arena, db  # noqa: E402

db.init_db()

GOOD_HTML = "<!DOCTYPE html><html><body><h1>ok</h1></body></html>"
PROMPT = "app.todo"


def save(aid, model):
    db.save_artifact(aid, kind="app", prompt_id=PROMPT, model=model,
                     html=GOOD_HTML, ok=True, gen_ms=10)


def mk_user():
    uid = uuid.uuid4().hex[:12]
    db.create_user(uid, username="u" + uid, pw_hash="x", pw_salt="", signup_ip="127.0.0.1")
    return uid


save("rated_a", "rated-A")
save("rated_b", "rated-B")
save("rated_c", "rated-C")

db.record_vote(uuid.uuid4().hex[:12], kind="app", prompt_id=PROMPT,
               a_id="rated_a", b_id="rated_b", a_model="rated-A", b_model="rated-B",
               winner="a", user_id=mk_user())
db.record_vote(uuid.uuid4().hex[:12], kind="app", prompt_id=PROMPT,
               a_id="rated_b", b_id="rated_c", a_model="rated-B", b_model="rated-C",
               winner="b", user_id=mk_user())

save("fresh_unrated", "fresh-model")

todo = next((p for p in arena.gallery("app") if p["id"] == PROMPT), None)
assert todo, "app.todo gallery section should exist"
fresh = [a for a in todo["artifacts"] if a["id"] == "fresh_unrated"]
assert fresh, "mature prompt should include newest unrated artifact"
assert fresh[0].get("unrated") is True, "fresh artifact should be marked unrated"

print("PASS: mature Code Gallery prompt includes fresh unrated artifact")
