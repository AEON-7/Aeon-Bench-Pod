"""Endpoint discovery scan (pod/endpoints.py) — the 'scan for a live instance' half of easy
verified target mode. Injectable transport, no network: two ports serve, the rest are dead;
asserts the scan finds the live ones, dedups a server fronted on two ports, caps hosts, and
never raises on an unreachable port."""
import json
import os
import sys
import tempfile

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

from pod import diskscan  # noqa: E402
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

# ================================ HF AUTODETECT ================================
# The scan doesn't just list served ids — it autodetects the Hugging Face repo behind each so the
# HF-link field prefills itself. Two tiers: endpoint-only (/v1/models `root`, or an org/model
# served id) and a docker fallback (the backing container's --model mount) for local-path serves.

# don't let a real host env leak into the path-translation logic under test
for _v in ("AEON_HOST_HOME_DIR", "AEON_MODELS_DIR", "AEON_MODELS_HOST_DIR"):
    os.environ.pop(_v, None)

# a fake LAN of serves launched four different ways:
#   :8000 two aliases of ONE local-path model (root="/model") — endpoint reveals nothing
#   :8001 launched from a Hub id -> root is the exact repo (endpoint-only, high)
#   :8003 the served id IS an org/model repo (endpoint-only, high)
#   :8005 a local-path serve whose folder has NO breadcrumb -> only a local name hint
LIVE_AD = {
    "http://127.0.0.1:8000/v1/models": {"data": [{"id": "aeon-ultimate", "root": "/model"},
                                                  {"id": "qwen36-ultimate", "root": "/model"}]},
    "http://127.0.0.1:8001/v1/models": {"data": [{"id": "qwen3-asr", "root": "Qwen/Qwen3-ASR-0.6B"}]},
    "http://127.0.0.1:8003/v1/models": {"data": [{"id": "Org/Direct-Repo"}]},
    "http://127.0.0.1:8005/v1/models": {"data": [{"id": "mystery", "root": "/opt/weights/mystery"}]},
}


def ad_transport(url):
    if url in LIVE_AD:
        return LIVE_AD[url]
    raise ConnectionError("refused")


# the backing containers: :8000's --model /model is bind-mounted from a dir carrying an
# .aeon-modelref.json (exact repo); :8005's folder has nothing to reconcile
_side = tempfile.mkdtemp()
json.dump({"repo": "Local/Repo", "revision": "rev123"},
          open(os.path.join(_side, ".aeon-modelref.json"), "w", encoding="utf-8"))
_empty = tempfile.mkdtemp()
os.makedirs(os.path.join(_empty, "mystery"))

INSPECT = [
    {"Name": "/openclaw",
     "Config": {"Cmd": ["-lc", "exec vllm serve /model --served-model-name aeon-ultimate "
                        "qwen36-ultimate --port 8000 --quantization compressed-tensors"]},
     "Mounts": [{"Destination": "/model", "Source": _side}]},
    {"Name": "/mystery-serve",
     "Config": {"Entrypoint": None,
                "Cmd": ["vllm", "serve", "/opt/weights/mystery", "--served-model-name", "mystery",
                        "--port", "8005"]},
     "Mounts": [{"Destination": "/opt/weights", "Source": _empty}]},
]


def docker_runner(argv):
    if argv[:3] == ["docker", "ps", "-q"]:
        return "c1\nc5\n"
    if argv[:2] == ["docker", "inspect"]:
        return json.dumps(INSPECT)
    return ""


rA = ep.scan(ports=[8000, 8001, 8003, 8005], transport=ad_transport, docker_runner=docker_runner)
eps = {e["url"]: e for e in rA["endpoints"]}


def served_of(port):
    e = eps.get(f"http://127.0.0.1:{port}/v1")
    return (e or {}).get("served") or []


# ---- endpoint-only autodetect (no docker needed) ----
s8001 = served_of(8001)
check(len(s8001) == 1 and s8001[0]["hf_guess"] == "Qwen/Qwen3-ASR-0.6B"
      and s8001[0]["confidence"] == "high" and s8001[0]["source"] == "served-root",
      "a serve launched from a Hub id -> HF repo autodetected from /v1/models root (endpoint-only, high)")
s8003 = served_of(8003)
check(len(s8003) == 1 and s8003[0]["hf_guess"] == "Org/Direct-Repo"
      and s8003[0]["source"] == "served-id",
      "a served id that is itself org/model -> autodetected as the repo (served-id, high)")

# ---- alias folding: many served-model-name aliases of ONE physical model collapse to one entry ----
s8000 = served_of(8000)
check(len(s8000) == 1 and s8000[0]["ids"] == ["aeon-ultimate", "qwen36-ultimate"],
      "aliases sharing one root fold into a single served entry (not over-counted)")

# ---- docker fallback: local-path serve resolved via the backing container's --model mount ----
check(s8000[0]["hf_guess"] == "Local/Repo" and s8000[0]["hf_revision"] == "rev123"
      and s8000[0]["confidence"] == "medium" and s8000[0]["source"].startswith("docker-mount"),
      "a local-path serve's repo is recovered from the container's --model mount (.aeon-modelref -> medium)")

