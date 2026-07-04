# AEON Bench — local MVP

A small, runnable slice of the [AEON Bench design](../DESIGN.md): drive any
**OpenAI-compatible** model through a tiny **deterministic** suite, score it
(Tier-0 programmatic + Tier-1 binary-rubric judging where the **judge defaults
to the model under test**), capture speed, and serve a leaderboard dashboard.

Faithful to the design, collapsed for one machine: in-process runner (no
containers), SQLite (not Postgres), static dashboard (not Next.js).

## Run

```bash
cd mvp
python serve.py            # → http://localhost:8080
```

Open the dashboard, point "Target endpoint" at your model server, pick a model,
press **Run**. Speed and Tier-0 scores always record; Tier-1 uses the launching
model as judge by default.

### Get a small model (Ollama)

```bash
ollama serve &                       # or run the Ollama app
ollama pull qwen2.5:0.5b-instruct    # ~400 MB
ollama pull llama3.2:1b              # ~1.3 GB
# endpoint = http://localhost:11434/v1
```

Works with any OpenAI-compatible server (LM Studio `:1234/v1`, vLLM, TGI,
llama.cpp `--api`, OpenAI itself).

### No model handy?

```bash
python -m aeon.runner mock-good   mock      # canned correct answers
python -m aeon.runner mock-sloppy mock      # canned weak-model mistakes
```
…then open the dashboard to see the leaderboard.

## The suite (`aeon/suite.py`)

11 cases across **Math, Instruction-following, Reasoning, Coding, Prose**.
Tier-0 cases are pure programmatic checks (numeric/exact match, regex
constraints, structural counts, executed unit tests). The one Tier-1 Prose case
shows the binary-rubric path: two criteria are Tier-0-shadowed (program-decided,
authoritative) and one is left to the (self-)judge.

## Layout

```
mvp/
├─ serve.py            # uvicorn launcher
├─ aeon/
│  ├─ app.py           # FastAPI: dashboard + /api (leaderboard, runs, models)
│  ├─ runner.py        # the run loop (in-process probe)
│  ├─ targets.py       # OpenAI-compatible client (streamed speed) + mock
│  ├─ evaluators.py    # Tier-0 checkers + Tier-1 binary-rubric judge
│  ├─ suite.py         # the deterministic suite
│  ├─ scoring.py       # category + leaderboard aggregation
│  └─ db.py            # sqlite store
└─ web/                # index.html + app.js + styles.css
```

## Not in the MVP (see DESIGN.md)

Container orchestration & trust tiers, signed manifests, server-side
re-derivation, the cross-family verifier, the arena, gVisor sandboxing
(here: subprocess isolation + timeout), Postgres/MinIO/Redis.
