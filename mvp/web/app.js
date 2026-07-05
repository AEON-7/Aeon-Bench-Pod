"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const api = (p, o) => fetch(p, o).then((r) => r.ok ? r.json() : r.json().then((e) => Promise.reject(e)));
// radar series palette — identity colors only; NO verdict hexes (green means "passed", never "model 5")
const COLORS = ["#00f0ff", "#ff5ea8", "#ffd166", "#9d7bff", "#2ec4b6", "#ff8a5c", "#5ad1ff", "#e3e3ee"];
const RM = matchMedia("(prefers-reduced-motion: reduce)").matches;

// skeleton shimmer rows (replaces bare "loading…" strings)
const skel = (n, h = 14) => Array.from({ length: n },
  (_, i) => `<div class="skel" style="height:${h}px;width:${88 - (i % 3) * 14}%"></div>`).join("");

// one-shot count-up for composite readouts (fresh-load only; RM sets final value)
function countUp(el, target, dur = 550) {
  if (RM) { el.textContent = fmtComp(target); return; }
  const t0 = performance.now();
  (function f() {
    const p = Math.min(1, (performance.now() - t0) / dur), e = 1 - Math.pow(1 - p, 3);
    el.textContent = p < 1 ? (target * e).toFixed(1) : fmtComp(target);
    if (p < 1) requestAnimationFrame(f);
  })();
}

// arena reveal: names unscramble from block glyphs, resolving left-to-right.
// DECRYPT_GEN cancels in-flight scrambles when the match changes (skip mid-reveal),
// so a stale rAF loop can never overwrite the next match's labels.
let DECRYPT_GEN = 0;
function decrypt(el, text, dur = 500) {
  if (RM || !text) { el.textContent = text; return; }
  const gen = DECRYPT_GEN;
  const pool = "█▓▒░<>/#";
  const t0 = performance.now();
  (function f() {
    if (gen !== DECRYPT_GEN) return;               // match changed — abandon silently
    const p = Math.min(1, (performance.now() - t0) / dur);
    const solved = Math.floor(text.length * p);
    el.textContent = text.slice(0, solved) + [...text.slice(solved)]
      .map((c) => c === " " ? " " : pool[Math.random() * pool.length | 0]).join("");
    if (p < 1) requestAnimationFrame(f); else el.textContent = text;
  })();
}

// ---- instrument formatters (AAA pass): ONE grammar per quantity, everywhere ----
const fmtDur = (ms) => ms == null ? "—" : ms < 1000 ? Math.round(ms) + "ms" : (ms / 1000).toFixed(1) + "s";
const fmtTps = (v) => v == null ? "—" : v >= 100 ? String(Math.round(v)) : (+v).toFixed(1);
const fmtComp = (v) => v >= 99.95 ? "100" : v.toFixed(1);
const fmtDate = (ts) => ts ? new Date(ts * 1000).toISOString().slice(0, 10) : "unknown";
const fmtClock = (ts) => ts ? new Date(ts * 1000).toTimeString().slice(0, 8) : "—";
const fmtDT = (ts) => ts ? fmtDate(ts) + " " + fmtClock(ts) : "—";
const fmtHver = (v) => {                       // uniform version grammar: #sha8 / 2026.6.11 / v1.17.12
  if (!v) return "";
  if (/^sha256:/i.test(v)) return "#" + v.slice(7, 15);
  if (/^\d+\.\d+/.test(v) && !/^\d{4}\./.test(v)) return "v" + v;
  return v;
};
// UI-owned truncation must SAY it is the UI's, or a cut answer reads as a broken model
const cut = (s, n) => { s = String(s); return s.length > n ? s.slice(0, n) + " …[+" + (s.length - n) + " chars]" : s; };
// dim the org prefix so the model name pops (raw name stays in data-* attrs / titles)
const fmtModel = (m) => {
  const s = String(m), i = s.indexOf("/");
  return i < 0 ? escH(s) : `<span class="morg">${escH(s.slice(0, i + 1))}</span>${escH(s.slice(i + 1))}`;
};
// fixed capability set shown as boxes in every row (available ones highlighted)
const CAP_SET = ["Vision", "Audio", "Tool Calling", "Reasoning", "Coding", "Math", "Instruction", "Uncensored"];
const CAP_ABBR = { Vision: "VIS", Audio: "AUD", "Tool Calling": "TOOL", Reasoning: "RSN",
  Coding: "CODE", Math: "MATH", Instruction: "INST", Uncensored: "UNC" };

const BOARDS = {
  text:   { suite: "/api/suite",        lb: "/api/leaderboard",        runs: "/api/runs",
            speed: [["avg_decode_tps", "tok/s", fmtTps], ["avg_ttft_ms", "TTFT", fmtDur]], coverage: false },
  vision: { suite: "/api/vision/suite", lb: "/api/vision/leaderboard", runs: "/api/vision/runs",
            speed: [["avg_ttft_after_image_ms", "img TTFT", fmtDur], ["avg_decode_tps", "tok/s", fmtTps]], coverage: true },
  audio:  { audio: true },
};
let active = "text";
// Global-leaderboard lens: when true, show ONLY record-eligible (verified HF-pull, signed) runs
// — the true global ranking. Default off so local runs stay visible (clearly badged) and the
// board is never bare; the toggle flips to the pure verified view.
let verifiedOnly = false;
const ST = { text: {}, vision: {}, audio: {}, harness: {} };
let HARNESS = null;   // cached /api/harness_board (model × harness matrix)
// client-side model->meta cache (creator/org card + avatar), so the board fetches
// each model's metadata at most once. Values: "pending" (Promise) or the meta dict.
const META = new Map();

function fetchMeta(model) {
  const cached = META.get(model);
  if (cached && cached !== "pending") return Promise.resolve(cached);
  if (cached === "pending") return null;        // already in flight; img is updated when it lands
  const p = api("/api/model/meta?model=" + encodeURIComponent(model))
    .then((meta) => { META.set(model, meta); return meta; })
    .catch(() => { META.delete(model); return null; });
  META.set(model, "pending");
  p.then((meta) => { if (meta) applyMeta(model, meta); });
  return p;
}

