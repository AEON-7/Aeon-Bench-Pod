"""Hardware label normalization: detected labels -> canonical comparison buckets.

The pod stamps every run with a DETECTED hardware label (aeon_pod._detect_label grammar:
'single DGX Spark (GB10)', '2× RTX 5090 32GB', 'NVIDIA GPU ×1 (unidentified)',
'MacBook Pro M4 48GB', 'x86_64 (CPU)', ...). Labels are precise but too granular to
cluster a board on — this module maps each label to the BUCKET it competes in:

  Spark counts      -> 'Single DGX Spark' / '2× DGX Spark' / '3× DGX Spark' / ...
  RTX cards         -> 'NVIDIA RTX <model>' (memory + GPU count stay on the ROW label,
                        never the bucket: 'RTX 5090 32GB' and '2× RTX 5090 32GB' cluster
                        together under 'NVIDIA RTX 5090')
  other NVIDIA GPUs -> their cleaned name (count multiplier stripped)
  Apple silicon     -> the chip family ('Apple M4', 'Apple M3 Ultra')
  blind containers  -> 'NVIDIA GPU (unidentified)'
  CPU / None / ???  -> 'Unlabeled' (honest: includes the known aarch64-(CPU) mislabels
                        pending backfill — never silently folded into a GPU bucket)

Pure string logic, zero imports beyond re — safe for scoring, cards and the pod alike.
"""
from __future__ import annotations

import re

FAMILY_SPARK = "dgx-spark"
FAMILY_RTX = "nvidia-rtx"
FAMILY_NVIDIA = "nvidia-other"
FAMILY_APPLE = "apple"
FAMILY_AMD = "amd"
FAMILY_UNLABELED = "unlabeled"

# The canonical always-shown filter presets, in board order.
SPARK_BUCKETS = ["Single DGX Spark", "2× DGX Spark", "3× DGX Spark", "4× DGX Spark"]

_SPARK = re.compile(r"DGX\s*Spark", re.I)
# labeler grammar: single/dual/triple/quad/Nx/N× ahead of 'DGX Spark'
_SPARK_MULT = re.compile(r"\b(single|dual|triple|quad|(\d+)\s*[x×])\s+DGX\s*Spark", re.I)
_SPARK_WORDS = {"single": 1, "dual": 2, "triple": 3, "quad": 4}
_COUNT_PREFIX = re.compile(r"^\s*(\d+)\s*[x×]\s+", re.I)
_UNIDENTIFIED = re.compile(r"NVIDIA\s+GPU\s*[x×]\s*\d+\s*\(unidentified\)", re.I)
_CPU = re.compile(r"\(CPU\)\s*$", re.I)
# 'RTX 5090', 'RTX 3080 Ti', 'RTX 2080 SUPER', 'RTX A6000' — model number + optional suffix
_RTX = re.compile(r"\bRTX\s+([A-Z]?\d{3,4}[A-Z]*(?:\s+(?:Ti|SUPER|Ada))?)\b", re.I)
_APPLE_HINT = re.compile(r"\b(Apple|MacBook|Mac\s*mini|Mac\s*Studio|Mac\s*Pro|iMac)\b", re.I)
_APPLE_CHIP = re.compile(r"\bM(\d{1,2})(?:\s+(Pro|Max|Ultra))?\b", re.I)
_AMD_HINT = re.compile(r"\b(AMD|Radeon|Instinct|MI\d{2,3}[AX]?)\b", re.I)
# named datacenter/workstation NVIDIA parts + explicit vendor words (RTX handled above)
_NVIDIA_HINT = re.compile(
    r"\b(NVIDIA|GeForce|Tesla|Quadro|TITAN|GTX|[AHB]\d{2,3}|L4|L40S?|T4|V100|P100|GH200|GB[23]00)\b",
    re.I)


def _spark_bucket(n):
    return "Single DGX Spark" if n == 1 else f"{n}× DGX Spark"


def _clean_ws(s):
    return re.sub(r"\s+", " ", s).strip()


def normalize_label(label):
    """One detected (or operator-claimed) hardware label -> its canonical bucket.

    Returns {"bucket", "label", "family", "spark_count", "search"}:
      bucket      the canonical cluster name the board groups/filters on
      label       the verbatim row label (never rewritten; 'Unlabeled' when absent)
      family      dgx-spark | nvidia-rtx | nvidia-other | apple | amd | unlabeled
      spark_count Spark node count (int) — None outside the Spark family
      search      lowercase haystack (bucket + label + family + aliases) for live search
    """
    raw = _clean_ws(str(label)) if label else ""

    def out(bucket, family, spark_count=None, extra=""):
        lbl = raw or "Unlabeled"
        hay = " ".join(x for x in (bucket, lbl, family.replace("-", " "), extra) if x)
        return {"bucket": bucket, "label": lbl, "family": family,
                "spark_count": spark_count, "search": _clean_ws(hay.lower())}

    if not raw or _CPU.search(raw):
        # None / '' / 'aarch64 (CPU)' / 'x86_64 (CPU)': CPU labels include the known
        # blind-container mislabel era — shown honestly as Unlabeled until backfilled.
        return out("Unlabeled", FAMILY_UNLABELED, extra="cpu unknown none")

    if _SPARK.search(raw):
        m = _SPARK_MULT.search(raw)
        n = 1                                            # bare 'DGX Spark (GB10)' = one node
        if m:
            n = int(m.group(2)) if m.group(2) else _SPARK_WORDS[m.group(1).lower()]
        return out(_spark_bucket(n), FAMILY_SPARK, spark_count=n,
                   extra=f"dgx spark gb10 nvidia {n}x {n}×")

    if _UNIDENTIFIED.search(raw):
        # blind container on a GPU host: honest count-only label, one shared bucket
        return out("NVIDIA GPU (unidentified)", FAMILY_NVIDIA, extra="nvidia gpu unidentified")

    m = _RTX.search(raw)
    if m:
        model = _clean_ws(m.group(1))
        return out(f"NVIDIA RTX {model}", FAMILY_RTX, extra="nvidia rtx geforce")

    if _APPLE_HINT.search(raw):
        c = _APPLE_CHIP.search(raw)
        if c:
            chip = f"M{c.group(1)}" + (f" {c.group(2).title()}" if c.group(2) else "")
            return out(f"Apple {chip}", FAMILY_APPLE, extra="apple silicon mac metal mlx")
        return out(_clean_ws(_COUNT_PREFIX.sub("", raw)), FAMILY_APPLE,
                   extra="apple silicon mac metal mlx")

    if _AMD_HINT.search(raw) or re.search(r"\bRX\s*\d{3,4}\b", raw, re.I):
        return out(_clean_ws(_COUNT_PREFIX.sub("", raw)), FAMILY_AMD, extra="amd radeon rocm")

    if _NVIDIA_HINT.search(raw):
        # other named NVIDIA parts cluster by their cleaned name: count multiplier stripped,
        # vendor noise dropped (memory kept — on A100/H100 class parts it names the SKU)
        cleaned = _COUNT_PREFIX.sub("", raw)
        cleaned = re.sub(r"\b(NVIDIA|GeForce)\s+", "", cleaned, flags=re.I)
        return out(_clean_ws(cleaned) or raw, FAMILY_NVIDIA, extra="nvidia gpu")

    return out("Unlabeled", FAMILY_UNLABELED, extra="unknown unrecognized")
