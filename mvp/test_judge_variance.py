"""A correct answer must not be marked wrong because of how it was dressed.

An atomic checker that rejects a right answer over a wrapping asterisk is worse than no checker:
it looks authoritative, and it is silent, so nobody finds out. These are the decorations a model
actually adds when it is being helpful.

THE SAFETY PROPERTY under test: normalisation is applied to the EXPECTED value and the CANDIDATE
identically. That is what makes it impossible to break a case whose correct answer genuinely ends
in a period or contains emphasis — both sides lose it and still compare equal. Half these tests
exist to prove the loosening did NOT go too far: a wrong answer must still be wrong.

Run: python3 mvp/test_judge_variance.py
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.environ.setdefault("AEON_DB", "/tmp/aeon_judge_variance_test.db")

from aeon import evaluators as E   # noqa: E402

fails = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


TRUE = "CfL5wRDdZqB2411"


def m(cand, value=TRUE, **p):
    p.setdefault("value", value)
    return E.chk_exact_match(cand, p)[0]


print("a correct answer survives the clothes a model puts on it")
check("bare", m(TRUE))
check("leading/trailing whitespace", m("\n\n  %s  \n" % TRUE))
check("**bold**", m("**%s**" % TRUE))
check("*italic*", m("*%s*" % TRUE))
check("`code`", m("`%s`" % TRUE))
check("***bold italic***", m("***%s***" % TRUE))
check("'Answer:' prefix", m("Answer: %s" % TRUE))
check("'The final answer is'", m("The final answer is %s" % TRUE))
check("trailing period", m("%s." % TRUE))
check("double quotes", m('"%s"' % TRUE))
check("curly quotes", m("“%s”" % TRUE))
check("fenced block", m("```\n%s\n```" % TRUE))
check("fenced block with language", m("```text\n%s\n```" % TRUE))
check("combined: Answer: **x**.", m("Answer: **%s**." % TRUE))

print("a WRONG answer is still wrong (the loosening must not go too far)")
check("different value", m("CfL5wRDdZqB2412") is False)
check("truncated value", m("CfL5wRDdZqB24") is False)
check("value plus extra content", m("%s and also 99" % TRUE) is False)
check("empty", m("") is False)
check("prose that merely mentions it",
      m("I think the answer might be %s but I am not sure" % TRUE) is False)

print("case sensitivity is still honoured when the case asks for it")
check("ignore_case=False rejects wrong case",
      m(TRUE.lower(), ignore_case=False) is False)
check("ignore_case=False accepts exact case", m(TRUE, ignore_case=False))
check("ignore_case default accepts either", m(TRUE.lower()))

print("SYMMETRY: an expected value that is itself decorated still matches")
check("expected ends in a period, answer does too",
      m("it is sealed.", value="it is sealed."))
check("expected ends in a period, answer does not",
      m("it is sealed", value="it is sealed."))
check("expected is bold, answer is bare", m("hello", value="**hello**"))

print("real answers from the live god suite still grade the same")
SEALED = ("my ledger of letters is sealed: it holds exactly one hundred ninety-eight letters all "
          "told, and among them the letter e occurs exactly thirty-seven times, the letter n "
          "occurs exactly eleven times, and the letter s occurs exactly thirteen times.")
check("prose.03 ground truth matches itself (internal punctuation preserved)",
      m(SEALED, value=SEALED))
check("prose.03 wrong numbers still fail",
      m(SEALED.replace("ninety-eight", "seventy-two"), value=SEALED) is False)
check("instruction.01 model's actual wrong answer still fails",
      m("FLOwRABd3qz1") is False)

print("normalize=False is still a hard literal compare")
check("normalize=False rejects decoration",
      m("**%s**" % TRUE, normalize=False) is False)

print("brace-balanced \\boxed{} extraction")
check("simple", E.extract_boxed(r"so \boxed{42} yes") == "42")
check("nested braces (regex version returned None)",
      E.extract_boxed(r"\boxed{\frac{1}{2}}") == r"\frac{1}{2}")
check("last box wins", E.extract_boxed(r"\boxed{1} then \boxed{2}") == "2")
check("spaced brace", E.extract_boxed(r"\boxed {7}") == "7")
check("<answer> still works", E.extract_boxed("<answer>99</answer>") == "99")
check("unclosed box is not extracted", E.extract_boxed(r"\boxed{oops") is None)
check("no box -> None", E.extract_boxed("just text") is None)


print("whitespace variance (symmetric - cannot break a correct answer that contains it)")
check("double space inside", m("my  ledger", value="my ledger"))
check("tab instead of space", m("my\tledger", value="my ledger"))
check("newline inside a one-line answer", m("my\nledger", value="my ledger"))
check("non-breaking space", m("my\u00a0ledger", value="my ledger"))
check("expected itself has a double space", m("my ledger", value="my  ledger"))

print("typography variance")
check("curly apostrophe", m("don\u2019t stop", value="don't stop"))
check("curly quotes inside", m("he said \u201chi\u201d", value='he said "hi"'))
check("en dash for hyphen", m("twenty\u2013two", value="twenty-two"))
check("expected has the curly form", m("don't stop", value="don\u2019t stop"))

print("extra line entry - answer on the final line after an unwanted preamble")
check("preamble then answer", m("Let me work through it.\n\n%s" % TRUE))
check("reasoning then bare answer", m("step 1\nstep 2\n%s" % TRUE))
check("preamble + decorated answer", m("Here goes:\n**%s**" % TRUE))

print("HALLUCINATION STILL SCORES ZERO (the whole point)")
check("right shape, wrong value", m("CfL5wRDdZqB2412") is False)
check("confident wrong answer", m("The final answer is **CfL5wRDdZqB9999**.") is False)
check("answer buried in a wall of text",
      m("\n".join(["blah"] * 20 + [TRUE] + ["blah"] * 20)) is False)
check("plausible near-miss on prose",
      m(SEALED.replace("eleven", "twelve"), value=SEALED) is False)
check("empty after all normalisation", m("   \n\n  ") is False)

print()
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("all judge-variance tests passed")
