"""Generated-artifact arena (DESIGN §12).

Same prompt -> many models -> a single self-contained HTML artifact each. The
artifacts are rendered side-by-side (in SANDBOXED iframes, client-side) and ranked
by HUMAN blind A/B votes (Elo). The tasks are deliberately the kind a strong model
clears easily but a weak one fumbles, so the human vote is a real signal of quality.

Security: model HTML is untrusted. It is stored verbatim and only ever rendered in
an iframe with `sandbox="allow-scripts"` and NO `allow-same-origin` (opaque origin,
no access to the parent, cookies, or storage) — see web/app.js.
"""
from __future__ import annotations

import itertools
import json
import os
import random
import re
import secrets
import uuid

from . import db
from .targets import MockTarget, OpenAITarget

# Mothership-only integrity internals (honeypot decoys, spot-check policy, voter-trust
# threshold). NOT part of the public pod distribution: a pod runs a no-honeypot arena
# (it has no evaluator accounts), and every call site below degrades cleanly.
try:
    from . import arena_trust as _trust
except ImportError:
    _trust = None

KINDS = ["app", "game", "animation"]
KIND_LABEL = {"app": "Generated Apps", "game": "Generated Games", "animation": "Generated Animations"}

# Minimum number of DISTINCT eligible voters that must weigh in on a (prompt, model-pair)
# before that matchup's votes are allowed to move Elo. At quorum 2 a lone account's votes
# stay inert until a second evaluator corroborates — but with a small evaluator community
# that leaves the WHOLE arena unrated (a trusted evaluator casts 100+ honeypot-verified
# votes and sees nothing move), and its marginal security is thin: a determined attacker
# just runs a second account. Default 1 — the honeypot trust gate, per-(prompt,pair)
# dedup, per-IP account caps and admin moderation carry the integrity; raise via
# AEON_QUORUM_VOTERS as the evaluator population grows.
QUORUM_VOTERS = max(1, int(os.environ.get("AEON_QUORUM_VOTERS", "1")))

SYS = (
    "You are an expert front-end engineer. Respond with ONE complete, self-contained "
    "HTML document and NOTHING else — no explanation, no commentary, no markdown code "
    "fences. Put ALL CSS and JavaScript inline. Use NO external resources, CDNs, imports, "
    "fonts, or network calls of any kind; it must run fully offline from this single file. "
    "Begin the response with <!DOCTYPE html>."
)

