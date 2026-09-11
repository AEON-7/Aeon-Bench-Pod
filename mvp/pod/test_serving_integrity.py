"""Unit tests for the GPU-free serving-integrity check (endpoints.serving_integrity).

Runs entirely offline: a fake `runner` returns canned `docker ps` / `docker inspect` output, and the
container's mount Source points at a real temp dir the check reads config.json / index.json / weight
files from. `local_dir` (the HF-verified reference) is a SEPARATE temp dir, so a mismatch is a genuine
diff. The deep check recomputes the bundle weights_hash from `per_file` (rel-path keyed), so these
tests also guard the three review findings: basename collapse, GGUF single-quant, and re-shard halts.
"""
import hashlib
import json
import os
import tempfile

from pod import endpoints as ep


def _write(d, name, obj):
    p = os.path.join(d, name)
    os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(name) else None
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def _write_bytes(d, rel, data):
    p = os.path.join(d, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return ep._sha256_file(p)


def _wh(per_file):
    """Reproduce modelhost.verify()'s weights_hash from a {rel: sha256} map."""
    manifest = ";".join(f"{rel}:{per_file[rel]}" for rel in sorted(per_file))
    return hashlib.sha256(manifest.encode()).hexdigest()


def _runner_for(serve_dir, cmd=None, mount="/models"):
    cmd = cmd or ["serve", mount, "--port", "8000", "--served-model-name", "m"]
    inspect = [{
        "Name": "/vllm", "Config": {"Image": "vllm/vllm-openai:latest", "Cmd": cmd, "Env": []},
        "Mounts": [{"Destination": mount, "Source": serve_dir}],
        "NetworkSettings": {"Ports": {}},
    }]

    def run(argv):
        if argv[:2] == ["docker", "ps"]:
            return "c1\n"
        if argv[:2] == ["docker", "inspect"]:
            return json.dumps(inspect)
        return ""
    return run


CFG = {"architectures": ["GemmaForCausalLM"], "model_type": "gemma", "num_hidden_layers": 62,
       "hidden_size": 5376, "vocab_size": 262144,
       "quantization_config": {"quant_method": "modelopt_fp4"}}
IDX = {"weight_map": {"model.embed_tokens.weight": "model-00001-of-00002.safetensors",
                      "model.norm.weight": "model-00002-of-00002.safetensors"}}
REF = {"repo": "AEON-7/Gemma-4-26B-NVFP4", "revision": "main",
       "files": {"config.json": None,
                 "model-00001-of-00002.safetensors": "h1",
                 "model-00002-of-00002.safetensors": "h2"}}
SERVE = "http://127.0.0.1:8000/v1"


def test_match():
    with tempfile.TemporaryDirectory() as ref_dir, tempfile.TemporaryDirectory() as serve_dir:
        for d in (ref_dir, serve_dir):
            _write(d, "config.json", CFG)
            _write(d, "model.safetensors.index.json", IDX)
        r = ep.serving_integrity(SERVE, ["m"], ref=REF, local_dir=ref_dir,
                                 runner=_runner_for(serve_dir))
        assert r["status"] == "match", r
        assert r["ok"] is True
        names = {c["name"] for c in r["checks"]}
        assert "architectures" in names and "num_hidden_layers" in names and "weight_tensors" in names
        assert all(c["status"] == "match" for c in r["checks"] if c["status"] != "unavailable")


def test_mismatch_wrong_size():
    """The classic accident: pointed at a structurally different (smaller) model."""
    with tempfile.TemporaryDirectory() as ref_dir, tempfile.TemporaryDirectory() as serve_dir:
        _write(ref_dir, "config.json", CFG)
        _write(ref_dir, "model.safetensors.index.json", IDX)
        _write(serve_dir, "config.json", dict(CFG, num_hidden_layers=32, hidden_size=4096))
        _write(serve_dir, "model.safetensors.index.json", IDX)
        r = ep.serving_integrity(SERVE, ["m"], ref=REF, local_dir=ref_dir,
                                 runner=_runner_for(serve_dir))
        assert r["status"] == "mismatch", r
        bad = {c["name"] for c in r["checks"] if c["status"] == "mismatch"}
        assert "num_hidden_layers" in bad and "hidden_size" in bad
        assert "SERVE MISMATCH" in r["summary"]


def test_mismatch_wrong_quant():
    with tempfile.TemporaryDirectory() as ref_dir, tempfile.TemporaryDirectory() as serve_dir:
        _write(ref_dir, "config.json", CFG)
        _write(ref_dir, "model.safetensors.index.json", IDX)
        _write(serve_dir, "config.json", dict(CFG, quantization_config={"quant_method": "awq"}))
        _write(serve_dir, "model.safetensors.index.json", IDX)
        r = ep.serving_integrity(SERVE, ["m"], ref=REF, local_dir=ref_dir,
                                 runner=_runner_for(serve_dir))
        assert r["status"] == "mismatch", r
        assert any(c["name"] == "quant_method" and c["status"] == "mismatch" for c in r["checks"])


def test_mismatch_wrong_tensor_set():
    """A genuinely different tensor set (not just re-sharded) is still caught."""
    with tempfile.TemporaryDirectory() as ref_dir, tempfile.TemporaryDirectory() as serve_dir:
        _write(ref_dir, "config.json", CFG)
        _write(ref_dir, "model.safetensors.index.json", IDX)
        _write(serve_dir, "config.json", CFG)
        _write(serve_dir, "model.safetensors.index.json",
               {"weight_map": {"some.other.tensor": "model-00001-of-00001.safetensors"}})
        r = ep.serving_integrity(SERVE, ["m"], ref=REF, local_dir=ref_dir,
                                 runner=_runner_for(serve_dir))
        assert r["status"] == "mismatch", r
        assert any(c["name"] == "weight_tensors" and c["status"] == "mismatch" for c in r["checks"])


def test_reshard_not_flagged():
    """FIX #3: a bit-identical model re-sharded (same tensors, different shard FILENAMES) must NOT
    be flagged as a mismatch — the check compares tensor names, which are shard-invariant."""
    with tempfile.TemporaryDirectory() as ref_dir, tempfile.TemporaryDirectory() as serve_dir:
        _write(ref_dir, "config.json", CFG)
        _write(ref_dir, "model.safetensors.index.json", IDX)          # 2 shards
        _write(serve_dir, "config.json", CFG)
        _write(serve_dir, "model.safetensors.index.json",             # same tensors, 3 shards
               {"weight_map": {"model.embed_tokens.weight": "m-00001-of-00003.safetensors",
                               "model.norm.weight": "m-00003-of-00003.safetensors"}})
        r = ep.serving_integrity(SERVE, ["m"], ref=REF, local_dir=ref_dir,
                                 runner=_runner_for(serve_dir))
        assert r["status"] == "match", r
        assert any(c["name"] == "weight_tensors" and c["status"] == "match" for c in r["checks"])


def test_deep_hash_match_and_mismatch():
    with tempfile.TemporaryDirectory() as ref_dir, tempfile.TemporaryDirectory() as serve_dir:
        for d in (ref_dir, serve_dir):
            _write(d, "config.json", CFG)
            _write(d, "model.safetensors.index.json", IDX)
        # served shards + their real hashes; the pod's verified per_file == the served bytes -> match
        pf = {}
        for n in ("model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"):
            pf[n] = _write_bytes(serve_dir, n, n.encode() + b"-weights")
        wh = _wh(pf)
        r = ep.serving_integrity(SERVE, ["m"], ref=REF, local_dir=ref_dir,
                                 runner=_runner_for(serve_dir), deep=True, weights_hash=wh, per_file=pf)
        assert r["status"] == "match" and r["weights_verified"] is True, r
        assert r.get("weights_hash") == wh, r
        assert any(c["name"] == "weight_sha256" and c["status"] == "match" for c in r["checks"])
        # verified pull's shard-1 differs from the served bytes -> mismatch, never verified
        pf_bad = dict(pf, **{"model-00001-of-00002.safetensors": "0" * 64})
        r2 = ep.serving_integrity(SERVE, ["m"], ref=REF, local_dir=ref_dir,
                                  runner=_runner_for(serve_dir), deep=True,
                                  weights_hash=_wh(pf_bad), per_file=pf_bad)
        assert r2["status"] == "mismatch" and r2["weights_verified"] is False, r2
        assert "weights_hash" not in r2, r2


def test_deep_incomplete_not_verified():
    """If the pod verified TWO shards but the serve only has one, the recompute can't complete."""
    with tempfile.TemporaryDirectory() as ref_dir, tempfile.TemporaryDirectory() as serve_dir:
        _write(serve_dir, "config.json", CFG)
        _write(serve_dir, "model.safetensors.index.json", IDX)
        pf = {"model-00001-of-00002.safetensors":
              _write_bytes(serve_dir, "model-00001-of-00002.safetensors", b"x"),
              "model-00002-of-00002.safetensors": "b" * 64}          # never written on the serve
        r = ep.serving_integrity(SERVE, ["m"], ref=REF, local_dir=serve_dir,
                                 runner=_runner_for(serve_dir), deep=True, weights_hash=_wh(pf), per_file=pf)
        assert r["weights_verified"] is False and "weights_hash" not in r, r


def test_deep_basename_collapse_fixed():
    """FIX #1: two weight files sharing a basename in different sub-dirs must both be hashed (rel-path
    keyed). If the sub-dir file's verified hash differs from what's served, it must NOT verify."""
    with tempfile.TemporaryDirectory() as ref_dir, tempfile.TemporaryDirectory() as serve_dir:
        _write(serve_dir, "config.json", CFG)
        h_root = _write_bytes(serve_dir, "model.safetensors", b"root-bytes")
        _write_bytes(serve_dir, "vision_tower/model.safetensors", b"vision-bytes")
        # the pod's verified pull expects a DIFFERENT sub-dir file (adulterated on the serve)
        pf = {"model.safetensors": h_root, "vision_tower/model.safetensors": "f" * 64}
        r = ep.serving_integrity(SERVE, ["m"], ref=REF, local_dir=serve_dir,
                                 runner=_runner_for(serve_dir), deep=True, weights_hash=_wh(pf), per_file=pf)
        assert r["weights_verified"] is False, r          # pre-fix this falsely verified (root only)
        assert any(c["name"] == "weight_sha256" and c["status"] == "mismatch" for c in r["checks"]), r


def test_gguf_single_quant_attests():
    """FIX #2: serving ONE quant from a multi-quant GGUF repo must be able to verify — per_file holds
    only the served file, so the recompute is over that file alone."""
    with tempfile.TemporaryDirectory() as ref_dir, tempfile.TemporaryDirectory() as serve_dir:
        served = "Model-Q4_K_M.gguf"
        h = _write_bytes(serve_dir, served, b"gguf-weights")
        multi_ref = {"repo": "org/Model-GGUF", "files": {
            served: h, "Model-Q8_0.gguf": "z1", "Model-F16.gguf": "z2"}}
        pf = {served: h}                                  # verify() for a gguf pull -> just this file
        cmd = ["-m", "/models/" + served, "--port", "8000", "--served-model-name", "m"]
        r = ep.serving_integrity(SERVE, ["m"], ref=multi_ref, local_dir=ref_dir,
                                 runner=_runner_for(serve_dir, cmd=cmd), deep=True,
                                 weights_hash=_wh(pf), per_file=pf)
        assert r["status"] == "match" and r["weights_verified"] is True, r
        assert r.get("weights_hash") == _wh(pf), r
        assert any(c["name"] == "weight_file" and c["status"] == "match" for c in r["checks"])


def test_unavailable_no_container_match():
    with tempfile.TemporaryDirectory() as ref_dir, tempfile.TemporaryDirectory() as serve_dir:
        _write(ref_dir, "config.json", CFG)
        r = ep.serving_integrity("http://127.0.0.1:9999/v1", ["m"], ref=REF, local_dir=ref_dir,
                                 runner=_runner_for(serve_dir))
        assert r["status"] == "unavailable" and r["ok"] is None, r


def test_unavailable_remote_without_docker_host():
    r = ep.serving_integrity("http://192.168.1.116:8000/v1", ["m"], ref=REF, local_dir=None)
    assert r["status"] == "unavailable" and r["ok"] is None, r


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all serving-integrity tests passed")
