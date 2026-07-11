"""hwnorm guard: every detected-label shape seen in prod maps to its canonical bucket.

Grammar sources: pod/aeon_pod.py _detect_label (Spark multiples, RTX + memory, honest
blind-container label, Apple sysctl labels, CPU fallback) plus the known aarch64-(CPU)
mislabel era — those must land in 'Unlabeled', never a GPU bucket.

    python test_hwnorm.py
"""
from __future__ import annotations

import os
import sys

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

from aeon.hwnorm import SPARK_BUCKETS, normalize_label  # noqa: E402


def _n(label):
    d = normalize_label(label)
    assert set(d) == {"bucket", "label", "family", "spark_count", "search"}, sorted(d)
    assert d["search"] == d["search"].lower()
    # the bucket and the verbatim label are always findable through the search haystack
    assert d["bucket"].lower() in d["search"]
    assert d["label"].lower() in d["search"]
    return d


def test_spark_counts():
    assert SPARK_BUCKETS == ["Single DGX Spark", "2× DGX Spark", "3× DGX Spark", "4× DGX Spark"]
    for label, n, bucket in [
        ("single DGX Spark (GB10)", 1, "Single DGX Spark"),
        ("dual DGX Spark (GB10)",   2, "2× DGX Spark"),
        ("triple DGX Spark (GB10)", 3, "3× DGX Spark"),
        ("quad DGX Spark (GB10)",   4, "4× DGX Spark"),
        ("5x DGX Spark (GB10)",     5, "5× DGX Spark"),   # labeler grammar for n>4
        ("2× DGX Spark (GB10)",     2, "2× DGX Spark"),
        ("DGX Spark (GB10)",        1, "Single DGX Spark"),  # bare desc = one node
    ]:
        d = _n(label)
        assert d["bucket"] == bucket, (label, d["bucket"])
        assert d["family"] == "dgx-spark" and d["spark_count"] == n, (label, d)
        assert d["label"] == label                        # row label stays verbatim
        assert "dgx spark" in d["search"]                 # 'dgx spark' finds every Spark


def test_rtx_grouped_by_model():
    a = _n("RTX 5090 32GB")
    b = _n("2× RTX 5090 32GB")
    assert a["bucket"] == b["bucket"] == "NVIDIA RTX 5090"   # memory/count never in the bucket
    assert a["family"] == b["family"] == "nvidia-rtx"
    assert a["spark_count"] is None and b["spark_count"] is None
    assert b["label"] == "2× RTX 5090 32GB"               # multi-GPU stays on the row label
    assert _n("RTX 4090 24GB")["bucket"] == "NVIDIA RTX 4090"
    assert _n("NVIDIA GeForce RTX 3080 Ti 12GB")["bucket"] == "NVIDIA RTX 3080 Ti"
    assert _n("4× RTX A6000 48GB")["bucket"] == "NVIDIA RTX A6000"
    assert "rtx" in a["search"] and "nvidia" in a["search"]


def test_blind_container_label():
    for label in ("NVIDIA GPU ×1 (unidentified)", "NVIDIA GPU x3 (unidentified)"):
        d = _n(label)
        assert d["bucket"] == "NVIDIA GPU (unidentified)", (label, d["bucket"])
        assert d["family"] == "nvidia-other" and d["spark_count"] is None


def test_other_nvidia_cleaned_name():
    d = _n("2× H100 80GB HBM3 80GB")
    assert d["bucket"] == "H100 80GB HBM3 80GB"           # count stripped, SKU memory kept
    assert d["family"] == "nvidia-other"
    assert _n("NVIDIA Tesla T4 16GB")["bucket"] == "Tesla T4 16GB"
    assert _n("A100-SXM4-80GB 80GB")["family"] == "nvidia-other"
    assert _n("GH200 480GB")["family"] == "nvidia-other"


def test_apple_chip_family():
    for label, bucket in [
        ("MacBook Pro M4 48GB", "Apple M4"),
        ("Apple M4 48GB",       "Apple M4"),
        ("Mac Studio M3 Ultra 512GB", "Apple M3 Ultra"),
        ("MacBook Air M2 16GB", "Apple M2"),
    ]:
        d = _n(label)
        assert d["bucket"] == bucket, (label, d["bucket"])
        assert d["family"] == "apple" and d["spark_count"] is None


def test_amd_family():
    d = _n("AMD Radeon RX 7900 XTX 24GB")
    assert d["family"] == "amd"
    assert _n("2× Radeon RX 7900 XTX 24GB")["bucket"] == "Radeon RX 7900 XTX 24GB"


def test_unlabeled_honesty():
    # the aarch64-(CPU) mislabel era + genuine CPU/None/unknown all read as Unlabeled
    for label in (None, "", "   ", "aarch64 (CPU)", "x86_64 (CPU)", "unknown (CPU)", "TPU v7"):
        d = _n(label)
        assert d["bucket"] == "Unlabeled", (label, d["bucket"])
        assert d["family"] == "unlabeled" and d["spark_count"] is None, (label, d)
    assert _n(None)["label"] == "Unlabeled"
    assert _n("aarch64 (CPU)")["label"] == "aarch64 (CPU)"   # the row still shows the truth


def test_query_normalization():
    # champion queries route through the same normalizer: a count-less Spark query means one node
    assert normalize_label("dgx spark")["bucket"] == "Single DGX Spark"
    assert normalize_label("2x dgx spark")["bucket"] == "2× DGX Spark"
    assert normalize_label("rtx 5090")["bucket"] == "NVIDIA RTX 5090"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name[5:]}")
    print("OK  hwnorm: Spark counts, RTX model grouping, blind-container label, Apple chip "
          "families, AMD, Unlabeled honesty, query normalization")


if __name__ == "__main__":
    main()