// Populate the avatar/creator-link/get-model-button for every rendered row of `model`.
// model identity comes from the server; the model NAME is untrusted -> escA on the
// selector attribute and escA on every URL/text we inject (DOM-XSS guard).
function applyMeta(model, meta) {
  if (!meta) return;
  const sel = `[data-meta="${cssEsc(model)}"]`;
  $$(sel).forEach((a) => {                                    // document-wide: board + submissions + harness
    if (meta.creator_url) { a.href = meta.creator_url; a.removeAttribute("hidden"); }
    a.title = "by " + (meta.creator || "unknown");
  });
  $$(`[data-meta-avatar="${cssEsc(model)}"]`).forEach((img) => {
    img.alt = (meta.creator || "") + " avatar";
    img.classList.toggle("own", !!meta.is_own);
    // Load the remote HF avatar via a detached probe and swap it in only once it has
    // ACTUALLY loaded — the visible <img> keeps the local placeholder meanwhile, so an
    // offline/blocked CDN (lab pods) never leaves an empty or broken circle.
    if (meta.avatar_url && img.dataset.ava !== meta.avatar_url) {
      img.dataset.ava = meta.avatar_url;
      const probe = new Image();
      probe.onload = () => { img.src = meta.avatar_url; };
      probe.src = meta.avatar_url;
    }
  });
  $$(`[data-meta-card="${cssEsc(model)}"]`).forEach((btn) => {
    if (meta.card_url) { btn.href = meta.card_url; btn.removeAttribute("hidden"); btn.textContent = "Get Model"; }
  });
}
// CSS.escape fallback for attribute-selector values built from untrusted model names
const cssEsc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s)
  : String(s).replace(/["\\\]]/g, "\\$&");

function key() {
  const el = $("#apikey");                 // launch form removed; safe no-op
  if (!el) return "";
  const k = el.value.trim();
  try { localStorage.setItem("aeon_key", k); } catch (e) {}
  return k;
}

function composite(m, cats, weights) {
  let sw = 0, s = 0;
  for (const c of cats) {
    if (m.categories[c] == null) continue;
    const w = weights[c] ?? 1; sw += w; s += w * m.categories[c];
  }
  return sw ? s / sw : 0;
}

function cellBar(score) {
  if (score == null) return '<span class="meter na">—</span>';
  const cls = score >= 80 ? "pass" : score >= 40 ? "part" : "fail";
  const full = score >= 99.5 ? " full" : "";      // needle on the peg: blade closes flush at 100
  return `<span class="meter ${cls}${full}"><span class="meter-fill" style="width:${Math.max(4, score)}%"></span>` +
         `<b class="meter-val">${score.toFixed(0)}</b></span>`;
}

function filteredModels() {
  const st = ST[active], f = [...st.filters];
  let ms = (st.data && st.data.models) || [];
  if (f.length) ms = ms.filter((m) => {
    const names = new Set((m.tags || []).map((t) => t.name));
    return f.every((t) => names.has(t));
  });
  if (st.vramLimit) ms = ms.filter((m) => m.vram_est_gb == null || m.vram_est_gb <= st.vramLimit);
  if (verifiedOnly) ms = ms.filter((m) => m.record_eligible);
  return ms.map((m) => ({ ...m, comp: composite(m, st.cats || [], st.weights) }))
    .sort((a, b) => b.comp - a.comp);
}

function allTags() {
  const s = new Set();
  ((ST[active].data && ST[active].data.models) || []).forEach((m) =>
    (m.tags || []).forEach((t) => s.add(t.name)));
  return [...s].sort();
}

function renderFilters() {
  const st = ST[active], tags = allTags(), el = $("#filters");
  if (!tags.length) { el.innerHTML = ""; return; }
  el.innerHTML = `<span class="flabel">capabilities:</span>` +
    tags.map((t) => `<button class="chip filter ${st.filters.has(t) ? "on" : ""}" data-tag="${t}">${t}</button>`).join("") +
    (st.filters.size ? `<button class="chip clear" id="clearF">clear</button>` : "") +
    `<span class="flegend"><span class="capbox on tested">tested</span><span class="capbox on declared">declared</span><span class="capbox off">n/a</span></span>`;
  $$("#filters .filter").forEach((b) => b.onclick = () => {
    st.filters.has(b.dataset.tag) ? st.filters.delete(b.dataset.tag) : st.filters.add(b.dataset.tag);
    renderBoard();
  });
  const cf = $("#clearF"); if (cf) cf.onclick = () => { st.filters.clear(); renderBoard(); };
}

let PRESETS = [];
async function loadPresets() {
  if (PRESETS.length) return;
  try { PRESETS = (await api("/api/system_presets")).presets || []; } catch (e) {}
}
function renderVramFilter() {
  const st = ST[active], el = $("#vramFilter"); if (!el) return;
  const lim = st.vramLimit || 0;
  const opts = PRESETS.map((p) => `<option value="${p.vram}"${lim === p.vram ? " selected" : ""}>${escH(p.name)}</option>`).join("");
  const lab = (v) => v ? "≤ " + v + " GB" : "any";
  // slider + readout are one non-wrapping unit — the value must never orphan from its control
  el.innerHTML = `<span class="vlabel">▣ fits my system</span>` +
    `<select id="vramPreset"><option value="0">any VRAM</option>${opts}</select>` +
    `<span class="vrgroup"><input type="range" id="vramSlider" min="0" max="160" step="2" value="${lim}">` +
    `<span class="vval mono" id="vramVal">${lab(lim)}</span></span>`;
  $("#vramPreset").onchange = () => {
    st.vramLimit = parseInt($("#vramPreset").value) || 0;
    $("#vramSlider").value = st.vramLimit; $("#vramVal").textContent = lab(st.vramLimit); renderBoard();
  };
  $("#vramSlider").oninput = () => {
    st.vramLimit = parseInt($("#vramSlider").value) || 0;
    $("#vramVal").textContent = lab(st.vramLimit); $("#vramPreset").value = "0"; renderBoard();
  };
}

function renderEligBar() {
  const el = $("#eligBar"); if (!el) return;
  const data = (ST[active].data && ST[active].data.models) || [];
  const nElig = data.filter((m) => m.record_eligible).length;
  el.innerHTML =
    `<label class="eligtoggle" data-tip="Show only the verified global ranking (HF-pull controlled + signed)">`
    + `<input type="checkbox" id="verifiedOnlyCb" ${verifiedOnly ? "checked" : ""}>`
    + `<span>Verified only</span></label>`
    + `<span class="elignote">Global rank counts <b class="ev-ok">verified</b> runs only · <b>${nElig}</b> verified · <b>${data.length - nElig}</b> local · `
    + `<a href="/api/pod/run-a-benchmark.md" target="_blank" rel="noopener">submit a verified run →</a></span>`;
  const cb = $("#verifiedOnlyCb");
  if (cb) cb.onchange = (e) => { verifiedOnly = e.target.checked; renderBoard(); };
}

function renderBoard() {
  const st = ST[active], cfg = BOARDS[active], cats = st.cats || [];
  renderFilters();
  const models = filteredModels();
  const speedDefs = cfg.speed || [];
  // Spacious wide cards (replaces the cramped fixed-width table): a big circular creator avatar,
  // the FULL model name (wraps, never truncates), and every metric LABELLED on the card — so no
  // shared header row can overlap at any width.
  $("#board").innerHTML = models.map((m, i) => {
    const checked = st.selected.has(m.model) ? "checked" : "";
    const caps = (m.tags || []).slice()
      .sort((a, b) => CAP_SET.indexOf(a.name) - CAP_SET.indexOf(b.name))
      .map((t) => `<span class="capbox on ${t.source}" title="${escA(t.name)} — ${t.source}">${escH(t.name)}</span>`)
      .join("") || `<span class="capnone">—</span>`;
    const catCells = cats.map((c) =>
      `<div class="catchip"><span class="catk">${escH(c)}</span>${cellBar(m.categories[c])}</div>`).join("");
    const covCell = cfg.coverage
      ? `<div class="spdchip"><span class="catk">coverage</span><span class="catv">${m.coverage || "—"}</span></div>` : "";
    const spdCells = speedDefs.map((s) =>
      `<div class="spdchip"><span class="catk">${escH(s[1])}</span><span class="catv">${s[2] ? s[2](m[s[0]]) : (m[s[0]] != null ? Math.round(m[s[0]]) : "—")}</span></div>`).join("");
    const vram = m.vram_est_gb != null
      ? `<span class="mcard-vram" title="estimated VRAM at load">~${m.vram_est_gb} GB</span>` : "";
    const band = m.comp >= 80 ? "pass" : m.comp >= 40 ? "part" : "fail";
    return `<div class="mcard${i === 0 ? " top" : ""}${i < 3 ? " p" + (i + 1) : ""}" data-model="${escA(m.model)}" data-trust="${m.record_eligible ? "verified" : "local"}" style="--i:${i}">
      <span class="mcard-ghost" aria-hidden="true">${String(i + 1).padStart(2, "0")}</span>
      <label class="mcard-sel"><input type="checkbox" class="rsel" data-model="${escA(m.model)}" ${checked}></label>
      <div class="mcard-rank">${String(i + 1).padStart(2, "0")}</div>
      <a class="model-creator mcard-ava" data-meta="${escA(m.model)}" target="_blank" rel="noopener noreferrer" title="creator profile">
        <img class="model-avatar" data-meta-avatar="${escA(m.model)}" src="/static/generic-avatar.svg" alt="" loading="lazy" width="52" height="52">
      </a>
      <div class="mcard-id">
        <div class="mcard-name">
          <a class="mlink" data-run="${escA(m.run)}" data-model="${escA(m.model)}">${fmtModel(m.model)}</a>
          ${m.record_eligible
            ? `<span class="elig-badge verified" title="verified HF-pull controlled run — globally ranked">✓ verified</span>`
            : `<span class="elig-badge local" title="local / self-reported run — stored &amp; shown, not globally ranked">local</span>`}
          ${vram}
          <a class="get-model-btn" data-meta-card="${escA(m.model)}" target="_blank" rel="noopener noreferrer" hidden>Get&nbsp;Model</a>
        </div>
        <div class="mcard-caps">${caps}</div>
      </div>
      <div class="mcard-comp ${band}" style="--pct:${m.comp.toFixed(1)}"><span class="composite">${fmtComp(m.comp)}</span><span class="mcard-complabel">composite</span></div>
      <div class="mcard-metrics">${catCells}${covCell}${spdCells}</div>
    </div>`;
  }).join("") ||
    `<div class="board-empty">${verifiedOnly
      ? "No <b>verified</b> submissions yet. The global leaderboard ranks only models benchmarked through the controlled <b>HF-pull flow</b> — pulled fresh from Hugging Face → hash-verified → run through the harnesses → cryptographically signed. Direct-endpoint runs are stored as <b>local</b> (toggle off to see them)."
      : "No models match these filters."}</div>`;
  $$("#board .rsel").forEach((cb) => cb.onchange = () => {
    cb.checked ? st.selected.add(cb.dataset.model) : st.selected.delete(cb.dataset.model);
    renderChart();
  });
  $$("#board .mlink").forEach((a) => a.onclick = () => openSubmissionsFor(a.dataset.model));
  // instrument boot: composite counts up in sync with the gauge-ring sweep — first load only
  if ($("#board").classList.contains("fresh")) {
    $$("#board .mcard .composite").forEach((el, i) => { if (models[i]) countUp(el, models[i].comp); });
  }
  models.forEach((m) => {
    const cached = META.get(m.model);
    if (cached && cached !== "pending") applyMeta(m.model, cached);
    else fetchMeta(m.model);
  });
  renderChart();
}

function renderChart() {
  const st = ST[active], cats = st.cats || [], el = $("#radar"), note = $("#chartNote"), n = cats.length;
  if (!n) { el.innerHTML = ""; note.textContent = ""; return; }
  const all = filteredModels();
  const sel = all.filter((m) => st.selected.has(m.model));
  const models = sel.length ? sel : all.slice(0, 5);   // selected models, else top 5
  const W = 640, H = 500, cx = 320, cy = 250, R = 200;
  const ang = (i) => (-90 + i * 360 / n) * Math.PI / 180;
  const pt = (i, r) => [cx + Math.cos(ang(i)) * r, cy + Math.sin(ang(i)) * r];
  const fx = (a) => a.map((v) => v.toFixed(1)).join(",");
  let g = "";
  for (const p of [25, 50, 75, 100])
    g += `<polygon points="${cats.map((_, i) => fx(pt(i, R * p / 100))).join(" ")}" fill="none" stroke="#17223f" stroke-width="0.9"/>`;
  for (const p of [50, 100])  // scale ticks — grey anchors with a knockout so series strokes never overplot them
    g += `<text x="${cx + 7}" y="${(cy - R * p / 100).toFixed(1)}" fill="#55607f" font-size="10" paint-order="stroke" stroke="#0a0a14" stroke-width="3" dominant-baseline="middle">${p}</text>`;
  cats.forEach((c, i) => {
    const [x, y] = pt(i, R);
    g += `<line x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" stroke="#17223f" stroke-width="0.8"/>`;
    const [lx, ly] = pt(i, R + 22);
    const an = Math.abs(lx - cx) < 8 ? "middle" : (lx > cx ? "start" : "end");
    g += `<text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" fill="#9aa3c0" font-size="12.5" text-anchor="${an}" dominant-baseline="middle">${c}</text>`;
  });
  const shown = models.slice(0, 8);
  // the spring-in class only on data arrival (#board.fresh) — slider/filter re-renders draw instantly
  const seriesCls = $("#board").classList.contains("fresh") ? "series" : "";
  shown.forEach((m, k) => {
    const col = COLORS[k % COLORS.length];
    const pts = cats.map((c, i) => fx(pt(i, R * (m.categories[c] ?? 0) / 100))).join(" ");
    g += `<polygon class="${seriesCls}" points="${pts}" fill="${col}1f" stroke="${col}" stroke-width="2" style="animation-delay:${k * 60}ms"/>`;
  });
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:640px;display:block;margin:0 auto">${g}</svg>`;
  // legend chips: click toggles compare selection; hover spotlights the matching card
  note.innerHTML = `<span style="color:var(--muted)">${sel.length ? `comparing ${shown.length} selected` : `top ${shown.length} — click a chip or tick cards to compare`}</span><br>` +
    shown.map((m, k) => `<button class="legend-chip${st.selected.has(m.model) ? " sel" : ""}" data-model="${escA(m.model)}">` +
      `<i style="background:${COLORS[k % COLORS.length]}"></i>${escH(m.model.split("/").pop())}</button>`).join("");
  $$("#chartNote .legend-chip").forEach((b) => {
    const model = b.dataset.model;
    b.onclick = () => { st.selected.has(model) ? st.selected.delete(model) : st.selected.add(model); renderBoard(); };
    b.onmouseenter = () => { const c = document.querySelector(`.mcard[data-model="${cssEsc(model)}"]`); if (c) c.classList.add("hint"); };
    b.onmouseleave = () => { const c = document.querySelector(`.mcard[data-model="${cssEsc(model)}"]`); if (c) c.classList.remove("hint"); };
  });
}

function renderWeights() {
  const st = ST[active], cats = st.cats || [];
  $("#weights").innerHTML = cats.map((c) =>
    `<span class="w">${c}<input type="range" min="0" max="3" step="0.5" value="${st.weights[c] ?? 1}" data-cat="${c}"/>` +
    `<span class="mono" id="wv-${c}">${(st.weights[c] ?? 1).toFixed(1)}</span></span>`).join("");
  $$('#weights input[type=range]').forEach((el) => {
    el.oninput = () => {
      st.weights[el.dataset.cat] = parseFloat(el.value);
      $("#wv-" + el.dataset.cat).textContent = parseFloat(el.value).toFixed(1);
      renderBoard();
    };
  });
}

async function showDetail(run, model) {
  const r = await api("/api/runs/" + run);
  $("#detailPanel").hidden = false;
  $("#detailModel").textContent = model;
  $("#detailRun").textContent = run;
  $("#detail tbody").innerHTML = (r.results || []).map((x) => {
    const sc = x.score == null ? "—" : (x.score * 100).toFixed(0);
    const cls = x.score == null ? "" : x.score >= 0.8 ? "pass" : x.score >= 0.4 ? "part" : "fail";
    const ev = evidence(x), sp = x.speed || {};
    return `<tr><td class="mono" data-label="case">${x.case_id}</td><td data-label="cat">${x.category}</td><td class="num" data-label="tier">${x.tier}</td>
      <td class="num ${cls} mono" data-label="score">${sc}</td><td class="evidence" data-label="evidence" title="${escA(ev)}">${escH(ev)}</td>
      <td class="num mono" data-label="ttft">${fmtDur(sp.ttft_ms ?? sp.ttft_after_image_ms)}</td>
      <td class="num mono" data-label="tok/s">${fmtTps(sp.decode_tps)}</td><td class="num mono" data-label="e2e">${fmtDur(sp.e2e_ms)}</td></tr>`;
  }).join("");
  $("#detailPanel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function evidence(x) {
  const e = x.evidence || {};
  if (e.tier === 0 && e.checkers) return e.checkers.map((c) => `${c.satisfied ? "✓" : "✗"} ${c.evidence}`).join(" | ");
  if (e.tier === 1 && e.criteria) return e.criteria.map((c) => `${c.satisfied ? "✓" : "✗"}${c.id}[${c.decided_by}] ${c.evidence}`).join(" | ");
  if (e.skipped) return "skipped — requires " + e.skipped;
  return JSON.stringify(e).slice(0, 200);
}
const escH = (s) => String(s).replace(/[&<>]/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[m]));
const escA = (s) => escH(s).replace(/"/g, "&quot;");

async function loadModels() {
  const target = $("#target").value.trim();
  $("#status").innerHTML = `<span class="spin">⟳</span> listing models…`;
  try {
    const r = await api("/api/models?target=" + encodeURIComponent(target) + "&api_key=" + encodeURIComponent(key()));
    const opts = r.models.length
      ? r.models.map((m) => `<option>${escH(m)}</option>`).join("")
      : `<option value="">(no models — endpoint running?)</option>`;
    $("#model").innerHTML = opts;
    $("#judge").innerHTML = `<option value="">self (the model under test)</option>` +
      r.models.map((m) => `<option>${escH(m)}</option>`).join("");
    $("#status").textContent = r.models.length ? `${r.models.length} model(s) available` : "no models found";
  } catch (e) {
    $("#status").innerHTML = `<span class="err">could not reach ${target}</span>`;
  }
}

async function loadBoard() {
  const cfg = BOARDS[active];
  $("#arenaPanel").hidden = true;
  $("#adminPanel").hidden = true;
  $("#subsPanel").hidden = true;
  $("#boardPanel").hidden = !!cfg.audio;
  $("#audioPanel").hidden = !cfg.audio;
  $("#detailPanel").hidden = true;
  { const rp = $("#runPanel"); if (rp) rp.hidden = true; }
  { const _r = $("#run"); if (_r) _r.style.display = cfg.audio ? "none" : ""; }   // launch button removed — guard
  if (cfg.audio) return;
  const st = ST[active];
  if (!st.selected) st.selected = new Set();
  if (!st.filters) st.filters = new Set();
  if (!st.data) $("#board").innerHTML = skel(5, 90);   // first load: card-shaped shimmer, never a blank hole
  let s;
  try {
    s = await api(cfg.suite);
    st.cats = s.categories;
    if (!st.weights) { st.weights = {}; s.categories.forEach((c) => st.weights[c] = 1); }
    $("#suiteInfo").textContent = `${s.suite_id} · ${s.n_cases} cases · ${s.suite_hash}`;
    renderWeights();
    st.data = await api(cfg.lb);
  } catch (e) {
    // a dead API must SAY so — a silent blank board reads as "no models exist"
    $("#board").innerHTML = `<div class="board-empty"><b class="err">✗ link down</b> — could not reach the mothership API. ` +
      `<button class="ghost" id="bRetry">↻ retry</button></div>`;
    const rb = $("#bRetry"); if (rb) rb.onclick = loadBoard;
    return;
  }
  await loadPresets();
  renderVramFilter();
  renderEligBar();
  freshBoard();          // data arrived → one instrument-boot pass, then still
  renderBoard();
}

// Gate load choreography to DATA ARRIVAL only: slider drags / filter clicks re-render
// with no `fresh` class, so they stay instant with zero animation replay.
function freshBoard() {
  const b = $("#board"); if (!b) return;
  b.classList.add("fresh");
  setTimeout(() => b.classList.remove("fresh"), 950);
}

async function reloadBoardData() {
  const cfg = BOARDS[active];
  if (cfg.audio) return;
  ST[active].data = await api(cfg.lb);
  freshBoard();
  renderBoard();
}

async function launch() {
  const cfg = BOARDS[active];
  if (cfg.audio) return;
  const model = ($("#model") || {}).value || "";   // launch form removed — guard
  if (!model) { $("#status").innerHTML = `<span class="err">pick a model first</span>`; return; }
  $("#run").disabled = true;
  const body = { model, target_url: $("#target").value.trim(), judge_model: $("#judge").value || null, api_key: key() || null };
  let run;
  try { run = await api(cfg.runs, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); }
  catch (e) { $("#status").innerHTML = `<span class="err">launch failed: ${JSON.stringify(e)}</span>`; $("#run").disabled = false; return; }
  const id = run.run_id, boardAtLaunch = active;
  const poll = setInterval(async () => {
    let r;
    try { r = await api("/api/runs/" + id); } catch { $("#status").innerHTML = `<span class="spin">⟳</span> starting…`; return; }
    if (r.status === "running") {
      $("#status").innerHTML = `<span class="spin">⟳</span> running <b>${model}</b> — ${r.progress}/${r.n_cases} cases`;
    } else {
      clearInterval(poll); $("#run").disabled = false;
      $("#status").innerHTML = r.status === "succeeded"
        ? `<span class="ok">✓ ${model} done</span>`
        : (r.status === "capability_absent"
            ? `<span class="err">✗ ${model}: no ${boardAtLaunch} capability — excluded from board</span>`
            : `<span class="err">✗ ${model}: ${r.error || r.status}</span>`);
      if (active === boardAtLaunch) { await reloadBoardData(); if (r.status === "succeeded") showDetail(id, model); }
    }
  }, 800);
}

async function probeAudio() {
  const model = ($("#model") || {}).value || "";   // launch form removed — guard
  if (!model) { $("#audioStatus").innerHTML = `<span class="err">pick an audio model above (e.g. qwen3-omni)</span>`; return; }
  $("#audioProbe").disabled = true;
  $("#audioStatus").innerHTML = `<span class="spin">⟳</span> probing <b>${model}</b> for input_audio transport…`;
  try {
    const r = await api("/api/audio/probe", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model, target_url: $("#target").value.trim(), api_key: key() || null }) });
    const t = r.transport;
    if (r.audio_ok) {
      $("#audioStatus").innerHTML = `<span class="ok">✓ audio transport ACCEPTED</span> by ${model}. ` +
        `Reply: "${escH((r.evidence || "").slice(0, 90))}". The audio suite (ASR / translation / understanding) can now be built.`;
    } else if (t === "model_unavailable") {
      $("#audioStatus").innerHTML = `<span class="warn">⚠ inconclusive</span> — <b>${model}</b> didn't load, so audio can't be assessed yet. ` +
        `Load it in LM Studio first (a plain text chat must work), then re-probe. <span class="mono">${escH(r.error || "")}</span>`;
    } else if (t === "rejected") {
      $("#audioStatus").innerHTML = `<span class="err">✗ audio NOT supported</span> — ${model} loads, but the endpoint rejected <code>input_audio</code>. ` +
        `Board stays gated, by design. <span class="mono">${escH(r.error || "")}</span>`;
    } else {
      $("#audioStatus").innerHTML = `<span class="err">probe error</span> <span class="mono">${escH(r.error || t || "")}</span>`;
    }
  } catch (e) {
    $("#audioStatus").innerHTML = `<span class="err">probe failed: ${escH(JSON.stringify(e))}</span>`;
  } finally { $("#audioProbe").disabled = false; }
}