# Each prompt: a clear, fair task — challenging but easily within reach of a strong model.
_BUILTIN_PROMPTS = {
    "app": [
        {"id": "app.tip", "title": "Tip calculator",
         "brief": "Bill + tip% (with quick-pick buttons) + split, live per-person total.",
         "prompt": "Build a tip calculator as a single self-contained HTML file. Inputs: bill amount, "
                   "tip percentage with quick-pick buttons (10/15/18/20/25%), and number of people to split. "
                   "Show the tip total, grand total, and amount per person, all updating live as inputs change. "
                   "Make it clean and usable."},
        {"id": "app.todo", "title": "To-do list",
         "brief": "Add / complete / delete tasks, filter all·active·done, count remaining, persists.",
         "prompt": "Build a to-do list app as a single self-contained HTML file. Users can add a task, mark it "
                   "complete (with a strikethrough), and delete it. Include filter buttons for All / Active / Done, "
                   "a counter of remaining tasks, and persist tasks in localStorage so they survive a reload. "
                   "Make it look polished."},
        {"id": "app.markdown", "title": "Markdown previewer",
         "brief": "Type Markdown on the left, see live rendered HTML on the right.",
         "prompt": "Build a live Markdown previewer as a single self-contained HTML file: a textarea on the left "
                   "and a live-rendered preview on the right. Support headings (#..######), bold, italics, inline "
                   "code, code blocks, links, unordered and ordered lists, and blockquotes. Write the small Markdown "
                   "parser yourself — no external libraries. Start with some sample Markdown loaded."},
    ],
    "game": [
        {"id": "game.snake", "title": "Snake",
         "brief": "Arrow-key snake on a canvas: food, growth, score, game-over + restart.",
         "prompt": "Build the classic Snake game as a single self-contained HTML file on a <canvas>. Arrow keys "
                   "steer; the snake grows when it eats food; show the score; on collision show Game Over and allow "
                   "restart with a key press. Keep the controls responsive and the speed playable."},
        {"id": "game.breakout", "title": "Breakout",
         "brief": "Paddle + ball + brick rows, score, win and lose states.",
         "prompt": "Build a Breakout / brick-breaker game as a single self-contained HTML file on a <canvas>. A "
                   "paddle at the bottom (moved with the mouse or arrow keys) bounces a ball into rows of colored "
                   "bricks that disappear when hit. Track score, handle losing the ball (lives) and clearing all "
                   "bricks (win), and allow restart."},
        {"id": "game.memory", "title": "Memory match",
         "brief": "Grid of cards, flip to find matching pairs, move counter, win state.",
         "prompt": "Build a memory matching game as a single self-contained HTML file: a 4x4 grid of face-down "
                   "cards hiding 8 pairs of symbols. Clicking flips a card; two matching cards stay face-up, a "
                   "mismatch flips back after a short delay. Count moves, detect when all pairs are found (win "
                   "message), and offer a new game that reshuffles."},
    ],
    "animation": [
        {"id": "anim.balls", "title": "Bouncing balls",
         "brief": "Several balls, gravity, wall bounce, fading motion trails.",
         "prompt": "Create a canvas animation as a single self-contained HTML file: a dozen colorful balls of "
                   "varying sizes bouncing inside the window with gravity and energy loss on wall collisions, each "
                   "leaving a soft fading trail. It should fill the window and look lively and smooth."},
        {"id": "anim.starfield", "title": "Starfield warp",
         "brief": "Stars streaming toward the viewer with depth and parallax.",
         "prompt": "Create a 'warp speed' starfield as a single self-contained HTML file on a full-window <canvas>: "
                   "stars stream outward from the center toward the viewer with a sense of depth and acceleration, "
                   "nearer stars moving faster and appearing as short streaks. Smooth 60fps feel."},
        {"id": "anim.boids", "title": "Boids flocking",
         "brief": "~80 agents with separation, alignment, cohesion — emergent flocking.",
         "prompt": "Create a boids flocking simulation as a single self-contained HTML file on a full-window "
                   "<canvas>: around 80 triangular agents that move with the three classic rules — separation, "
                   "alignment, and cohesion — producing emergent flocking. They wrap around the edges and point in "
                   "their direction of travel."},
    ],
}


# The large, diverse arena corpus (generated + screened) lives in
# suites/arena_prompts.json (a flat array of {kind,id,title,brief,prompt}). It is
# folded on top of the built-ins; a missing/malformed file degrades to built-ins.
_PROMPTS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "suites", "arena_prompts.json")
_PROMPT_REQ = ("kind", "id", "title", "brief", "prompt")


def _load_prompts():
    out = {k: list(v) for k, v in _BUILTIN_PROMPTS.items()}
    seen = {p["id"] for items in out.values() for p in items}
    try:
        if os.path.exists(_PROMPTS_FILE):
            with open(_PROMPTS_FILE, "r", encoding="utf-8") as f:
                extra = json.load(f)
            for p in extra:
                if not isinstance(p, dict) or not all(k in p for k in _PROMPT_REQ):
                    continue
                if p["kind"] not in out or p["id"] in seen:
                    continue
                seen.add(p["id"])
                entry = {k: p[k] for k in ("id", "title", "brief", "prompt")}
                if p.get("agent_only"):
                    entry["agent_only"] = True   # gallery/match group only — never the chat-generation pool
                out[p["kind"]].append(entry)
    except Exception:
        pass  # never let a bad corpus file break the arena — fall back to built-ins
    return out


PROMPTS = _load_prompts()


def all_prompts():
    out = []
    for kind, items in PROMPTS.items():
        for p in items:
            out.append({"kind": kind, "id": p["id"], "title": p["title"], "brief": p["brief"]})
    return out


def find_prompt(kind, prompt_id):
    for p in PROMPTS.get(kind, []):
        if p["id"] == prompt_id:
            return p
    return None


_FENCE = re.compile(r"```(?:html|HTML)?\s*\n?(.*?)```", re.S)


