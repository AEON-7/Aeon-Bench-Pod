#!/usr/bin/env node
"use strict";
// ============================================================================
// DOM-stub fixture test — Global Leaderboard frontend units (no browser, no net).
//   · dial()       the reusable SVG arc gauge: value bands, honest null state
//   · globalRow()  instrument row WITH the new `dials` leaderboard contract and
//                  WITHOUT it (old-server compat), escaping, best-run target
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
ok(svgCount(rowNew) === 5, "hero + intelligence + performance + agentic + vision (untested audio/video skipped), got " + svgCount(rowNew));
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
ok(svgCount(rowProv) === 4, "hero + core trio always drawn (perf/agentic as na), got " + svgCount(rowProv));
ok((rowProv.match(/not yet tested/g) || []).length >= 2, "missing core dials say 'not yet tested'");
ok(/aeon-prov/.test(rowProv) && /prov/.test(rowProv), "provisional AEON dims with an honest chip");
ok(!/>0</.test(rowProv), "no fake zero anywhere in the provisional row");

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
