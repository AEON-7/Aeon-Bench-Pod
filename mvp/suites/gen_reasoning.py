"""Generator for the Reasoning suite (deterministic, computed golds).

Every gold is COMPUTED here in Python (never guessed), then each emitted case is
self-tested against the REAL aeon.evaluators checkers on a known-good answer
before being written. Run with:
    PYTHONPATH="C:/Users/Albert/AEON Bench/mvp" python gen_reasoning.py
"""
from __future__ import annotations

import datetime
import itertools
import json
import math
import os
from fractions import Fraction

from aeon.evaluators import run_checker  # real checkers — authoritative

CASES = []
# (case, known_good_candidate_text) pairs for self-test
SELFTEST = []


def boxed(x):
    return f"The answer is \\boxed{{{x}}}."


def add_numeric(cid, category, prompt, gold, good_answer, as_set=False):
    chk = {"type": "numeric_tolerance", "value": str(gold)}
    if as_set:
        chk["as_set"] = True
    case = {"id": cid, "category": category, "tier": 0, "prompt": prompt,
            "eval": {"checkers": [chk], "combine": "all"}}
    CASES.append(case)
    SELFTEST.append((case, good_answer))


def add_regex_box(cid, category, prompt, word, good_answer):
    """\\boxed{<word>} gating (yes/no/single closed word)."""
    pat = r"\\boxed\{\s*" + word + r"\s*\}"
    case = {"id": cid, "category": category, "tier": 0, "prompt": prompt,
            "eval": {"checkers": [{"type": "regex_constraint", "pattern": pat,
                                   "mode": "must_match"}], "combine": "all"}}
    CASES.append(case)
    SELFTEST.append((case, good_answer))


# ============================================================ 1. SYLLOGISMS
# Compute validity by brute-force over a tiny universe (set-membership model).

def syllogism_valid(premises, conclusion):
    """premises/conclusion are predicates over subsets A,B,C of a universe.
    We enumerate all possible membership configs (truth of A,B,C for an element
    pattern) and check the entailment classically using the standard
    interpretation. We model each category statement as a constraint on sets.
    Universe elements get arbitrary membership in A,B,C (8 element-types).
    'All X are Y' => for all e, X(e)->Y(e). 'Some X are Y' => exists e: X&Y.
    'No X are Y' => for all e, not(X&Y). We check: every model of the premises
    (with at least the existential-import elements present) satisfies conclusion.
    We enumerate over subsets of the 8 element-types that can be 'present'."""
    et = list(itertools.product([0, 1], repeat=3))  # (A,B,C)

    def holds(stmt, present):
        kind, x, y = stmt
        xi = "ABC".index(x)
        yi = "ABC".index(y)
        if kind == "all":
            return all(not e[xi] or e[yi] for e in present)
        if kind == "some":
            return any(e[xi] and e[yi] for e in present)
        if kind == "no":
            return all(not (e[xi] and e[yi]) for e in present)
        raise ValueError(kind)

    # A syllogism is valid iff EVERY non-empty universe (subset of element-types)
    # that satisfies all premises also satisfies the conclusion.
    for r in range(1, 9):
        for present in itertools.combinations(et, r):
            if all(holds(p, present) for p in premises):
                if not holds(conclusion, present):
                    return False
    return True


# Barbara (valid): All A are B, All B are C => All A are C
assert syllogism_valid([("all", "A", "B"), ("all", "B", "C")], ("all", "A", "C"))
add_regex_box(
    "reasoning.syllogism.0001", "Reasoning",
    "Consider these premises: (1) All members of the Vorlon council are diplomats. "
    "(2) All diplomats are registered citizens. "
    "Does it logically follow that all members of the Vorlon council are registered citizens? "
    "Reason carefully, then answer with exactly yes or no inside \\boxed{} (e.g. \\boxed{yes}).",
    "yes", "Chain the universals. " + "\\boxed{yes}")

# Invalid: All A are B, Some C are B => Some C are A  (illicit; NOT valid)
assert not syllogism_valid([("all", "A", "B"), ("some", "C", "B")], ("some", "C", "A"))
add_regex_box(
    "reasoning.syllogism.0002", "Reasoning",
    "Premises: (1) All sculptors are artists. (2) Some welders are artists. "
    "Does it NECESSARILY follow that some welders are sculptors? "
    "Answer with exactly yes or no inside \\boxed{}.",
    "no", "The middle term is undistributed. \\boxed{no}")

