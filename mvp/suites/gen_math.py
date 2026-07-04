"""Generator for AEON Bench Math (Tier-0) cases.

Every gold answer is COMPUTED in Python so it is provably correct, then each
emitted case is self-tested against the REAL aeon checker on a known-good
candidate ("\\boxed{<gold>}") before being written. Run with:

    PYTHONPATH="C:/Users/Albert/AEON Bench/mvp" python suites/gen_math.py

Notes on the numeric_tolerance checker (see aeon/evaluators.py):
  - extracts the LAST \\boxed{...}; parses numbers with -?\\d+(?:\\.\\d+)?
  - non-set mode: want must be a subset of got AND len(got) <= len(want)+1
    (at most one stray number tolerated).
  - as_set mode: exact set equality of all parsed numbers.
  - Fractions like 3/4 parse as the TWO numbers {3,4}; never use a "/" gold
    with this checker. For exact fractions we ask for numerator,denominator
    as a set, or for a reduced single integer / decimal.
"""
from __future__ import annotations

import json
import math
import os
from fractions import Fraction

from aeon.evaluators import chk_numeric_tolerance, extract_boxed

CASES = []


def _fmt(x):
    """Format a gold number as a clean string (ints stay ints)."""
    if isinstance(x, float) and x.is_integer():
        x = int(x)
    return str(x)


def add_numeric(cid, prompt, gold, as_set=False):
    """Register a numeric_tolerance case and self-verify it."""
    if isinstance(gold, (list, tuple, set)):
        value = ", ".join(_fmt(g) for g in gold)
    else:
        value = _fmt(gold)
    checker = {"type": "numeric_tolerance", "value": value}
    if as_set:
        checker["as_set"] = True
    case = {
        "id": cid,
        "category": "Math",
        "tier": 0,
        "prompt": prompt,
        "eval": {"checkers": [checker], "combine": "all"},
    }
    # --- SELF-TEST: a known-good candidate must pass this checker ---
    good = f"Reasoning... the answer is \\boxed{{{value}}}"
    ok, ev = chk_numeric_tolerance(good, checker)
    assert ok, f"SELF-TEST FAILED for {cid}: value={value!r} ev={ev}"
    # --- SANITY: a deliberately wrong candidate must FAIL ---
    bad_set = chk_numeric_tolerance("\\boxed{999999999}", checker)[0]
    assert not bad_set, f"SANITY FAILED (wrong passes) for {cid}"
    CASES.append(case)


# =====================================================================
# 1. Multi-digit arithmetic (no calculator-trivial magnitudes)
# =====================================================================
add_numeric(
    "math.arith.0001",
    "Compute the exact value of 4873 * 6219. Put the final answer inside \\boxed{}.",
    4873 * 6219,
)
add_numeric(
    "math.arith.0002",
    "Compute 91234 * 8765 - 12345678. Put the final answer inside \\boxed{}.",
    91234 * 8765 - 12345678,
)
add_numeric(
    "math.arith.0003",
    "Evaluate 2^31 - 5^11. Put the final answer inside \\boxed{}.",
    2**31 - 5**11,
)
add_numeric(
    "math.arith.0004",
    "Compute the integer quotient and you need not show the remainder: what is "
    "floor(987654321 / 12345)? Put the final answer inside \\boxed{}.",
    987654321 // 12345,
)
add_numeric(
    "math.arith.0005",
    "Compute 123456789 mod 99991 (the remainder when 123456789 is divided by "
    "99991). Put the final answer inside \\boxed{}.",
    123456789 % 99991,
)

# =====================================================================
# 2. Modular arithmetic / modular exponentiation
# =====================================================================
add_numeric(
    "math.modexp.0001",
    "Compute 7^222 mod 1000 (the last three digits of 7 raised to the 222). "
    "Put the final answer inside \\boxed{}.",
    pow(7, 222, 1000),
)
add_numeric(
    "math.modexp.0002",
    "Compute 3^1000 mod 1009. Put the final answer inside \\boxed{}.",
    pow(3, 1000, 1009),
)
add_numeric(
    "math.modinv.0001",
    "Find the modular inverse of 17 modulo 101, i.e. the unique integer x with "
    "0 <= x < 101 such that 17*x is congruent to 1 modulo 101. "
    "Put the final answer inside \\boxed{}.",
    pow(17, -1, 101),
)
add_numeric(
    "math.crt.0001",
    "Find the smallest positive integer x such that x is congruent to 2 mod 5, "
    "x is congruent to 3 mod 7, and x is congruent to 2 mod 9. "
    "Put the final answer inside \\boxed{}.",
    next(x for x in range(1, 5 * 7 * 9 + 1) if x % 5 == 2 and x % 7 == 3 and x % 9 == 2),
)