# ---- docker fallback with no breadcrumb: surface the folder name, never a false repo ----
s8005 = served_of(8005)
check(len(s8005) == 1 and not s8005[0]["hf_guess"] and s8005[0].get("local_name") == "mystery",
      "an unreconcilable local-path serve yields a local_name hint, not a fabricated repo")

# ---- back-compat: models[] flat id list still present alongside served[] ----
check(eps["http://127.0.0.1:8000/v1"]["models"] == ["aeon-ultimate", "qwen36-ultimate"],
      "the flat models[] id list is preserved for older front-ends")

# ---- docker is only consulted for LOCALHOST endpoints (can't inspect a remote node's containers) ----
calls = []


def spy_runner(argv):
    calls.append(argv)
    return docker_runner(argv)


ep.scan(hosts=["10.9.9.9"], ports=[8000], transport=lambda u: {"data": [{"id": "x", "root": "/model"}]},
        docker_runner=spy_runner)
check(calls == [], "a remote (non-localhost) endpoint never triggers the local docker fallback")

# ================================ MULTI-ENGINE COVERAGE ================================
# Autodetect must be engine-agnostic — vLLM/SGLang/TGI/llama.cpp/Ollama/LM Studio expose identity
# differently. Endpoint-only tier first (no docker): whatever the engine puts in root/id.
LIVE_ENG = {
    # SGLang (no alias): id == root == the model-path, an org/model repo
    "http://127.0.0.1:8010/v1/models": {"data": [{"id": "Qwen/Qwen2.5-7B-Instruct",
                                                   "root": "Qwen/Qwen2.5-7B-Instruct", "owned_by": "sglang"}]},
    # TGI: id == --model-id (the repo); no root; owned_by mirrors the repo
    "http://127.0.0.1:3000/v1/models": {"data": [{"id": "meta-llama/Meta-Llama-3-8B",
                                                   "owned_by": "meta-llama/Meta-Llama-3-8B"}]},
    # Ollama hf.co tag: the source repo is encoded in the tag
    "http://127.0.0.1:11434/v1/models": {"data": [{"id": "hf.co/bartowski/Model-GGUF:Q4_K_M",
                                                    "owned_by": "library"}]},
    # Ollama library tag: no HF identity available -> honest no-guess, but format known (gguf)
    "http://127.0.0.1:8011/v1/models": {"data": [{"id": "llama3.1:8b", "owned_by": "library"}]},
    # a huggingface.co URL in root
    "http://127.0.0.1:8012/v1/models": {"data": [{"id": "served", "root": "https://huggingface.co/Org/UrlModel"}]},
}


def eng_transport(url):
    if url in LIVE_ENG:
        return LIVE_ENG[url]
    raise ConnectionError("refused")


rE = ep.scan(ports=[8010, 3000, 11434, 8011, 8012], transport=eng_transport,
             docker_runner=lambda a: "")               # no docker: prove endpoint-only coverage
epsE = {e["url"]: (e.get("served") or [{}])[0] for e in rE["endpoints"]}
check(epsE["http://127.0.0.1:8010/v1"]["hf_guess"] == "Qwen/Qwen2.5-7B-Instruct"
      and epsE["http://127.0.0.1:8010/v1"]["confidence"] == "high"
      and epsE["http://127.0.0.1:8010/v1"]["format"] == "safetensors",
      "SGLang: id==root==repo -> autodetected endpoint-only (safetensors)")
check(epsE["http://127.0.0.1:3000/v1"]["hf_guess"] == "meta-llama/Meta-Llama-3-8B"
      and epsE["http://127.0.0.1:3000/v1"]["source"] == "served-id",
      "TGI: id == --model-id repo -> autodetected endpoint-only")
oll = epsE["http://127.0.0.1:11434/v1"]
check(oll["hf_guess"] == "bartowski/Model-GGUF" and "hf-tag" in oll["source"]
      and oll["format"] == "gguf",
      "Ollama hf.co/owner/repo:quant tag -> repo parsed out of the tag (gguf)")
lib = epsE["http://127.0.0.1:8011/v1"]
check(not lib["hf_guess"] and lib["format"] == "gguf",
      "Ollama library tag -> honest no-guess, format still known (gguf, from owned_by)")
check(epsE["http://127.0.0.1:8012/v1"]["hf_guess"] == "Org/UrlModel",
      "a huggingface.co URL in root -> repo id extracted")

# ---- docker fallback across engines: matched BY PORT (cmd or published), broad model flags ----
_lms = tempfile.mkdtemp()                               # LM Studio layout: <pub>/<repo>/<file>.gguf
os.makedirs(os.path.join(_lms, "Publisher", "RepoGGUF"))
open(os.path.join(_lms, "Publisher", "RepoGGUF", "model.Q4_K_M.gguf"), "w").close()
_sg = tempfile.mkdtemp()                                # a safetensors dir with a pull sidecar
os.makedirs(os.path.join(_sg, "sg"))
json.dump({"repo": "Org/SgModel"}, open(os.path.join(_sg, "sg", ".aeon-modelref.json"), "w", encoding="utf-8"))