// ---- Generated-artifact arena (Apps / Games / Animations + human voting) ----

const ARENA = { kind: null, pinned: "", byKind: {}, labels: {}, match: null, voted: false };
const AUTH = { token: null, user: null };
const blankFrame = (msg) =>
  `<!doctype html><html><body style="margin:0;display:flex;align-items:center;justify-content:center;height:100vh;background:#0b0b14;color:#5b6b7a;font-family:monospace;font-size:13px;text-align:center;padding:20px">${msg}</body></html>`;
const loadingFrame = (msg) =>
  `<!doctype html><html><body style="margin:0;display:flex;flex-direction:column;gap:14px;align-items:center;justify-content:center;height:100vh;background:#0b0b14;color:#5b6b7a;font-family:monospace;font-size:13px;text-align:center;padding:20px">` +
  `<div style="width:22px;height:22px;border:2px solid #262640;border-top-color:#00f0ff;border-radius:50%;animation:s 1s linear infinite"></div>${msg}` +
  `<style>@keyframes s{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){*{animation:none!important}}</style></body></html>`;

function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  if (AUTH.token) h["Authorization"] = "Bearer " + AUTH.token;
  return h;
}

async function loadArenaMeta() {
  const r = await api("/api/arena/prompts");
  ARENA.labels = r.labels || {};
  ARENA.byKind = {};
  (r.prompts || []).forEach((p) => { (ARENA.byKind[p.kind] = ARENA.byKind[p.kind] || []).push(p); });
}

// ---- evaluator accounts ----
async function loadMe() {
  if (!AUTH.token) { AUTH.user = null; return; }
  try {
    const r = await fetch("/api/auth/me", { headers: authHeaders() });
    if (r.ok) AUTH.user = (await r.json()).user;
    else { AUTH.token = null; AUTH.user = null; try { localStorage.removeItem("aeon_eval_token"); } catch (e) {} }
  } catch (e) { AUTH.user = null; }
}

function renderAuth() {
  const at = $("#adminTab"); if (at) at.hidden = !(AUTH.user && AUTH.user.admin);
  let html;
  if (AUTH.user) {
    const u = AUTH.user;
    const badge = u.verified
      ? `<span class="ev-badge ok" title="you passed an integrity check — your votes count toward the ranking">✓ verified</span>`
      : `<span class="ev-badge pending" title="cast a few honest votes to verify your account">verifying…</span>`;
    html = `<span class="ev-who">▣ <b>${escH(u.username)}</b></span> ${badge} ` +
      `<span class="ev-credit">${u.counted} counted · ${u.votes} cast</span> ` +
      `<button class="ghost ev-out" id="evLogout">log out</button>`;
  } else {
    html = `<button class="primary ev-in" id="evSignin">sign in / sign up</button>`;
  }
  const hdr = $("#headerAuth");
  if (hdr) hdr.innerHTML = html;
  const ab = $("#arenaAuth"); if (ab) ab.innerHTML = "";   // auth lives in the header now
  const lo = $("#evLogout"); if (lo) lo.onclick = logout;
  const si = $("#evSignin"); if (si) si.onclick = () => openAuth("login");
}

let authMode = "signup";
function openAuth(mode) {
  authMode = mode;
  $("#authTitle").textContent = mode === "signup" ? "Create an evaluator account" : "Log in";
  $("#authSubmit").textContent = mode === "signup" ? "Create account" : "Log in";
  $("#authToggle").textContent = mode === "signup" ? "have an account? log in" : "need an account? sign up";
  $("#authErr").textContent = "";
  const ap = $("#authPass"); if (ap) ap.type = "password";
  const sh = $("#authShow"); if (sh) sh.textContent = "show";
  $("#authModal").hidden = false;
  setTimeout(() => $("#authUser").focus(), 30);
}
function closeAuth() {
  $("#authModal").hidden = true;
  const b = $("#evSignin"); if (b) b.focus();       // return focus to the opener
}

// ---- Tip jar ----
// address + wallet URI scheme per chain (QR SVGs are pre-generated at /static/qr-<chain>.svg)
const TIP_WALLETS = {
  btc: { addr: "bc1q09xmzn00q4z3c5raene0f3pzn9d9pvawfm0py4", scheme: "bitcoin:" },
  eth: { addr: "0x1512667F6D61454ad531d2E45C0a5d1fd82D0500", scheme: "ethereum:" },
  sol: { addr: "DgQsjHdAnT5PNLQTNpJdpLS3tYGpVcsHQCkpoiAKsw8t", scheme: "solana:" },
  xmr: { addr: "836XrSKw4R76vNi3QPJ5Fa9ugcyvE2cWmKSPv3AhpTNNKvqP8v5ba9JRL4Vh7UnFNjDz3E2GXZDVVenu3rkZaNdUFhjAvgd", scheme: "monero:" },
};
function tipSelectChain(chain) {
  const w = TIP_WALLETS[chain]; if (!w) return;
  const qr = $("#tipQr"); if (qr) { qr.src = "/static/qr-" + chain + ".svg"; qr.alt = chain.toUpperCase() + " wallet QR code"; }
  const a = $("#tipAddr"); if (a) a.textContent = w.addr;
  const o = $("#tipOpen"); if (o) o.href = w.scheme + w.addr;   // deep-links to any installed wallet app
  $$(".tip-chip").forEach((c) => c.classList.toggle("is-active", c.dataset.chain === chain));
}
let _tipOpener = null;
function openTip(ev) {
  _tipOpener = (ev && ev.currentTarget) || null;
  tipSelectChain("btc");                 // always open on a consistent default chain
  $("#tipModal").hidden = false;
  const c = $("#tipClose"); if (c) setTimeout(() => c.focus(), 30);
}
function closeTip() {
  $("#tipModal").hidden = true;
  if (_tipOpener && _tipOpener.focus) _tipOpener.focus();   // return focus to the opener
}
async function _copyTipAddr() {
  const el = $("#tipAddr"), btn = $("#tipCopy");
  const addr = ((el && el.textContent) || "").trim();
  try { await navigator.clipboard.writeText(addr); }
  catch {                                             // clipboard API unavailable/denied → select+execCommand
    const r = document.createRange(); r.selectNodeContents(el);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    try { document.execCommand("copy"); } catch (_) {}
    s.removeAllRanges();
  }
  if (btn) {
    const prev = btn.textContent; btn.textContent = "Copied ✓"; btn.classList.add("copied");
    setTimeout(() => { btn.textContent = prev; btn.classList.remove("copied"); }, 1400);
  }
}