def extract_html(text):
    """Pull a clean HTML document out of a model response (fenced or raw, with
    or without leading prose)."""
    if not text:
        return ""
    m = _FENCE.search(text)
    if m:
        text = m.group(1)
    low = text.lower()
    for marker in ("<!doctype html", "<html"):
        i = low.find(marker)
        if i != -1:
            text = text[i:]
            break
    # trim trailing prose after the closing html tag
    end = text.lower().rfind("</html>")
    if end != -1:
        text = text[: end + len("</html>")]
    return text.strip()


def generate_artifact(kind, prompt_id, model, target_url, api_key=None, params=None):
    """Ask one model for the artifact, store it. Returns a summary dict."""
    p = find_prompt(kind, prompt_id)
    if not p:
        raise ValueError(f"unknown prompt {kind}/{prompt_id}")
    params = params or {"temperature": 0.4, "max_tokens": 8000}
    target = MockTarget(model) if target_url == "mock" else OpenAITarget(
        target_url, model, api_key=api_key, timeout=600)
    msgs = [{"role": "system", "content": SYS}, {"role": "user", "content": p["prompt"]}]
    resp = target.chat(msgs, temperature=params["temperature"], max_tokens=params["max_tokens"])
    html = extract_html(resp.get("text", ""))
    ok = bool(html.strip()) and "<" in html
    aid = uuid.uuid4().hex[:10]
    # the model name is rendered in the (non-sandboxed) ranking UI — never store markup
    stored_model = re.sub(r"[<>\"'`]", "", model or "")[:80]
    db.save_artifact(aid, kind=kind, prompt_id=prompt_id, model=stored_model, html=html,
                     ok=ok, gen_ms=resp.get("e2e_ms"))
    return {"id": aid, "kind": kind, "prompt_id": prompt_id, "model": stored_model,
            "ok": ok, "bytes": len(html), "gen_ms": resp.get("e2e_ms")}


def _eligible_votes(kind=None):
    """The trust-filtered vote stream EVERY rating replays — the per-model ranking()
    and the per-artifact gallery rating share this so the gallery can never rank on a
    weaker filter. Only votes from evaluators whose integrity record currently clears
    the trust bar count (mothership policy; a pod's local arena has no voters and
    counts all of its — typically zero — votes)."""
    if _trust:
        eligible = _trust.eligible_users(db.honeypot_accuracy())
        votes = [v for v in db.real_votes(kind) if v.get("user_id") in eligible]
    else:
        votes = list(db.real_votes(kind))
    # Cap ballot-stuffing: collapse repeated votes by the same user on the same
    # (prompt, unordered model pair) to a single latest observation, so one account
    # cannot re-vote one pairing to steer the Elo. real_votes is ts-ordered.
    latest = {}
    for v in votes:
        a, b = v.get("a_model"), v.get("b_model")
        # model identity is case-insensitive here too — the same (prompt, pairing) under
        # different submitted casings must collapse to ONE dedup/quorum key
        if not a or not b or a.lower() == b.lower():
            continue
        latest[(v.get("user_id"), v.get("prompt_id"), frozenset((a.lower(), b.lower())))] = v
    # Per-user influence cap (single-account Elo steering): the dedup above already limits
    # a user to ONE vote per (prompt, pairing), but one account could still cast a biased
    # vote on every DISTINCT pairing at full K. Require a QUORUM of distinct eligible
    # voters on a (prompt, pairing) before ANY of its votes move Elo — so no lone account
    # can steer a matchup, while genuine multi-voter consensus counts normally. The
    # honeypot-accuracy eligibility gate above still applies (only eligible voters count
    # toward the quorum).
    voters_per_key = {}
    for (uid, pid, pair) in latest:
        voters_per_key.setdefault((pid, pair), set()).add(uid)
    latest = {k: v for k, v in latest.items()
              if len(voters_per_key[(k[1], k[2])]) >= QUORUM_VOTERS}
    return sorted(latest.values(), key=lambda v: v.get("ts") or 0)


