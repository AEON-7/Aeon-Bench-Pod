#!/usr/bin/env node
"use strict";
// ============================================================================
// DOM-stub fixture test — hash ROUTER units (no browser, no net).
//   · parseRoute()  every route form: all 10 tabs, args (perf run / submission id /
//                   compare pair / arena kind), LEGACY #compare=A,B, encoded ids,
//                   malformed + unknown hashes → board (never throws)
//   · routeHash()   canonical hash builder + encode/decode round-trips
//   · gateRoute()   role gating: mothership + #/run|#/live → board redirect;
//                   #/admin only for a visible admin tab (mirrors applyRole())
//   · syncHash()    hash-write on tab switch: replaceState for tab hops, pushState
//                   for detail opens, push→replace demotion while a route is being
//                   applied (deep links never double-stack), identical-hash loop guard
// Run:  node mvp/web/test_routing.js
// (sets AEON_WEB_TEST=1 itself; app.js then exports its pure units instead of booting
//  init() — in a browser that branch is inert.)
// ============================================================================
process.env.AEON_WEB_TEST = "1";

// ---- minimal DOM/global stubs (app.js touches these at require time) ----
global.matchMedia = () => ({ matches: true });
global.window = { addEventListener() {}, CSS: undefined };
const el = () => ({
  hidden: false, textContent: "", style: {},
  classList: { toggle() {}, add() {}, remove() {} },
});
global.document = {
  querySelector: () => null,
  querySelectorAll: () => [],
  getElementById: () => null,
  addEventListener() {},
  createElement: () => el(),
};
global.location = { hash: "", origin: "http://test", pathname: "/", search: "" };
global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
// RECORDING history stub — syncHash assertions read .calls
global.history = {
  calls: [],
  replaceState(s, t, u) { this.calls.push(["replace", u]); },
  pushState(s, t, u) { this.calls.push(["push", u]); },
};
try { global.navigator = {}; } catch (e) { /* Node ≥21 read-only global navigator — fine */ }
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
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ---------------------------------------------------------------- parseRoute()
console.log("parseRoute() — plain tab routes");
const P = app.parseRoute;
ok(eq(P("#/board"), { tab: "board", arg: null, redirect: false }), "#/board");
ok(eq(P("#/performance"), { tab: "performance", arg: null, redirect: false }), "#/performance");
ok(eq(P("#/live"), { tab: "live", arg: null, redirect: false }), "#/live");
ok(eq(P("#/run"), { tab: "run", arg: null, redirect: false }), "#/run");
ok(eq(P("#/harnesses"), { tab: "harnesses", arg: null, redirect: false }), "#/harnesses");
ok(eq(P("#/compare"), { tab: "compare", arg: null, redirect: false }), "#/compare");
ok(eq(P("#/submissions"), { tab: "submissions", arg: null, redirect: false }), "#/submissions");
ok(eq(P("#/gallery"), { tab: "gallery", arg: null, redirect: false }), "#/gallery");
ok(eq(P("#/admin"), { tab: "admin", arg: null, redirect: false }), "#/admin");

console.log("parseRoute() — default (empty) forms");
ok(eq(P(""), { tab: "board", arg: null, redirect: false }), "empty hash → board, no rewrite");
ok(eq(P("#"), { tab: "board", arg: null, redirect: false }), "bare # → board");
ok(eq(P("#/"), { tab: "board", arg: null, redirect: false }), "#/ → board");
ok(eq(P(null), { tab: "board", arg: null, redirect: false }), "null-safe");

console.log("parseRoute() — deep-state args");
ok(eq(P("#/submissions/r-abc123"), { tab: "submissions", arg: "r-abc123", redirect: false }),
  "#/submissions/<run_id>");
ok(eq(P("#/submissions/r%20x%2F1"), { tab: "submissions", arg: "r x/1", redirect: false }),
  "submission ids decode (%20, %2F)");
ok(eq(P("#/performance/org%2Fmodel-9b"), { tab: "performance", arg: "org/model-9b", redirect: false }),
  "#/performance/<canonical-model> decodes the slash");
ok(eq(P("#/performance/r-perf-7"), { tab: "performance", arg: "r-perf-7", redirect: false }),
  "#/performance/<run id> (rows key by run)");
ok(eq(P("#/compare/jg%3A1,jg%3A2"), { tab: "compare", arg: ["jg:1", "jg:2"], redirect: false }),
  "#/compare/<cardA>,<cardB> decodes both sides");
ok(eq(P("#/arena/app"), { tab: "arena", arg: "app", redirect: false }), "#/arena/app");
ok(eq(P("#/arena/game"), { tab: "arena", arg: "game", redirect: false }), "#/arena/game");
ok(eq(P("#/arena/animation"), { tab: "arena", arg: "animation", redirect: false }), "#/arena/animation");

console.log("parseRoute() — LEGACY #compare=A,B keeps working (redirect flag set)");
ok(eq(P("#compare=jg%3A1,jg%3A2"), { tab: "compare", arg: ["jg:1", "jg:2"], redirect: true }),
  "legacy compare parses + flags a redirect to #/compare/A,B");
