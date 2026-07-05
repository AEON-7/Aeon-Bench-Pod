"""SQLite storage for the MVP (stands in for Postgres in the full design).

Append-only-ish: a run row + one result row per case. Aggregates (category
scores, leaderboard) are computed on read in scoring.py.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import blobstore

# Payloads larger than this go to the content-addressed blob store (ref+hash in the DB,
# bytes out of the DB — docs/data-and-security.md §2.1). Smaller ones stay inline.
BLOB_THRESHOLD = 8192

DB_PATH = os.environ.get(
    "AEON_DB",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "aeon.db"),
)

# Backend select: AEON_DB_URL set -> PostgreSQL mothership; unset -> SQLite (pod / dev).
# The query LAYER is shared; a thin translator (below) adapts the SQLite-dialect SQL to
# Postgres so the ~50 db functions run unchanged on both. PART-A operational tables are a
# faithful SQLite mirror on PG (epoch double precision, integer flags, text json, citext
# usernames) so no per-call type coercion is needed.
AEON_DB_URL = os.environ.get("AEON_DB_URL")
IS_PG = bool(AEON_DB_URL)

_init_lock = threading.Lock()
_initialized = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    model         TEXT NOT NULL,
    target_url    TEXT NOT NULL,
    judge_model   TEXT,                 -- the model that judges Tier-1 (defaults to `model`)
    judge_is_self INTEGER DEFAULT 1,
    suite_id      TEXT,
    suite_hash    TEXT,
    bench_seed    TEXT,                 -- fast-bench A/B seed: same seed + same suite_hash => same questions
    trust_tier    TEXT DEFAULT 'self_reported',
    status        TEXT DEFAULT 'queued',-- queued|running|succeeded|failed
    progress      INTEGER DEFAULT 0,    -- cases completed
    n_cases       INTEGER DEFAULT 0,
    error         TEXT,
    params_json   TEXT,
    env_json      TEXT,
    started_at    REAL,
    finished_at   REAL
);
CREATE TABLE IF NOT EXISTS results (
    run_id        TEXT NOT NULL,
    case_id       TEXT NOT NULL,
    category      TEXT,
    tier          INTEGER,
    status        TEXT,                 -- scored|tier1_pending|killed_resource_limit|error
    score         REAL,                 -- 0..1 (correctness; the gate)
    creativity    REAL,                 -- 0..3 parallel novelty bonus (gated by correctness)
    raw_output    TEXT,
    evidence_json TEXT,                 -- per-checker / per-criterion detail
    speed_json    TEXT,                 -- ttft_ms, decode_tps, e2e_ms, output_tokens
    disputed      INTEGER DEFAULT 0,    -- agent-judge flagged a LIKELY checker false-negative (QA only; not a re-score)
    disputed_reason TEXT,
    PRIMARY KEY (run_id, case_id)
);
CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id);

-- Generated-artifact arena (DESIGN §12): same prompt -> many models -> playable
-- side-by-side artifacts, ranked by HUMAN votes. The html is untrusted model
-- output and is ONLY ever rendered in a sandboxed iframe on the client.
CREATE TABLE IF NOT EXISTS arena_artifacts (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,          -- app|game|animation
    prompt_id   TEXT NOT NULL,
    model       TEXT NOT NULL,
    html        TEXT NOT NULL,
    ok          INTEGER DEFAULT 1,
    bytes       INTEGER DEFAULT 0,
    gen_ms      REAL,
    bogus       INTEGER DEFAULT 0,      -- honeypot decoy (intentionally broken/awful)
    created_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_art_kind ON arena_artifacts(kind, prompt_id);
CREATE TABLE IF NOT EXISTS arena_votes (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    prompt_id   TEXT NOT NULL,
    a_id        TEXT NOT NULL,
    b_id        TEXT NOT NULL,
    a_model     TEXT,
    b_model     TEXT,
    winner      TEXT,                   -- a|b|tie
    user_id     TEXT,
    is_test     INTEGER DEFAULT 0,      -- was this a honeypot integrity check?
    test_passed INTEGER,                -- 1 pass / 0 fail / NULL (non-test or tie)
    ts          REAL
);

-- Evaluator accounts + sessions (anonymous: username + password only) ----------
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    pw_hash       TEXT NOT NULL,
    pw_salt       TEXT NOT NULL,
    signup_ip     TEXT,
    status        TEXT DEFAULT 'active', -- active | flagged (failed honeypot -> votes discarded)
    trusted       INTEGER DEFAULT 0,     -- passed >= 1 honeypot -> votes counted
    ever_verified INTEGER DEFAULT 0,     -- was ever trusted (sticky; drives the public badge so a
                                         -- later flag doesn't visibly flip 'verified'->'verifying')
    created_at    REAL
);
CREATE TABLE IF NOT EXISTS arena_sessions (
    token       TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    created_at  REAL
);
-- A served comparison awaiting the user's vote (server owns the pairing + secret).
CREATE TABLE IF NOT EXISTS arena_matches (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    prompt_id   TEXT NOT NULL,
    a_id        TEXT NOT NULL,
    b_id        TEXT NOT NULL,
    is_test     INTEGER DEFAULT 0,
    bogus_side  TEXT,                   -- 'a' | 'b' | NULL (which side is the decoy)
    served_at   REAL,
    voted       INTEGER DEFAULT 0
);

-- Pod submission channel (trust-chain P0): enrolled device keys + signed run bundles.
CREATE TABLE IF NOT EXISTS enrolled_keys (
    id            TEXT PRIMARY KEY,
    public_key    TEXT UNIQUE NOT NULL,  -- base64 ed25519
    fingerprint   TEXT UNIQUE NOT NULL,  -- sha256(public_key) short
    owner_user_id TEXT,
    status        TEXT DEFAULT 'active', -- active | revoked
    fail_count    INTEGER DEFAULT 0,
    created_at    REAL,
    revoked_at    REAL
);
CREATE TABLE IF NOT EXISTS pod_submissions (
    run_id       TEXT PRIMARY KEY,
    public_key   TEXT NOT NULL,
    run_nonce    TEXT NOT NULL,         -- single-use; consumed on first results submit
    run_token    TEXT NOT NULL,         -- run-scoped bearer for the results POST
    model        TEXT,
    suite_id     TEXT,
    board        TEXT DEFAULT 'text',
    status       TEXT DEFAULT 'open',   -- open | committed | quarantined
    reason       TEXT,                  -- reject/quarantine reason code on failure
    created_at   REAL,
    committed_at REAL
);

-- Shared (multi-replica-safe) challenge + rate-limit state, replacing in-process dicts.
CREATE TABLE IF NOT EXISTS enroll_challenges (
    challenge   TEXT PRIMARY KEY,
    expires_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_events (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    k    TEXT NOT NULL,                 -- bucket key, e.g. 'signup:<ip>' / 'fail:u:<name>'
    ts   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_events_k_ts ON rate_events(k, ts);

-- Pod-LOCAL secrets: target-endpoint API keys + HuggingFace tokens the operator saves in their
-- own lab. POD ONLY by construction: every pod entrypoint pops AEON_DB_URL and targets pod.db,
-- so this table can never live on / reach the mothership. Plaintext-in-DB is acceptable for a
-- single-operator box IF pod.db is 0600 — the value is MASKED on the wire and NEVER returned.
CREATE TABLE IF NOT EXISTS pod_secrets (
    name        TEXT PRIMARY KEY,
    kind        TEXT DEFAULT 'api_key',   -- api_key | hf_token
    value       TEXT NOT NULL,
    created_at  REAL,
    updated_at  REAL
);
"""