def ranking(kind=None):
    """Elo + W/L/T per model from the human votes (replayed in time order). Vote
    eligibility — the honeypot trust gate, ballot-stuffing dedup and voter quorum —
    lives in _eligible_votes, shared with the gallery's per-artifact rating."""
    votes = _eligible_votes(kind)
    elo, rec, disp = {}, {}, {}

    # Model identity is CASE-INSENSITIVE: artifacts submitted at different times carry the
    # display casing ('AEON-7/…') or the lowercased canonical ('aeon-7/…') for the SAME
    # model — keying on the raw string split one model's record in two. Votes replay in ts
    # order, so the most recent casing wins the display name.
    def _k(m):
        return (m or "").lower()

    def seen(m):
        k = _k(m)
        elo.setdefault(k, 1000.0)
        rec.setdefault(k, {"w": 0, "l": 0, "t": 0, "games": 0})
        disp[k] = m

    K = 24
    for v in votes:
        a, b, w = v["a_model"], v["b_model"], v["winner"]
        if not a or not b or _k(a) == _k(b):
            continue
        seen(a); seen(b)
        ka, kb = _k(a), _k(b)
        Ra, Rb = elo[ka], elo[kb]
        Ea = 1 / (1 + 10 ** ((Rb - Ra) / 400))
        Sa = 1.0 if w == "a" else (0.0 if w == "b" else 0.5)
        elo[ka] = Ra + K * (Sa - Ea)
        elo[kb] = Rb + K * ((1 - Sa) - (1 - Ea))
        rec[ka]["games"] += 1; rec[kb]["games"] += 1
        if w == "a":
            rec[ka]["w"] += 1; rec[kb]["l"] += 1
        elif w == "b":
            rec[kb]["w"] += 1; rec[ka]["l"] += 1
        else:
            rec[ka]["t"] += 1; rec[kb]["t"] += 1

    rows = []
    for k, r in rec.items():
        rows.append({**r, "model": disp[k], "elo": round(elo[k]),
                     "win_rate": round(100 * r["w"] / r["games"], 1) if r["games"] else 0.0})
    rows.sort(key=lambda x: -x["elo"])
    return rows


# ---- public Code Gallery (top-rated artifacts per prompt, per kind) ----

GALLERY_TOP_N = 10
# A young prompt with almost no counted votes still deserves content: pad it with its
# newest UNRATED artifacts (flagged "unrated") only while it has fewer rated than this.
GALLERY_MIN_RATED = 3


def artifact_ratings(kind):
    """Per-ARTIFACT Elo + W/L/T by replaying the SAME eligible vote stream ranking()
    uses (honeypot trust gate, ballot-stuffing dedup, voter quorum — _eligible_votes),
    with a_id/b_id as the two players instead of the model names. Honeypot decoys can
    never earn a rating: they exist only in is_test matches (already excluded from the
    stream) and are dropped by id here as defense-in-depth.
    Returns {artifact_id: {"elo": float, "w": int, "l": int, "t": int, "votes": int}}."""
    bogus_ids = {x["id"] for x in db.list_bogus(kind)}
    out = {}

    def seen(aid):
        out.setdefault(aid, {"elo": 1000.0, "w": 0, "l": 0, "t": 0, "votes": 0})

    K = 24
    for v in _eligible_votes(kind):
        a, b, w = v.get("a_id"), v.get("b_id"), v.get("winner")
        if not a or not b or a == b or a in bogus_ids or b in bogus_ids:
            continue
        seen(a); seen(b)
        Ra, Rb = out[a]["elo"], out[b]["elo"]
        Ea = 1 / (1 + 10 ** ((Rb - Ra) / 400))
        Sa = 1.0 if w == "a" else (0.0 if w == "b" else 0.5)
        out[a]["elo"] = Ra + K * (Sa - Ea)
        out[b]["elo"] = Rb + K * ((1 - Sa) - (1 - Ea))
        out[a]["votes"] += 1; out[b]["votes"] += 1
        if w == "a":
            out[a]["w"] += 1; out[b]["l"] += 1
        elif w == "b":
            out[b]["w"] += 1; out[a]["l"] += 1
        else:
            out[a]["t"] += 1; out[b]["t"] += 1
    return out