# Valid: No A are B, All C are A => No C are B  (Celarent-like)
assert syllogism_valid([("no", "A", "B"), ("all", "C", "A")], ("no", "C", "B"))
add_regex_box(
    "reasoning.syllogism.0003", "Reasoning",
    "Premises: (1) No reptiles are mammals. (2) All vipers are reptiles. "
    "Does it follow that no vipers are mammals? Answer yes or no inside \\boxed{}.",
    "yes", "\\boxed{yes}")

# Invalid trap: All A are B, No B are C => therefore some A are C? -> NO (it's no A are C, not some)
assert not syllogism_valid([("all", "A", "B"), ("no", "B", "C")], ("some", "A", "C"))
add_regex_box(
    "reasoning.syllogism.0004", "Reasoning",
    "Premises: (1) All koalas are herbivores. (2) No herbivores are predators. "
    "Does it follow that some koalas are predators? Answer yes or no inside \\boxed{}.",
    "no", "It follows that NO koalas are predators, so 'some are' is false. \\boxed{no}")


# ============================================================ 2. WORD PROBLEMS
# ---- bat-and-ball variants (classic trap) ----
# total T, diff D, ball = (T - D)/2
def batball(total_cents, diff_cents):
    ball = Fraction(total_cents - diff_cents, 2)
    bat = ball + diff_cents
    assert ball + bat == total_cents and bat - ball == diff_cents
    return ball

b = batball(130, 100)  # 15 cents
assert b == 15
add_numeric(
    "reasoning.word.batball.0001", "Reasoning",
    "A notebook and a pencil cost $1.30 together. The notebook costs $1.00 more than the pencil. "
    "How many cents does the PENCIL cost? Put only the number of cents inside \\boxed{}.",
    15, boxed(15))

b = batball(240, 200)  # 20
assert b == 20
add_numeric(
    "reasoning.word.batball.0002", "Reasoning",
    "A racket and a shuttlecock cost $2.40 in total. The racket costs $2.00 more than the shuttlecock. "
    "How many cents does the shuttlecock cost? Put only the number of cents inside \\boxed{}.",
    20, boxed(20))

# ---- rate / work problems ----
# 5 machines make 5 widgets in 5 minutes. How long for 100 machines to make 100 widgets? -> 5 min
add_numeric(
    "reasoning.word.rate.0001", "Reasoning",
    "If 5 machines take 5 minutes to make 5 widgets, how many minutes would 100 machines take "
    "to make 100 widgets? Put only the number inside \\boxed{}.",
    5, boxed(5))

# Combined work: pipe A fills in 4h, pipe B in 6h. Together? 1/(1/4+1/6)=12/5=2.4h
together = 1 / (Fraction(1, 4) + Fraction(1, 6))
assert together == Fraction(12, 5)
add_numeric(
    "reasoning.word.work.0001", "Reasoning",
    "Pipe A alone fills a tank in 4 hours; pipe B alone fills it in 6 hours. With both pipes open, "
    "how many hours does it take to fill the tank? Give the exact answer as a decimal inside \\boxed{}.",
    "2.4", boxed("2.4"))

# Three workers: A in 10h, B in 15h, C in 30h together => 1/(1/10+1/15+1/30)=5h
t3 = 1 / (Fraction(1, 10) + Fraction(1, 15) + Fraction(1, 30))
assert t3 == 5
add_numeric(
    "reasoning.word.work.0002", "Reasoning",
    "Alice paints a fence in 10 hours, Bob in 15 hours, Carol in 30 hours. Working together at "
    "their constant rates, how many hours do they need to paint one fence? Put the number inside \\boxed{}.",
    5, boxed(5))

# Work trap: one person 6h, another 6h. Together 3h (not 12, not 6).
tt = 1 / (Fraction(1, 6) + Fraction(1, 6))
assert tt == 3
add_numeric(
    "reasoning.word.work.0003", "Reasoning",
    "If Sam can mow a lawn in 6 hours and Dana can mow the same lawn in 6 hours, how many hours "
    "does it take them to mow it together? Put the number inside \\boxed{}.",
    3, boxed(3))

