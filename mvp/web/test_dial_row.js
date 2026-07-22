#!/usr/bin/env node
"use strict";
// ============================================================================
// DOM-stub fixture test — Global Leaderboard frontend units (no browser, no net).
//   · dial()       the reusable SVG arc gauge: value bands, honest null state
//   · globalRow()  instrument row WITH the new `dials` leaderboard contract and
//                  WITHOUT it (old-server compat), escaping, best-run target
//   · explorer     EXPLORE THE DATA pure renderers: expHeat (cell count, luminance
//                  bands, honest dashes, speed re-color), expLine (polylines +
//                  reference lines), expToggleModel / expDefaultSel filter fns
//   · applyRole()  mothership vs pod nav gating (Live/Run tabs + pod CTA)
// Run:  node mvp/web/test_dial_row.js
// (sets AEON_WEB_TEST=1 itself; app.js then exports its pure renderers instead
//  of booting init() — in a browser that branch is inert.)
// ============================================================================
process.env.AEON_WEB_TEST = "1";

// ---- minimal DOM/global stubs (app.js touches these at require time) ----
global.matchMedia = () => ({ matches: true });   // reduced-motion: count-ups short-circuit
global.window = { addEventListener() {}, CSS: undefined };
const el = () => ({
  hidden: false, textContent: "", style: {},
  classList: { toggle() {}, add() {}, remove() {} },
});
// the exact selectors applyRole() gates the two-role nav with
const NAV = {
  "#tabs [data-live]": el(),
  "#tabs [data-run]": el(),
  "#podCta": el(),
  "#podTokenRow": el(),
  ".brand .tag": el(),
};
global.document = {
  querySelector: (s) => NAV[s] || null,
  querySelectorAll: () => [],
  getElementById: () => null,
  addEventListener() {},
  createElement: () => el(),
};
global.location = { hash: "", origin: "http://test", pathname: "/", search: "" };
global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.history = { replaceState() {} };
try { global.navigator = {}; } catch (e) { /* Node ≥21 ships a read-only global navigator — fine */ }
global.fetch = () => Promise.reject(new Error("network disabled in test"));
global.requestAnimationFrame = (f) => setTimeout(f, 0);
global.MutationObserver = class { observe() {} };
global.Image = class {};

const app = require("./app.js");

let fails = 0;
function ok(cond, msg) {
  if (cond) console.log("  ✓ " + msg);
  else { fails++; console.error("  ✗ FAIL: " + msg); }
}
const svgCount = (html) => (html.match(/viewBox="0 0 80 80"/g) || []).length;