async function authSubmit() {
  const username = $("#authUser").value.trim(), password = $("#authPass").value;
  $("#authErr").textContent = ""; $("#authSubmit").disabled = true;
  try {
    const r = await fetch("/api/auth/" + (authMode === "signup" ? "signup" : "login"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const data = await r.json();
    if (!r.ok) {
      if (authMode === "signup" && /taken|exists/i.test(data.error || "")) {
        openAuth("login"); $("#authUser").value = username;
        $("#authErr").textContent = "that username already exists — enter your password to log in";
      } else {
        $("#authErr").textContent = data.error || "failed";
      }
      return;
    }
    AUTH.token = data.token; AUTH.user = data.user;
    try { localStorage.setItem("aeon_eval_token", AUTH.token); } catch (e) {}
    closeAuth();
    // Re-initialize fully logged-in (header, admin tab, arena state) in one shot —
    // avoids any partial-update glitch where you had to refresh manually to "land".
    location.reload();
    return;
  } catch (e) { $("#authErr").textContent = "network error"; }
  finally { $("#authSubmit").disabled = false; }
}

async function logout() {
  try { await fetch("/api/auth/logout", { method: "POST", headers: authHeaders() }); } catch (e) {}
  AUTH.token = null; AUTH.user = null;
  try { localStorage.removeItem("aeon_eval_token"); } catch (e) {}
  renderAuth(); gateOrMatch();
}

// ---- arena view (server-driven random matches across the category) ----
async function setArena(kind) {
  $$("#tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.arena === kind));
  $("#boardPanel").hidden = true; $("#audioPanel").hidden = true; $("#detailPanel").hidden = true;
  $("#adminPanel").hidden = true; $("#subsPanel").hidden = true; $("#runPanel").hidden = true;
  $("#arenaPanel").hidden = false; { const _r = $("#run"); if (_r) _r.style.display = "none"; }
  if (!ARENA.byKind[kind]) await loadArenaMeta();
  ARENA.kind = kind;
  $("#arenaTitle").textContent = ARENA.labels[kind] || "Generated";
  const prompts = ARENA.byKind[kind] || [];
  $("#arenaPrompt").innerHTML = `<option value="">🎲 shuffle all (${prompts.length})</option>` +
    prompts.map((p) => `<option value="${escA(p.id)}">${escH(p.title)}</option>`).join("");
  ARENA.pinned = "";
  renderAuth();
  loadRanking();
  gateOrMatch();
}

function gateOrMatch() {
  if (!AUTH.user) {
    ARENA.match = null;
    $("#frameA").srcdoc = blankFrame("create an account to evaluate");
    $("#frameB").srcdoc = blankFrame("create an account to evaluate");   // symmetric gate — a blank side reads as broken
    setModels("—", "—"); setVoteEnabled(false);
    $("#arenaNow").innerHTML = "";
    $("#arenaMsg").innerHTML = `<span class="warn">Sign in to compare and vote.</span> Honest votes build your reputation; low-effort or broken voting is filtered out automatically.`;
    return;
  }
  nextMatch();
}

let ARENA_ADV = null;   // pending auto-advance timer — cleared on any manual advance
let MATCH_GEN = 0;      // in-flight guard: a newer request abandons an older one (no double-allocation)
async function nextMatch() {
  if (ARENA_ADV) { clearTimeout(ARENA_ADV); ARENA_ADV = null; }   // skip beats auto-advance (no double-load)
  if (!AUTH.user) return gateOrMatch();
  const gen = ++MATCH_GEN;
  const sk = $("#arenaSkip"); if (sk) sk.disabled = true;
  try {
    setVoteEnabled(false); setModels("…", "…");
    $("#frameA").srcdoc = loadingFrame("compiling artifact…"); $("#frameB").srcdoc = loadingFrame("compiling artifact…");
    let r;
    try {
      r = await fetch(`/api/arena/match?kind=${ARENA.kind}&prompt_id=${encodeURIComponent(ARENA.pinned)}`,
        { headers: authHeaders() });
    } catch (e) { if (gen === MATCH_GEN) $("#arenaMsg").innerHTML = `<span class="err">network error</span>`; return; }
    if (gen !== MATCH_GEN) return;
    if (r.status === 401) { AUTH.user = null; renderAuth(); return gateOrMatch(); }
    const data = await r.json();
    if (gen !== MATCH_GEN) return;
    if (!r.ok) {
      ARENA.match = null; setModels("—", "—");
      const idle = data.exhausted ? "all caught up — check back soon" : "nothing to compare yet";
      $("#frameA").srcdoc = blankFrame(idle); $("#frameB").srcdoc = blankFrame(idle);
      $("#arenaNow").innerHTML = "";
      $("#arenaMsg").innerHTML = data.exhausted
        ? `<span class="ok">✓ You've reviewed every available comparison.</span> New artifacts arrive as each benchmark completes — or generate one with the model under test.`
        : `<span class="warn">${data.error || "no comparison available"}.</span> Use “generate with model under test”, or run two models on this category first.`;
      return;
    }
    ARENA.match = data; ARENA.voted = false;
    $("#arenaNow").innerHTML = `now comparing&nbsp; <b>${escH(data.prompt_title)}</b>`;
    $("#arenaMsg").textContent = "";
    setModels("hidden", "hidden"); setVoteEnabled(true);
    renderFrame("#frameA", "a"); renderFrame("#frameB", "b");
  } finally {
    if (sk && gen === MATCH_GEN) sk.disabled = false;
  }
}

async function renderFrame(sel, side) {
  const fr = $(sel);
  if (!ARENA.match) { fr.srcdoc = blankFrame(""); return; }
  try {
    const r = await fetch(`/api/arena/render?match_id=${encodeURIComponent(ARENA.match.match_id)}&side=${side}`,
      { headers: authHeaders() });
    const a = r.ok ? await r.json() : null;
    fr.srcdoc = (a && a.html) || blankFrame("failed to load");
  } catch (e) { fr.srcdoc = blankFrame("failed to load"); }
}

function setModels(a, b) {
  DECRYPT_GEN++;                                   // cancel any in-flight name reveal
  const ma = $("#modelA"), mb = $("#modelB");
  ma.textContent = a; mb.textContent = b; ma.className = "arena-model"; mb.className = "arena-model";
  $("#metaA").textContent = ""; $("#metaB").textContent = "";
  // clear the previous reveal state (winner ring / dim / picked button)
  $$(".arena-side").forEach((s) => s.classList.remove("side-won", "side-lost", "side-tie"));
  $$(".arena-vote .vote").forEach((b) => b.classList.remove("picked"));
}
function setVoteEnabled(on) { $$(".arena-vote .vote").forEach((b) => b.disabled = !on); }

async function arenaVote(w) {
  if (!ARENA.match || ARENA.voted) return;
  if (!AUTH.user) return openAuth("signup");
  setVoteEnabled(false);
  const m = ARENA.match;
  let r, data;
  try {
    r = await fetch("/api/arena/vote", {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ match_id: m.match_id, winner: w }),
    });
    data = await r.json();
  } catch (e) { $("#arenaMsg").innerHTML = `<span class="err">vote failed</span>`; setVoteEnabled(true); return; }
  if (r.status === 401) { AUTH.user = null; renderAuth(); return openAuth("signup"); }
  if (!r.ok) { $("#arenaMsg").innerHTML = `<span class="warn">${data.error || "vote rejected"}</span>`; ARENA_ADV = setTimeout(nextMatch, 1000); return; }
  ARENA.voted = true;
  const ma = $("#modelA"), mb = $("#modelB");
  // the decrypt reveal: names unscramble, winner frame locks green, loser dims, pick stays lit
  decrypt(ma, data.a_model); decrypt(mb, data.b_model);
  ma.className = "arena-model " + (w === "a" ? "won" : w === "b" ? "lost" : "tie");
  mb.className = "arena-model " + (w === "b" ? "won" : w === "a" ? "lost" : "tie");
  const sa = ma.closest(".arena-side"), sb = mb.closest(".arena-side");
  if (sa) sa.classList.add(w === "a" ? "side-won" : w === "b" ? "side-lost" : "side-tie");
  if (sb) sb.classList.add(w === "b" ? "side-won" : w === "a" ? "side-lost" : "side-tie");
  const pb = document.querySelector(`.arena-vote .vote[data-w="${w}"]`);
  if (pb) pb.classList.add("picked");
  renderRanking(data.ranking);
  if (data.you) { AUTH.user = data.you; renderAuth(); }
  $("#arenaMsg").innerHTML = `<span class="ok">✓ recorded</span> — A was <b>${escH(data.a_model)}</b>, B was <b>${escH(data.b_model)}</b>. Next…`;
  ARENA_ADV = setTimeout(nextMatch, 1700);
}

async function arenaGenerate() {
  const model = ($("#model") || {}).value || "";   // launch form removed — guard
  if (!model) { $("#arenaMsg").innerHTML = `<span class="err">pick a model in the top panel first</span>`; return; }
  const kind = ARENA.kind;
  const promptId = ARENA.pinned || (ARENA.match && ARENA.match.prompt_id) ||
    (ARENA.byKind[kind] && ARENA.byKind[kind][0] && ARENA.byKind[kind][0].id);
  if (!promptId) return;
  const title = ((ARENA.byKind[kind] || []).find((p) => p.id === promptId) || {}).title || promptId;
  $("#arenaGenBtn").disabled = true;
  $("#arenaMsg").innerHTML = `<span class="spin">⟳</span> generating <b>${escH(title)}</b> with <b>${escH(model)}</b> — up to a minute…`;
  let before = 0;
  try { before = (await api(`/api/arena/artifacts?kind=${kind}&prompt_id=${promptId}`)).artifacts.length; } catch (e) {}
  try {
    await api("/api/arena/generate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, prompt_id: promptId, model,
        target_url: $("#target").value.trim(), api_key: key() || null }),
    });
  } catch (e) { $("#arenaMsg").innerHTML = `<span class="err">generate failed</span>`; $("#arenaGenBtn").disabled = false; return; }
  let tries = 0;
  const poll = setInterval(async () => {
    tries++;
    let now = before;
    try { now = (await api(`/api/arena/artifacts?kind=${kind}&prompt_id=${promptId}`)).artifacts.length; } catch { return; }
    if (now > before || tries > 240) {
      clearInterval(poll); $("#arenaGenBtn").disabled = false;
      if (now > before) { $("#arenaMsg").innerHTML = `<span class="ok">✓ generated for ${escH(title)}</span>`; loadRanking(); nextMatch(); }
      else { $("#arenaMsg").innerHTML = `<span class="warn">no artifact returned — endpoint slow or refused</span>`; }
    }
  }, 1500);
}

function renderRanking(rows) {
  $("#arenaRank tbody").innerHTML = (rows && rows.length)
    ? rows.map((r, i) =>
        `<tr${i === 0 ? ' class="top"' : ""}><td class="rank mono">${String(i + 1).padStart(2, "0")}</td><td class="mono">${fmtModel(r.model)}</td>` +
        `<td class="num"><b>${Math.round(r.elo)}</b></td><td class="num">${r.w}</td><td class="num">${r.l}</td>` +
        `<td class="num">${r.t}</td><td class="num">${Math.round(r.win_rate)}%</td></tr>`).join("")
    : `<tr><td colspan="7" style="color:var(--muted)">No counted votes yet — only verified evaluators' votes appear here.</td></tr>`;
  const games = (rows || []).reduce((s, r) => s + r.games, 0);
  $("#arenaRankNote").textContent = games ? `· ${games / 2 | 0} matchups counted` : "";
}
async function loadRanking() { try { const r = await api("/api/arena/ranking?kind=" + ARENA.kind); renderRanking(r.ranking); } catch (e) {} }

// ---- admin (integrity + moderation; tab visible only to AEON_ADMIN_USERS) ----
async function setAdmin() {
  $$("#tabs .tab").forEach((t) => t.classList.toggle("active", !!t.dataset.admin));
  $("#boardPanel").hidden = true; $("#audioPanel").hidden = true; $("#detailPanel").hidden = true;
  $("#arenaPanel").hidden = true; $("#subsPanel").hidden = true; $("#runPanel").hidden = true;
  $("#adminPanel").hidden = false; { const _r = $("#run"); if (_r) _r.style.display = "none"; }
  loadAdminBenches(); loadEvaluators(); loadAdminArtifacts();
}