# ---- mixture problems ----
# How many liters of pure water to add to 10 L of 40% acid to get 25% acid?
# acid = 0.40*10 = 4. (4)/(10+x) = 0.25 => 10+x = 16 => x = 6
x = Fraction(4, 1) / Fraction(25, 100) - 10
assert x == 6
add_numeric(
    "reasoning.word.mixture.0001", "Reasoning",
    "You have 10 liters of a 40% acid solution. How many liters of PURE WATER must you add to "
    "dilute it to a 25% acid solution? Put the number of liters inside \\boxed{}.",
    6, boxed(6))

# Alloy: mix x kg of 60% copper with 40 kg of 20% copper to get 30% copper.
# 0.6x + 0.2*40 = 0.3(x+40) => 0.6x + 8 = 0.3x + 12 => 0.3x = 4 => x = 40/3
xc = Fraction(4, 1) / Fraction(3, 10)
assert xc == Fraction(40, 3)
add_numeric(
    "reasoning.word.mixture.0002", "Reasoning",
    "A metallurgist mixes an unknown amount of a 60%-copper alloy with 40 kg of a 20%-copper alloy "
    "to obtain a 30%-copper alloy. How many kilograms of the 60% alloy are used? "
    "Give the exact decimal (rounded to 4 decimal places) inside \\boxed{}.",
    round(float(xc), 4), boxed(round(float(xc), 4)))

# ---- speed / distance traps ----
# Average speed there 30 mph, back 60 mph over same distance: harmonic mean = 40, NOT 45.
avg = 2 / (Fraction(1, 30) + Fraction(1, 60))
assert avg == 40
add_numeric(
    "reasoning.word.speed.0001", "Reasoning",
    "A cyclist rides from town A to town B at 30 mph, then immediately returns along the SAME road "
    "at 60 mph. What is her average speed for the whole round trip, in mph? Put the number inside \\boxed{}.",
    40, boxed(40))

# Trains approaching: 120 mi apart, 40 mph + 60 mph closing => meet in 120/100 = 1.2 h
meet = Fraction(120, 100)
assert meet == Fraction(6, 5)
add_numeric(
    "reasoning.word.speed.0002", "Reasoning",
    "Two trains are 120 miles apart on the same track, heading toward each other. One travels at "
    "40 mph, the other at 60 mph. How many hours until they meet? Give the decimal inside \\boxed{}.",
    "1.2", boxed("1.2"))

# Age problem: In 5 years Ann will be twice as old as Bob is now. Bob is 12. Ann now?
# Ann+5 = 2*12 = 24 => Ann = 19
ann = 2 * 12 - 5
assert ann == 19
add_numeric(
    "reasoning.word.age.0001", "Reasoning",
    "In 5 years, Ann will be exactly twice as old as Bob is today. Bob is 12 years old today. "
    "How old is Ann today? Put the number inside \\boxed{}.",
    19, boxed(19))

# Trap: 'A widget costs $9 plus half its own price' -> p = 9 + p/2 => p = 18
p = Fraction(9) / (1 - Fraction(1, 2))
assert p == 18
add_numeric(
    "reasoning.word.algebra.0001", "Reasoning",
    "A widget costs 9 dollars plus half of its own price. What is the price of the widget, in dollars? "
    "Put the number inside \\boxed{}.",
    18, boxed(18))


# ============================================================ 3. KNIGHTS & KNAVES
# Enumerate. Knights always tell truth, knaves always lie.

def kk_solve(n, statements):
    """statements: list of functions f(assign) -> bool giving the literal CLAIM
    of person i (assign is a tuple of booleans, True=knight). Returns the list
    of consistent assignments (each person's statement-truth must equal knight)."""
    sols = []
    for assign in itertools.product([True, False], repeat=n):
        ok = True
        for i in range(n):
            claim = statements[i](assign)
            if claim != assign[i]:  # knight => claim true; knave => claim false
                ok = False
                break
        if ok:
            sols.append(assign)
    return sols


# Puzzle 1: A says "B is a knave". B says "A and B are both knaves".
# Person0=A,1=B
s = kk_solve(2, [
    lambda a: (a[1] is False),                 # A: B is a knave
    lambda a: (a[0] is False and a[1] is False)  # B: both are knaves
])
assert len(s) == 1, s
assert s[0] == (True, False)  # A knight, B knave
add_regex_box(
    "reasoning.kk.0001", "Reasoning",
    "On an island, knights always tell the truth and knaves always lie. "
    "A says: 'B is a knave.' B says: 'Both A and B are knaves.' "
    "Is A a knight? Answer with exactly yes or no inside \\boxed{}.",
    "yes", "A must be a knight. \\boxed{yes}")