def gallery(kind):
    """The Code Gallery clusters for one kind: for each prompt (corpus order), the top
    GALLERY_TOP_N ok, non-bogus artifacts by (elo desc, votes desc). Young prompts with
    fewer than GALLERY_MIN_RATED rated artifacts are padded with their newest unrated
    ones, flagged "unrated" so the UI can say so. Prompts with no artifacts at all are
    skipped. Metadata only — bodies stay behind the sandboxed render/download routes."""
    ratings = artifact_ratings(kind)
    by_prompt = _real_by_prompt(kind)          # ok=1 only, bogus already excluded
    out = []
    for p in PROMPTS.get(kind, []):
        arts = by_prompt.get(p["id"]) or []
        rated = sorted((a for a in arts if a["id"] in ratings),
                       key=lambda a: (-ratings[a["id"]]["elo"], -ratings[a["id"]]["votes"]))
        top = rated[:GALLERY_TOP_N]
        if len(rated) < GALLERY_MIN_RATED:     # arts is created_at DESC → newest first
            top += [a for a in arts if a["id"] not in ratings][:GALLERY_TOP_N - len(top)]
        if not top:
            continue
        items = []
        for a in top:
            r = ratings.get(a["id"])
            it = {"id": a["id"], "model": a["model"], "bytes": a.get("bytes"),
                  "gen_ms": a.get("gen_ms"), "created_at": a.get("created_at"),
                  "elo": round(r["elo"]) if r else None,
                  "w": r["w"] if r else 0, "l": r["l"] if r else 0,
                  "t": r["t"] if r else 0, "votes": r["votes"] if r else 0}
            if not r:
                it["unrated"] = True
            items.append(it)
        out.append({"id": p["id"], "title": p["title"], "brief": p["brief"], "artifacts": items})
    return out


# ---- seeded demo artifacts (so the side-by-side works before any model is run) ----

_GOOD_BALLS = """<!DOCTYPE html><html><head><meta charset=utf-8><style>html,body{margin:0;height:100%;background:#0b0b14;overflow:hidden}canvas{display:block}</style></head><body><canvas id=c></canvas><script>
const cv=document.getElementById('c'),x=cv.getContext('2d');function R(){cv.width=innerWidth;cv.height=innerHeight}R();onresize=R;
const C=['#00f0ff','#ff2e97','#2bff88','#ffb000','#b06bff','#ff5e5e'];
let B=[];for(let i=0;i<14;i++)B.push({x:Math.random()*cv.width,y:Math.random()*cv.height/2,vx:(Math.random()-.5)*7,vy:0,r:8+Math.random()*18,c:C[i%C.length]});
function f(){x.fillStyle='rgba(11,11,20,.16)';x.fillRect(0,0,cv.width,cv.height);
for(const b of B){b.vy+=.25;b.x+=b.vx;b.y+=b.vy;if(b.x<b.r||b.x>cv.width-b.r){b.vx*=-.92;b.x=Math.max(b.r,Math.min(cv.width-b.r,b.x))}if(b.y>cv.height-b.r){b.y=cv.height-b.r;b.vy*=-.8;b.vx*=.99}x.beginPath();x.arc(b.x,b.y,b.r,0,7);x.fillStyle=b.c;x.shadowColor=b.c;x.shadowBlur=18;x.fill()}requestAnimationFrame(f)}f();
</script></body></html>"""

_WEAK_BALLS = """<!DOCTYPE html><html><head><meta charset=utf-8><style>body{margin:0;background:#111}#b{width:40px;height:40px;border-radius:50%;background:red;position:absolute;top:80px;left:0}</style></head><body><div id=b></div><script>
let p=0,d=2;setInterval(function(){p+=d;if(p>window.innerWidth-40||p<0)d=-d;document.getElementById('b').style.left=p+'px'},16);
</script></body></html>"""