# =====================================================================
# 3. gcd / lcm
# =====================================================================
add_numeric(
    "math.gcd.0001",
    "Compute gcd(0461952, 0327600) — the greatest common divisor of 461952 and "
    "327600. Put the final answer inside \\boxed{}.",
    math.gcd(461952, 327600),
)
add_numeric(
    "math.lcm.0001",
    "Compute the least common multiple lcm(420, 1078). "
    "Put the final answer inside \\boxed{}.",
    math.lcm(420, 1078),
)
add_numeric(
    "math.gcd.0002",
    "Compute gcd(2^20 - 1, 2^12 - 1). Put the final answer inside \\boxed{}.",
    math.gcd(2**20 - 1, 2**12 - 1),
)

# =====================================================================
# 4. Base conversion (answers given in plain decimal digits to stay
#    parseable; the work is the conversion)
# =====================================================================
add_numeric(
    "math.base.0001",
    "Convert the binary number 11010110101 to decimal (base 10). "
    "Put the final answer inside \\boxed{} as a base-10 integer.",
    int("11010110101", 2),
)
add_numeric(
    "math.base.0002",
    "Convert the hexadecimal number 0x1F3A7 to decimal (base 10). "
    "Put the final answer inside \\boxed{} as a base-10 integer.",
    int("1F3A7", 16),
)
add_numeric(
    "math.base.0003",
    "Convert the base-7 number 6541 (written in base 7) to decimal (base 10). "
    "Put the final answer inside \\boxed{} as a base-10 integer.",
    int("6541", 7),
)
def _to_base(n, b):
    if n == 0:
        return "0"
    digits = []
    while n:
        digits.append(str(n % b))
        n //= b
    return "".join(reversed(digits))


add_numeric(
    "math.base.0004",
    "Write the decimal number 2026 in base 3. Take that base-3 representation as "
    "a literal string of digits and read it as a base-10 integer. "
    "Put the final answer inside \\boxed{}.",
    int(_to_base(2026, 3)),
)

# =====================================================================
# 5. Percentages / compound interest
# =====================================================================
add_numeric(
    "math.pct.0001",
    "A price is increased by 25% and then the new price is decreased by 20%. "
    "If the original price was 480 dollars, what is the final price in dollars? "
    "Put the final answer inside \\boxed{}.",
    480 * 1.25 * 0.80,
)
add_numeric(
    "math.pct.0002",
    "What is 37.5% of 1024? Put the final answer inside \\boxed{}.",
    0.375 * 1024,
)
# Compound interest: 1000 at 8% annual, compounded quarterly, 3 years.
_amt = round(1000 * (1 + 0.08 / 4) ** (4 * 3), 2)
add_numeric(
    "math.interest.0001",
    "You invest 1000 dollars at a nominal annual interest rate of 8%, compounded "
    "quarterly, for 3 years. What is the final amount in dollars, rounded to the "
    "nearest cent (two decimal places)? Put the final answer inside \\boxed{}.",
    _amt,
)
# Continuous-ish but kept discrete: monthly compounding
_amt2 = round(2500 * (1 + 0.06 / 12) ** (12 * 5), 2)
add_numeric(
    "math.interest.0002",
    "You deposit 2500 dollars at a nominal annual rate of 6%, compounded monthly, "
    "for 5 years. Give the final balance in dollars rounded to the nearest cent. "
    "Put the final answer inside \\boxed{}.",
    _amt2,
)

# =====================================================================
# 6. Linear equations
# =====================================================================
add_numeric(
    "math.linear.0001",
    "Solve for x: 7x - 19 = 3x + 53. Put the final answer inside \\boxed{}.",
    (53 + 19) / (7 - 3),
)
# 2x2 system, solve for the product x*y to make it a single number.
# 3x + 2y = 16 ; 5x - y = 9  ->  x=2, y=5  -> ask for x then y? give as set.
import numpy as _np  # noqa: E402

_A = _np.array([[3.0, 2.0], [5.0, -1.0]])
_b = _np.array([16.0, 9.0])
_sol = _np.linalg.solve(_A, _b)
_xy = (round(_sol[0]), round(_sol[1]))
add_numeric(
    "math.linear.0002",
    "Solve the system of equations  3x + 2y = 16  and  5x - y = 9. "
    "Put the solution inside \\boxed{} as the value of x followed by the value "
    "of y, comma-separated (x, y).",
    list(_xy),
    as_set=True,
)

