"""Build the Prose (Tier-1, all-criteria-shadowed) creativity corpus.

Every rubric criterion carries a tier0_check, so NO model judgment is ever
required: the verdict is a pure function of the candidate text. We self-test
each case by constructing a known-good answer and asserting that the real
aeon.evaluators code produces score==1.0, and (sanity) that a deliberately
broken answer scores <1.0.
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aeon import evaluators as ev  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases_creativity.json")

CASES = []
# (case, good_answer, bad_answer) tuples for self-test
SELFTEST = []


def add(case, good, bad):
    CASES.append(case)
    SELFTEST.append((case, good, bad))


# Convenience builders -------------------------------------------------------

def line_count(n, required=True, qid="lines"):
    return {
        "id": qid,
        "question": f"Does the response have exactly {n} non-empty lines?",
        "decision_rule": f"Count non-empty lines; true iff exactly {n}.",
        "required": required,
        "tier0_check": {"type": "structural_count", "unit": "line", "op": "==", "n": n},
    }


def stanza_count(n, required=True, qid="stanzas"):
    return {
        "id": qid,
        "question": f"Does the response have exactly {n} stanzas (blank-line-separated)?",
        "decision_rule": f"Count blank-line-separated blocks; true iff exactly {n}.",
        "required": required,
        "tier0_check": {"type": "structural_count", "unit": "stanza", "op": "==", "n": n},
    }


def sentence_count(op, n, required=False, qid="sentences"):
    return {
        "id": qid,
        "question": f"Does the response have {op} {n} sentences?",
        "decision_rule": f"Count sentences (split on .!?); true iff count {op} {n}.",
        "required": required,
        "tier0_check": {"type": "structural_count", "unit": "sentence", "op": op, "n": n},
    }


def must_contain(pattern, label, qid, required=False):
    return {
        "id": qid,
        "question": f"Does the response contain {label}?",
        "decision_rule": f"True iff /{pattern}/i matches.",
        "required": required,
        "tier0_check": {"type": "regex_constraint", "pattern": pattern, "mode": "must_match"},
    }


def must_not_contain(pattern, label, qid, required=False):
    # negative polarity: tier0_check 'must_match' finds the forbidden token,
    # polarity flips it so 'absent' == satisfied. (Equivalent to must_not_match;
    # we vary the mechanism across cases for diversity.)
    return {
        "id": qid,
        "question": f"Does the response AVOID {label}?",
        "decision_rule": f"True iff /{pattern}/i is absent.",
        "required": required,
        "polarity": "negative",
        "tier0_check": {"type": "regex_constraint", "pattern": pattern, "mode": "must_match"},
    }


# ============================================================ CASES

# 1. Haiku 5-7-5 — 3 lines, must mention frost, must avoid first person "I"
add(
    {"id": "creativity.haiku.0001", "category": "Prose", "tier": 1,
     "prompt": ("Write a haiku about winter frost. Output ONLY the three lines of the haiku, "
                "one line each, no title, no commentary. The poem MUST contain the word 'frost'. "
                "Do NOT use the standalone word 'I' anywhere."),
     "eval": {"rubric": [
         line_count(3, required=True, qid="lines"),
         must_contain(r"frost", "the word 'frost'", "has_frost", required=True),
         must_not_contain(r"\bI\b", "the standalone pronoun 'I'", "no_first_person"),
     ]}},
    good="Silver frost at dawn\nClinging to the brittle reeds\nMorning breath turns white",
    bad="I love the frost\nIt is cold outside today",
)

# 2. Acrostic poem spelling RAVEN down the first letters — 5 lines
add(
    {"id": "creativity.acrostic.0002", "category": "Prose", "tier": 1,
     "prompt": ("Write a 5-line acrostic poem where the FIRST letter of each line spells RAVEN "
                "(line 1 starts with R, line 2 with A, line 3 with V, line 4 with E, line 5 with N). "
                "Output only the five lines."),
     "eval": {"rubric": [
         line_count(5, required=True, qid="lines"),
         {"id": "acrostic", "question": "Do the first letters of the 5 lines spell RAVEN?",
          "decision_rule": "True iff line1 begins R, line2 A, line3 V, line4 E, line5 N (case-insensitive).",
          "tier0_check": {"type": "regex_constraint",
                          "pattern": r"^\s*R[^\n]*\n\s*A[^\n]*\n\s*V[^\n]*\n\s*E[^\n]*\n\s*N",
                          "mode": "must_match"}},
     ]}},
    good="Restless wings against the grey\nA shadow circling overhead\nVigil kept on a withered branch\nEbony feathers catch the dusk\nNevermore the silence breaks",
    bad="Black bird flying\nAcross the moor\nVery quiet now\nEvening falls\nNight arrives",
)

# 3. Six-word story (the famous constraint) — exactly 6 words, must mention "shoes"
add(
    {"id": "creativity.sixword.0003", "category": "Prose", "tier": 1,
     "prompt": ("Write a six-word story. It MUST be exactly six words on a single line, and it MUST "
                "include the word 'shoes'. Output only the six words."),
     "eval": {"rubric": [
         line_count(1, required=True, qid="one_line"),
         {"id": "six_words", "question": "Is the story exactly six words?",
          "decision_rule": "True iff the line has exactly 6 whitespace-separated word tokens.",
          "required": True,
          "tier0_check": {"type": "regex_constraint",
                          "pattern": r"^\s*\S+(?:\s+\S+){5}\s*$", "mode": "must_match"}},
         must_contain(r"shoes", "the word 'shoes'", "has_shoes", required=True),
     ]}},
    good="For sale: baby shoes, never worn.",
    bad="For sale, baby shoes never used today",
)

# 4. Three-stanza poem, each stanza exactly... total lines == 9, mentions river
add(
    {"id": "creativity.stanza.0004", "category": "Prose", "tier": 1,
     "prompt": ("Write a poem about a river in EXACTLY three stanzas separated by blank lines. "
                "The whole poem must have exactly nine non-empty lines (three lines per stanza). "
                "It must contain the word 'river'. Output only the poem."),
     "eval": {"rubric": [
         stanza_count(3, required=True, qid="stanzas"),
         line_count(9, required=False, qid="nine_lines"),
         must_contain(r"river", "the word 'river'", "has_river", required=True),
     ]}},
    good=("The river wakes before the town\nIts grey skin shivering with light\nCarrying yesterday downstream\n"
          "\nUnder the iron bridge it bends\nGathering the names of drowned things\nAnd letting most of them go\n"
          "\nBy dusk the river is a road\nThat no one walks but everyone follows\nHome to the patient sea"),
    bad="A river flows\nQuietly along",
)

# 5. Limerick — 5 lines, must NOT contain digits, must mention "Nantucket" town not allowed? require "moon"
add(
    {"id": "creativity.limerick.0005", "category": "Prose", "tier": 1,
     "prompt": ("Write a limerick (5 lines) about an astronaut. It must mention the word 'moon'. "
                "Do not use any digits (0-9). Output only the five lines."),
     "eval": {"rubric": [
         line_count(5, required=True, qid="lines"),
         must_contain(r"moon", "the word 'moon'", "has_moon", required=True),
         must_not_contain(r"[0-9]", "any digit", "no_digits"),
     ]}},
    good=("There once was a pilot named June\nWho rocketed up to the moon\nShe planted a flag\nIn the cold lunar slag\n"
          "And floated back home none too soon"),
    bad="An astronaut counted to 3\nThen flew to the moon for tea\nLine three\nLine four\nLine five",
)

# 6. Product blurb — between 2 and 4 sentences, must contain brand "Lumora", must avoid "best"
add(
    {"id": "creativity.blurb.0006", "category": "Prose", "tier": 1,
     "prompt": ("Write a marketing blurb for a desk lamp called Lumora. Use between 2 and 4 sentences. "
                "You MUST mention the brand name 'Lumora'. You must NOT use the word 'best'. "
                "Output only the blurb."),
     "eval": {"rubric": [
         sentence_count(">=", 2, required=True, qid="min_sentences"),
         sentence_count("<=", 4, required=False, qid="max_sentences"),
         must_contain(r"Lumora", "the brand name 'Lumora'", "has_brand", required=True),
         must_not_contain(r"\bbest\b", "the word 'best'", "no_best"),
     ]}},
    good=("Lumora turns any desk into a pool of calm, focused light. "
          "Its warm dimmable glow follows your mood from sunrise drafts to midnight edits. "
          "Folded flat, Lumora slips into a bag as easily as a notebook."),
    bad="Lumora is the best lamp ever made.",
)

# 7. Tongue-twister style: every line must start with the letter S — 4 lines
add(
    {"id": "creativity.alliteration.0007", "category": "Prose", "tier": 1,
     "prompt": ("Write 4 lines of poetry where EVERY line begins with the letter S. "
                "Output only the four lines."),
     "eval": {"rubric": [
         line_count(4, required=True, qid="lines"),
         {"id": "all_S", "question": "Does every one of the 4 lines begin with the letter S?",
          "decision_rule": "True iff lines 1-4 each start (after optional whitespace) with S/s.",
          "required": True,
          "tier0_check": {"type": "regex_constraint",
                          "pattern": r"^\s*S[^\n]*\n\s*S[^\n]*\n\s*S[^\n]*\n\s*S",
                          "mode": "must_match"}},
     ]}},
    good="Silver shadows slip across the snow\nSilent sparrows settle on the sill\nSummer seems a story sleep once told\nStill the season slowly slides away",
    bad="Silver shadows fall\nQuiet sparrows rest\nSummer fades\nSeasons turn",
)

# 8. Couplet count: poem must have an even structure — exactly 6 lines AND contain "and" at least... mention "clock"
add(
    {"id": "creativity.couplets.0008", "category": "Prose", "tier": 1,
     "prompt": ("Write a 6-line poem about an old clock. It must contain the word 'clock' and the word 'time'. "
                "Output only the six lines."),
     "eval": {"rubric": [
         line_count(6, required=True, qid="lines"),
         must_contain(r"clock", "the word 'clock'", "has_clock", required=True),
         must_contain(r"\btime\b", "the word 'time'", "has_time", required=True),
     ]}},
    good=("The old clock leans against the wall\nIts pendulum a tired heart\nIt swallowed years and gave back time\n"
          "In measured coins it could not spend\nEach tick a small and patient theft\nEach chime the sound of letting go"),
    bad="An old clock sits in the hall\nQuiet and slow\nLine three\nLine four\nLine five\nLine six",
)

# 9. Microfiction with mandatory dialogue line and an em-dash; 3 sentences min; mention "letter"
add(
    {"id": "creativity.microfic.0009", "category": "Prose", "tier": 1,
     "prompt": ("Write a piece of microfiction of at least 3 sentences about an unopened letter. "
                "It must contain at least one line of quoted dialogue (text inside double quotes) and "
                "must contain the word 'letter'. Output only the story."),
     "eval": {"rubric": [
         sentence_count(">=", 3, required=True, qid="min_sentences"),
         must_contain(r'"[^"]+"', "at least one double-quoted passage", "has_dialogue", required=True),
         must_contain(r"letter", "the word 'letter'", "has_letter", required=True),
     ]}},
    good=('The letter sat on the table for three days like an accusation. '
          'Mara finally picked it up and turned it over in the lamplight. '
          '"If I never open it," she said, "then it can still mean anything." '
          'She set the letter back down, unopened, and poured the cold tea away.'),
    bad="The letter sat there. Nobody opened it.",
)

# 10. Poem where forbidden letter 'e' never appears (lipogram) — 4 lines, mention "moon"
add(
    {"id": "creativity.lipogram.0010", "category": "Prose", "tier": 1,
     "prompt": ("Write a 4-line poem that NEVER uses the letter 'e' (a lipogram). "
                "It must contain the word 'moon'. Output only the four lines."),
     "eval": {"rubric": [
         line_count(4, required=True, qid="lines"),
         must_not_contain(r"e", "the letter e", "no_e"),  # required handled via combine? keep non-required
         must_contain(r"moon", "the word 'moon'", "has_moon", required=True),
     ]}},
    good="A full moon climbs uncommon high\nIts cold light fills a high dark sky\nNo bird, no wind, no honking cry\nJust moon and night drifting by",
    bad="The moon is bright tonight\nA gentle silver light\nLine three here\nLine four here",
)

# 11. Closed structure: an ABAB-ish poem requiring exactly 4 lines and the word "autumn" capital somewhere? mention season
add(
    {"id": "creativity.seasonal.0011", "category": "Prose", "tier": 1,
     "prompt": ("Write a 4-line poem about autumn. It must contain the word 'leaves' and the word 'gold'. "
                "Output only the four lines."),
     "eval": {"rubric": [
         line_count(4, required=True, qid="lines"),
         must_contain(r"leaves", "the word 'leaves'", "has_leaves", required=True),
         must_contain(r"gold", "the word 'gold'", "has_gold", required=True),
     ]}},
    good="The leaves let go in drifts of gold\nA slow bright surrender to the cold\nThe maples burn without a sound\nAnd lay their treasure on the ground",
    bad="Autumn comes\nThe wind is cool\nLine three\nLine four",
)

# 12. Two-stanza poem, each stanza separated by blank line, must reference a named entity "Orion"
add(
    {"id": "creativity.named.0012", "category": "Prose", "tier": 1,
     "prompt": ("Write a poem in EXACTLY two stanzas (separated by one blank line) about the night sky. "
                "It must name the constellation 'Orion'. Output only the poem."),
     "eval": {"rubric": [
         stanza_count(2, required=True, qid="stanzas"),
         must_contain(r"Orion", "the constellation 'Orion'", "has_orion", required=True),
     ]}},
    good=("The sky unrolls its oldest map\nAnd pins it to the dark with light\nWe trace the hunter as he climbs\n"
          "\nOrion lifts his frozen bow\nAcross the slow wheel of the year\nAnd never lets the arrow go"),
    bad="The stars come out at night\nOrion is up there somewhere bright",
)

# 13. Story opener: must be one sentence, end with a question mark, contain "midnight"
add(
    {"id": "creativity.opener.0013", "category": "Prose", "tier": 1,
     "prompt": ("Write a single-sentence story opening that ends with a question mark and contains the word "
                "'midnight'. Output only the sentence."),
     "eval": {"rubric": [
         {"id": "ends_q", "question": "Does the response end with a question mark?",
          "decision_rule": "True iff the trimmed text ends with '?'.",
          "required": True,
          "tier0_check": {"type": "regex_constraint", "pattern": r"\?\s*$", "mode": "must_match"}},
         must_contain(r"midnight", "the word 'midnight'", "has_midnight", required=True),
         {"id": "no_internal_period", "question": "Is it a single sentence (no internal sentence-ending punctuation)?",
          "decision_rule": "True iff there is no '.', '!' or '?' before the final character.",
          "tier0_check": {"type": "regex_constraint", "pattern": r"[.!?].*\S", "mode": "must_not_match"}},
     ]}},
    good="When the grandfather clock struck midnight and kept on striking, well past twelve, who in that sleeping house would be the first to start counting?",
    bad="It was midnight. Something moved in the dark. What was it?",
)

# 14. Rhyming pair: 2 lines, both must end in words rhyming on -ight (light/night), mention "star"
add(
    {"id": "creativity.rhyme.0014", "category": "Prose", "tier": 1,
     "prompt": ("Write a rhyming couplet (exactly 2 lines) about a star. Line 1 must end with the word 'light' "
                "and line 2 must end with the word 'night'. Output only the two lines."),
     "eval": {"rubric": [
         line_count(2, required=True, qid="lines"),
         {"id": "ends_light_night",
          "question": "Does line 1 end in 'light' and line 2 end in 'night'?",
          "decision_rule": "True iff line1 ends with 'light' and line2 ends with 'night' (ignoring trailing punctuation).",
          "required": True,
          "tier0_check": {"type": "regex_constraint",
                          "pattern": r"light[\s\W]*\n[^\n]*night[\s\W]*$", "mode": "must_match"}},
         must_contain(r"star", "the word 'star'", "has_star", required=True),
     ]}},
    good="A single star let down its silver light,\nAnd stitched a seam across the open night.",
    bad="A star shines bright,\nAll through the dark.",
)

# 15. Numbered list poem: exactly 3 lines, each beginning with a number 1. 2. 3.
add(
    {"id": "creativity.numbered.0015", "category": "Prose", "tier": 1,
     "prompt": ("Write a 3-item numbered list of instructions for brewing tea. Each item on its own line, "
                "starting with '1.', '2.', '3.' respectively. It must mention 'water'. Output only the list."),
     "eval": {"rubric": [
         line_count(3, required=True, qid="lines"),
         {"id": "numbered", "question": "Do the three lines start with 1., 2., 3. in order?",
          "decision_rule": "True iff line1 starts '1.', line2 '2.', line3 '3.'.",
          "required": True,
          "tier0_check": {"type": "regex_constraint",
                          "pattern": r"^\s*1\.[^\n]*\n\s*2\.[^\n]*\n\s*3\.", "mode": "must_match"}},
         must_contain(r"water", "the word 'water'", "has_water", required=True),
     ]}},
    good="1. Heat fresh water until it just begins to boil.\n2. Pour the water over the leaves and steep three minutes.\n3. Strain into a warmed cup and drink slowly.",
    bad="First boil water\nThen add tea\nThen drink",
)

# 16. Dialogue scene: exactly 4 lines, each must be a quoted line, alternating speakers; mention "key"
add(
    {"id": "creativity.dialogue.0016", "category": "Prose", "tier": 1,
     "prompt": ("Write a 4-line dialogue exchange. EVERY line must be wrapped in double quotes. "
                "The conversation must mention a 'key'. Output only the four quoted lines."),
     "eval": {"rubric": [
         line_count(4, required=True, qid="lines"),
         {"id": "all_quoted", "question": "Is every one of the 4 lines wrapped in double quotes?",
          "decision_rule": "True iff each non-empty line both starts and ends with a double quote.",
          "required": True,
          "tier0_check": {"type": "regex_constraint",
                          "pattern": r'^\s*"[^"\n]*"\s*\n\s*"[^"\n]*"\s*\n\s*"[^"\n]*"\s*\n\s*"[^"\n]*"\s*$',
                          "mode": "must_match"}},
         must_contain(r"key", "the word 'key'", "has_key", required=True),
     ]}},
    good='"Did you find the key?"\n"It was under the third flowerpot."\n"Then why are your hands still empty?"\n"Because the key turned to rust the moment I touched it."',
    bad="Did you find the key?\nNo, it was gone.\nWhere?\nUnder the pot.",
)


# ============================================================ SELF-TEST


class _NoJudge:
    """Tier-1 eval requires a judge arg, but every criterion is shadowed,
    so the judge must never be called. If it is, fail loudly."""
    def chat(self, *a, **k):
        raise AssertionError("judge was invoked — a criterion is NOT shadowed!")


def selftest():
    judge = _NoJudge()
    problems = []
    for case, good, bad in SELFTEST:
        cid = case["id"]
        # every criterion must carry a tier0_check (judge-free mandate)
        for cr in case["eval"]["rubric"]:
            if "tier0_check" not in cr:
                problems.append(f"{cid}: criterion {cr['id']} has NO tier0_check")
        # exactly one required structural gate expected (mandate: required structural gate)
        score_good, det_good = ev.evaluate(case, good, judge)
        if score_good != 1.0:
            fails = [c for c in det_good["criteria"] if not c["satisfied"]]
            problems.append(f"{cid}: GOOD answer scored {score_good} (fails: "
                            + "; ".join(f"{c['id']}:{c['evidence']}" for c in fails) + ")")
        score_bad, det_bad = ev.evaluate(case, bad, judge)
        if score_bad >= 1.0:
            problems.append(f"{cid}: BAD answer scored {score_bad} (should be <1.0)")
    return problems


if __name__ == "__main__":
    problems = selftest()
    if problems:
        print("SELF-TEST FAILURES:")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(CASES, f, indent=2, ensure_ascii=False)
    subtypes = sorted({c["id"].split(".")[1] for c in CASES})
    print(f"OK: {len(CASES)} cases written to {OUT}")
    print(f"subtypes: {', '.join(subtypes)}")