// ---------------------------------------------------------------- dial()
console.log("dial()");
const d1 = app.dial(87.4, "intelligence");
ok(d1.includes(">87<"), "value renders rounded (87.4 → 87)");
ok(/class="dial pass/.test(d1), "≥80 lands in the pass band");
ok(/INTELLIGENCE|intelligence/.test(d1), "label is engraved under the gauge");
ok(!d1.includes("dial-na"), "a tested dial is not in the na state");
ok(d1.includes("stroke-dashoffset"), "arc sweep is dashoffset-driven");
ok(/class="dial part/.test(app.dial(55, "x")), "40-79 → part band");
ok(/class="dial fail/.test(app.dial(12, "x")), "<40 → fail band");
ok(app.dial(140, "x").includes(">100<"), "values clamp to 100");

const dn = app.dial(null, "audio");
ok(dn.includes("dial-na"), "null dims (na state)");
ok(dn.includes("—"), "null shows an em dash — never a fake zero");
ok(/not yet tested/i.test(dn), "null carries the honest micro-label");
ok(!dn.includes("dial-arc"), "null draws no value arc (dashed track only)");
ok(!/class="dial (pass|part|fail)/.test(dn), "null gets no verdict band");

const dx = app.dial(50, '<img src=x onerror=alert(1)>');
ok(!dx.includes("<img"), "label is escaped (XSS guard)");

// ------------------------------------------- globalRow(): NEW server contract
console.log("globalRow() — new `dials` contract");
const rowNew = app.globalRow({
  model: "org/model-9b", run: "r-text-1", canonical: "org/model-9b",
  aeon_score: 78.2, aeon_provisional: false,
  aeon_score_parts: { intelligence: 0.6, performance: 0.2, agentic: 0.2 },
  best_intelligence_run: "r-best-7",
  record_eligible: true, comp: 74.1, categories: {},
  dials: {
    intelligence: { score: 81.3, run: "r-best-7" },
    performance: { score: 66, peak_agg_tps: 517.2, hw: "Single DGX Spark" },
    agentic: { score: 44, harnesses: { hermes: 51, openclaw: 40, opencode: 41 } },
    vision: { score: 72.5, run: "r-vis" },
    audio: null, video: null,
  },
}, 0);
ok(rowNew.includes('data-run="r-best-7"'), "row click target = best_intelligence_run");
ok(rowNew.includes("78.2"), "AEON score is the headline number");
ok(/aeon score/i.test(rowNew), "headline is labelled AEON SCORE");
ok(svgCount(rowNew) === 4, "hero + intelligence + agentic + vision dials (perf is an instrument, untested audio/video skipped), got " + svgCount(rowNew));
ok(/perf-inst/.test(rowNew) && !/pi-na/.test(rowNew), "performance renders as the race instrument (tested)");
ok(rowNew.indexOf("perf-inst") > rowNew.lastIndexOf('viewBox="0 0 80 80"'),
   "race instrument renders AFTER every dial — the far-right aligned slot on each row");
ok(/pi-tps">517/.test(rowNew) || /517/.test(rowNew), "instrument shows the peak tok/s readout");
ok(/tok\/s peak/.test(rowNew), "readout is labelled tok/s peak");
ok(/dial-hero/.test(rowNew), "the AEON headline is its own HERO gauge");
ok(/dial-sub">overall</.test(rowNew), "hero gauge is engraved OVERALL");
ok(/dial-val aeon-val/.test(rowNew), "hero value carries aeon-val (count-up hook)");
ok(/dial-ticks/.test(rowNew), "hero gauge has the instrument tick ring");
ok(!/not yet tested/.test(rowNew), "no na dials when every drawn dial is tested");
ok(/hermes 51/.test(rowNew), "agentic tooltip breaks down the three harnesses");
ok(/517/.test(rowNew), "performance tooltip carries the peak tok/s");
ok(/intelligence 60%/.test(rowNew), "aeon_score_parts weights ride the headline tooltip");
ok(!/aeon-prov/.test(rowNew), "non-provisional row shows no provisional chip");
ok(/✓ verified/.test(rowNew), "trust badge present");
ok(/share-btn/.test(rowNew) && /get-model-btn/.test(rowNew), "GET MODEL + share stay on the row");

// provisional: missing components dim honestly, never zero
const rowProv = app.globalRow({
  model: "org/fresh-4b", run: "r-p1", aeon_score: 61, aeon_provisional: true,
  best_intelligence_run: "r-p1", comp: 61, categories: {},
  dials: { intelligence: { score: 61, run: "r-p1" }, performance: null, agentic: null,
           vision: null, audio: null, video: null },
}, 1);
ok(svgCount(rowProv) === 3, "hero + intelligence + agentic drawn (perf = na instrument), got " + svgCount(rowProv));
ok(/perf-inst pi-na/.test(rowProv), "untested performance renders the dim na instrument");
ok((rowProv.match(/not yet tested/g) || []).length >= 2, "missing core dials say 'not yet tested'");
ok(/aeon-prov/.test(rowProv) && /prov/.test(rowProv), "provisional AEON dims with an honest chip");
ok(!/>0</.test(rowProv), "no fake zero anywhere in the provisional row");

// completeness gate: verified weights but not a full run -> "verified · not counted" badge
const rowInc = app.globalRow({
  model: "org/inc-9b", run: "r-inc", aeon_score: 70, best_intelligence_run: "r-inc",
  comp: 70, categories: {}, record_eligible: false, ranked_excluded: "incomplete",
  dials: { intelligence: { score: 70, run: "r-inc" }, performance: null, agentic: null },
}, 1);
ok(/elig-badge incomplete/.test(rowInc) && /not counted/.test(rowInc),
   "attested-but-incomplete row badges 'verified · not counted', not 'local'");
ok(!/elig-badge local/.test(rowInc), "an incomplete verified row is never mislabelled local");

// ------------------------------------------- globalRow(): OLD server compat
console.log("globalRow() — old server (no dials/aeon fields)");
const rowOld = app.globalRow({
  model: "org/legacy-7b", run: "r-old-3", comp: 63.4, categories: {}, record_eligible: false,
}, 2);
ok(rowOld.includes('data-run="r-old-3"'), "falls back to the row's own run id");
ok(rowOld.includes(">composite<") && !rowOld.includes(">aeon score<"), "headline honestly relabelled composite");
ok(rowOld.includes("63.4"), "composite value shown");
ok(svgCount(rowOld) === 2, "hero + single intelligence dial from the composite, got " + svgCount(rowOld));
ok(/local/.test(rowOld), "local trust badge");

// dials contract present but no top-level best run: intelligence.run wins over m.run
const rowIntel = app.globalRow({
  model: "o/m", run: "r-generic", comp: 50, categories: {},
  dials: { intelligence: { score: 50, run: "r-intel" }, performance: null, agentic: null },
}, 3);
ok(rowIntel.includes('data-run="r-intel"'), "dials.intelligence.run beats the generic row run");

// escaping
const rowEsc = app.globalRow({ model: 'evil"/><script>alert(1)</script>', comp: 10, categories: {} }, 4);
ok(!rowEsc.includes("<script>"), "model name is escaped (XSS guard)");

// ---------------------------------------------- served-context chip (ctx_len)
console.log("globalRow() — served-context chip");
const rowCtx = app.globalRow({ model: "o/ctx-model", comp: 50, categories: {}, ctx_len: 65536 }, 5);
ok(/mcard-ctx/.test(rowCtx) && />64K ctx</.test(rowCtx), "ctx_len 65536 renders the quiet 64K ctx chip");
ok(/max context length this benchmark was served at/.test(rowCtx), "ctx chip carries its tooltip");
ok(!rowOld.includes("mcard-ctx") && !rowNew.includes("mcard-ctx"),
   "rows without ctx_len render no ctx chip (old-payload compat)");
ok(app.fmtCtx(65536) === "64K" && app.fmtCtx(131072) === "128K" && app.fmtCtx(512) === "512",
   "fmtCtx: /1024 rounded + K, sub-1K literal");
ok(app.ctxChip(null) === "" && app.ctxChip(undefined) === "", "ctxChip is empty for null/undefined");

// ------------------------------------------------ EXPLORE THE DATA (pure fns)
console.log("explorer — expBand()");
ok(app.expBand(null) === -1, "null → -1 (no band, honest gap)");
ok(app.expBand(0) === 0 && app.expBand(19.9) === 0, "0-19.9 → band 0");
ok(app.expBand(20) === 1 && app.expBand(55) === 2, "band edges (20 → 1, 55 → 2)");
ok(app.expBand(80) === 4 && app.expBand(100) === 4, "80-100 → band 4 (brightest)");

console.log("explorer — expHeat()");
const DIFFS = ["easy", "medium", "hard", "expert", "frontier", "god_mode"];
const CATS = ["Math", "Coding"];
const M1 = {
  model: "org/model-a", canonical: "org/model-a", aeon_score: 88, composite: 90,
  cells: {
    Math:   { easy: { score: 92.5, n: 5, tps: 100.0 }, medium: { score: 55, n: 4, tps: 50.0 },
              hard: { score: 12, n: 5, tps: 25.0 } },
    Coding: { easy: { score: 71, n: 5, tps: 80.0 } },
  },
};
const heat = app.expHeat(M1, CATS, DIFFS, "quality", 100);
ok((heat.match(/<td/g) || []).length === 12, "cell count = cats × diffs (2×6 = 12), got " + (heat.match(/<td/g) || []).length);
ok((heat.match(/exp-na/g) || []).length === 8, "8 never-scored cells render the dashed honest —");
ok(/class="xb4"[^>]*>93</.test(heat), "92.5 → brightest luminance band xb4, numeral rounds to 93");
ok(/class="xb2"[^>]*>55</.test(heat), "55 → mid band xb2");
ok(/class="xb0"[^>]*>12</.test(heat), "12 → darkest band xb0 (luminance-ordered, no verdict hues)");
ok(/n=5/.test(heat) && /n=4/.test(heat), "n rides the tooltip, never the cell");
ok(!/pass|part|fail/.test(heat), "heatmap never borrows the verdict classes");

const heatSpd = app.expHeat(M1, CATS, DIFFS, "speed", 100);
ok(/class="xb4"[^>]*>100</.test(heatSpd), "speed: 100 tok/s at max → brightest band, numeral = tok/s");
ok(/class="xb2"[^>]*>50/.test(heatSpd), "speed: 50 of 100 tok/s → mid band (normalized to fastest shown)");
ok((heatSpd.match(/exp-na/g) || []).length === 8, "speed mode keeps the honest dashes for missing cells");

const heatX = app.expHeat({ model: "m", cells: { Math: { easy: { score: 50, n: 1, tps: null } } } },
  ['<img src=x onerror=alert(1)>'], DIFFS, "quality", 0);
ok(!heatX.includes("<img"), "category labels are escaped (XSS guard)");

console.log("explorer — expLine()");
const M2 = { model: "org/model-b", canonical: "org/model-b",
  cells: { Math: { easy: { score: 80, n: 5, tps: null }, hard: { score: 40, n: 5, tps: null } } } };
const line = app.expLine([M1, M2], CATS, DIFFS);
ok((line.match(/<polyline/g) || []).length === 2, "one series polyline per selected model");
ok((line.match(/exp-ref/g) || []).length === 3, "exactly 3 faint reference lines (0/50/100), no axis box");
ok(/viewBox=/.test(line) && !/<rect/.test(line), "pure SVG, gridless — no chart-junk frame");
ok(/god_mode|GOD MODE/i.test(line), "x axis names the difficulty tiers in order");

console.log("explorer — filter fns");
let sel = app.expDefaultSel([
  { canonical: "a", aeon_score: 70, composite: 90 },
  { canonical: "b", aeon_score: 91, composite: 50 },
]);
ok(sel.length === 1 && sel[0] === "b", "default selection = top 1 by aeon_score");
ok(app.expDefaultSel([{ canonical: "x", composite: 60 }, { canonical: "y", composite: 80 }])
    .join(",") === "y", "old-server rows without aeon_score fall back to composite");
ok(app.expDefaultSel([]).length === 0, "empty board → empty default selection");
sel = ["b"];
sel = app.expToggleModel(sel, "a");
sel = app.expToggleModel(sel, "c");
ok(sel.join(",") === "b,a,c", "toggling adds models in click order");
ok(app.expToggleModel(sel, "d").join(",") === "b,a,c", "a 4th model is a no-op (max 3)");
ok(app.expToggleModel(sel, "a").join(",") === "b,c", "re-click removes a selected model");
ok(app.expTpsMax([M1], CATS) === 100, "expTpsMax finds the fastest cell in the selection");

console.log("explorer — facet filters (hardware bucket × trust tier)");
const FM = [
  { canonical: "a", model: "o/a", hw_bucket: "NVIDIA RTX 5090", trust_tier: "attested",
    aeon_score: 90, composite: 90, ctx_len: 65536, cells: {} },
  { canonical: "b", model: "o/b", hw_bucket: "DGX Spark", trust_tier: "attested",
    aeon_score: 80, composite: 80, cells: {} },
  { canonical: "c", model: "o/c", trust_tier: "self_reported",
    aeon_score: 95, composite: 95, cells: {} },          // no hw_bucket recorded
];
ok(app.expFacetFilter(FM, "all", "all").length === 3, "all × all offers every board model");
ok(app.expFacetFilter(FM, "NVIDIA RTX 5090", "all").map((m) => m.canonical).join(",") === "a",
   "hardware-bucket filter isolates its rig");
ok(app.expFacetFilter(FM, "all", "attested").map((m) => m.canonical).join(",") === "a,b",
   "trust-tier filter keeps only attested rows");
ok(app.expFacetFilter(FM, "Unlabeled", "all").map((m) => m.canonical).join(",") === "c",
   "a model without hw_bucket buckets as Unlabeled (honest, never guessed)");
ok(app.expFacetFilter(FM, "DGX Spark", "self_reported").length === 0,
   "hardware and trust facets compose (AND)");

console.log("explorer — difficulty columns are a live filter axis");
const heatD = app.expHeat(M1, CATS, ["easy", "hard"], "quality", 100);
ok((heatD.match(/<td/g) || []).length === 4, "toggled-off tiers drop their columns (2 cats × 2 diffs)");
ok(!/medium/i.test(heatD), "an untoggled tier never renders anywhere in the table");
const lineD = app.expLine([M1], CATS, ["easy", "hard"]);
ok(/>easy</.test(lineD) && />hard</.test(lineD) && !/GOD MODE/.test(lineD),
   "decay x-axis follows the toggled tiers only");

console.log("explorer — expPlateHead() (served-ctx axis + rig facts)");
const ph = app.expPlateHead(FM[0], "#00f0ff");
ok(/>64K ctx</.test(ph), "plate carries the served-context fact (65536 → 64K ctx)");
ok(/max context length this benchmark was served at/.test(ph), "ctx fact keeps the board's tooltip");
ok(/NVIDIA RTX 5090/.test(ph), "plate names the rig the run was benched on");
ok(/AEON 90\.0/.test(ph), "plate keeps the AEON headline");
const ph2 = app.expPlateHead({ model: "o/c", aeon_score: null, cells: {} }, "#fff");
ok(!/ctx/.test(ph2) && !/benched on/.test(ph2) && !/AEON/.test(ph2),
   "nothing recorded → no fact rendered (absence, never a fabricated figure)");
const ph3 = app.expPlateHead({ model: "m", hw_bucket: '<img src=x onerror=alert(1)>' }, "#fff");
ok(!ph3.includes("<img"), "rig label is escaped (XSS guard)");

// ---------------------------------------------------------------- galCard()
console.log("galCard() — gallery provenance chips");
const NOW_S = Math.floor(Date.now() / 1000);
const gcNew = app.galCard(
  { id: "a1", model: "org/m", created_at: NOW_S - 3600, unrated: true, bytes: 9000, gen_ms: 4200 },
  { id: "p1", title: "Orbital Toy", brief: "b", difficulty: "hard" }, 2);
ok(/gal-date/.test(gcNew) && new RegExp(new Date((NOW_S - 3600) * 1000).toISOString().slice(0, 10)).test(gcNew),
   "card shows the submission date (ISO day)");
ok(/gal-new/.test(gcNew), "an artifact <48h old gets the bright NEW badge");
ok(/gal-diff gd-hard/.test(gcNew) && />hard</.test(gcNew), "prompt difficulty chip renders with its band hue");
const gcOld = app.galCard(
  { id: "a2", model: "org/m", created_at: NOW_S - 5 * 86400, elo: 1210, w: 3, l: 1, t: 0, votes: 4 },
  { id: "p2", title: "T", brief: "b" }, 0);
ok(!/gal-new/.test(gcOld), "older artifacts carry no NEW badge");
ok(!/gal-diff/.test(gcOld), "difficulty chip omitted when the prompt has no rating (old payloads)");
const gcBare = app.galCard({ id: "a3", model: "org/m" }, { id: "p3", title: "T", brief: "b" }, 1);
ok(!/gal-date/.test(gcBare) && !/gal-new/.test(gcBare), "missing created_at renders no date and no NEW");
const gcXss = app.galCard(
  { id: "a4", model: "org/m", created_at: NOW_S },
  { id: "p4", title: "T", brief: "b", difficulty: '"><img src=x onerror=alert(1)>' }, 1);
ok(!gcXss.includes("onerror") && !/gal-diff/.test(gcXss),
   "a non-closed-set difficulty renders NO chip at all (class tokens never carry input)");

// ---------------------------------------------------------------- godRow()
console.log("godRow() — GOD MODE board rows");
const gFull = app.godRow({
  canonical: "o/god-9b", model: "o/god-9b", run: "r-god-1", god_score: 28.4,
  god_provisional: false, record_eligible: true,
  sentinels: { run: "r-god-1", composite: 22.0, categories: { Math: 25, Coding: 0 },
               n_attempted: 24, n_total: 24 },
  agentic: { score: 38, harnesses: { hermes: 41, opencode: 35 } },
}, 0);
ok(/god-score">28.4</.test(gFull), "GOD SCORE renders");
ok(/data-run="r-god-1"/.test(gFull), "row opens the sentinel run");
ok(/24\/24 sentinels/.test(gFull), "sentinel coverage disclosed");
ok(/hermes <b>41(\.0)?</.test(gFull), "per-harness god agentic chips");
ok(!/aeon-prov/.test(gFull), "full row carries no provisional chip");
const gProv = app.godRow({
  canonical: "o/god-4b", model: "o/god-4b", god_score: 10.0, god_provisional: true,
  record_eligible: true, sentinels: { run: "r-g2", composite: 10.0, categories: { Math: 10 },
  n_attempted: 24, n_total: 24 }, agentic: null,
}, 1);
ok(/aeon-prov/.test(gProv) && /agentic not yet tested/.test(gProv),
   "missing agentic dims honestly (provisional + untested label, never 0)");
const gX = app.godRow({ canonical: "e", model: '<img src=x onerror=1>', god_score: 1,
  god_provisional: true, record_eligible: false, sentinels: null, agentic: null }, 2);
ok(!gX.includes("<img"), "model name escaped (XSS guard)");
ok(/sentinels not yet tested/.test(gX), "absent sentinels render the honest untested label");

// ---------------------------------------------------------------- applyRole()
console.log("applyRole() — two-role nav gating");
app.CFG.role = "mothership";
app.applyRole();
ok(NAV["#tabs [data-live]"].hidden === true, "mothership: Live tab hidden");
ok(NAV["#tabs [data-run]"].hidden === true, "mothership: Run tab hidden");
ok(NAV["#podCta"].hidden === false, "mothership: Run-a-Bench-Pod CTA shown");
ok(NAV[".brand .tag"].textContent === "mothership", "mothership: brand tag");

app.CFG.role = "pod";
app.applyRole();
ok(NAV["#tabs [data-live]"].hidden === false, "pod: Live tab shown");
ok(NAV["#tabs [data-run]"].hidden === false, "pod: Run tab shown");
ok(NAV["#podCta"].hidden === true, "pod: mothership CTA hidden");
ok(NAV[".brand .tag"].textContent === "your lab", "pod: brand tag");

// ----------------------------------------------------------------
if (fails) { console.error("\n" + fails + " assertion(s) FAILED"); process.exit(1); }
console.log("\nall assertions passed");