_GOOD_TIP = """<!DOCTYPE html><html><head><meta charset=utf-8><style>body{font-family:system-ui;background:#0f1117;color:#e8e8f0;display:flex;justify-content:center;padding:28px}.card{background:#171a24;padding:22px;border-radius:14px;width:300px;box-shadow:0 8px 30px #0008}h2{margin:0 0 6px}label{display:block;margin:12px 0 4px;font-size:12px;color:#9aa6b8}input{width:100%;padding:9px;border-radius:8px;border:1px solid #2a2f3d;background:#0f1117;color:#fff;box-sizing:border-box}.tips button{margin:6px 6px 0 0;padding:7px 11px;border:1px solid #2a2f3d;background:#1f2330;color:#fff;border-radius:8px;cursor:pointer}.tips button.on{background:#00b8d4;border-color:#00b8d4;color:#001}.out{margin-top:16px;font-size:14px;display:flex;justify-content:space-between}.out b{color:#2bff88}</style></head><body><div class=card><h2>Tip calculator</h2>
<label>Bill amount</label><input id=bill type=number value=50 oninput=calc()>
<label>Tip %</label><div class=tips id=tips></div>
<input id=tip type=number value=18 oninput="clearOn();calc()" style="margin-top:8px">
<label>Split between</label><input id=ppl type=number value=2 min=1 oninput=calc()>
<div class=out><span>Tip total</span><b id=tt></b></div><div class=out><span>Grand total</span><b id=gt></b></div><div class=out><span>Per person</span><b id=pp></b></div></div><script>
const tips=document.getElementById('tips');function clearOn(){document.querySelectorAll('.tips button').forEach(b=>b.classList.remove('on'))}
[10,15,18,20,25].forEach(function(p){var b=document.createElement('button');b.textContent=p+'%';b.onclick=function(){document.getElementById('tip').value=p;clearOn();b.classList.add('on');calc()};tips.appendChild(b)});
function calc(){var bill=+document.getElementById('bill').value||0,tip=+document.getElementById('tip').value||0,ppl=Math.max(1,+document.getElementById('ppl').value||1);var t=bill*tip/100,g=bill+t;document.getElementById('tt').textContent='$'+t.toFixed(2);document.getElementById('gt').textContent='$'+g.toFixed(2);document.getElementById('pp').textContent='$'+(g/ppl).toFixed(2)}calc();
</script></body></html>"""

_WEAK_TIP = """<!DOCTYPE html><html><head><meta charset=utf-8></head><body style="font-family:Arial;padding:20px">
<h3>Tip Calculator</h3>
Bill: <input><br><br>Tip %: <input value=15><br><br>People: <input value=1><br><br>
<button>Calculate</button>
<p>Total per person: $0.00</p>
</body></html>"""

_GOOD_SNAKE = """<!DOCTYPE html><html><head><meta charset=utf-8><style>html,body{margin:0;background:#0b0b14;color:#cfe;font-family:monospace;text-align:center}canvas{background:#11131c;display:block;margin:10px auto;border:1px solid #2a2f3d}</style></head><body><div id=s>Score: 0 — use arrow keys</div><canvas id=c width=320 height=320></canvas><script>
const cv=document.getElementById('c'),x=cv.getContext('2d'),G=16,N=20,s=document.getElementById('s');let sn=[{x:8,y:8}],d={x:1,y:0},nd=d,fo={x:5,y:5},sc=0,dead=false;
onkeydown=function(e){var k=e.key;if(k=='ArrowUp'&&d.y==0)nd={x:0,y:-1};else if(k=='ArrowDown'&&d.y==0)nd={x:0,y:1};else if(k=='ArrowLeft'&&d.x==0)nd={x:-1,y:0};else if(k=='ArrowRight'&&d.x==0)nd={x:1,y:0};else if(dead){sn=[{x:8,y:8}];d=nd={x:1,y:0};sc=0;dead=false;s.textContent='Score: 0'}};
function step(){if(dead)return;d=nd;var h={x:(sn[0].x+d.x+N)%N,y:(sn[0].y+d.y+N)%N};if(sn.some(p=>p.x==h.x&&p.y==h.y)){dead=true;s.textContent='Game over — Score '+sc+' (press a key)';return}sn.unshift(h);if(h.x==fo.x&&h.y==fo.y){sc++;s.textContent='Score: '+sc;fo={x:(Math.random()*N)|0,y:(Math.random()*N)|0}}else sn.pop();
x.fillStyle='#11131c';x.fillRect(0,0,320,320);x.fillStyle='#ff2e97';x.fillRect(fo.x*G,fo.y*G,G-1,G-1);x.fillStyle='#2bff88';sn.forEach(p=>x.fillRect(p.x*G,p.y*G,G-1,G-1))}
setInterval(step,110);
</script></body></html>"""