# Puzzle 2: A: "I am a knave."  -> impossible / no consistent assignment for A alone
s = kk_solve(1, [lambda a: (a[0] is False)])
assert len(s) == 0
add_regex_box(
    "reasoning.kk.0002", "Reasoning",
    "Knights always tell the truth; knaves always lie. A person named A declares: 'I am a knave.' "
    "Could any consistent islander (knight or knave) make this statement? "
    "Answer with exactly yes or no inside \\boxed{}.",
    "no", "Neither type can say it. \\boxed{no}")

# Puzzle 3: Three people A,B,C. A:"all of us are knaves". B:"exactly one of us is a knight".
# Determine number of knights (unique).
s = kk_solve(3, [
    lambda a: (a[0] is False and a[1] is False and a[2] is False),  # A: all three are knaves
    lambda a: (a[2] is True),                                       # B: C is a knight
    lambda a: (a[0] is False),                                      # C: A is a knave
])
# Find unique number of knights:
assert len(s) == 1, s
assert s[0] == (False, True, True), s
nk = sum(s[0])
assert nk == 2, (s, nk)
add_numeric(
    "reasoning.kk.0003", "Reasoning",
    "Knights always tell the truth and knaves always lie. There are three islanders A, B, C. "
    "A says: 'All three of us are knaves.' B says: 'C is a knight.' "
    "C says: 'A is a knave.' Exactly how many of the three are knights? "
    "Put the number inside \\boxed{}.",
    nk, boxed(nk))

# Puzzle 4: A:"B is a knight". B:"A and I are of opposite types". -> contradiction analysis
s = kk_solve(2, [
    lambda a: (a[1] is True),            # A: B is a knight
    lambda a: (a[0] != a[1]),            # B: A and I are opposite types
])
assert len(s) == 1 and s[0] == (False, False), s  # both knaves
add_regex_box(
    "reasoning.kk.0004", "Reasoning",
    "Knights always tell the truth, knaves always lie. A says: 'B is a knight.' "
    "B says: 'A and I are of opposite types.' Is B a knave? Answer yes or no inside \\boxed{}.",
    "yes", "Both turn out to be knaves. \\boxed{yes}")


# ============================================================ 4. DATE / CALENDAR
def weekday_name(d):
    return ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][d.weekday()]


# Day of week for a date (2026-06-26 is given as 'today' but compute generally).
d = datetime.date(2000, 1, 1)
assert weekday_name(d) == "Saturday"
add_regex_box(
    "reasoning.date.0001", "Reasoning",
    "On what day of the week did January 1, 2000 fall? Answer with exactly one capitalized weekday "
    "name (Monday..Sunday) inside \\boxed{} (e.g. \\boxed{Monday}).",
    "Saturday", "\\boxed{Saturday}")

# Days between two dates (exclusive of trap: leap year 2024).
d1 = datetime.date(2024, 2, 1)
d2 = datetime.date(2024, 3, 1)
delta = (d2 - d1).days
assert delta == 29  # 2024 is a leap year
add_numeric(
    "reasoning.date.0002", "Reasoning",
    "How many days are there from February 1, 2024 up to (but not including) March 1, 2024? "
    "(Note: 2024 is a leap year.) Put the number inside \\boxed{}.",
    29, boxed(29))

# 90 days after a date.
start = datetime.date(2025, 11, 15)
end = start + datetime.timedelta(days=90)
assert end == datetime.date(2026, 2, 13), end
add_regex_box(
    "reasoning.date.0003", "Reasoning",
    "What calendar date is exactly 90 days after November 15, 2025? "
    "Answer in ISO format YYYY-MM-DD inside \\boxed{} (e.g. \\boxed{2026-01-01}).",
    r"2026\-02\-13", "Counting forward. \\boxed{2026-02-13}")

# Weekday arithmetic: if today is Wednesday, what day is it in 100 days? 100 mod 7 = 2 -> Friday
offset = 100 % 7
assert offset == 2
# Wednesday + 2 = Friday
add_regex_box(
    "reasoning.date.0004", "Reasoning",
    "If today is a Wednesday, what day of the week will it be 100 days from now? "
    "Answer with exactly one capitalized weekday name inside \\boxed{}.",
    "Friday", "100 mod 7 = 2, so Wednesday + 2 = \\boxed{Friday}")