// ---- admin: bench oversight (disqualify / re-judge / open any run) ----
async function loadAdminBenches() {
  const box = $("#adminBenches"); if (!box) return;
  let d; try { d = await api("/api/submissions"); } catch (e) { box.innerHTML = ""; return; }
  const rows = (d.submissions || []).slice(0, 40);
  if (!rows.length) { box.innerHTML = `<p class="note" style="text-align:left">No benches yet.</p>`; return; }
  box.innerHTML = rows.map((r) => {
    const sc = r.mean_score != null ? Math.round(r.mean_score) : "—";
    const scls = r.mean_score == null ? "" : r.mean_score >= 80 ? "pass" : r.mean_score >= 40 ? "part" : "fail";
    const t = fmtDT(r.started_at);
    const trust = r.trust_tier === "attested" ? `<span class="elig-badge verified">✓</span>` : "";
    const flag = r.flagged ? `<span class="ev-badge bad" title="${escA(r.flag_reason || "disqualified")}">disqualified</span>` : "";
    return `<div class="bench-row${r.flagged ? " flagged" : ""}" data-run="${escA(r.id)}">
      <span class="bench-t">${escH(t)}</span>
      <span class="bench-m mono">${fmtModel(r.model)}</span>
      <span class="subs-b">${escH(r.board)}</span>${trust}${flag}
      <span class="bench-s mono ${scls}">${sc}</span>
      <span class="bench-acts">
        <button class="ghost ev-act bench-open" data-id="${escA(r.id)}">open</button>
        <button class="ghost ev-act bench-rejudge" data-id="${escA(r.id)}">re-judge</button>
        ${r.flagged
          ? `<button class="ghost ev-act bench-flag" data-id="${escA(r.id)}" data-f="0">restore</button>`
          : `<button class="ghost ev-act bench-flag warnact" data-id="${escA(r.id)}" data-f="1">disqualify</button>`}
      </span></div>`;
  }).join("");
  $$("#adminBenches .bench-open").forEach((b) => b.onclick = () => {
    const t = $("#tabs [data-subs]"); if (t) t.click();
    openSubmission(b.dataset.id);
  });
  $$("#adminBenches .bench-flag").forEach((b) => b.onclick = async () => {
    const flagged = b.dataset.f === "1";
    const reason = flagged ? (prompt("Reason for disqualifying this bench?") || "disqualified by admin") : null;
    if (flagged && reason === null) return;
    await fetch("/api/admin/run/flag", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ run_id: b.dataset.id, flagged, reason }) });
    loadAdminBenches();
  });
  $$("#adminBenches .bench-rejudge").forEach((b) => b.onclick = async () => {
    if (!confirm("Reset this bench's Tier-1 cases to pending for re-judging?")) return;
    await fetch("/api/admin/run/rejudge", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ run_id: b.dataset.id }) });
    loadAdminBenches();
  });
}

async function loadEvaluators() {
  let d;
  try {
    const r = await fetch("/api/admin/evaluators", { headers: authHeaders() });
    if (!r.ok) throw r; d = await r.json();
  } catch (e) { $("#adminSummary").innerHTML = `<span class="err">not authorized</span>`; return; }
  $("#adminSummary").innerHTML =
    `trust bar <b>${Math.round(d.threshold * 100)}%</b> honeypot accuracy · ${d.total} account(s) · ` +
    `<span style="color:var(--good)">${d.eligible} counted</span> · ` +
    `<span style="color:var(--warn)">${d.below_bar} below bar</span> · ` +
    `<span style="color:var(--bad)">${d.banned} banned</span>`;
  renderEvaluators(d.evaluators || []);
}

function renderEvaluators(rows) {
  $("#evalTable tbody").innerHTML = rows.map((r) => {
    const adj = r.adjudicated;
    const acc = adj ? `${(r.accuracy * 100).toFixed(0)}%` : "—";
    const hp = adj ? `${r.passed}/${adj}` : "0";
    const standing = r.status !== "active"
      ? `<span class="ev-badge bad">banned</span>`
      : r.eligible ? `<span class="ev-badge ok">counted</span>`
        : adj ? `<span class="ev-badge pending">below bar</span>` : `<span class="ev-badge pending">pending</span>`;
    const adminB = r.admin ? ` <span class="ev-badge ok">admin</span>` : "";
    const action = r.admin ? "" : r.status === "active"
      ? `<button class="ghost ev-act" data-act="ban" data-id="${escA(r.id)}">ban</button>`
      : `<button class="ghost ev-act" data-act="unban" data-id="${escA(r.id)}">reinstate</button>`;
    return `<tr><td class="mono">${escH(r.username)}${adminB}</td><td class="num">${hp}</td>` +
      `<td class="num">${acc}</td><td>${standing}</td><td class="num">${r.eligible ? r.real_votes : 0}</td>` +
      `<td class="num">${r.votes}</td><td>${action} <button class="ghost ev-act ev-hist" data-id="${escA(r.id)}">history</button></td></tr>` +
      `<tr class="evh-row" data-for="${escA(r.id)}" hidden><td colspan="7" class="evh-cell"></td></tr>`;
  }).join("") || `<tr><td colspan="7" style="color:var(--muted)">No evaluators yet.</td></tr>`;
  $$("#evalTable .ev-act:not(.ev-hist)").forEach((b) => b.onclick = async () => {
    await fetch("/api/admin/" + b.dataset.act, {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ user_id: b.dataset.id }),
    });
    loadEvaluators();
  });
  // vote-trail drill-down: the evidence behind each trust score
  $$("#evalTable .ev-hist").forEach((b) => b.onclick = async () => {
    const row = document.querySelector(`#evalTable .evh-row[data-for="${cssEsc(b.dataset.id)}"]`);
    if (!row) return;
    if (!row.hidden) { row.hidden = true; return; }               // toggle closed
    row.hidden = false;
    const cell = row.querySelector(".evh-cell");
    cell.innerHTML = skel(4, 12);
    let d; try {
      d = await fetch("/api/admin/evaluator/history?user_id=" + encodeURIComponent(b.dataset.id),
        { headers: authHeaders() }).then((r) => r.json());
    } catch (e) { cell.innerHTML = `<span class="err">failed to load</span>`; return; }
    const votes = d.votes || [];
    if (!votes.length) { cell.innerHTML = `<span class="note" style="text-align:left">No votes yet.</span>`; return; }
    cell.innerHTML = `<div class="evh-list">` + votes.map((v) => {
      const t = fmtDT(v.ts);
      const hp = v.is_test
        ? (v.test_passed === 1 ? `<span class="ev-badge ok">honeypot ✓</span>`
          : v.test_passed === 0 ? `<span class="ev-badge bad">honeypot ✗</span>`
          : `<span class="ev-badge pending">honeypot —</span>`)
        : "";
      return `<div class="evh-vote">
        <span class="bench-t">${escH(t)}</span>
        <span class="subs-b">${escH(v.kind)}</span>
        <span class="mono evh-p">${escH(v.prompt_id)}</span>
        <span class="evh-w">picked <b>${escH(v.winner || "—")}</b></span>${hp}</div>`;
    }).join("") + `</div>`;
  });
}

async function loadAdminArtifacts() {
  let d;
  try { d = await fetch("/api/admin/artifacts?kind=" + $("#adminKind").value, { headers: authHeaders() }).then((r) => r.json()); }
  catch (e) { return; }
  const rows = d.artifacts || [];
  $("#adminArtifacts").innerHTML = rows.map((a) =>
    `<div class="art-card">
      <div class="art-meta"><b class="mono">${escH(a.model)}</b><br><span class="note">${escH(a.prompt_id)} · ${(a.bytes / 1024).toFixed(1)}KB${a.ok ? "" : " · broken"}</span></div>
      <iframe class="art-frame" sandbox="allow-scripts" data-id="${escA(a.id)}"></iframe>
      <div class="art-actions"><button class="ghost art-del" data-id="${escA(a.id)}">delete</button></div>
    </div>`).join("") || `<p class="note" style="text-align:left">No generated artifacts in this category yet.</p>`;
  $$("#adminArtifacts .art-frame").forEach(async (fr) => {
    try { const a = await fetch("/api/admin/artifact/" + fr.dataset.id, { headers: authHeaders() }).then((r) => r.json()); fr.srcdoc = a.html || ""; }
    catch (e) {}
  });
  $$("#adminArtifacts .art-del").forEach((b) => b.onclick = async () => {
    if (!confirm("Delete this artifact permanently?")) return;
    await fetch("/api/admin/artifact_delete", {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ artifact_id: b.dataset.id }),
    });
    loadAdminArtifacts();
  });
}

// ---- Submissions transparency browser (every run fully inspectable) ----
const SUBS = { board: "", model: null };

function setSubs(model) {
  $$("#tabs .tab").forEach((t) => t.classList.toggle("active", !!t.dataset.subs));
  ["#boardPanel", "#audioPanel", "#detailPanel", "#arenaPanel", "#adminPanel", "#runPanel"].forEach((s) => { const e = $(s); if (e) e.hidden = true; });
  $("#subsPanel").hidden = false; { const _r = $("#run"); if (_r) _r.style.display = "none"; }
  SUBS.model = model || null;
  loadSubs();
}
function openSubmissionsFor(model) { setSubs(model); }

async function loadSubs() {
  const q = [];
  if (SUBS.board) q.push("board=" + SUBS.board);
  if (SUBS.model) q.push("model=" + encodeURIComponent(SUBS.model));
  $("#subsList").innerHTML = skel(6, 30);
  let d;
  try { d = await api("/api/submissions" + (q.length ? "?" + q.join("&") : "")); }
  catch (e) {
    $("#subsList").innerHTML = `<p class="board-empty"><b class="err">✗ link down</b> — could not load submissions. <button class="ghost" id="sRetry">↻ retry</button></p>`;
    const rb = $("#sRetry"); if (rb) rb.onclick = loadSubs;
    return;
  }
  renderSubsList(d.submissions || []);
}

// one time grammar, instrument-style: 2026-07-02 for days, 24h clocks for rows
const _fmtTime = (ts) => fmtDT(ts);
const _dayKey = (ts) => fmtDate(ts);

function renderSubsList(rows) {
  const hdr = SUBS.model
    ? `<div class="subs-filter">for <b>${escH(SUBS.model)}</b> · <button class="ghost" id="subsClear">all models</button></div>` : "";
  const groups = {};
  rows.forEach((r) => { (groups[_dayKey(r.started_at)] = groups[_dayKey(r.started_at)] || []).push(r); });
  const body = Object.keys(groups).map((day) =>
    `<div class="subs-day">${escH(day)}</div>` + groups[day].map((r) => {
      const flag = r.flagged ? ` <span class="ev-badge bad">bad</span>` : "";
      const sc = r.mean_score != null ? Math.round(r.mean_score) : "—";
      // score color = verdict band, not always-green: green must MEAN good
      const scls = r.mean_score == null ? "" : r.mean_score >= 80 ? " pass" : r.mean_score >= 40 ? " part" : " fail";
      const t = r.started_at ? fmtClock(r.started_at).slice(0, 5) : "—";
      return `<div class="subs-row${r.flagged ? " flagged" : ""}" data-run="${escA(r.id)}">
        <a class="model-creator subs-ava" data-meta="${escA(r.model)}" target="_blank" rel="noopener noreferrer" title="creator profile">
          <img class="model-avatar" data-meta-avatar="${escA(r.model)}" src="/static/generic-avatar.svg" alt="" loading="lazy" width="36" height="36"></a>
        <div class="subs-id"><span class="subs-m">${fmtModel(r.model)}</span>
          <span class="subs-tags"><span class="subs-b">${escH(r.board)}</span><span class="subs-st">${escH(r.status)}</span>${flag}</span></div>
        <span class="subs-s${scls}">${sc}</span><span class="subs-t">${t}</span></div>`;
    }).join("")).join("") || `<p class="note" style="text-align:left">No submissions${SUBS.model ? " for this model" : ""} yet.</p>`;
  $("#subsList").innerHTML = hdr + body;
  const clr = $("#subsClear"); if (clr) clr.onclick = () => setSubs(null);
  $$("#subsList .subs-row").forEach((el) => el.onclick = (ev) => {
    if (ev.target.closest(".subs-ava")) return;                // avatar click = creator link, not open-run
    $$("#subsList .subs-row").forEach((x) => x.classList.remove("sel")); el.classList.add("sel");
    openSubmission(el.dataset.run);
  });
  [...new Set(rows.map((r) => r.model))].forEach((model) => {  // hydrate avatars (same mechanism as the board)
    const cached = META.get(model);
    if (cached && cached !== "pending") applyMeta(model, cached); else fetchMeta(model);
  });
}

async function openSubmission(runId) {
  $("#subsDetail").innerHTML = `<div class="sub-cases">${skel(8, 16)}</div>`;
  let d; try { d = await api("/api/submissions/" + runId); }
  catch (e) { $("#subsDetail").innerHTML = `<p class="err">failed to load</p>`; return; }
  renderSubmissionDetail(d);
}

function _rationale(c) {
  const e = c.evidence || {};
  // agentic-v2 (harness) evidence is a LIST of {criterion, ok, detail} — one row per checked criterion.
  if (Array.isArray(e)) {
    if (!e.length) return `<span class="note">no criteria recorded</span>`;
    return e.map((k) => `${k.ok ? "✓" : "✗"} <span class="crit">${escH(k.criterion || "")}</span>`
      + (k.detail ? ` <span class="crit-detail">${escH(k.detail)}</span>` : "")).join("<br>");
  }
  if (e.checkers) return e.checkers.map((k) => `${k.satisfied ? "✓" : "✗"} <span class="mono">${escH(k.type)}</span> ${escH(k.evidence || "")}`).join("<br>");
  if (e.criteria) return e.criteria.map((k) => `${k.satisfied ? "✓" : "✗"} <b>${escH(k.id)}</b> <span class="subs-by">[${escH(k.decided_by)}]</span> ${escH(k.evidence || "")}`).join("<br>");
  if (e.pending) return `<span class="note">awaiting judgement</span>`;
  return escH(JSON.stringify(e).slice(0, 200));
}