# =====================================================================
# 7. Quadratic equations (real integer roots -> as_set)
# =====================================================================
def _quad_roots(a, b, c):
    disc = b * b - 4 * a * c
    s = math.isqrt(disc)
    assert s * s == disc, "non-perfect-square disc; pick nicer coeffs"
    r1 = Fraction(-b + s, 2 * a)
    r2 = Fraction(-b - s, 2 * a)
    # keep integers as ints for clean formatting
    def conv(r):
        return int(r) if r.denominator == 1 else float(r)
    return sorted({conv(r1), conv(r2)})


_r = _quad_roots(1, -7, -18)  # x^2 -7x -18 = (x-9)(x+2) -> 9, -2
add_numeric(
    "math.quad.0001",
    "Solve x^2 - 7x - 18 = 0. Put the two roots inside \\boxed{} as a "
    "comma-separated list.",
    _r,
    as_set=True,
)
_r2 = _quad_roots(2, -1, -15)  # 2x^2 - x - 15 = (2x+5)(x-3) -> 3, -5/2
add_numeric(
    "math.quad.0002",
    "Solve 2x^2 - x - 15 = 0. The roots are rational; one is an integer and one "
    "is a fraction. Put both roots inside \\boxed{} as a comma-separated list "
    "(write the fraction as a decimal).",
    _r2,
    as_set=True,
)
_r3 = _quad_roots(1, 0, -529)  # x^2 = 529 -> 23, -23
add_numeric(
    "math.quad.0003",
    "Solve x^2 = 529. Put both solutions inside \\boxed{} as a comma-separated "
    "list.",
    _r3,
    as_set=True,
)

# =====================================================================
# 8. Sequences (nth term)
# =====================================================================
add_numeric(
    "math.seq.0001",
    "An arithmetic sequence has first term a_1 = 7 and common difference d = 4. "
    "What is the 100th term a_100? Put the final answer inside \\boxed{}.",
    7 + (100 - 1) * 4,
)
add_numeric(
    "math.seq.0002",
    "A geometric sequence has first term 3 and common ratio 2. What is the sum of "
    "the first 12 terms? Put the final answer inside \\boxed{}.",
    3 * (2**12 - 1) // (2 - 1),
)


