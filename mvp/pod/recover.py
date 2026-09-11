"""pod/recover.py — boot-time orphan reconciler (pod only).

A fresh pod boot means NO bench job can be alive (jobs are child processes of the pod —
a `docker restart aeon-pod` mid-run kills the bench silently). What such a kill leaves
behind, and what this fixes at every startup:

  aeon-bench-serve        an orphaned engine container still holding weights + the port
                          -> removed (it is always ours and always ephemeral)
  ~/.aeon/paused.json     production containers the dead run paused (AEON_PAUSE_CONTAINERS /
                          clear-host mode) and never restored -> `docker start` each
                          (start only, NEVER rm; skipped if the operator disabled restore)
  local 'running' runs    pod-local SQLite run rows stranded mid-flight -> marked
                          'interrupted' (RESUMABLE: their per-case results are intact, so
                          the Run tab can offer ⟲ RESUME) — Live and the boards stop
                          showing a ghost bench either way

This is what turns "the benchmark mysteriously disappeared" into a logged, self-healed
event: every action is printed as [pod][recover] in `docker logs aeon-pod`."""
from __future__ import annotations

import json
import os
import subprocess


def _run(argv, timeout=60):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def restore_paused() -> list[str]:
    """Consume the paused.json ledger: `docker start` everything it lists (start only,
    NEVER rm; honors a disabled-restore flag), then remove it. No-op without a ledger.
    Called at pod boot (reconcile) and by the job queue when it drains — queue-managed
    benches accumulate into this ledger instead of restoring between runs."""
    acts = []
    pf = os.path.join(os.path.expanduser("~"), ".aeon", "paused.json")
    if not os.path.exists(pf):
        return acts
    try:
        st = json.load(open(pf, encoding="utf-8"))
        names = st.get("paused") or []
        if st.get("restore", True):
            for name in names:
                rr = _run(["docker", "start", name], timeout=180)
                acts.append(f"restored paused container '{name}'"
                            if rr and rr.returncode == 0 else
                            f"could not restore '{name}': {((rr.stderr if rr else '') or 'docker unavailable').strip()[:160]}")
        elif names:
            acts.append(f"restore was disabled — leaving stopped: {', '.join(names)}")
    except Exception as e:
        acts.append(f"paused.json unreadable ({e}) — no containers restored")
    try:
        os.unlink(pf)
    except OSError:
        pass
    return acts


def reconcile() -> list[str]:
    acts = []
    # 1) orphaned engine container (at boot it can never be legitimately in use)
    r = _run(["docker", "ps", "-a", "--filter", "name=aeon-bench-serve", "--format", "{{.Names}}"])
    if r and "aeon-bench-serve" in (r.stdout or ""):
        _run(["docker", "rm", "-f", "aeon-bench-serve"], timeout=120)
        acts.append("removed orphaned aeon-bench-serve container")
    # 1b) orphaned harness task containers (labelled aeon.pod.harness at `docker create` in
    #     adapters/base.py) — the runner's own in-process rm never executes when the runner
    #     was SIGTERM'd/killed mid-stage, so historical orphans self-heal here at boot
    r = _run(["docker", "ps", "-aq", "--filter", "label=aeon.pod.harness"])
    ids = [c for c in ((r.stdout or "").split() if r else []) if c]
    if ids:
        _run(["docker", "rm", "-f", *ids], timeout=120)
        acts.append(f"removed {len(ids)} orphaned harness container(s)")
    # 2) production containers a dead run paused and never restored
    acts += restore_paused()
    # 3) stranded pod-LOCAL run rows (SQLite only — never touch a shared Postgres, where
    #    'running' can belong to someone else's live pod). 'interrupted', NOT failed: the
    #    per-case results survive in pod.db, so the Run tab can offer resume.
    if not os.environ.get("AEON_DB_URL"):
        try:
            from aeon import db
            n = db.interrupt_orphaned_runs("interrupted: pod restarted mid-run")
            if n:
                acts.append(f"marked {n} stranded 'running' run row(s) interrupted (resumable)")
        except Exception:
            pass
    for a in acts:
        print(f"[pod][recover] {a}", flush=True)
    return acts
