# Before flipping this repository PUBLIC

This pod repo currently ships the **full `aeon` package** for simplicity while private.
Several modules are mothership-only and must be removed or gated before public release,
because they document integrity mechanisms whose value depends on secrecy:

- [ ] **`mvp/aeon/arena.py` honeypot internals** — `_build_test_match`, `_should_test`
      rates/thresholds (`TRUST_ACCURACY`), `_BOGUS_MODELS` decoy naming, `seed_bogus`.
      A public copy tells bad actors the spot-check rate and how decoys are camouflaged.
      → strip to the pod-needed surface only (`PROMPTS`, `generate_artifact`).
- [ ] **`mvp/aeon/accounts.py`** — IP-cap value and throttle policy are deliberately
      undisclosed in the UI; don't disclose them in code either. Pods don't need accounts.
- [ ] **`mvp/aeon/admin.py` + admin endpoints in `app.py`** — mothership-only.
- [ ] **`mvp/aeon/ingest.py`** — mothership-side acceptance logic (what gets checked
      before a run is trusted). Publishing the exact checks helps forgery attempts.
- [ ] **`mvp/aeon/app.py`** — split: pods need config/live/run/keys/submission-view
      routes; strip arena voting, honeypot, admin, auth routes.
- [ ] Rotate any mothership URLs/tokens committed in examples; re-verify `.gitignore`
      catches `pod.db*`, `device_key.pem`, `.aeon_attest_key.pem`, `*.log`.
- [ ] License decision (the harness adapters shell out to third-party CLIs — review
      their trademark/usage language in README).
- [ ] Re-run the full pod flow from a clean clone on a non-AEON machine.

None of these weaken the *pod's* function — they are all mothership-trust surface.