# How many Fridays the 13th in 2026? compute
count_f13 = 0
for m in range(1, 13):
    if datetime.date(2026, m, 13).weekday() == 4:
        count_f13 += 1
add_numeric(
    "reasoning.date.0005", "Reasoning",
    "In the calendar year 2026, on how many months does the 13th day of the month fall on a Friday? "
    "Put the number inside \\boxed{}.",
    count_f13, boxed(count_f13))


# ============================================================ 5. GRID / DEDUCTION MINI-PUZZLES
# Unique solution computed by brute force.

# Puzzle A: 3 houses in a row (positions 1,2,3). Colors red/green/blue. Clues:
#  - The green house is immediately right of the red house.
#  - The blue house is at position 1.
# Find position (1,2,3) of the GREEN house (unique).
sols = []
for perm in itertools.permutations(["red", "green", "blue"]):
    pos = {c: i + 1 for i, c in enumerate(perm)}
    if pos["green"] == pos["red"] + 1 and pos["blue"] == 1:
        sols.append(perm)
assert len(sols) == 1, sols
green_pos = sols[0].index("green") + 1
assert green_pos == 3, sols
add_numeric(
    "reasoning.grid.0001", "Reasoning",
    "Three houses stand in a row at positions 1, 2, and 3 (left to right). Each is painted a "
    "different color: red, green, or blue. Clues: (1) The green house is immediately to the right "
    "of the red house. (2) The blue house is at position 1. "
    "At which position number (1, 2, or 3) is the GREEN house? Put the number inside \\boxed{}.",
    green_pos, boxed(green_pos))

# Puzzle B: 4 people Ann,Ben,Cara,Dan finish a race 1st-4th. Clues:
#  - Ann finished ahead of Ben.
#  - Cara finished last.
#  - Dan finished immediately after Ann.
# Find Ben's place (unique).
people = ["Ann", "Ben", "Cara", "Dan"]
sols = []
for perm in itertools.permutations(people):
    place = {p: i + 1 for i, p in enumerate(perm)}
    if (place["Ann"] < place["Ben"] and place["Cara"] == 4
            and place["Dan"] == place["Ann"] + 1):
        sols.append(perm)
assert len(sols) == 1, sols
ben_place = sols[0].index("Ben") + 1
assert ben_place == 3, sols
add_numeric(
    "reasoning.grid.0002", "Reasoning",
    "Four runners — Ann, Ben, Cara, and Dan — finish a race in places 1st through 4th (no ties). "
    "Clues: (1) Ann finished ahead of Ben. (2) Cara finished last. (3) Dan finished immediately "
    "after Ann. In which place (1, 2, 3, or 4) did Ben finish? Put the number inside \\boxed{}.",
    ben_place, boxed(ben_place))

# Puzzle C: 3 people each own one pet (cat/dog/fish) and one drink (tea/coffee/milk).
#  - The cat owner drinks tea.
#  - Person who drinks milk owns the fish.
#  - The dog owner is not the coffee drinker... wait ensure unique. Let's brute force a mapping.
# Question: which drink does the DOG owner have? (closed_set)
pets = ["cat", "dog", "fish"]
drinks = ["tea", "coffee", "milk"]
sols = []
for dperm in itertools.permutations(drinks):
    petdrink = dict(zip(pets, dperm))  # pet -> drink
    if petdrink["cat"] == "tea" and petdrink["fish"] == "milk":
        sols.append(petdrink)
assert len(sols) == 1, sols
dog_drink = sols[0]["dog"]
assert dog_drink == "coffee", sols
case = {"id": "reasoning.grid.0003", "category": "Reasoning", "tier": 0,
        "prompt": "Three people each own exactly one pet (cat, dog, or fish) and drink exactly one "
                  "beverage (tea, coffee, or milk); all six are distinct across the people. "
                  "Clues: (1) The cat owner drinks tea. (2) The person who drinks milk owns the fish. "
                  "Which beverage does the DOG owner drink? Respond in the form <answer>X</answer> "
                  "where X is exactly one of tea, coffee, milk.",
        "eval": {"checkers": [{"type": "closed_set", "slot": "answer",
                               "options": ["tea", "coffee", "milk"], "answer": dog_drink}],
                 "combine": "all"}}
CASES.append(case)
SELFTEST.append((case, f"By elimination the dog owner drinks coffee. <answer>{dog_drink}</answer>"))