def _fib(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a


add_numeric(
    "math.seq.0003",
    "Let F(1)=1, F(2)=1, and F(n)=F(n-1)+F(n-2). Compute F(40), the 40th "
    "Fibonacci number. Put the final answer inside \\boxed{}.",
    _fib(40),
)
# Sum of an arithmetic series
add_numeric(
    "math.seq.0004",
    "Compute the sum of all multiples of 3 between 1 and 1000 inclusive. "
    "Put the final answer inside \\boxed{}.",
    sum(k for k in range(1, 1001) if k % 3 == 0),
)

# =====================================================================
# 9. Combinatorics / probability (exact reduced integers or as_set fracs)
# =====================================================================
add_numeric(
    "math.comb.0001",
    "How many distinct ways are there to choose a committee of 5 people from a "
    "group of 18? Put the final answer inside \\boxed{}.",
    math.comb(18, 5),
)
add_numeric(
    "math.comb.0002",
    "How many distinct arrangements are there of all the letters of the word "
    "MISSISSIPPI? Put the final answer inside \\boxed{}.",
    math.factorial(11) // (math.factorial(4) * math.factorial(4) * math.factorial(2)),
)
add_numeric(
    "math.comb.0003",
    "In how many ways can 8 distinct books be arranged on a shelf if 3 particular "
    "books must all be together (treated as one block, and the 3 may be ordered "
    "among themselves)? Put the final answer inside \\boxed{}.",
    math.factorial(6) * math.factorial(3),
)
# Probability as an exact reduced fraction -> ask as numerator, denominator set.
# P(exactly 2 heads in 5 fair coin flips) = C(5,2)/2^5 = 10/32 = 5/16.
_p = Fraction(math.comb(5, 2), 2**5)
add_numeric(
    "math.prob.0001",
    "A fair coin is flipped 5 times. The probability of getting exactly 2 heads "
    "is a fraction p/q in lowest terms. Put your answer inside \\boxed{} as "
    "\"p, q\" (numerator, denominator), comma-separated and fully reduced.",
    [_p.numerator, _p.denominator],
    as_set=True,
)
# Probability: drawing 2 aces from a standard 52-card deck without replacement.
_p2 = Fraction(math.comb(4, 2), math.comb(52, 2))  # 6/1326 = 1/221
add_numeric(
    "math.prob.0002",
    "Two cards are drawn at random without replacement from a standard 52-card "
    "deck. The probability that both are aces is a fraction p/q in lowest terms. "
    "Put your answer inside \\boxed{} as \"p, q\" (numerator, denominator), "
    "comma-separated and fully reduced.",
    [_p2.numerator, _p2.denominator],
    as_set=True,
)

# =====================================================================
# 10. Number theory (primes, divisors, totient)
# =====================================================================
def _is_prime(n):
    if n < 2:
        return False
    if n % 2 == 0:
        return n == 2
    i = 3
    while i * i <= n:
        if n % i == 0:
            return False
        i += 2
    return True


def _nth_prime(k):
    c, n = 0, 1
    while c < k:
        n += 1
        if _is_prime(n):
            c += 1
    return n


add_numeric(
    "math.nt.prime.0001",
    "What is the 100th prime number? (The 1st prime is 2.) "
    "Put the final answer inside \\boxed{}.",
    _nth_prime(100),
)


def _num_divisors(n):
    cnt, i = 0, 1
    while i * i <= n:
        if n % i == 0:
            cnt += 2 if i * i != n else 1
        i += 1
    return cnt


add_numeric(
    "math.nt.div.0001",
    "How many positive divisors does 360 have? Put the final answer inside "
    "\\boxed{}.",
    _num_divisors(360),
)
add_numeric(
    "math.nt.div.0002",
    "Compute the sum of all positive divisors of 496 (including 1 and 496). "
    "Put the final answer inside \\boxed{}.",
    sum(d for d in range(1, 497) if 496 % d == 0),
)


def _totient(n):
    result, p, m = n, 2, n
    while p * p <= m:
        if m % p == 0:
            while m % p == 0:
                m //= p
            result -= result // p
        p += 1
    if m > 1:
        result -= result // m
    return result


add_numeric(
    "math.nt.totient.0001",
    "Compute Euler's totient phi(360): the number of integers from 1 to 360 that "
    "are coprime to 360. Put the final answer inside \\boxed{}.",
    _totient(360),
)
# Largest prime factor
def _largest_prime_factor(n):
    largest = 1
    d = 2
    while d * d <= n:
        while n % d == 0:
            largest = d
            n //= d
        d += 1
    if n > 1:
        largest = n
    return largest


add_numeric(
    "math.nt.factor.0001",
    "What is the largest prime factor of 600851475143? "
    "Put the final answer inside \\boxed{}.",
    _largest_prime_factor(600851475143),
)

# =====================================================================
# 11. A couple of harder mixed / trap items
# =====================================================================
# Digit-sum trap (answer is small but the path is long)
add_numeric(
    "math.digits.0001",
    "Let N = 2^100. What is the sum of the decimal digits of N? "
    "Put the final answer inside \\boxed{}.",
    sum(int(c) for c in str(2**100)),
)
# Factorial trailing zeros
add_numeric(
    "math.nt.trailzeros.0001",
    "How many trailing zeros does 100! (100 factorial) have? "
    "Put the final answer inside \\boxed{}.",
    sum(100 // 5**k for k in range(1, 4)),
)


# =====================================================================
# WRITE + FINAL VERIFICATION PASS
# =====================================================================
def main():
    # Re-run every checker against its known-good candidate one more time,
    # and assert id uniqueness.
    seen = set()
    for c in CASES:
        cid = c["id"]
        assert cid not in seen, f"duplicate id {cid}"
        seen.add(cid)
        for chk in c["eval"]["checkers"]:
            assert chk["type"] == "numeric_tolerance"
            good = f"\\boxed{{{chk['value']}}}"
            ok, ev = chk_numeric_tolerance(good, chk)
            assert ok, f"FINAL VERIFY FAILED {cid}: {ev}"
            # extra: confirm extraction is non-None
            assert extract_boxed(good) is not None

    out_path = os.path.join(os.path.dirname(__file__), "cases_math.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(CASES, f, indent=2, ensure_ascii=False)

    subtypes = sorted({".".join(c["id"].split(".")[1:-1]) for c in CASES})
    print(f"WROTE {len(CASES)} cases to {out_path}")
    print(f"SUBTYPES ({len(subtypes)}): {subtypes}")
    # Print 2 sample cases
    print("\n--- SAMPLE 1 ---")
    print(json.dumps(CASES[0], indent=2))
    print("\n--- SAMPLE 2 ---")
    # pick a representative as_set probability case
    samp = next(c for c in CASES if c["id"] == "math.prob.0001")
    print(json.dumps(samp, indent=2))
    return CASES


if __name__ == "__main__":
    main()