# ---- Postgres dialect adapter (SQLite-dialect SQL -> Postgres) --------------

class _Row(dict):
    """A result row that supports BOTH r["col"] (like sqlite3.Row / dict) and r[0]
    (positional), so the existing db code — which mixes both — works unchanged on PG."""
    def __init__(self, cols, vals):
        super().__init__(zip(cols, vals))
        self._vals = list(vals)

    def __getitem__(self, k):
        if isinstance(k, int):
            return self._vals[k]
        return dict.__getitem__(self, k)


def _pg_row_factory(cursor):
    cols = [c.name for c in (cursor.description or [])]
    def make(values):
        return _Row(cols, values)
    return make


import re as _re

_OR_REPLACE_RE = _re.compile(r"INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]*)\)", _re.I)
_OR_IGNORE_RE = _re.compile(r"INSERT\s+OR\s+IGNORE\s+INTO", _re.I)
# conflict targets for the handful of SQLite upserts (verified via grep, db.py)
_UPSERT_PK = {"results": ["run_id", "case_id"],
              "arena_artifacts": ["id"],
              "enroll_challenges": ["challenge"]}


def _translate(sql: str) -> str:
    """SQLite-dialect SQL -> Postgres. Narrow + verified: strip COLLATE NOCASE (citext
    columns are case-insensitive), rewrite the 4 OR REPLACE / OR IGNORE upserts to
    ON CONFLICT, and convert ? placeholders to %s. No % literals exist in our SQL."""
    s = _re.sub(r"\s+COLLATE\s+NOCASE", "", sql, flags=_re.I)
    s = _re.sub(r"\browid\b", "ctid", s)        # SQLite implicit rowid -> PG physical ctid
    m = _OR_REPLACE_RE.search(s)
    if m:
        table = m.group(1).lower()
        cols = [c.strip() for c in m.group(2).split(",")]
        pk = _UPSERT_PK.get(table, [])
        s = _re.sub(r"INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=_re.I)
        if pk:
            sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in pk)
            s = s.rstrip().rstrip(";") + f" ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {sets}"
    elif _OR_IGNORE_RE.search(s):
        s = _OR_IGNORE_RE.sub("INSERT INTO", s).rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return s.replace("?", "%s")


def _to_pg_ddl(ddl: str) -> str:
    """Translate db.py's SQLite CREATE-TABLE schema to a faithful Postgres mirror:
    REAL->double precision, AUTOINCREMENT->bigserial, username->citext (case-insensitive).
    Strips `--` line comments first (some contain ';' which would break statement splitting)."""
    s = _re.sub(r"--[^\n]*", "", ddl)
    s = _re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "BIGSERIAL PRIMARY KEY", s, flags=_re.I)
    s = _re.sub(r"\bREAL\b", "DOUBLE PRECISION", s)
    s = _re.sub(r"\busername(\s+)TEXT\b", "username CITEXT", s)
    return "CREATE EXTENSION IF NOT EXISTS citext;\n" + s


class _PgConn:
    """Wraps a psycopg connection so .execute() translates SQLite-dialect SQL first and
    returns the psycopg cursor (fetchone/fetchall/rowcount/iteration all behave)."""
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        return self._conn.execute(_translate(sql), params)

    def executescript(self, script):       # only used by SQLite init_db; never on PG
        raise RuntimeError("executescript is not used on the Postgres backend")


def _pg_connect():
    import psycopg
    return psycopg.connect(AEON_DB_URL, row_factory=_pg_row_factory,
                           autocommit=False, connect_timeout=10)