// Render a harness case's tool-call trajectory: one row per step (TOOL + its args as JSON).
function _trajectory(c) {
  const t = c.trajectory || [];
  if (c.harness_error) return `<div class="traj-err">harness error: ${escH(c.harness_error)}</div>`;
  if (!t.length) return `<div class="traj-empty">no tool calls recorded</div>`;
  return `<ol class="traj">` + t.map((s) => {
    let args = "";
    if (s.args != null) { try { args = typeof s.args === "string" ? s.args : JSON.stringify(s.args); } catch (e) { args = String(s.args); } }
    return `<li class="traj-step"><span class="traj-tool">${escH(s.tool || "?")}</span>`
      + (args ? ` <span class="traj-args">${escH(args)}</span>` : "") + `</li>`;
  }).join("") + `</ol>`;
}

function renderSubmissionDetail(d) {
  const r = d.run, admin = AUTH.user && AUTH.user.admin;
  const flagBtn = admin ? (r.flagged ? `<button class="ghost" id="subUnflag">un-flag</button>`
    : `<button class="ghost" id="subFlag">flag as bad bench</button>`) : "";
  const rejudge = admin ? `<button class="ghost" id="subRejudge">re-judge Tier-1</button>` : "";
  const judge = r.judge_is_self ? `self (${escH(r.model)})` : escH(r.judge_model || "—");
  const meta = `<div class="sub-meta">
    <h3>${escH(r.model)} <span class="tag">${escH(r.board)}</span> ${r.flagged ? '<span class="ev-badge bad">bad bench</span>' : ""}</h3>
    <div class="note" style="text-align:left">run <span class="mono">${escH(r.id)}</span> · ${escH(r.status)} · ${escH(r.n_cases)} cases · ${_fmtTime(r.started_at)}<br>
      judge: <b>${judge}</b> · suite ${escH(r.suite_id)} <span class="mono">${escH(r.suite_hash || "")}</span>${r.bench_seed ? ' · fast-bench seed <span class="mono cmp-seedtag">' + escH(r.bench_seed) + "</span>" : ""} ·
      <a class="mlink" href="${escA(d.manifest_url)}" target="_blank">signed manifest ↗</a></div>
    <div class="sub-actions">${flagBtn} ${rejudge}</div>
    ${r.flag_reason ? `<div class="note err" style="text-align:left">flag reason: ${escH(r.flag_reason)}</div>` : ""}</div>`;
  const cases = (d.cases || []).map((c) => {
    const sc = c.score == null ? (c.status === "tier1_pending" ? "pending" : "—") : (c.score * 100).toFixed(0);
    const cls = c.score == null ? "" : c.score >= 0.8 ? "pass" : c.score >= 0.4 ? "part" : "fail";
    const cr = (c.creativity != null && c.creativity > 0) ? ` <span class="ev-badge ok">+${c.creativity} creativity</span>` : "";
    const head = `<div class="sub-case-h"><span class="mono">${escH(c.case_id)}</span> <span class="tag">${escH(c.category)} · T${c.tier}</span>
        <span class="sub-score ${cls}">${sc}</span>${cr}${c.disputed ? ` <span class="ev-badge disputed" title="${escA(c.disputed_reason || "")}">⚠ agent-judge: likely checker false-negative</span>` : ""}<span class="subs-by">judged by: ${escH(c.judged_by)}</span></div>`;
    if (c.harness_case) {
      // harness transparency: what the agent was ASKED, the TOOL-CALL trajectory it ran, what it
      // finally ANSWERED, and the deterministic per-criterion JUDGEMENT.
      return `<div class="sub-case harness-case">
      ${head}
      <div class="sub-q"><b>asked:</b> ${escH(c.prompt)}</div>
      <div class="sub-traj"><b class="micro">tool calls</b>${_trajectory(c)}</div>
      <div class="sub-a"><b>answered:</b><pre>${escH(c.final_answer != null ? c.final_answer : c.answer)}</pre></div>
      <div class="sub-r"><b>judgement:</b> ${_rationale(c)}</div></div>`;
    }
    return `<div class="sub-case">
      ${head}
      <div class="sub-q"><b>asked:</b> ${escH(c.prompt)}</div>
      <div class="sub-a"><b>answered:</b><pre>${escH(c.answer)}</pre></div>
      <div class="sub-r"><b>judgement:</b> ${_rationale(c)}</div></div>`;
  }).join("");
  $("#subsDetail").innerHTML = meta + `<div class="sub-cases">${cases}</div>`;
  const fb = $("#subFlag"); if (fb) fb.onclick = () => _flagRun(r.id, true);
  const ub = $("#subUnflag"); if (ub) ub.onclick = () => _flagRun(r.id, false);
  const rj = $("#subRejudge"); if (rj) rj.onclick = () => _rejudgeRun(r.id);
}

async function _flagRun(runId, flagged) {
  const reason = flagged ? (prompt("Reason for flagging this run as a bad bench?") || "flagged by admin") : null;
  await fetch("/api/admin/run/flag", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ run_id: runId, flagged, reason }) });
  loadSubs(); openSubmission(runId);
}
async function _rejudgeRun(runId) {
  if (!confirm("Reset this run's Tier-1 cases to pending for re-judging?")) return;
  await fetch("/api/admin/run/rejudge", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ run_id: runId }) });
  openSubmission(runId);
}