LIVE_DK = {
    "http://127.0.0.1:8080/v1/models": {"data": [{"id": "my-local-model", "owned_by": "llamacpp"}]},  # llama.cpp
    "http://127.0.0.1:3001/v1/models": {"data": [{"id": "tgi-alias"}]},                                # TGI --model-id
    "http://127.0.0.1:9000/v1/models": {"data": [{"id": "sglang-alias", "root": "sglang-alias"}]},     # SGLang bridge
    "http://127.0.0.1:8082/v1/models": {"data": [{"id": "envtgi"}]},                                   # TGI env MODEL_ID
}


def dk_transport(url):
    if url in LIVE_DK:
        return LIVE_DK[url]
    raise ConnectionError("refused")


INSPECT_DK = [
    # llama.cpp: -m <gguf FILE>, matched by --port 8080; gguf -> LM Studio publisher/repo layout
    {"Name": "/llamacpp", "Config": {"Cmd": ["llama-server", "-m",
        "/lms/Publisher/RepoGGUF/model.Q4_K_M.gguf", "--port", "8080"]},
     "Mounts": [{"Destination": "/lms", "Source": _lms}]},
    # TGI: --model-id is the repo, matched by --port 3001
    {"Name": "/tgi", "Config": {"Cmd": ["text-generation-launcher", "--model-id",
        "meta-llama/TgiRepo", "--port", "3001"]}, "Mounts": []},
    # SGLang bridge: no --port in cmd, matched by PUBLISHED host port 9000; --model-path dir
    {"Name": "/sglang", "Config": {"Cmd": ["python", "-m", "sglang.launch_server",
        "--model-path", "/data/sg"]}, "Mounts": [{"Destination": "/data", "Source": _sg}],
     "NetworkSettings": {"Ports": {"30000/tcp": [{"HostPort": "9000"}]}}},
    # TGI via ENV MODEL_ID (no model flag on the cmd), matched by --port 8082
    {"Name": "/tgienv", "Config": {"Cmd": ["text-generation-launcher", "--port", "8082"],
        "Env": ["MODEL_ID=Org/EnvRepo", "PORT=8082"]}, "Mounts": []},
]


def dk_runner(argv):
    if argv[:3] == ["docker", "ps", "-q"]:
        return "a\nb\nc\nd\n"
    if argv[:2] == ["docker", "inspect"]:
        return json.dumps(INSPECT_DK)
    return ""


rD = ep.scan(ports=[8080, 3001, 9000, 8082], transport=dk_transport, docker_runner=dk_runner)
epsD = {e["url"]: (e.get("served") or [{}])[0] for e in rD["endpoints"]}
check(epsD["http://127.0.0.1:8080/v1"]["hf_guess"] == "Publisher/RepoGGUF"
      and epsD["http://127.0.0.1:8080/v1"]["format"] == "gguf"
      and epsD["http://127.0.0.1:8080/v1"]["confidence"] == "medium",
      "llama.cpp: -m <gguf> matched by cmd --port -> repo from LM Studio publisher/repo layout (gguf)")
check(epsD["http://127.0.0.1:3001/v1"]["hf_guess"] == "meta-llama/TgiRepo"
      and epsD["http://127.0.0.1:3001/v1"]["source"] == "docker-model-arg",
      "TGI: --model-id read from the backing container (matched by port)")
check(epsD["http://127.0.0.1:9000/v1"]["hf_guess"] == "Org/SgModel",
      "SGLang bridge: container matched by PUBLISHED host port, --model-path dir reconciled")
check(epsD["http://127.0.0.1:8082/v1"]["hf_guess"] == "Org/EnvRepo",
      "TGI via ENV MODEL_ID -> model reference read from the container env")

# ================================ reconcile_path ================================
# the path-only HF reconciler reused by the docker fallback: breadcrumbs, most->least confident
check(diskscan.reconcile_path("/any/models--Org--Name/snapshots/abc123")
      == ("Org/Name", "abc123", "hf-cache-layout"),
      "reconcile_path: HF hub-cache snapshot layout -> exact repo + sha")
check(diskscan.reconcile_path(os.path.join(tempfile.gettempdir(), "Org__Name"))
      == ("Org/Name", None, "aeon-layout"),
      "reconcile_path: the org__name pull convention -> repo")
_cfg = tempfile.mkdtemp()
json.dump({"_name_or_path": "Org/FromConfig"}, open(os.path.join(_cfg, "config.json"), "w", encoding="utf-8"))
r, rev, src = diskscan.reconcile_path(_cfg)
check((r, src) == ("Org/FromConfig", "config.json"),
      "reconcile_path: config.json _name_or_path (org/model) -> repo")
check(diskscan.reconcile_path(tempfile.mkdtemp()) == (None, None, None),
      "reconcile_path: a folder with no breadcrumb -> no guess (never fabricates)")

print(f"\nOK  endpoint scan: {PASSED} checks passed")