@contextmanager
def connect():
    if IS_PG:
        conn = _pg_connect()
        try:
            yield _PgConn(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
            conn.commit()
        finally:
            conn.close()


def _ensure_columns(c):
    """Additive migrations (SQLite has no ADD COLUMN IF NOT EXISTS)."""
    runs_cols = {r["name"] for r in c.execute("PRAGMA table_info(runs)")}
    if "board" not in runs_cols:
        c.execute("ALTER TABLE runs ADD COLUMN board TEXT DEFAULT 'text'")
    if "vision_probe_json" not in runs_cols:
        c.execute("ALTER TABLE runs ADD COLUMN vision_probe_json TEXT")
    if "flagged" not in runs_cols:
        c.execute("ALTER TABLE runs ADD COLUMN flagged INTEGER DEFAULT 0")
    if "flag_reason" not in runs_cols:
        c.execute("ALTER TABLE runs ADD COLUMN flag_reason TEXT")
    # model identity: canonical id groups runs across local aliases (HF repo when known)
    # + the agentic harness and its EXACT release version (disclosed in the report)
    for col, ddl in (("hf_repo", "TEXT"), ("hf_revision", "TEXT"),
                     ("model_verified", "TEXT"), ("canonical_id", "TEXT"),
                     ("harness", "TEXT"), ("harness_version", "TEXT"),
                     # controlled HF-pull flow evidence (what makes a run globally eligible):
                     # the pod-computed weights hash, the serve recipe, and the deployment
                     # manifest (build hash + verification). Present only on attested runs.
                     ("weights_hash", "TEXT"), ("recipe", "TEXT"),
                     ("deployment_manifest", "TEXT"), ("bench_seed", "TEXT")):
        if col not in runs_cols:
            c.execute(f"ALTER TABLE runs ADD COLUMN {col} {ddl}")
    res_cols = {r["name"] for r in c.execute("PRAGMA table_info(results)")}
    if "board" not in res_cols:
        c.execute("ALTER TABLE results ADD COLUMN board TEXT DEFAULT 'text'")
    if "creativity" not in res_cols:
        c.execute("ALTER TABLE results ADD COLUMN creativity REAL")
    if "raw_output_ref" not in res_cols:        # large payloads -> blob store (ref+hash)
        c.execute("ALTER TABLE results ADD COLUMN raw_output_ref TEXT")
    if "raw_output_hash" not in res_cols:
        c.execute("ALTER TABLE results ADD COLUMN raw_output_hash TEXT")
    if "disputed" not in res_cols:
        c.execute("ALTER TABLE results ADD COLUMN disputed INTEGER DEFAULT 0")
    if "disputed_reason" not in res_cols:
        c.execute("ALTER TABLE results ADD COLUMN disputed_reason TEXT")
    # arena additive migrations (tables may predate the honeypot/accounts work)
    art_cols = {r["name"] for r in c.execute("PRAGMA table_info(arena_artifacts)")}
    if art_cols and "bogus" not in art_cols:
        c.execute("ALTER TABLE arena_artifacts ADD COLUMN bogus INTEGER DEFAULT 0")
    vote_cols = {r["name"] for r in c.execute("PRAGMA table_info(arena_votes)")}
    if vote_cols:
        if "user_id" not in vote_cols:
            c.execute("ALTER TABLE arena_votes ADD COLUMN user_id TEXT")
        if "is_test" not in vote_cols:
            c.execute("ALTER TABLE arena_votes ADD COLUMN is_test INTEGER DEFAULT 0")
        if "test_passed" not in vote_cols:
            c.execute("ALTER TABLE arena_votes ADD COLUMN test_passed INTEGER")
    user_cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
    if user_cols and "ever_verified" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN ever_verified INTEGER DEFAULT 0")


def _ensure_columns_pg(c):
    """Postgres mirror of _ensure_columns (PG supports ADD COLUMN IF NOT EXISTS)."""
    adds = [
        ("runs", "board", "TEXT DEFAULT 'text'"), ("runs", "vision_probe_json", "TEXT"),
        ("runs", "flagged", "INTEGER DEFAULT 0"), ("runs", "flag_reason", "TEXT"),
        ("runs", "hf_repo", "TEXT"), ("runs", "hf_revision", "TEXT"),
        ("runs", "model_verified", "TEXT"), ("runs", "canonical_id", "TEXT"),
        ("runs", "harness", "TEXT"), ("runs", "harness_version", "TEXT"),
        ("runs", "weights_hash", "TEXT"), ("runs", "recipe", "TEXT"),
        ("runs", "deployment_manifest", "TEXT"), ("runs", "bench_seed", "TEXT"),
        ("results", "board", "TEXT DEFAULT 'text'"), ("results", "creativity", "DOUBLE PRECISION"),
        ("results", "raw_output_ref", "TEXT"), ("results", "raw_output_hash", "TEXT"),
        ("results", "disputed", "INTEGER DEFAULT 0"), ("results", "disputed_reason", "TEXT"),
        ("arena_artifacts", "bogus", "INTEGER DEFAULT 0"),
        ("arena_votes", "user_id", "TEXT"), ("arena_votes", "is_test", "INTEGER DEFAULT 0"),
        ("arena_votes", "test_passed", "INTEGER"),
        ("users", "ever_verified", "INTEGER DEFAULT 0"),
    ]
    for t, col, typ in adds:
        c.execute(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {col} {typ}")


def init_db():
    global _initialized
    with _init_lock:
        if _initialized:
            return
        if IS_PG:
            # The app self-provisions its operational tables on PG (a faithful mirror of
            # the SQLite schema). A transaction-scoped advisory lock serializes concurrent
            # workers — `CREATE ... IF NOT EXISTS` is NOT race-safe across processes, so two
            # workers booting at once would otherwise collide on pg_extension/pg_class.
            with connect() as c:
                c.execute("SELECT pg_advisory_xact_lock(?)", (0x4145_4F4E,))   # 'AEON'
                for stmt in _to_pg_ddl(SCHEMA).split(";"):
                    if stmt.strip():
                        c.execute(stmt)
                _ensure_columns_pg(c)
            _initialized = True
            return
        with connect() as c:
            c.executescript(SCHEMA)
            _ensure_columns(c)
        _initialized = True


def canonical_model_id(model, hf_repo=None):
    """The identity runs are GROUPED by on the board, so the same model under different
    local aliases lines up: the HF repo (lowercased) when known, else the declared name."""
    if hf_repo:
        return hf_repo.strip().lower()
    return (model or "").strip()


def create_run(run_id, *, model, target_url, judge_model, judge_is_self,
               suite_id, suite_hash, n_cases, params, env,
               board="text", vision_probe_json=None,
               hf_repo=None, hf_revision=None, model_verified=None, canonical_id=None,
               harness=None, harness_version=None,
               trust_tier="self_reported", weights_hash=None, recipe=None,
               deployment_manifest=None, bench_seed=None):
    init_db()
    canonical_id = canonical_id or canonical_model_id(model, hf_repo)
    model_verified = model_verified or ("claim" if hf_repo else "declared")
    recipe_json = json.dumps(recipe) if recipe is not None else None
    dm_json = json.dumps(deployment_manifest) if deployment_manifest is not None else None
    with connect() as c:
        c.execute(
            """INSERT INTO runs (id, model, target_url, judge_model, judge_is_self,
                 suite_id, suite_hash, board, vision_probe_json, status, n_cases,
                 params_json, env_json, started_at,
                 hf_repo, hf_revision, model_verified, canonical_id, harness, harness_version,
                 trust_tier, weights_hash, recipe, deployment_manifest, bench_seed)
               VALUES (?,?,?,?,?,?,?,?,?, 'running', ?, ?, ?, ?, ?,?,?,?,?,?, ?,?,?,?,?)""",
            (run_id, model, target_url, judge_model, 1 if judge_is_self else 0,
             suite_id, suite_hash, board, vision_probe_json, n_cases,
             json.dumps(params), json.dumps(env), time.time(),
             hf_repo, hf_revision, model_verified, canonical_id, harness, harness_version,
             trust_tier, weights_hash, recipe_json, dm_json, bench_seed),
        )


def save_result(run_id, case_id, *, category, tier, status, score, raw_output, evidence, speed,
                board="text", creativity=None):
    # Large raw_output goes to the content-addressed blob store (bytes out of the DB);
    # small outputs stay inline. Either way record a sha256 for integrity. Write the blob
    # BEFORE the row commits (durability precedes the ref — §2.1).
    text = raw_output or ""
    out_hash = blobstore.hash_text(text) if text else None
    blobbed = bool(text) and len(text.encode("utf-8")) > BLOB_THRESHOLD
    out_ref = blobstore.put_text(text) if blobbed else None
    inline = "" if blobbed else text
    with connect() as c:
        c.execute(
            """INSERT OR REPLACE INTO results
                 (run_id, case_id, category, tier, board, status, score, creativity,
                  raw_output, raw_output_ref, raw_output_hash, evidence_json, speed_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, case_id, category, tier, board, status, score, creativity,
             inline, out_ref, out_hash, json.dumps(evidence), json.dumps(speed)),
        )
        c.execute("UPDATE runs SET progress = progress + 1 WHERE id = ?", (run_id,))


def result_output(row):
    """The raw_output text of a result row, whether stored inline (small) or in the blob
    store (large). Accepts a dict-like result row carrying raw_output[_ref]."""
    ref = row.get("raw_output_ref") if hasattr(row, "get") else None
    if ref:
        return blobstore.get_text(ref)
    return (row.get("raw_output") if hasattr(row, "get") else None) or ""


def result_case_ids(run_id):
    """The case_ids already stored for a run — lets incremental submission append only NEW
    cases so a re-sent cumulative checkpoint never double-counts progress or re-blobs output."""
    with connect() as c:
        return {r[0] for r in c.execute("SELECT case_id FROM results WHERE run_id=?", (run_id,))}


def update_result(run_id, case_id, *, status, score, evidence, creativity=None):
    """Finalize a previously-pending result (e.g. agent Tier-1 verdict submission)."""
    with connect() as c:
        c.execute(
            "UPDATE results SET status=?, score=?, evidence_json=?, creativity=? WHERE run_id=? AND case_id=?",
            (status, score, json.dumps(evidence), creativity, run_id, case_id),
        )


def flag_disputed(run_id, case_id, disputed=True, reason=None):
    """Mark a result as agent-judge DISPUTED — a LIKELY checker false-negative. Does NOT change the
    deterministic score; a QA flag surfaced for checker review + re-score (determinism stays the ranker)."""
    with connect() as c:
        c.execute("UPDATE results SET disputed=?, disputed_reason=? WHERE run_id=? AND case_id=?",
                  (1 if disputed else 0, reason, run_id, case_id))


def disputed_cases(limit=200):
    """Currently-disputed results (agent-judge-flagged likely checker false-negatives) for review."""
    with connect() as c:
        rows = c.execute(
            "SELECT r.run_id, r.case_id, r.category, r.score, r.disputed_reason, ru.model "
            "FROM results r JOIN runs ru ON ru.id=r.run_id WHERE COALESCE(r.disputed,0)=1 "
            "ORDER BY ru.started_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(x) for x in rows]


def finish_run(run_id, status, error=None):
    with connect() as c:
        c.execute("UPDATE runs SET status=?, error=?, finished_at=? WHERE id=?",
                  (status, error, time.time(), run_id))


def get_run(run_id):
    with connect() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not r:
            return None
        run = dict(r)
        rows = c.execute(
            "SELECT * FROM results WHERE run_id=? ORDER BY rowid", (run_id,)
        ).fetchall()
        run["results"] = [_result_row(x) for x in rows]
        return run


def list_runs(limit=100):
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(x) for x in rows]


def list_submissions(board=None, model=None, limit=300):
    """Run rows for the transparency browser (newest first), optionally per model."""
    cols = ("id, model, board, status, started_at, finished_at, n_cases, judge_model, "
            "judge_is_self, COALESCE(flagged,0) AS flagged, flag_reason, suite_id, suite_hash, "
            "trust_tier, bench_seed")
    q = f"SELECT {cols} FROM runs WHERE 1=1"
    args = []
    if board:
        q += " AND board=?"; args.append(board)
    if model:
        q += " AND model=?"; args.append(model)
    q += " ORDER BY started_at DESC LIMIT ?"; args.append(limit)
    with connect() as c:
        return [dict(x) for x in c.execute(q, args).fetchall()]


def run_mean_scores():
    """{run_id: mean(score)} over scored cases — a cheap summary for the list view."""
    with connect() as c:
        return {row[0]: row[1] for row in c.execute(
            "SELECT run_id, AVG(score) FROM results WHERE score IS NOT NULL GROUP BY run_id")}


def flag_run(run_id, flagged, reason=None):
    with connect() as c:
        c.execute("UPDATE runs SET flagged=?, flag_reason=? WHERE id=?",
                  (1 if flagged else 0, reason, run_id))


def reset_tier1_pending(run_id):
    """Admin re-judge: send a run's Tier-1 results back to pending so they can be
    re-judged (via the agent/MCP verdict flow). Returns how many were reset."""
    with connect() as c:
        cur = c.execute(
            "UPDATE results SET status='tier1_pending', score=NULL, creativity=NULL, "
            "evidence_json=? WHERE run_id=? AND tier=1",
            (json.dumps({"tier": 1, "pending": True, "rejudge": True}), run_id))
        return cur.rowcount


def all_results_with_runs(board="text"):
    """Flat join used by a board's leaderboard."""
    with connect() as c:
        rows = c.execute(
            """SELECT r.run_id, r.case_id, r.category, r.tier, r.status, r.score, r.creativity, r.speed_json,
                      ru.model, ru.id AS run, ru.started_at, ru.status AS run_status,
                      ru.hf_repo, ru.model_verified, ru.harness, ru.harness_version,
                      COALESCE(ru.trust_tier, 'self_reported') AS trust_tier, ru.bench_seed, ru.suite_hash,
                      ru.suite_id, COALESCE(ru.canonical_id, ru.model) AS canonical_id
                 FROM results r JOIN runs ru ON ru.id = r.run_id
                WHERE ru.status = 'succeeded' AND COALESCE(ru.flagged,0) = 0 AND r.board = ?""",
            (board,),
        ).fetchall()
        out = []
        for x in rows:
            d = dict(x)
            d["speed"] = json.loads(d.pop("speed_json") or "{}")
            out.append(d)
        return out


def perf_results():
    """Flat join for the PERFORMANCE board. Perf cells keep their metrics in evidence_json
    (TTFT/TPOT/tok-s per scope × concurrency), so this variant selects it too."""
    with connect() as c:
        rows = c.execute(
            """SELECT r.run_id, r.case_id, r.status, r.evidence_json,
                      ru.model, ru.id AS run, ru.started_at,
                      ru.hf_repo, ru.model_verified,
                      COALESCE(ru.trust_tier, 'self_reported') AS trust_tier, ru.suite_id,
                      COALESCE(ru.canonical_id, ru.model) AS canonical_id
                 FROM results r JOIN runs ru ON ru.id = r.run_id
                WHERE ru.status = 'succeeded' AND COALESCE(ru.flagged,0) = 0 AND r.board = 'perf'"""
        ).fetchall()
        out = []
        for x in rows:
            d = dict(x)
            try:
                d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
            except Exception:
                d["evidence"] = {}
            out.append(d)
        return out


# ---- arena artifacts ----

def save_artifact(aid, *, kind, prompt_id, model, html, ok=True, gen_ms=None, bogus=False):
    init_db()
    with connect() as c:
        c.execute(
            """INSERT OR REPLACE INTO arena_artifacts
                 (id, kind, prompt_id, model, html, ok, bytes, gen_ms, bogus, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (aid, kind, prompt_id, model, html, 1 if ok else 0,
             len(html or ""), gen_ms, 1 if bogus else 0, time.time()),
        )


def list_artifacts(kind=None, prompt_id=None, with_html=False, include_bogus=False):
    cols = "id, kind, prompt_id, model, ok, bytes, gen_ms, bogus, created_at" + (", html" if with_html else "")
    q = f"SELECT {cols} FROM arena_artifacts WHERE 1=1"
    args = []
    if not include_bogus:
        q += " AND bogus=0"
    if kind:
        q += " AND kind=?"; args.append(kind)
    if prompt_id:
        q += " AND prompt_id=?"; args.append(prompt_id)
    q += " ORDER BY created_at DESC"
    with connect() as c:
        return [dict(x) for x in c.execute(q, args).fetchall()]


def list_bogus(kind=None):
    """Every bogus row for a kind — BOTH the seeded base templates (prompt_id="_bogus")
    and the per-match mutated decoys (prompt_id="_bogus_live"). Callers that want only the
    fixed base pool (e.g. arena._build_test_match) must filter on prompt_id themselves."""
    q = "SELECT id, kind, prompt_id, model FROM arena_artifacts WHERE bogus=1"
    args = []
    if kind:
        q += " AND kind=?"; args.append(kind)
    with connect() as c:
        return [dict(x) for x in c.execute(q, args).fetchall()]


def get_artifact(aid):
    with connect() as c:
        r = c.execute("SELECT * FROM arena_artifacts WHERE id=?", (aid,)).fetchone()
        return dict(r) if r else None


def artifact_exists(model=None, bogus=None):
    q, args = "SELECT 1 FROM arena_artifacts WHERE 1=1", []
    if model is not None:
        q += " AND model=?"; args.append(model)
    if bogus is not None:
        q += " AND bogus=?"; args.append(1 if bogus else 0)
    q += " LIMIT 1"
    with connect() as c:
        return bool(c.execute(q, args).fetchone())


# Per-match honeypot decoys are stored as arena_artifacts rows (prompt_id="_bogus_live",
# bogus=1) — one is minted per honeypot served (arena._build_test_match) so the served HTML
# is byte-unique. Left alone they grow one small row per integrity check forever on a busy
# public arena. These two helpers bound that: count to trigger a prune, prune to reclaim the
# decoys that are safe to drop. The seeded BASE templates (prompt_id="_bogus") — the fixed
# pool the live decoys are mutated from — are NEVER touched, so list_bogus base selection is
# unaffected.

def count_live_decoys(kind=None):
    """How many per-match decoys ("_bogus_live") are currently stored (base templates
    excluded). A cheap trigger for opportunistic pruning."""
    q = "SELECT COUNT(*) FROM arena_artifacts WHERE bogus=1 AND prompt_id='_bogus_live'"
    args = []
    if kind:
        q += " AND kind=?"; args.append(kind)
    with connect() as c:
        return c.execute(q, args).fetchone()[0]


def prune_live_decoys(kind=None, ttl=None, now=None):
    """Delete per-match honeypot decoys ("_bogus_live") that are safe to reclaim: their
    match has RESOLVED (voted=1, whether it was voted on or retired by a skip), or — when a
    `ttl` in seconds is given — the decoy has aged past it (a backstop that also reclaims
    orphans whose match row was cancelled/deleted). NEVER deletes a decoy still referenced
    by a PENDING (unvoted) match — that match can still be rendered and voted — and NEVER
    touches the seeded base templates (prompt_id="_bogus"). Returns the rows deleted."""
    now = time.time() if now is None else now
    conds = ["id IN (SELECT a_id FROM arena_matches WHERE voted=1 "
             "       UNION SELECT b_id FROM arena_matches WHERE voted=1)"]
    args = []
    if ttl is not None:
        conds.append("created_at < ?"); args.append(now - ttl)
    q = ("DELETE FROM arena_artifacts "
         "WHERE bogus=1 AND prompt_id='_bogus_live' "
         "  AND id NOT IN (SELECT a_id FROM arena_matches WHERE voted=0 "
         "                 UNION SELECT b_id FROM arena_matches WHERE voted=0) "
         "  AND (" + " OR ".join(conds) + ")")
    if kind:
        q += " AND kind=?"; args.append(kind)
    with connect() as c:
        return c.execute(q, args).rowcount


# ---- arena votes ----

def record_vote(vid, *, kind, prompt_id, a_id, b_id, a_model, b_model, winner,
                user_id=None, is_test=False, test_passed=None):
    init_db()
    with connect() as c:
        c.execute(
            """INSERT INTO arena_votes
                 (id, kind, prompt_id, a_id, b_id, a_model, b_model, winner,
                  user_id, is_test, test_passed, ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (vid, kind, prompt_id, a_id, b_id, a_model, b_model, winner,
             user_id, 1 if is_test else 0, test_passed, time.time()),
        )


def real_votes(kind=None):
    """All real (non-test) votes from non-banned users, in time order. Eligibility by
    honeypot accuracy is applied in arena.ranking: a user's votes count only while
    their accuracy stays at/above the trust threshold, and a failed honeypot is NOT a
    permanent ban — they can lose and regain standing (redemption)."""
    q = ("SELECT v.* FROM arena_votes v JOIN users u ON u.id = v.user_id "
         "WHERE v.is_test = 0 AND u.status = 'active'")
    args = []
    if kind:
        q += " AND v.kind = ?"; args.append(kind)
    q += " ORDER BY v.ts"
    with connect() as c:
        return [dict(x) for x in c.execute(q, args).fetchall()]


def honeypot_accuracy():
    """{user_id: {passed, failed, adjudicated, accuracy}} over each user's ADJUDICATED
    honeypots (ties excluded). accuracy is None when nothing has been adjudicated yet."""
    with connect() as c:
        rows = c.execute(
            "SELECT user_id, "
            "SUM(CASE WHEN test_passed=1 THEN 1 ELSE 0 END) AS p, "
            "SUM(CASE WHEN test_passed=0 THEN 1 ELSE 0 END) AS f "
            "FROM arena_votes "
            "WHERE is_test=1 AND test_passed IS NOT NULL AND user_id IS NOT NULL "
            "GROUP BY user_id"
        ).fetchall()
    out = {}
    for r in rows:
        p, f = r["p"] or 0, r["f"] or 0
        adj = p + f
        out[r["user_id"]] = {"passed": p, "failed": f, "adjudicated": adj,
                             "accuracy": (p / adj) if adj else None}
    return out


# ---- evaluator accounts / sessions ----

def create_user(uid, *, username, pw_hash, pw_salt, signup_ip):
    init_db()
    with connect() as c:
        c.execute(
            """INSERT INTO users (id, username, pw_hash, pw_salt, signup_ip, status, trusted, created_at)
               VALUES (?,?,?,?,?, 'active', 0, ?)""",
            (uid, username, pw_hash, pw_salt, signup_ip, time.time()),
        )


def update_user_password(uid, *, pw_hash, pw_salt):
    with connect() as c:
        c.execute("UPDATE users SET pw_hash=?, pw_salt=? WHERE id=?", (pw_hash, pw_salt, uid))


def get_user(uid):
    with connect() as c:
        r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(r) if r else None


def get_user_by_username(username):
    # Usernames are case-INSENSITIVE for lookup + uniqueness (display case is preserved
    # as signed up). Passwords remain case-sensitive. COLLATE NOCASE is ASCII-only, which
    # matches the username charset [A-Za-z0-9_.-].
    with connect() as c:
        r = c.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
        return dict(r) if r else None


def count_users_by_ip(ip):
    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM users WHERE signup_ip=?", (ip,)).fetchone()[0]


def create_user_if_capped(uid, *, username, pw_hash, pw_salt, signup_ip, cap):
    """Transactional signup — replaces the in-process _signup_lock so the per-IP cap and
    username-uniqueness hold across replicas (the check + insert run in one transaction).
    Returns 'ok' | 'taken' | 'capped'."""
    init_db()
    with connect() as c:
        if c.execute("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone():
            return "taken"
        n = c.execute("SELECT COUNT(*) FROM users WHERE signup_ip = ?", (signup_ip,)).fetchone()[0]
        if n >= cap:
            return "capped"
        c.execute(
            """INSERT INTO users (id, username, pw_hash, pw_salt, signup_ip, status, trusted, created_at)
               VALUES (?,?,?,?,?, 'active', 0, ?)""",
            (uid, username, pw_hash, pw_salt, signup_ip, time.time()))
        return "ok"


# ---- shared challenge + rate state (multi-replica-safe; replaces in-process dicts) ----

def issue_challenge(challenge, expires_at):
    init_db()
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO enroll_challenges (challenge, expires_at) VALUES (?,?)",
                  (challenge, expires_at))


def consume_challenge(challenge, now):
    """Atomic single-use consume: True iff the challenge existed AND was unexpired."""
    with connect() as c:
        cur = c.execute("DELETE FROM enroll_challenges WHERE challenge=? AND expires_at>=?",
                        (challenge, now))
        return cur.rowcount == 1


def purge_challenges(now):
    with connect() as c:
        c.execute("DELETE FROM enroll_challenges WHERE expires_at < ?", (now,))


def rate_hit(key, limit, window, now):
    """Sliding-window limiter in one transaction: prune old events for `key`, count the
    rest, and (only if under `limit`) record this event. True = allowed. Atomic so two
    concurrent callers can't both slip past the limit."""
    init_db()
    with connect() as c:
        c.execute("DELETE FROM rate_events WHERE k=? AND ts < ?", (key, now - window))
        n = c.execute("SELECT COUNT(*) FROM rate_events WHERE k=?", (key,)).fetchone()[0]
        if n >= limit:
            return False
        c.execute("INSERT INTO rate_events (k, ts) VALUES (?,?)", (key, now))
        return True


def rate_record(key, now):
    """Record an event (e.g. a login failure) without a limit check."""
    init_db()
    with connect() as c:
        c.execute("INSERT INTO rate_events (k, ts) VALUES (?,?)", (key, now))


def rate_count(key, window, now):
    """Prune + count events for `key` in the last `window` seconds."""
    with connect() as c:
        c.execute("DELETE FROM rate_events WHERE k=? AND ts < ?", (key, now - window))
        return c.execute("SELECT COUNT(*) FROM rate_events WHERE k=?", (key,)).fetchone()[0]


def purge_rate_events(now, max_age=86400):
    with connect() as c:
        c.execute("DELETE FROM rate_events WHERE ts < ?", (now - max_age,))


def all_users():
    with connect() as c:
        return [dict(x) for x in c.execute(
            "SELECT id, username, status, ever_verified, signup_ip, created_at "
            "FROM users ORDER BY created_at"
        ).fetchall()]


def delete_artifact(aid):
    """Admin moderation: remove an artifact and cancel any unvoted matches using it."""
    with connect() as c:
        c.execute("DELETE FROM arena_artifacts WHERE id=?", (aid,))
        c.execute("DELETE FROM arena_matches WHERE voted=0 AND (a_id=? OR b_id=?)", (aid, aid))


def set_user_flags(uid, *, status=None, trusted=None, ever_verified=None):
    sets, args = [], []
    if status is not None:
        sets.append("status=?"); args.append(status)
    if trusted is not None:
        sets.append("trusted=?"); args.append(1 if trusted else 0)
    if ever_verified is not None:
        sets.append("ever_verified=?"); args.append(1 if ever_verified else 0)
    if not sets:
        return
    args.append(uid)
    with connect() as c:
        c.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", args)


def user_recent_matches(user_id, kind, limit=60):
    """The user's recently SERVED normal matches (voted or skipped), newest first —
    the anti-repeat memory for match building. Honeypots excluded (they're forced)."""
    with connect() as c:
        rows = c.execute(
            "SELECT prompt_id, a_id, b_id FROM arena_matches "
            "WHERE user_id=? AND kind=? AND is_test=0 "
            "ORDER BY served_at DESC LIMIT ?",
            (user_id, kind, int(limit))).fetchall()
    return [dict(r) for r in rows]


def admin_vote_history(user_id, limit=120):
    """Admin oversight: one evaluator's full vote trail, newest first — every vote
    including honeypot integrity checks (is_test/test_passed), so an admin can see
    exactly WHY an account's trust score is what it is."""
    with connect() as c:
        rows = c.execute(
            "SELECT id, kind, prompt_id, a_model, b_model, winner, is_test, test_passed, ts "
            "FROM arena_votes WHERE user_id=? ORDER BY ts DESC LIMIT ?",
            (user_id, int(limit))).fetchall()
    return [dict(r) for r in rows]


def create_session(token, user_id):
    with connect() as c:
        c.execute("INSERT INTO arena_sessions (token, user_id, created_at) VALUES (?,?,?)",
                  (token, user_id, time.time()))


SESSION_TTL = 30 * 86400   # bearer tokens expire after 30 days (absolute)


def user_for_token(token):
    if not token:
        return None
    with connect() as c:
        r = c.execute(
            "SELECT u.*, s.created_at AS _sess_at FROM arena_sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token=?",
            (token,),
        ).fetchone()
        if not r:
            return None
        d = dict(r)
        if (time.time() - (d.pop("_sess_at") or 0)) > SESSION_TTL:
            c.execute("DELETE FROM arena_sessions WHERE token=?", (token,))
            return None
        return d


def delete_session(token):
    with connect() as c:
        c.execute("DELETE FROM arena_sessions WHERE token=?", (token,))


def delete_user_sessions(user_id, except_token=None):
    """Invalidate all of a user's bearer sessions (e.g. on a password change, so other devices are
    logged out). If except_token is given, that one session survives so the caller stays signed in."""
    with connect() as c:
        if except_token:
            c.execute("DELETE FROM arena_sessions WHERE user_id=? AND token<>?", (user_id, except_token))
        else:
            c.execute("DELETE FROM arena_sessions WHERE user_id=?", (user_id,))


def user_stats(uid):
    with connect() as c:
        def one(sql):
            return c.execute(sql, (uid,)).fetchone()[0]
        return {
            "votes": one("SELECT COUNT(*) FROM arena_votes WHERE user_id=?"),
            "real_votes": one("SELECT COUNT(*) FROM arena_votes WHERE user_id=? AND is_test=0"),
            "tests": one("SELECT COUNT(*) FROM arena_votes WHERE user_id=? AND is_test=1"),
            "passed": one("SELECT COUNT(*) FROM arena_votes WHERE user_id=? AND is_test=1 AND test_passed=1"),
            "failed": one("SELECT COUNT(*) FROM arena_votes WHERE user_id=? AND is_test=1 AND test_passed=0"),
        }


# ---- arena matches (server-owned pending comparisons) ----

def create_match(mid, *, user_id, kind, prompt_id, a_id, b_id, is_test, bogus_side):
    init_db()
    with connect() as c:
        c.execute(
            """INSERT INTO arena_matches
                 (id, user_id, kind, prompt_id, a_id, b_id, is_test, bogus_side, served_at, voted)
               VALUES (?,?,?,?,?,?,?,?,?,0)""",
            (mid, user_id, kind, prompt_id, a_id, b_id, 1 if is_test else 0, bogus_side, time.time()),
        )


def get_match(mid):
    with connect() as c:
        r = c.execute("SELECT * FROM arena_matches WHERE id=?", (mid,)).fetchone()
        return dict(r) if r else None


def claim_match(mid):
    """Atomically claim a match for voting. Returns True iff THIS call flipped it
    from unvoted->voted (so exactly one of N racing votes on the same match wins)."""
    with connect() as c:
        cur = c.execute("UPDATE arena_matches SET voted=1 WHERE id=? AND voted=0", (mid,))
        return cur.rowcount == 1


def latest_unvoted_match(user_id, kind, is_test=None):
    q = "SELECT * FROM arena_matches WHERE user_id=? AND kind=? AND voted=0"
    args = [user_id, kind]
    if is_test is not None:
        q += " AND is_test=?"; args.append(1 if is_test else 0)
    q += " ORDER BY served_at DESC LIMIT 1"
    with connect() as c:
        r = c.execute(q, args).fetchone()
        return dict(r) if r else None


# ---- pod submission channel (enrolled keys + signed runs) ----

def enroll_key(kid, *, public_key, fingerprint, owner_user_id=None):
    init_db()
    with connect() as c:
        c.execute(
            "INSERT OR IGNORE INTO enrolled_keys (id, public_key, fingerprint, owner_user_id, status, fail_count, created_at) "
            "VALUES (?,?,?,?, 'active', 0, ?)",
            (kid, public_key, fingerprint, owner_user_id, time.time()))


def get_enrolled_key(public_key):
    with connect() as c:
        r = c.execute("SELECT * FROM enrolled_keys WHERE public_key=?", (public_key,)).fetchone()
        return dict(r) if r else None


def bump_key_fail(public_key):
    """Increment a key's forgery counter; returns the new count (caller may revoke)."""
    with connect() as c:
        c.execute("UPDATE enrolled_keys SET fail_count = fail_count + 1 WHERE public_key=?", (public_key,))
        r = c.execute("SELECT fail_count FROM enrolled_keys WHERE public_key=?", (public_key,)).fetchone()
        return r[0] if r else 0


def revoke_key(public_key):
    with connect() as c:
        c.execute("UPDATE enrolled_keys SET status='revoked', revoked_at=? WHERE public_key=?",
                  (time.time(), public_key))


def create_pod_run(run_id, *, public_key, run_nonce, run_token, model, suite_id, board):
    init_db()
    with connect() as c:
        c.execute(
            "INSERT INTO pod_submissions (run_id, public_key, run_nonce, run_token, model, suite_id, board, status, created_at) "
            "VALUES (?,?,?,?,?,?,?, 'open', ?)",
            (run_id, public_key, run_nonce, run_token, model, suite_id, board, time.time()))


# ---- pod-LOCAL secrets (target API keys + HF tokens; pod.db only, never the mothership) ----

def _mask_secret(v):
    v = v or ""
    return ("…" + v[-4:]) if len(v) > 4 else "…"


def set_secret(name, value, kind="api_key"):
    """Save/replace a pod-local secret. Plaintext-in-DB (pod.db is 0600 on the operator's box)."""
    init_db()
    now = time.time()
    with connect() as c:
        prev = c.execute("SELECT created_at FROM pod_secrets WHERE name=?", (name,)).fetchone()
        created = (prev["created_at"] if prev else None) or now
        c.execute("INSERT OR REPLACE INTO pod_secrets (name, kind, value, created_at, updated_at) "
                  "VALUES (?,?,?,?,?)", (name, kind, value, created, now))


def get_secret(name):
    """Plaintext value — SERVER-SIDE use ONLY (inject into a subprocess env / chat target).
    NEVER return this to a client."""
    with connect() as c:
        r = c.execute("SELECT value FROM pod_secrets WHERE name=?", (name,)).fetchone()
        return r[0] if r else None


def list_secrets():
    """Masked metadata for the UI: name/kind/timestamps + a masked preview (last 4). NEVER the value."""
    init_db()
    with connect() as c:
        rows = c.execute("SELECT name, kind, value, created_at, updated_at FROM pod_secrets "
                         "ORDER BY name").fetchall()
    return [{"name": r["name"], "kind": r["kind"], "masked": _mask_secret(r["value"]),
             "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows]


def delete_secret(name):
    with connect() as c:
        c.execute("DELETE FROM pod_secrets WHERE name=?", (name,))


def get_pod_run(run_id):
    with connect() as c:
        r = c.execute("SELECT * FROM pod_submissions WHERE run_id=?", (run_id,)).fetchone()
        return dict(r) if r else None


def claim_pod_run(run_id, status, reason=None):
    """Atomically move an OPEN pod run to committed/quarantined (consumes the nonce).
    Returns True iff THIS call claimed it (so a replayed submit loses the race)."""
    with connect() as c:
        cur = c.execute(
            "UPDATE pod_submissions SET status=?, reason=?, committed_at=? WHERE run_id=? AND status='open'",
            (status, reason, time.time(), run_id))
        return cur.rowcount == 1


def _result_row(x):
    d = dict(x)
    d["evidence"] = json.loads(d.pop("evidence_json") or "null")
    d["speed"] = json.loads(d.pop("speed_json") or "{}")
    return d