ok(P("#compare=%zz,b").tab === "board" && P("#compare=%zz,b").redirect === true,
  "legacy compare with a bad %-escape → board");

console.log("parseRoute() — unknown/malformed → board, redirect, never a throw");
const bad = (h, why) => { const p = P(h); ok(p.tab === "board" && p.redirect === true, why + " (" + h + ")"); };
bad("#/bogus", "unknown tab");
bad("#wat", "non-route hash");
bad("#/arena", "arena without a kind");
bad("#/arena/rocket", "arena with an unknown kind");
bad("#/board/extra", "arg on an argless tab");
bad("#/live/xyz", "arg on a gated argless tab");
bad("#/submissions/%zz", "malformed %-escape in a run id");
bad("#/compare/onlyone", "compare without a comma pair");

console.log("parseRoute() — REMOVED modality tabs redirect to the leaderboard");
// Vision / Audio / Video are no longer tabs (their results live as dials + run-detail
// plates on the one board) — an old bookmark lands on #/board, URL rewritten honestly.
bad("#/vision", "removed Vision tab");
bad("#/audio", "removed Audio tab");
bad("#/video", "removed Video tab");

console.log("routeHash() — canonical builder + encode round-trips");
const H = app.routeHash;
ok(H("board", null) === "#/board", "board builds bare");
ok(H("arena", "game") === "#/arena/game", "arena kind rides the path");
ok(H("submissions", "r 7/x") === "#/submissions/r%207%2Fx", "run id is encodeURIComponent'd");
ok(H("compare", ["jg:1", "a,b"]) === "#/compare/jg%3A1,a%2Cb", "compare encodes commas inside ids");
const rt = (tab, arg) => eq(P(H(tab, arg)), { tab, arg, redirect: false });
ok(rt("submissions", 'r"7 <&>/π'), "submission id round-trips through encode/decode");
ok(rt("performance", "org/Ünïcode-9b"), "canonical model round-trips");
ok(rt("compare", ["jg:2026,x", "lg:7"]), "compare pair round-trips (comma in card id)");

// ---------------------------------------------------------------- gateRoute()
console.log("gateRoute() — role gating mirrors applyRole()");
const G = app.gateRoute;
const r = (tab, arg) => ({ tab, arg: arg == null ? null : arg, redirect: false });
ok(eq(G(r("run"), "mothership", false), { tab: "board", arg: null, redirect: true }),
  "mothership + #/run → redirected to #/board");
ok(eq(G(r("live"), "mothership", false), { tab: "board", arg: null, redirect: true }),
  "mothership + #/live → redirected to #/board");
ok(eq(G(r("run"), "pod", false), r("run")), "pod + #/run passes");
ok(eq(G(r("live"), "pod", false), r("live")), "pod + #/live passes");
ok(eq(G(r("admin"), "pod", false), { tab: "board", arg: null, redirect: true }),
  "#/admin with a hidden admin tab → board (never force-show a hidden tab)");
ok(eq(G(r("admin"), "mothership", true), r("admin")), "#/admin passes for a signed-in admin");
ok(eq(G(r("submissions", "r-1"), "mothership", false), r("submissions", "r-1")),
  "ungated routes pass through untouched (arg kept)");
ok(eq(G(r("board"), "mothership", false), r("board")), "board is never gated");

// ---------------------------------------------------------------- syncHash()
console.log("syncHash() — hash-write on tab switch (replace vs push + loop guard)");
const S = app.syncHash, ROUTE = app.ROUTE, hist = global.history;
const last = () => hist.calls[hist.calls.length - 1];

hist.calls.length = 0; global.location.hash = "#/board"; ROUTE.applying = false;
S("harnesses");
ok(eq(last(), ["replace", "#/harnesses"]), "tab switch writes #/harnesses via replaceState (no history spam)");
ok(ROUTE.cur === "#/harnesses", "ROUTE.cur tracks the written hash");

S("arena", "game");
ok(eq(last(), ["replace", "#/arena/game"]), "arena tab writes its kind");
S("compare", ["jg:1", "jg:2"]);
ok(eq(last(), ["replace", "#/compare/jg%3A1,jg%3A2"]), "compare load writes the encoded pair via replaceState");

S("submissions", "r x/1", true);
ok(eq(last(), ["push", "#/submissions/r%20x%2F1"]), "detail open pushes (Back closes it) with an encoded id");

// loop guard: an identical hash is NEVER rewritten
global.location.hash = "#/gallery";
const n = hist.calls.length;
S("gallery");
ok(hist.calls.length === n && ROUTE.cur === "#/gallery",
  "identical hash → no history call (loop guard), cur still tracked");

// applying demotion: a deep link being applied must not double-stack history
global.location.hash = "#/board";
ROUTE.applying = true;
S("submissions", "r-9", true);
ok(eq(last(), ["replace", "#/submissions/r-9"]),
  "push demoted to replace while ROUTE.applying (deep-link application)");
ROUTE.applying = false;

// ----------------------------------------------------------------
if (fails) { console.error("\n" + fails + " assertion(s) FAILED"); process.exit(1); }
console.log("\nall assertions passed");