_WEAK_SNAKE = """<!DOCTYPE html><html><head><meta charset=utf-8><style>body{background:#000;color:#0f0;font-family:monospace;text-align:center;padding-top:50px}</style></head><body>
<h2>SNAKE</h2><pre>
+--------------------+
|                    |
|     ####           |
|         o          |
|                    |
+--------------------+
</pre><p>Press arrow keys to play (coming soon)</p>
</body></html>"""

_DEMO = [
    ("animation", "anim.balls", "demo-strong", _GOOD_BALLS),
    ("animation", "anim.balls", "demo-weak", _WEAK_BALLS),
    ("app", "app.tip", "demo-strong", _GOOD_TIP),
    ("app", "app.tip", "demo-weak", _WEAK_TIP),
    ("game", "game.snake", "demo-strong", _GOOD_SNAKE),
    ("game", "game.snake", "demo-weak", _WEAK_SNAKE),
]


def seed_demo():
    """Insert canned strong/weak artifacts once, so the arena is demoable with no
    model running. Idempotent (keyed on the demo model names)."""
    if db.artifact_exists("demo-strong"):
        return
    for kind, pid, model, html in _DEMO:
        db.save_artifact(uuid.uuid4().hex[:10], kind=kind, prompt_id=pid,
                         model=model, html=html, ok=True, gen_ms=None)


# ======================================================================
# Human-vote integrity: server-driven random matches + secret honeypots
# ======================================================================
#
def seed_bogus():
    """Mothership-only integrity seeding (no-op in the public pod distribution)."""
    if _trust:
        _trust.seed_bogus()


def _real_by_prompt(kind):
    """{prompt_id: [real, ok artifacts]} for a kind (bogus + failed gens excluded)."""
    out = {}
    for a in db.list_artifacts(kind=kind):
        if not a.get("ok"):
            continue
        out.setdefault(a["prompt_id"], []).append(a)
    return out


def _payload(mid, kind, prompt_id):
    """The client gets ONLY the match id + the prompt title. It never receives the
    artifact ids or any per-side metadata, so a honeypot decoy is indistinguishable
    from a real side before voting (it renders each side via /api/arena/render)."""
    p = find_prompt(kind, prompt_id)
    return {"match_id": mid, "kind": kind, "prompt_id": prompt_id,
            "prompt_title": p["title"] if p else prompt_id}


# Sentinel: the pool's ONLY remaining pair is the one this user just reviewed — an honest
# "all caught up" beats re-dealing it (owner report: single demo pair recycled after voting).
EXHAUSTED = object()


def _build_normal_match(user, kind, prompt_id=None):
    """Pick the next comparison with PER-USER ANTI-REPEAT: never re-serve a
    (prompt × model-pair) combo while a fresh one exists; once every combo has been
    seen, serve the LEAST-recently-served one (true LRU, so the rotation cycles the
    whole pool instead of random.choice hammering the same matchup). Skipped matches
    count as served — skip-spamming walks the pool too. Same preference applies at
    the artifact-pair level within the chosen combo."""
    by_prompt = _real_by_prompt(kind)
    eligible = [pid for pid, lst in by_prompt.items()
                if len({x["model"] for x in lst}) >= 2]
    if not eligible:
        return None
    pinned = prompt_id if (prompt_id in by_prompt
                           and len({x["model"] for x in by_prompt[prompt_id]}) >= 2) else None

    # decode the user's recent history into combo/pair recency ranks (0 = most recent)
    id2model = {x["id"]: x["model"] for lst in by_prompt.values() for x in lst}
    combo_rank, pair_rank = {}, {}
    for rank, h in enumerate(db.user_recent_matches(user["id"], kind)):
        ma, mb = id2model.get(h["a_id"]), id2model.get(h["b_id"])
        if ma and mb:
            combo_rank.setdefault((h["prompt_id"], frozenset((ma, mb))), rank)
        pair_rank.setdefault((h["prompt_id"], frozenset((h["a_id"], h["b_id"]))), rank)

    candidates = []
    for pid in ([pinned] if pinned else eligible):
        for m_a, m_b in itertools.combinations(sorted({x["model"] for x in by_prompt[pid]}), 2):
            candidates.append((pid, m_a, m_b))
    fresh = [c for c in candidates if (c[0], frozenset((c[1], c[2]))) not in combo_rank]
    if fresh:
        chosen, m_a, m_b = random.choice(fresh)
    else:   # all combos seen recently -> the one seen longest ago (largest rank)
        chosen, m_a, m_b = max(candidates,
                               key=lambda c: combo_rank.get((c[0], frozenset((c[1], c[2]))), -1))
    lst = by_prompt[chosen]
    pairs = [(x, y) for x in lst if x["model"] == m_a for y in lst if y["model"] == m_b]
    fresh_pairs = [p for p in pairs
                   if (chosen, frozenset((p[0]["id"], p[1]["id"]))) not in pair_rank]
    a, b = random.choice(fresh_pairs or pairs)
    # degenerate pool: if this is the ONLY pair in the whole kind and it's the very pair
    # the user saw last, say "all caught up" instead of recycling it at them
    total_pairs = sum(
        sum(1 for x in by_prompt[pid] if x["model"] == ma) *
        sum(1 for y in by_prompt[pid] if y["model"] == mb)
        for pid, ma, mb in candidates)
    if total_pairs == 1 and pair_rank.get((chosen, frozenset((a["id"], b["id"])))) == 0:
        return EXHAUSTED
    if random.random() < 0.5:
        a, b = b, a
    mid = uuid.uuid4().hex[:14]
    db.create_match(mid, user_id=user["id"], kind=kind, prompt_id=chosen,
                    a_id=a["id"], b_id=b["id"], is_test=False, bogus_side=None)
    return _payload(mid, kind, chosen)