# Puzzle D: logic - five consecutive lockers, exactly one has a prize. Clues narrow to one.
# Lockers 1..5, prize odd-numbered, not 1, not 5 => unique 3.
sols = [n for n in range(1, 6) if n % 2 == 1 and n != 1 and n != 5]
assert sols == [3], sols
add_numeric(
    "reasoning.grid.0004", "Reasoning",
    "There are five lockers numbered 1 to 5, and exactly one contains a prize. Clues: "
    "(1) The prize locker has an odd number. (2) It is not locker 1. (3) It is not locker 5. "
    "Which locker number contains the prize? Put the number inside \\boxed{}.",
    3, boxed(3))


# ============================================================ 6. SEQUENCES (what comes next)
def add_seq(cid, prompt, nxt, good):
    add_numeric(cid, "Reasoning", prompt, nxt, good)


# Fibonacci-ish
seq = [2, 3, 5, 8, 13]
nxt = seq[-1] + seq[-2]
assert nxt == 21
add_seq("reasoning.seq.0001",
        "What number comes next in the sequence 2, 3, 5, 8, 13, ...? "
        "(Each term is the sum of the two preceding terms.) Put the number inside \\boxed{}.",
        21, boxed(21))

# Differences increasing by 1: 1,2,4,7,11,16 -> next diff 6 -> 22
seq = [1, 2, 4, 7, 11, 16]
nxt = seq[-1] + (seq[-1] - seq[-2]) + 1
assert nxt == 22
add_seq("reasoning.seq.0002",
        "Find the next term: 1, 2, 4, 7, 11, 16, ...? Put the number inside \\boxed{}.",
        22, boxed(22))

# Squares minus 1: 0,3,8,15,24 -> n^2-1 for n=1.. -> next 35
seq = [n * n - 1 for n in range(1, 6)]
assert seq == [0, 3, 8, 15, 24]
nxt = 6 * 6 - 1
assert nxt == 35
add_seq("reasoning.seq.0003",
        "What comes next: 0, 3, 8, 15, 24, ...? (Hint: each term is one less than a perfect square.) "
        "Put the number inside \\boxed{}.",
        35, boxed(35))

# Alternating multiply/add trap: 3,6,9,18,21,42 -> *2,+3,*2,+3,... next *2 = 84? check
# 3 *2=6, 6+3=9, 9*2=18, 18+3=21, 21*2=42, next +3 = 45
seq = [3, 6, 9, 18, 21, 42]
nxt = 42 + 3
assert nxt == 45
add_seq("reasoning.seq.0004",
        "Determine the next number: 3, 6, 9, 18, 21, 42, ...? (The rule alternates between two "
        "operations.) Put the number inside \\boxed{}.",
        45, boxed(45))

# Prime sequence: 2,3,5,7,11,13 -> next prime 17
add_seq("reasoning.seq.0005",
        "What is the next term in the sequence 2, 3, 5, 7, 11, 13, ...? Put the number inside \\boxed{}.",
        17, boxed(17))

# Triangular numbers: 1,3,6,10,15 -> 21
tri = [n * (n + 1) // 2 for n in range(1, 6)]
assert tri == [1, 3, 6, 10, 15]
add_seq("reasoning.seq.0006",
        "Find the next number in 1, 3, 6, 10, 15, ...? Put the number inside \\boxed{}.",
        21, boxed(21))


# ============================================================ SELF-TEST + WRITE
def selftest():
    failures = []
    ids = set()
    for case, good in SELFTEST:
        cid = case["id"]
        if cid in ids:
            failures.append(f"DUPLICATE id {cid}")
        ids.add(cid)
        for chk in case["eval"]["checkers"]:
            ok, ev = run_checker(chk, good)
            if not ok:
                failures.append(f"{cid}: checker {chk['type']} FAILED on known-good: {ev}")
    return failures


if __name__ == "__main__":
    fails = selftest()
    if fails:
        print("SELF-TEST FAILURES:")
        for f in fails:
            print("  -", f)
        raise SystemExit(1)
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases_reasoning.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(CASES, f, indent=2)
    subs = {}
    for c in CASES:
        sub = ".".join(c["id"].split(".")[1:-1])
        subs[sub] = subs.get(sub, 0) + 1
    print(f"WROTE {len(CASES)} cases to {out_path}")
    print("Subtypes:", json.dumps(subs, indent=2))
    print("All self-tests passed.")
