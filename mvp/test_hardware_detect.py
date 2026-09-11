"""Hardware detection labels: a GPU rig must never silently mislabel as CPU.

The 2026-07-11 incident: the containerized pod (no --gpus) had no nvidia-smi, so
every submission from the DGX Spark was stamped 'aarch64 (CPU)'. The label chain is
now: nvidia-smi (in-process or daemon probe) > AEON_SYSTEM declaration > tegra marker
> honest 'NVIDIA GPU xN (unidentified)' from the PCI bus > CPU.

    python test_hardware_detect.py
"""
from __future__ import annotations

import os
import sys

_MVP = os.path.dirname(os.path.abspath(__file__))
for p in (_MVP, os.path.join(_MVP, "pod")):
    if p not in sys.path:
        sys.path.insert(0, p)

from pod.aeon_pod import _detect_label  # noqa: E402


def main():
    os.environ.pop("AEON_SYSTEM", None)

    # nvidia-smi visible: canonical labels, incl. Spark multiples and GPU counts
    assert _detect_label({"gpus": ["NVIDIA GB10, [N/A], 580.1"], "machine": "aarch64"}) \
        == "single DGX Spark (GB10)"
    assert _detect_label({"gpus": ["NVIDIA GB10, [N/A], 580.1"] * 2, "machine": "aarch64"}) \
        == "dual DGX Spark (GB10)"
    assert _detect_label({"gpus": ["NVIDIA GB10, [N/A], 580.1"] * 4, "machine": "aarch64"}) \
        == "quad DGX Spark (GB10)"
    assert _detect_label({"gpus": ["NVIDIA GeForce RTX 5090, 32607 MiB, 575.2"],
                          "machine": "x86_64"}) == "RTX 5090 32GB"
    assert _detect_label({"gpus": ["NVIDIA GeForce RTX 5090, 32607 MiB, 575.2"] * 2,
                          "machine": "x86_64"}) == "2× RTX 5090 32GB"

    # blind container on a GPU host: PCI count -> honest unidentified label, never CPU
    assert _detect_label({"machine": "aarch64", "pci_nvidia_gpus": 1}) \
        == "NVIDIA GPU ×1 (unidentified)"

    # explicit host declaration wins over the blind fallbacks
    os.environ["AEON_SYSTEM"] = "dgx-spark"
    try:
        assert _detect_label({"machine": "aarch64", "pci_nvidia_gpus": 1}) \
            == "single DGX Spark (GB10)"
        assert _detect_label({"machine": "aarch64"}) == "single DGX Spark (GB10)"
    finally:
        os.environ.pop("AEON_SYSTEM", None)

    # genuinely no accelerator: the CPU label stands
    lbl = _detect_label({"machine": "x86_64"})
    assert lbl == "x86_64 (CPU)", lbl

    print("OK  hardware detection: smi labels, Spark multiples, blind-container honesty, AEON_SYSTEM override")


if __name__ == "__main__":
    main()