function setBoard(name) {
  active = name;
  $$("#tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.board === name));
  loadBoard();
}

// ---- AI Harness evaluation: model × {Hermes, OpenClaw, OpenCode} matrix ----
async function setHarness() {
  active = "harness";
  $$("#tabs .tab").forEach((t) => t.classList.toggle("active", !!t.dataset.harness));
  ["#boardPanel", "#audioPanel", "#arenaPanel", "#subsPanel", "#adminPanel", "#detailPanel", "#runPanel"]
    .forEach((s) => { const e = $(s); if (e) e.hidden = true; });
  const hp = $("#harnessPanel"); if (hp) hp.hidden = false;
  { const _r = $("#run"); if (_r) _r.style.display = "none"; }
  $("#harnessMatrix").innerHTML = skel(5, 20);
  try { HARNESS = await api("/api/harness_board"); } catch (e) { HARNESS = null; }
  renderHarnessMatrix();
}

function renderHarnessMatrix() {
  const wrap = $("#harnessMatrix"); if (!wrap) return;
  const d = HARNESS;
  if (!d || !d.models || !d.models.length) {
    wrap.innerHTML = `<p class="board-empty">No harness runs yet. This board fills as models are benchmarked through the three agentic harnesses by the controlled pod — each cell shows the model's agentic score on that harness, with the exact <b>release version</b> disclosed.</p>`;
    return;
  }
  const hs = d.harnesses, meta = d.harness_meta || {};
  const head = `<tr><th class="model">model</th>` + hs.map((h) => {
    const m = meta[h] || {};
    return `<th class="num hcol"><a href="${escA(m.repo || "#")}" target="_blank" rel="noopener">${escH(m.name || h)}</a></th>`;
  }).join("") + `</tr>`;
  const body = d.models.map((mdl) => {
    const row = d.matrix[mdl] || {};
    // heat-tint carries magnitude; the best harness per row gets weight + a green dot
    const best = Math.max(...hs.map((h) => (row[h] && row[h].score != null) ? row[h].score : -1));
    return `<tr><td class="model"><span class="hmodel">
        <a class="model-creator" data-meta="${escA(mdl)}" target="_blank" rel="noopener noreferrer" title="creator profile">
          <img class="model-avatar" data-meta-avatar="${escA(mdl)}" src="/static/generic-avatar.svg" alt="" loading="lazy" width="34" height="34"></a>
        <span class="hmodel-name">${fmtModel(mdl)}</span></span></td>` + hs.map((h) => {
      const c = row[h];
      if (!c || c.score == null) return `<td class="num hcell na">—</td>`;
      const hb = best >= 0 && c.score === best ? " hbest" : "";
      // clickable → drill into the per-case transparency for this model × harness run
      return `<td class="num hcell hclick${hb}" style="--s:${(c.score / 100).toFixed(3)}"`
        + ` data-model="${escA(c.model || mdl)}" data-harness="${escA(h)}" tabindex="0" role="button"`
        + ` title="${escA(c.harness_name || h)} ${escA(c.harness_version || "")} · ${c.n_cases} cases — click to inspect">`
        + `<span class="hscore">${c.score.toFixed(1)}</span> <span class="hver">${escH(fmtHver(c.harness_version))}</span></td>`;
    }).join("") + `</tr>`;
  }).join("");
  wrap.innerHTML = `<table class="harness-tbl"><thead>${head}</thead><tbody>${body}</tbody></table>`;
  wrap.querySelectorAll("td.hclick").forEach((td) => {
    const open = () => openHarnessCell(td.dataset.model, td.dataset.harness);
    td.onclick = open;
    td.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
  });
  [...new Set(d.models)].forEach((model) => {                 // hydrate creator avatars
    const cached = META.get(model);
    if (cached && cached !== "pending") applyMeta(model, cached); else fetchMeta(model);
  });
}

// Drill from a harness-matrix cell into the per-case transparency of its underlying run. A cell
// can aggregate several runs (same model × harness); open the most recent one.
async function openHarnessCell(model, harness) {
  let d;
  try { d = await api(`/api/harness_runs?model=${encodeURIComponent(model)}&harness=${encodeURIComponent(harness)}`); }
  catch (e) { return; }
  const runs = (d && d.runs) || [];
  if (!runs.length) return;
  // switch to the submissions panel (reusing its detail pane) and open the newest run
  active = "subs";
  $$("#tabs .tab").forEach((t) => t.classList.toggle("active", !!t.dataset.subs));
  ["#boardPanel", "#audioPanel", "#detailPanel", "#arenaPanel", "#adminPanel", "#runPanel", "#harnessPanel", "#comparePanel", "#livePanel"]
    .forEach((s) => { const e = $(s); if (e) e.hidden = true; });
  $("#subsPanel").hidden = false; { const _r = $("#run"); if (_r) _r.style.display = "none"; }
  SUBS.model = runs[0].model || model; loadSubs();
  openSubmission(runs[0].run_id);
}

// ---- Compare by seed: a TRUE A/B — every model on the IDENTICAL fast-bench questions ----
let CMP = { seeds: [], data: null };
async function setCompare() {
  active = "compare";
  $$("#tabs .tab").forEach((t) => t.classList.toggle("active", !!t.dataset.compare));
  ["#boardPanel", "#audioPanel", "#arenaPanel", "#subsPanel", "#adminPanel", "#detailPanel", "#harnessPanel", "#runPanel"]
    .forEach((s) => { const e = $(s); if (e) e.hidden = true; });
  const cp = $("#comparePanel"); if (cp) cp.hidden = false;
  { const _r = $("#run"); if (_r) _r.style.display = "none"; }
  try { CMP.seeds = (await api("/api/compare/seeds")).seeds || []; } catch (e) { CMP.seeds = []; }
  const sel = $("#cmpSeed");
  const pick = document.querySelector(".cmp-pick");
  if (!CMP.seeds.length) {
    if (pick) pick.hidden = true;                 // never show a dead, empty control
    if (sel) sel.innerHTML = "";
    $("#cmpBadge").textContent = "";
    $("#cmpBody").innerHTML = `<p class="board-empty">No fast-bench seeds yet. Run a <span class="mono">--fast</span> bench (one question per category × difficulty); pass a shared <span class="mono">--seed</span> so every model answers the identical questions, then compare them here.</p>`;
    return;
  }
  if (pick) pick.hidden = false;
  sel.innerHTML = CMP.seeds.map((s) =>
    `<option value="${escA(s.seed)}">${escH(s.seed)} — ${s.n_models} model${s.n_models === 1 ? "" : "s"}${s.suite_consistent ? "" : " ⚠ mixed suite"}</option>`).join("");
  loadCompare(CMP.seeds[0].seed);
}

async function loadCompare(seed) {
  $("#cmpBody").innerHTML = skel(10);
  let d; try { d = await api("/api/compare/" + encodeURIComponent(seed)); }
  catch (e) { $("#cmpBody").innerHTML = `<p class="err">failed to load</p>`; return; }
  CMP.data = d; renderCompare(d);
}

function renderCompare(d) {
  const badge = $("#cmpBadge");
  badge.innerHTML = d.suite_consistent
    ? `<span class="cmp-ab ok">✓ true A/B — identical questions</span> <span class="note mono">suite ${escH(d.suite_hash || "")}</span>`
    : `<span class="cmp-ab warn">⚠ models ran different suite versions — not a clean A/B</span>`;
  const ms = d.models || [];
  if (!ms.length) { $("#cmpBody").innerHTML = `<p class="board-empty">No runs for this seed.</p>`; return; }
  const mhead = (m) => `${escH(m.model.split("/").pop())}` +
    (m.trust_tier === "attested" ? ' <span class="elig-badge verified" title="attested / globally ranked">✓</span>' : "");
  // by category: rows = categories then composite, winner per row highlighted
  const row = (label, vals, cls) => {
    const nums = vals.map((v) => (v == null ? null : Number(v)));
    const present = nums.filter((v) => v != null);
    const best = present.length ? Math.max(...present) : null;
    return `<tr class="${cls || ""}"><td class="cmp-cat">${escH(label)}</td>` + nums.map((v) =>
      `<td class="num${v != null && best > 0 && v === best ? " cmp-win" : ""}">${v == null ? "—" : v.toFixed(1)}</td>`).join("") + `</tr>`;
  };
  const head = `<tr><th>category</th>` + ms.map((m) => `<th class="num">${mhead(m)}</th>`).join("") + `</tr>`;
  const catRows = d.categories.map((c) => row(c, ms.map((m) => m.categories[c]))).join("");
  const compRow = row("composite", ms.map((m) => m.composite), "cmp-comp");
  const catTbl = `<table class="cmp-tbl"><thead>${head}</thead><tbody>${catRows}${compRow}</tbody></table>`;
  // by question: same 20 questions across all models — ✓ / ✗ / partial per model
  const mark = (v) => v == null ? `<td class="num cmp-q na">—</td>`
    : v >= 0.999 ? `<td class="num cmp-q pass">✓</td>`
    : v <= 0.001 ? `<td class="num cmp-q fail">✗</td>`
    : `<td class="num cmp-q part">${Math.round(v * 100)}</td>`;   // same 0-100 grammar as every score
  const cHead = `<tr><th>tier</th><th>question</th>` + ms.map((m) => `<th class="num">${escH(m.model.split("/").pop())}</th>`).join("") + `</tr>`;
  const cRows = d.cases.map((c) =>
    `<tr><td class="cmp-diff t-${escA(c.difficulty || "")}">${escH(c.difficulty || "")}</td>` +
    `<td class="cmp-cid mono" title="${escA(c.category + " · " + c.case_id)}">${escH(c.case_id)}</td>` +
    ms.map((m) => mark(c.scores[m.model])).join("") + `</tr>`).join("");
  const caseTbl = `<table class="cmp-tbl cmp-cases"><thead>${cHead}</thead><tbody>${cRows}</tbody></table>`;
  $("#cmpBody").innerHTML =
    `<div class="cmp-sec"><h3>By category <span class="note">— bold = leads that category</span></h3>${catTbl}</div>` +
    `<div class="cmp-sec"><h3>By question <span class="note">— ✓ correct · ✗ wrong · all models got the SAME ${d.cases.length} questions</span></h3>${caseTbl}</div>`;
}

// ---- Live benchmark view: watch a RUNNING controlled run (per-category progress + prompt/answer feed) ----
let LIVE_TIMER = null;
async function setLive() {
  active = "live";
  $$("#tabs .tab").forEach((t) => t.classList.toggle("active", !!t.dataset.live));
  ["#boardPanel", "#audioPanel", "#arenaPanel", "#subsPanel", "#adminPanel", "#detailPanel", "#harnessPanel", "#comparePanel", "#runPanel"]
    .forEach((s) => { const e = $(s); if (e) e.hidden = true; });
  const lp = $("#livePanel"); if (lp) lp.hidden = false;
  { const _r = $("#run"); if (_r) _r.style.display = "none"; }
  await pollLive();
  if (LIVE_TIMER) clearInterval(LIVE_TIMER);
  LIVE_TIMER = setInterval(() => {                     // auto-refresh while the tab is active
    if (active === "live") pollLive();
    else { clearInterval(LIVE_TIMER); LIVE_TIMER = null; }
  }, 5000);
}

let LIVE_FAILS = 0;
async function pollLive() {
  let d;
  try { d = await api("/api/live"); LIVE_FAILS = 0; }
  catch (e) {
    // after 2 consecutive failures the REC light must stop lying
    if (++LIVE_FAILS >= 2) {
      const dot = $("#liveDot"); if (dot) dot.classList.remove("on");
      const lt = $("#tabs [data-live]"); if (lt) lt.classList.remove("has-live");
      if (active === "live" && LIVE_FAILS === 2)
        $("#liveBody").insertAdjacentHTML("afterbegin", `<p class="note err" style="text-align:left">stream lost — retrying…</p>`);
    }
    return;
  }
  renderLive(d);
}

// only genuinely NEW feed cases animate on each poll (innerHTML rebuilds everything).
// One seen-set PER RUN (keyed on the server's `run` id), pruned as runs finish — correct
// with multiple concurrent runs and with a killed+relaunched run of the same model.
let LIVE_SEEN_MAP = new Map();

function renderLive(d) {
  const runs = (d && d.running) || [];
  const dot = $("#liveDot"); if (dot) dot.classList.toggle("on", runs.length > 0);
  const lt = $("#tabs [data-live]"); if (lt) lt.classList.toggle("has-live", runs.length > 0);
  if (!runs.length) {
    LIVE_SEEN_MAP.clear();
    $("#liveBody").innerHTML = `<p class="board-empty">No benchmark is running right now. When a controlled pod is mid-run, its per-category progress and the prompts + answers stream here live.</p>`;
    return;
  }
  const liveKeys = new Set(runs.map((r) => r.run || r.run_id || r.id || r.model || "?"));
  [...LIVE_SEEN_MAP.keys()].forEach((k) => { if (!liveKeys.has(k)) LIVE_SEEN_MAP.delete(k); });
  // the 5s innerHTML rebuild must not steal the operator's reading position
  const _feedScroll = [...document.querySelectorAll("#liveBody .live-feed")].map((e) => e.scrollTop);
  const _preScroll = [...document.querySelectorAll("#liveBody .live-a pre")].map((e) => e.scrollTop);
  $("#liveBody").innerHTML = runs.map((r) => {
    const runKey = r.run || r.run_id || r.id || r.model || "?";
    let LIVE_SEEN = LIVE_SEEN_MAP.get(runKey);
    if (!LIVE_SEEN) { LIVE_SEEN = new Set(); LIVE_SEEN_MAP.set(runKey, LIVE_SEEN); }
    const pct = r.n_cases ? Math.round(100 * r.done / r.n_cases) : 0;
    const cats = r.categories.map((c) => {
      const cpct = c.expected ? Math.min(100, Math.round(100 * c.done / c.expected)) : 0;
      const mb = c.mean == null ? "" : c.mean >= 80 ? "pass" : c.mean >= 40 ? "part" : "fail";
      const mean = c.mean == null ? "—" : `<b class="${mb}">${c.mean.toFixed(0)}%</b>`;
      return `<div class="live-cat">
        <div class="live-cat-h"><span>${escH(c.category)}</span><span class="mono note">${c.done}/${c.expected} · ${mean}</span></div>
        <div class="live-bar"><div class="live-bar-fill" style="width:${cpct}%"></div></div></div>`;
    }).join("");
    const feed = (r.recent || []).map((x) => {
      const cls = x.score == null ? "part" : x.score >= 0.999 ? "pass" : x.score <= 0.001 ? "fail" : "part";
      const mk = x.score == null ? "…" : x.score >= 0.999 ? "✓" : x.score <= 0.001 ? "✗" : (x.score * 100).toFixed(0);
      const dsp = x.disputed ? ` <span class="ev-badge disputed" title="agent-judge: likely checker false-negative">⚠</span>` : "";
      const isNew = !LIVE_SEEN.has(x.case_id); LIVE_SEEN.add(x.case_id);
      return `<div class="live-case ${cls}${isNew ? " is-new" : ""}">
        <div class="live-case-h"><span class="live-mk ${cls}">${mk}</span> <span class="mono">${escH(x.case_id)}</span> <span class="tag">${escH(x.category)}</span>${dsp}</div>
        <div class="live-q"><b>Q</b> ${escH(cut(x.prompt || "", 320))}</div>
        <div class="live-a"><b>A</b> <pre>${escH(cut(x.answer || "(no answer yet)", 1200))}</pre></div></div>`;
    }).join("");
    const _mp = (r.model || "").split("/");
    const mName = _mp[_mp.length - 1] || "model";               // the model being tested (real repo, not the served alias)
    const mOrg = _mp.length > 1 ? _mp.slice(0, -1).join("/") + "/" : "";
    return `<div class="live-run">
      <div class="live-run-h"><b>${escH(mName)}</b>
        <span class="elig-badge run" title="a benchmark is running against this model right now">● Benchmarking Live</span>
        ${mOrg ? `<span class="note mono">${escH(mOrg)}</span>` : ""}
        <span class="mono">${r.done}/${r.n_cases} · ${pct}%</span>${r.mean != null ? ` · mean <b>${r.mean.toFixed(1)}</b>` : ""}
        ${r.trust_tier === "attested" ? ' <span class="elig-badge verified">✓ attested</span>' : ""}</div>
      <div class="live-bar big"><div class="live-bar-fill" style="width:${pct}%"></div></div>
      <div class="live-cats">${cats}</div>
      <h4 class="live-feed-h">latest answers</h4>
      <div class="live-feed">${feed}</div></div>`;
  }).join("");
  [...document.querySelectorAll("#liveBody .live-feed")].forEach((e, i) => { if (_feedScroll[i]) e.scrollTop = _feedScroll[i]; });
  [...document.querySelectorAll("#liveBody .live-a pre")].forEach((e, i) => { if (_preScroll[i]) e.scrollTop = _preScroll[i]; });
}

// ---- POD Run tab: launch benchmarks (endpoint / verified-HF) + manage saved keys (pod-only) ----
const RUN = { keys: [], jobsTimer: null };

function podToken() { try { return localStorage.getItem("aeon_pod_token") || ""; } catch (e) { return ""; } }
function podHeaders(extra) {                         // inject the optional lab token on every pod call
  const h = Object.assign({}, extra || {});
  const t = podToken();
  if (t) h["x-aeon-pod-token"] = t;
  return h;
}
function runStatus(msg, cls) {
  const el = $("#runStatus"); if (el) el.innerHTML = `<span class="${cls || ""}">${escH(msg)}</span>`;
}

async function setRun() {
  active = "run";
  $$("#tabs .tab").forEach((t) => t.classList.toggle("active", !!t.dataset.run));
  ["#boardPanel", "#audioPanel", "#arenaPanel", "#subsPanel", "#adminPanel", "#detailPanel", "#harnessPanel", "#comparePanel", "#livePanel"]
    .forEach((s) => { const e = $(s); if (e) e.hidden = true; });
  const rp = $("#runPanel"); if (rp) rp.hidden = false;
  { const _r = $("#run"); if (_r) _r.style.display = "none"; }
  await loadSavedKeys();
  await pollJobs();
  if (RUN.jobsTimer) clearInterval(RUN.jobsTimer);
  RUN.jobsTimer = setInterval(() => {                // refresh job progress while the tab is active
    if (active === "run") pollJobs();
    else { clearInterval(RUN.jobsTimer); RUN.jobsTimer = null; }
  }, 3000);
}

async function loadSavedKeys() {
  try { const d = await api("/api/pod/keys", { headers: podHeaders() }); RUN.keys = d.keys || []; }
  catch (e) { RUN.keys = []; }
  renderKeys();
  const fill = (sel, opts, none) => {
    const el = $(sel); if (!el) return;
    el.innerHTML = `<option value="">${none}</option>` +
      opts.map((k) => `<option value="${escA(k.name)}">${escH(k.name)} (${escH(k.masked)})</option>`).join("");
  };
  fill("#reKey", RUN.keys.filter((k) => k.kind !== "hf_token"), "— none —");
  fill("#hfKey", RUN.keys.filter((k) => k.kind === "hf_token"), "— public —");
}

function renderKeys() {
  const box = $("#savedKeys"); if (!box) return;
  if (!RUN.keys.length) { box.innerHTML = `<p class="note" style="text-align:left">No saved keys yet.</p>`; return; }
  box.innerHTML = RUN.keys.map((k) => `<div class="key-row">
    <span class="key-label mono">${escH(k.name)}</span>
    <span class="tag">${escH(k.kind)}</span>
    <span class="key-mask mono">${escH(k.masked)}</span>
    <button class="ghost key-del" data-name="${escA(k.name)}">delete</button></div>`).join("");
  $$(".key-del").forEach((b) => b.onclick = () => deleteKey(b.dataset.name));
}

async function addKey() {
  const name = $("#keyName").value.trim(), value = $("#keyVal").value, kind = $("#keyKind").value;
  if (!name || !value) { runStatus("name and value are required", "err"); return; }
  try {
    await api("/api/pod/keys", { method: "POST", headers: podHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ name, value, kind }) });
  } catch (e) { runStatus("save failed: " + JSON.stringify(e), "err"); return; }
  $("#keyName").value = ""; $("#keyVal").value = "";
  runStatus("saved '" + name + "'", "ok");
  loadSavedKeys();
}