def build_match(user, kind, prompt_id=None):
    """Pick the next comparison for this user. Integrity checks (mothership-only,
    see arena_trust) are woven in server-side; the payload never distinguishes them.
    Returns a payload or None if there aren't enough artifacts."""
    if kind not in KINDS:
        return None
    if _trust:
        # An outstanding (unvoted) integrity check is a sticky obligation: skipping
        # retires it and mints a fresh one, so the check is inescapable but never
        # reads as a repeated pairing.
        pending = db.latest_unvoted_match(user["id"], kind, is_test=True)
        if pending:
            fresh = _trust.build_test_match(user, kind)
            if fresh:
                db.claim_match(pending["id"])   # retire the abandoned one (a late vote 409s)
                return _payload(*fresh)
            return _payload(pending["id"], pending["kind"], pending["prompt_id"])
        if _trust.should_test(user, kind):
            m = _trust.build_test_match(user, kind)
            if m:
                return _payload(*m)
    return _build_normal_match(user, kind, prompt_id)


def submit_vote(user, match_id, winner):
    """Adjudicate a vote against the SERVER's record of the match. Applies honeypot
    consequences (flag on fail, verify on pass). Never tells the client it was a test."""
    if winner not in ("a", "b", "tie"):
        return {"error": "winner must be a, b, or tie"}, 400
    m = db.get_match(match_id)
    if not m or m["user_id"] != user["id"]:
        return {"error": "unknown match"}, 404
    a, b = db.get_artifact(m["a_id"]), db.get_artifact(m["b_id"])
    if not a or not b:
        return {"error": "artifact missing"}, 404
    if not db.claim_match(match_id):   # atomic claim: exactly one vote per match (kills the race)
        return {"error": "this comparison was already voted"}, 409

    # Integrity adjudication is mothership-only (arena_trust); a pod's local arena
    # has no test matches, so every vote records as a plain observation there.
    test_passed = _trust.adjudicate(m, winner) if (_trust and m["is_test"]) else None

    db.record_vote(uuid.uuid4().hex[:12], kind=m["kind"], prompt_id=m["prompt_id"],
                   a_id=m["a_id"], b_id=m["b_id"], a_model=a["model"], b_model=b["model"],
                   winner=winner, user_id=user["id"], is_test=bool(m["is_test"]),
                   test_passed=test_passed)

    if _trust and m["is_test"] and test_passed is not None:
        _trust.update_trust_flags(user["id"])
    # Reveal model names (the decoy's plausible weak name keeps the honeypot hidden).
    return {"ok": True, "a_model": a["model"], "b_model": b["model"], "winner": winner,
            "ranking": ranking(m["kind"]), "you": _self_state(user["id"])}, 200


def _self_state(uid):
    from . import accounts
    return accounts.public_state(uid)