async function deleteKey(name) {
  try {
    await api("/api/pod/keys/delete", { method: "POST", headers: podHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ name }) });
  } catch (e) { runStatus("delete failed", "err"); return; }
  loadSavedKeys();
}

async function runEndpointBench() {
  const base_url = $("#reBase").value.trim(), model = $("#reModel").value.trim();
  if (!model) { runStatus("model name is required", "err"); return; }
  await launchRun("/api/pod/run/endpoint",
    { base_url, model, difficulty: $("#reDiff").value || null, api_key_name: $("#reKey").value || null }, "#reLaunch");
}

async function runHfVerified() {
  const hf_link = $("#hfLink").value.trim();
  if (!hf_link) { runStatus("HF link is required", "err"); return; }
  await launchRun("/api/pod/run/verified",
    { hf_link, difficulty: $("#hfDiff").value || null, hf_token_name: $("#hfKey").value || null }, "#hfLaunch");
}

// One-shot preset launches — 'comprehensive' turns everything on; 'hard-bench' runs the
// hard,expert tiers through every harness only. The pod resolves the preset to the underlying
// knobs, so the Scope select is intentionally ignored here (the preset sets its own tiers).
async function runHfPreset(preset, btnSel) {
  const hf_link = $("#hfLink").value.trim();
  if (!hf_link) { runStatus("HF link is required", "err"); return; }
  await launchRun("/api/pod/run/verified",
    { hf_link, preset, hf_token_name: $("#hfKey").value || null }, btnSel);
}

async function launchRun(path, body, btnSel) {
  const btn = $(btnSel); if (btn) btn.disabled = true;
  let r;
  try { r = await api(path, { method: "POST", headers: podHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(body) }); }
  catch (e) { runStatus("launch failed: " + JSON.stringify(e), "err"); if (btn) btn.disabled = false; return; }
  if (btn) btn.disabled = false;
  runStatus("launched — job " + r.job_id + ". Progress below; the run streams into ● Live once benchmarking starts.", "ok");
  await pollJobs();
}

const JOB_STAGE = { queued: "queued", starting: "starting", resolving: "resolving HF ref",
  pulling: "pulling weights", verifying: "verifying signature", verify_failed: "✗ verification FAILED",
  serving: "serving model", benchmarking: "benchmarking", submitting: "submitting",
  done: "done", error: "error", stopped: "stopped" };

async function pollJobs() {
  let d; try { d = await api("/api/pod/jobs", { headers: podHeaders() }); } catch (e) { return; }
  renderJobs((d && d.jobs) || []);
}

let JOB_STAGES = {};   // job id -> last seen stage (drives the departures-board flash)

function renderJobs(jobs) {
  const box = $("#runJobs"); if (!box) return;
  if (!jobs.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<h4 class="live-feed-h">recent runs</h4>` + jobs.map((j) => {
    const stg = JOB_STAGE[j.stage] || j.stage || j.status;
    const cls = j.status === "done" ? "ok" : (j.status === "error" || j.stage === "verify_failed") ? "err"
      : j.status === "stopped" ? "warn" : "run";
    const kindB = j.kind === "verified" ? `<span class="elig-badge verified">✓ verified</span>` : `<span class="tag">endpoint</span>`;
    const live = (j.run_id && j.status === "running") ? `<button class="ghost job-live">● Live</button>` : "";
    const stop = (j.status === "running" || j.status === "queued") ? `<button class="ghost job-stop" data-id="${escA(j.id)}">stop</button>` : "";
    const err = j.error ? `<div class="note job-err">${escH(j.error)}</div>` : "";
    const flash = JOB_STAGES[j.id] !== undefined && JOB_STAGES[j.id] !== j.stage ? " stage-flash" : "";
    JOB_STAGES[j.id] = j.stage;
    return `<div class="job-row${flash}">
      <span class="job-mk ${cls}"></span>
      <span class="mono job-model">${escH((j.model || "").split("/").pop() || j.model || "?")}</span>
      ${kindB}<span class="job-stage">${escH(stg)}</span>
      ${j.preset ? `<span class="tag preset-tag">${escH(j.preset)}</span>` : ""}
      ${j.difficulty ? `<span class="tag">${escH(j.difficulty)}</span>` : ""}
      ${live}${stop}${err}</div>`;
  }).join("");
  $$(".job-live").forEach((b) => b.onclick = () => $("#tabs [data-live]").click());
  $$(".job-stop").forEach((b) => b.onclick = () => stopJob(b.dataset.id));
}

async function stopJob(id) {
  try { await api("/api/pod/jobs/" + encodeURIComponent(id) + "/stop", { method: "POST", headers: podHeaders() }); } catch (e) {}
  pollJobs();
}

let CFG = { role: "mothership", live: false };
function applyRole() {
  const isPod = CFG.role === "pod";
  const liveTab = document.querySelector("#tabs [data-live]");
  if (liveTab) liveTab.hidden = !isPod;              // Live is a POD-only view (local lab)
  const runTab = document.querySelector("#tabs [data-run]");
  if (runTab) runTab.hidden = !isPod;               // Run (launch a benchmark) is POD-only too
  const runBench = $("#runBench");
  if (runBench) runBench.hidden = isPod;            // the GitHub-pointer panel is a MOTHERSHIP affordance
  const ptr = $("#podTokenRow");
  if (ptr) ptr.hidden = !(isPod && CFG.pod_token_required);
  const tag = document.querySelector(".brand .tag");
  if (tag) tag.textContent = isPod ? "your lab" : "mothership";
}

async function init() {
  try { AUTH.token = localStorage.getItem("aeon_eval_token") || null; } catch (e) {}
  try { CFG = await api("/api/config"); } catch (e) {}
  applyRole();
  // The model-endpoint launch form was removed — benchmarks run from the pod, never here.
  // Bind only the controls that still exist.
  const bind = (sel, fn) => { const el = $(sel); if (el) el.onclick = fn; };
  bind("#audioProbe", probeAudio);
  bind("#reLaunch", runEndpointBench);
  bind("#hfLaunch", runHfVerified);
  bind("#hfComprehensive", () => runHfPreset("comprehensive", "#hfComprehensive"));
  bind("#hfHardBench", () => runHfPreset("hard-bench", "#hfHardBench"));
  bind("#keyAdd", addKey);
  bind("#podTokenSave", () => {
    try { localStorage.setItem("aeon_pod_token", $("#podToken").value.trim()); } catch (e) {}
    runStatus("pod token set", "ok"); loadSavedKeys();
  });
  $$("#tabs .tab").forEach((t) => t.onclick = () => {
    // hide ALL aux panels first — each setter then reveals its own (fixes panel stacking)
    ["#comparePanel", "#livePanel", "#runPanel", "#harnessPanel"].forEach((s) => { const e = $(s); if (e) e.hidden = true; });
    return t.dataset.admin ? setAdmin() : t.dataset.subs ? setSubs(null)
      : t.dataset.harness ? setHarness()
      : t.dataset.compare ? setCompare()
      : t.dataset.live ? setLive()
      : t.dataset.run ? setRun()
      : t.dataset.arena ? setArena(t.dataset.arena) : setBoard(t.dataset.board);
  });
  { const cs = $("#cmpSeed"); if (cs) cs.onchange = () => loadCompare(cs.value); }
  $("#subsBoard").onchange = () => { SUBS.board = $("#subsBoard").value; loadSubs(); };
  $("#adminRefresh").onclick = () => { loadAdminBenches(); loadEvaluators(); loadAdminArtifacts(); };
  $("#adminKind").onchange = loadAdminArtifacts;
  $("#arenaPrompt").onchange = () => { ARENA.pinned = $("#arenaPrompt").value; nextMatch(); };
  bind("#arenaGenBtn", arenaGenerate);     // generation moved to pods; button may be absent
  bind("#arenaSkip", nextMatch);
  $$(".arena-vote .vote").forEach((b) => b.onclick = () => arenaVote(b.dataset.w));
  // auth modal
  $("#authSubmit").onclick = authSubmit;
  $("#authToggle").onclick = () => openAuth(authMode === "signup" ? "login" : "signup");
  $("#authClose").onclick = closeAuth;
  $("#authShow").onclick = () => {
    const p = $("#authPass"); p.type = p.type === "password" ? "text" : "password";
    $("#authShow").textContent = p.type === "password" ? "show" : "hide";
  };
  $("#authPass").onkeydown = (e) => { if (e.key === "Enter") authSubmit(); };
  $("#authModal").onclick = (e) => { if (e.target.id === "authModal") closeAuth(); };
  // tip jar: header + footer triggers, close on X / backdrop, copy each wallet
  { const tb = $("#tipBtn"); if (tb) tb.onclick = openTip; }
  { const tf = $("#tipBtnFoot"); if (tf) tf.onclick = openTip; }
  { const tc = $("#tipClose"); if (tc) tc.onclick = closeTip; }
  { const tm = $("#tipModal"); if (tm) tm.onclick = (e) => { if (e.target.id === "tipModal") closeTip(); }; }
  { const tc = $("#tipCopy"); if (tc) tc.onclick = _copyTipAddr; }
  $$(".tip-chip").forEach((c) => c.onclick = () => tipSelectChain(c.dataset.chain));
  // arena hotkeys: A / B / T vote (ignored while typing, only when the arena is up + votable)
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#tipModal").hidden) { closeTip(); return; }      // Esc closes the tip modal
    if (e.key === "Escape" && !$("#authModal").hidden) { closeAuth(); return; }   // Esc always closes the dialog
    const ap = $("#arenaPanel");
    if (!ap || ap.hidden || e.ctrlKey || e.metaKey || e.altKey) return;
    if (/INPUT|SELECT|TEXTAREA/.test((e.target && e.target.tagName) || "")) return;
    const map = { a: "a", b: "b", t: "tie" };
    const w = map[e.key.toLowerCase()];
    if (!w) return;
    const btn = document.querySelector(`.arena-vote .vote[data-w="${w}"]`);
    if (btn && !btn.disabled) { e.preventDefault(); arenaVote(w); }
  });
  // Enter submits in every launch/key form (there are no <form> elements, so no native submit)
  [["#reBase", "#reLaunch"], ["#reModel", "#reLaunch"], ["#hfLink", "#hfLaunch"],
   ["#keyVal", "#keyAdd"], ["#podToken", "#podTokenSave"]].forEach(([i, b]) => {
    const el = $(i);
    if (el) el.onkeydown = (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      const btn = $(b); if (btn && !btn.disabled) btn.click();
    };
  });
  // Enter in the username field advances to the password field
  { const au = $("#authUser"); if (au) au.onkeydown = (e) => { if (e.key === "Enter") $("#authPass").focus(); }; }
  // HUD readouts — both are TRUE data, never decoration:
  // (a) a live UTC clock chip in the status header;
  { const hdr = document.querySelector("header");
    if (hdr && !document.getElementById("hudClock")) {
      const c = document.createElement("span");
      c.id = "hudClock"; c.className = "suite hud-clock";
      hdr.appendChild(c);
      const tick = () => { c.textContent = new Date().toISOString().slice(11, 19); };
      tick(); setInterval(tick, 1000);
    } }
  // (b) panel corner serials stamped from the real suite readout (id · cases · hash).
  { const si = $("#suiteInfo");
    if (si) {
      const stamp = () => {
        const t = (si.textContent || "").trim();
        if (!t || t.indexOf("·") < 0) return;               // wait for real data
        $$("main > .panel").forEach((p, i) =>
          p.setAttribute("data-serial", "AEON//" + String(i + 1).padStart(2, "0") + " · " + t));
      };
      new MutationObserver(stamp).observe(si, { childList: true, characterData: true, subtree: true });
      stamp();
    } }
  await loadMe();
  renderAuth();
  if (CFG.role === "pod") {
    pollLive();                              // arm the Live-tab REC ping before the tab is opened
    // keep the REC light truthful from ANY tab (the Live tab's own 5s timer covers active use)
    setInterval(() => { if (active !== "live") pollLive(); }, 15000);
  }
  await loadBoard();                        // no loadModels(): the launch form is gone
}
init();
