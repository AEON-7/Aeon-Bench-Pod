"""GOD-MODE frontier agentic tasks — aspirational multi-file builds that push agent AND
harness to their limits. Each produces a self-contained HTML ARTIFACT (case["artifact"])
that ships into the Agent arena for human eval, PLUS companion files whose content must stay
CONSISTENT with the artifact — cross-file coherence is the frontier skill chat models fake
and agents must earn. Scoring stays declarative (contains/answer needles, agentic_v2 rules)
so the perfect-run oracle holds for every case."""
from __future__ import annotations

# WALL-CLOCK BUDGET PER GOD TASK.
#
# Was 420s on god-01..05 and 540s on god-06..15, and both were too tight to measure what they
# claim to. A timed-out task raises AdapterError, lands as status='harness_error' with score None,
# and scoring.harness_board() drops null-scored rows from the mean (scoring.py:78) — so the budget
# does not merely penalise a slow serve, it DELETES the hardest tasks from the result and leaves a
# harness score computed over the easy ones, biased UPWARD, with only n_cases to hint at it.
#
# Measured 2026-08-13: Qwen3.6-27B on a DGX Spark timed out on all five 420s tasks consecutively
# (god-01..05); Gemma-4-26B timed out on god-12/13 at 540s under OpenCode. These tasks ask for
# multi-file builds — a path tracer with BVH, XPBD cloth, a 90s generative score — over several
# agent turns, and long-form decode on this class of hardware runs 10-40 tok/s. Generation alone
# can exhaust 7 minutes before the agent has finished reasoning.
#
# 1800s is deliberately generous: the point of a frontier tier is to measure capability, not to
# race a stopwatch. Raising this changes agentic comparability against god runs submitted before
# 2026-08-13, which is why it was done while very few exist.
GOD_TASK_TIMEOUT_S = 1800

GOD_CASES = [
    {"id": "av2-god-01-starship-bridge", "category": "Agentic", "tier": 0,
     "difficulty": "god_mode",
     "artifact": {"file": "bridge.html", "kind": "app", "prompt_id": "agent.starship_bridge"},
     "prompt": ("You are working in a sandbox: the CURRENT DIRECTORY is your workdir. Act "
                "autonomously with your file tools. BUILD A STARSHIP BRIDGE SIMULATOR as a single "
                "self-contained file bridge.html (all CSS/JS inline, no external resources, dark "
                'sci-fi cockpit styling): four live subsystem panels with EXACT ids - id="helm" '
                '(a <canvas> starfield with steerable heading via arrow keys or drag), id="power" '
                '(four reactor sliders that rebalance a total-output readout), id="comms" (a '
                'scrolling message log that autogenerates traffic), id="shields" (an animated '
                "strength arc that dips under random impacts and recharges) - all animated via "
                "requestAnimationFrame with delta-time. ALSO write MANIFEST.json: a JSON object "
                'with key "subsystems" listing exactly ["helm","power","comms","shields"] and key '
                '"status" = "online". Finish by answering exactly: BRIDGE ONLINE 4/4.'),
     "setup_files": {},
     "success": {
         "files": {"bridge.html": {"contains": ['id="helm"', 'id="power"', 'id="comms"',
                                                'id="shields"', "<canvas",
                                                "requestAnimationFrame"]},
                   "MANIFEST.json": {"contains": ['"subsystems"', '"helm"', '"power"',
                                                  '"comms"', '"shields"', '"online"']}},
         "answer_contains": ["bridge online 4/4"]},
     "timeout_s": GOD_TASK_TIMEOUT_S,
     "_expected": {"files": {
         "bridge.html": '<html><canvas id="helm"></canvas><div id="power"></div>'
                        '<div id="comms"></div><div id="shields"></div>'
                        "<script>requestAnimationFrame(()=>{})</script></html>",
         "MANIFEST.json": '{"subsystems": ["helm", "power", "comms", "shields"], '
                          '"status": "online"}'},
         "answer": "BRIDGE ONLINE 4/4"}},

    {"id": "av2-god-02-data-observatory", "category": "Agentic", "tier": 0,
     "difficulty": "god_mode",
     "artifact": {"file": "observatory.html", "kind": "app",
                  "prompt_id": "agent.data_observatory"},
     "prompt": ("You are working in a sandbox: the CURRENT DIRECTORY is your workdir. Act "
                "autonomously. The file readings.csv holds station temperature readings "
                "(station,temp_c). FIRST compute per-station statistics and write STATS.md "
                "containing one line per station in EXACTLY this form (integers, one decimal "
                "for mean): 'ALPHA min=<min> mean=<mean> max=<max>' (same for BETA and GAMMA), "
                "sorted alphabetically. THEN build observatory.html - a single self-contained "
                "dashboard (no external resources) that EMBEDS every reading inline and renders "
                "three linked views: a per-station bar chart of means, a scatter strip of all "
                "readings, and a min-max range band - with three filter buttons carrying "
                'data-station="alpha", data-station="beta", data-station="gamma" that highlight '
                "one station across ALL views. Finish by answering: "
                "OBSERVATORY <alpha mean> <beta mean> <gamma mean>."),
     "setup_files": {"readings.csv":
         "station,temp_c\nalpha,4\nalpha,6\nalpha,5\nalpha,9\nalpha,6\nalpha,6\n"
         "beta,12\nbeta,15\nbeta,11\nbeta,18\nbeta,14\nbeta,14\n"
         "gamma,21\ngamma,25\ngamma,23\ngamma,27\ngamma,24\ngamma,24\n"},
     "success": {
         "files": {"STATS.md": {"contains": ["alpha min=4 mean=6.0 max=9",
                                             "beta min=11 mean=14.0 max=18",
                                             "gamma min=21 mean=24.0 max=27"]},
                   "observatory.html": {"contains": ['data-station="alpha"',
                                                     'data-station="beta"',
                                                     'data-station="gamma"', "27"]}},
         "answer_contains": ["observatory", "6.0", "14.0", "24.0"]},
     "timeout_s": GOD_TASK_TIMEOUT_S,
     "_expected": {"files": {
         "STATS.md": "ALPHA min=4 mean=6.0 max=9\nBETA min=11 mean=14.0 max=18\n"
                     "GAMMA min=21 mean=24.0 max=27",
         "observatory.html": '<html><button data-station="alpha"></button>'
                             '<button data-station="beta"></button>'
                             '<button data-station="gamma"></button><svg>4 6 5 9 27</svg></html>'},
         "answer": "OBSERVATORY 6.0 14.0 24.0"}},

    {"id": "av2-god-03-rogue-vault", "category": "Agentic", "tier": 0,
     "difficulty": "god_mode",
     "artifact": {"file": "vault.html", "kind": "game", "prompt_id": "agent.rogue_vault"},
     "prompt": ("You are working in a sandbox: the CURRENT DIRECTORY is your workdir. Act "
                "autonomously. BUILD A ROGUELIKE, deterministically: create vault.html - a single "
                "self-contained canvas dungeon crawler (no external resources) whose 12-room "
                "vault is generated by a seeded PRNG: implement the mulberry32 algorithm and seed "
                "it with 1337 so every load produces the IDENTICAL dungeon. WASD/arrow movement "
                "with wall collision (keydown handling), fog of war that reveals as you explore, "
                "3 patrolling guards, a loot counter HUD, and an exit that ends the run with a "
                "victory screen. ALSO write DESIGN.txt containing the lines 'rooms=12' and "
                "'seed=1337' and one sentence on how determinism is guaranteed. "
                "Answer exactly: VAULT SEALED 12 1337."),
     "setup_files": {},
     "success": {
         "files": {"vault.html": {"contains": ["mulberry32", "1337", "<canvas", "keydown"]},
                   "DESIGN.txt": {"contains": ["rooms=12", "seed=1337"]}},
         "answer_contains": ["vault sealed 12 1337"]},
     "timeout_s": GOD_TASK_TIMEOUT_S,
     "_expected": {"files": {
         "vault.html": "<html><canvas></canvas><script>function mulberry32(s){}\n"
                       "mulberry32(1337);addEventListener('keydown',()=>{})</script></html>",
         "DESIGN.txt": "rooms=12\nseed=1337\nSame seed, same vault."},
         "answer": "VAULT SEALED 12 1337"}},

    {"id": "av2-god-04-generative-symphony", "category": "Agentic", "tier": 0,
     "difficulty": "god_mode",
     "artifact": {"file": "symphony.html", "kind": "animation",
                  "prompt_id": "agent.generative_symphony"},
     "prompt": ("You are working in a sandbox: the CURRENT DIRECTORY is your workdir. Act "
                "autonomously. COMPOSE AND VISUALIZE a 90-second generative piece: build "
                "symphony.html - a single self-contained file (WebAudio only, no external "
                "resources) that on a start button unlocks an AudioContext and performs a four-"
                "section A-minor generative composition (ambient intro, arpeggio build, chordal "
                "peak, decaying outro) at tempo 96, with an AnalyserNode-driven full-screen "
                "visualization (waveform + frequency bloom) animated via requestAnimationFrame, "
                "a progress bar over the 90 seconds, and a section-name readout. ALSO write "
                "score.json describing your composition with EXACTLY these keys and values: "
                '"tempo": 96, "scale": "a_minor", "sections": 4, "duration_s": 90. '
                "Answer exactly: SYMPHONY 90S 4 SECTIONS."),
     "setup_files": {},
     "success": {
         "files": {"symphony.html": {"contains": ["AudioContext", "AnalyserNode",
                                                  "requestAnimationFrame"]},
                   "score.json": {"contains": ['"tempo": 96', '"scale": "a_minor"',
                                               '"sections": 4', '"duration_s": 90']}},
         "answer_contains": ["symphony 90s 4 sections"]},
     "timeout_s": GOD_TASK_TIMEOUT_S,
     "_expected": {"files": {
         "symphony.html": "<html><script>new AudioContext();var a='AnalyserNode';"
                          "requestAnimationFrame(()=>{})</script></html>",
         "score.json": '{"tempo": 96, "scale": "a_minor", "sections": 4, "duration_s": 90}'},
         "answer": "SYMPHONY 90S 4 SECTIONS"}},

    {"id": "av2-god-05-living-atlas", "category": "Agentic", "tier": 0,
     "difficulty": "god_mode",
     "artifact": {"file": "atlas.html", "kind": "game", "prompt_id": "agent.living_atlas"},
     "prompt": ("You are working in a sandbox: the CURRENT DIRECTORY is your workdir. Act "
                "autonomously. BUILD A LIVING WORLD: atlas.html - a single self-contained "
                "civilization simulator (no external resources): a 48x28-cell canvas map with "
                "terrain, four color-coded factions that expand, trade and clash tick by tick, "
                "and SIX player controls wired as buttons with EXACT data-action attributes: "
                'data-action="pause", data-action="speed", data-action="seed", '
                'data-action="inspect" (click a cell for a detail readout), '
                'data-action="export" (dump a save string into a textarea), and '
                'data-action="reset". Drive the sim with a fixed-tick accumulator over '
                "requestAnimationFrame. ALSO write CONTROLS.md documenting all six controls, one "
                "line each, each line containing its action token (pause, speed, seed, inspect, "
                "export, reset). The documentation and the buttons MUST agree. "
                "Answer exactly: ATLAS ALIVE 6 CONTROLS."),
     "setup_files": {},
     "success": {
         "files": {"atlas.html": {"contains": ['data-action="pause"', 'data-action="speed"',
                                               'data-action="seed"', 'data-action="inspect"',
                                               'data-action="export"', 'data-action="reset"',
                                               "<canvas", "requestAnimationFrame"]},
                   "CONTROLS.md": {"contains": ["pause", "speed", "seed", "inspect",
                                                "export", "reset"]}},
         "answer_contains": ["atlas alive 6 controls"]},
     "timeout_s": GOD_TASK_TIMEOUT_S,
     "_expected": {"files": {
         "atlas.html": '<html><canvas></canvas><button data-action="pause"></button>'
                       '<button data-action="speed"></button><button data-action="seed"></button>'
                       '<button data-action="inspect"></button>'
                       '<button data-action="export"></button>'
                       '<button data-action="reset"></button>'
                       "<script>requestAnimationFrame(()=>{})</script></html>",
         "CONTROLS.md": "pause: stop\nspeed: faster\nseed: reroll\ninspect: detail\n"
                        "export: save\nreset: fresh"},
         "answer": "ATLAS ALIVE 6 CONTROLS"}},

    {"id": "av2-god-06-madinah-ruins", "category": "Agentic", "tier": 0,
     "difficulty": "god_mode",
     "artifact": {"file": "desert_relic.html", "kind": "game",
                  "prompt_id": "agent.madinah_ruins"},
     "prompt": ("You are working in a sandbox: the CURRENT DIRECTORY is your workdir. Act "
                "autonomously. BUILD AN ORIGINAL DESERT ACTION-ADVENTURE PROTOTYPE named "
                "desert_relic.html as one self-contained HTML file (all CSS/JS inline, no "
                "external resources). The scene is a respectful, fictional oasis ruin inspired "
                "by Middle Eastern desert architecture and Madinah-region stone, never a direct "
                "depiction of sacred spaces and never copied from any existing franchise. Include "
                "a heroic female explorer, a canvas map, keyboard movement, relic shards, light-"
                "beam or pressure-plate puzzle gates, a minimap/compass, tablet dialogue, hazards "
                "that reset the player, and a victory state. ALSO write WORLD_BIBLE.md with "
                "exactly five headings: # HEROINE, # CITY, # RELIC, # CONFLICT, # ENDING. The "
                "HTML and WORLD_BIBLE must agree on the heroine name: Amara. "
                "Answer exactly: GOD MODE DESERT RELIC ONLINE."),
     "setup_files": {},
     "success": {
         "files": {"desert_relic.html": {"contains": ["<canvas", "Amara",
                                                       "keydown", "relic", "compass",
                                                       "victory"]},
                   "WORLD_BIBLE.md": {"contains": ["# HEROINE", "# CITY", "# RELIC",
                                                   "# CONFLICT", "# ENDING", "Amara"]}},
         "answer_contains": ["god mode desert relic online"]},
     "timeout_s": GOD_TASK_TIMEOUT_S,
     "_expected": {"files": {
         "desert_relic.html": '<html><canvas id="map"></canvas><div id="compass"></div>'
                              '<script>const heroine="Amara"; addEventListener("keydown",()=>{});'
                              'let relic=0; let victory=true;</script></html>',
         "WORLD_BIBLE.md": "# HEROINE\nAmara\n# CITY\nFictional oasis ruin\n# RELIC\n"
                           "Luminous archive key\n# CONFLICT\nSealed light gates\n"
                           "# ENDING\nVictory below the dunes\n"},
         "answer": "GOD MODE DESERT RELIC ONLINE"}},

    {"id": "av2-god-07-worldforge-studio", "category": "Agentic", "tier": 0,
     "difficulty": "god_mode",
     "artifact": {"file": "worldforge.html", "kind": "app",
                  "prompt_id": "agent.worldforge"},
     "prompt": ("You are working in a sandbox: the CURRENT DIRECTORY is your workdir. Act "
                "autonomously. Read seed_lore.json, then build worldforge.html as a single "
                "self-contained story-world design app (all CSS/JS inline, no external "
                "resources). It must embed the seed lore inline and provide editable panels for "
                "factions, places, relics, protagonists, mysteries, and timeline beats; a canvas "
                "or SVG relationship graph; a contradiction inspector; localStorage save/load; "
                "and export/import JSON buttons. ALSO write STORY_BIBLE.md summarizing exactly "
                "the three factions from seed_lore.json and the central mystery. "
                "Answer exactly: GOD MODE WORLDFORGE READY 3 FACTIONS."),
     "setup_files": {"seed_lore.json":
         '{"factions":["Glass Nomads","Archive Choir","Obsidian Senate"],'
         '"mystery":"a signal arriving backward from the end of time",'
         '"heroine":"Mira","city":"Qamar Gate"}\n'},
     "success": {
         "files": {"worldforge.html": {"contains": ["Glass Nomads", "Archive Choir",
                                                    "Obsidian Senate", "localStorage",
                                                    "export", "import", "<canvas"]},
                   "STORY_BIBLE.md": {"contains": ["Glass Nomads", "Archive Choir",
                                                   "Obsidian Senate",
                                                   "signal arriving backward"]}},
         "answer_contains": ["god mode worldforge ready 3 factions"]},
     "timeout_s": GOD_TASK_TIMEOUT_S,
     "_expected": {"files": {
         "worldforge.html": '<html><canvas id="graph"></canvas><button id="export">export</button>'
                            '<button id="import">import</button><script>localStorage.setItem("x","y");'
                            'const factions=["Glass Nomads","Archive Choir","Obsidian Senate"];</script></html>',
         "STORY_BIBLE.md": "Glass Nomads\nArchive Choir\nObsidian Senate\n"
                           "central mystery: a signal arriving backward from the end of time\n"},
         "answer": "GOD MODE WORLDFORGE READY 3 FACTIONS"}},

    {"id": "av2-god-08-endtime-observatory", "category": "Agentic", "tier": 0,
     "difficulty": "god_mode",
     "artifact": {"file": "endtime.html", "kind": "animation",
                  "prompt_id": "agent.endtime_observatory"},
     "prompt": ("You are working in a sandbox: the CURRENT DIRECTORY is your workdir. Act "
                "autonomously. Build endtime.html as one self-contained cosmic animation (all "
                "CSS/JS inline, no external resources) visualizing a philosophical "
                "'transcendental object at the end of time'. It must use requestAnimationFrame, "
                "a canvas or vanilla WebGL renderer, a time slider with id=\"time-slider\", phase "
                "labels for exactly four phases (seed, city, mind, object), and pointer movement "
                "that distorts or reveals hidden layers. ALSO write SYMBOLS.json with keys "
                "\"phases\" equal to [\"seed\",\"city\",\"mind\",\"object\"] and \"claim\" equal "
                "to \"metaphor\". Answer exactly: GOD MODE OBSERVATORY FOUR PHASES."),
     "setup_files": {},
     "success": {
         "files": {"endtime.html": {"contains": ["requestAnimationFrame", "<canvas",
                                                 'id="time-slider"', "seed", "city",
                                                 "mind", "object"]},
                   "SYMBOLS.json": {"contains": ['"phases"', '"seed"', '"city"',
                                                '"mind"', '"object"', '"claim"',
                                                '"metaphor"']}},
         "answer_contains": ["god mode observatory four phases"]},
     "timeout_s": GOD_TASK_TIMEOUT_S,
     "_expected": {"files": {
         "endtime.html": '<html><canvas></canvas><input id="time-slider" type="range">'
                         '<script>const phases=["seed","city","mind","object"];'
                         'addEventListener("pointermove",()=>{});requestAnimationFrame(()=>{});</script></html>',
         "SYMBOLS.json": '{"phases":["seed","city","mind","object"],"claim":"metaphor"}'},
         "answer": "GOD MODE OBSERVATORY FOUR PHASES"}},

    {"id": "av2-god-09-crisis-oracle", "category": "Agentic", "tier": 0,
     "difficulty": "god_mode",
     "artifact": {"file": "crisis_oracle.html", "kind": "app",
                  "prompt_id": "agent.crisis_oracle"},
     "prompt": ("You are working in a sandbox: the CURRENT DIRECTORY is your workdir. Act "
                "autonomously. Read incidents.csv and compute a priority score for each row as "
                "severity * probability * dependency_count. Write PRIORITIES.md sorted from "
                "highest score to lowest, one line per incident in this exact form: "
                "<id> score=<score>. Then build crisis_oracle.html as a single self-contained "
                "operations dashboard that embeds the incident data inline, shows a risk table, "
                "a dependency graph on canvas or SVG, a mitigation playbook panel, and a what-if "
                "slider that changes probability. Answer exactly: GOD MODE CRISIS ORACLE READY."),
     "setup_files": {"incidents.csv":
         "id,severity,probability,dependency_count\n"
         "edge-waf-false-positive,5,4,3\n"
         "gpu-memory-pressure,4,5,2\n"
         "upload-bandwidth-saturation,3,5,4\n"
         "stale-api-key,5,2,5\n"},
     "success": {
         "files": {"PRIORITIES.md": {"contains": ["edge-waf-false-positive score=60",
                                                  "upload-bandwidth-saturation score=60",
                                                  "gpu-memory-pressure score=40",
                                                  "stale-api-key score=50"]},
                   "crisis_oracle.html": {"contains": ["edge-waf-false-positive",
                                                       "what-if", "<canvas",
                                                       "mitigation", "score"]}},
         "answer_contains": ["god mode crisis oracle ready"]},
     "timeout_s": GOD_TASK_TIMEOUT_S,
     "_expected": {"files": {
         "PRIORITIES.md": "edge-waf-false-positive score=60\n"
                          "upload-bandwidth-saturation score=60\n"
                          "stale-api-key score=50\n"
                          "gpu-memory-pressure score=40\n",
         "crisis_oracle.html": '<html><canvas id="graph"></canvas><input id="what-if" type="range">'
                               '<section>mitigation edge-waf-false-positive score</section></html>'},
         "answer": "GOD MODE CRISIS ORACLE READY"}},

    {"id": "av2-god-10-neon-ruins-fps", "category": "Agentic", "tier": 0,
     "difficulty": "god_mode",
     "artifact": {"file": "neon_ruins.html", "kind": "game",
                  "prompt_id": "agent.neon_ruins_fps"},
     "prompt": ("You are working in a sandbox: the CURRENT DIRECTORY is your workdir. Act "
                "autonomously. Build neon_ruins.html as one self-contained pseudo-3D sci-fi "
                "exploration game (all CSS/JS inline, no external resources). Use a canvas "
                "raycaster or equivalent pseudo-3D renderer with WASD movement, arrow/mouse "
                "turning, wall collision, doors, enemy drones, scan visor mode, ammo/energy HUD, "
                "objective beacons, minimap, and a win state. ALSO write LEVEL.json with keys "
                "\"rooms\": 4, \"doors\": 3, \"drones\": 5, and \"objective\": \"recover the "
                "signal prism\". Answer exactly: GOD MODE NEON RUINS COMPLETE."),
     "setup_files": {},
     "success": {
         "files": {"neon_ruins.html": {"contains": ["<canvas", "ray", "WASD",
                                                    "scan", "minimap", "drone",
                                                    "objective"]},
                   "LEVEL.json": {"contains": ['"rooms": 4', '"doors": 3',
                                               '"drones": 5',
                                               '"objective": "recover the signal prism"']}},
         "answer_contains": ["god mode neon ruins complete"]},
     "timeout_s": GOD_TASK_TIMEOUT_S,
     "_expected": {"files": {
         "neon_ruins.html": '<html><canvas></canvas><script>const controls="WASD";'
                            'const raycaster=true, scan=true, minimap=true, drone=5, '
                            'objective="recover the signal prism";</script></html>',
         "LEVEL.json": '{"rooms": 4, "doors": 3, "drones": 5, '
                       '"objective": "recover the signal prism"}'},
         "answer": "GOD MODE NEON RUINS COMPLETE"}},
    # ---- category balance: 5 apps / 5 games / 5 animations ------------------------------------
    # Everything below is deliberately beyond frontier: a correct implementation requires a real
    # algorithm, and the manifest must stay numerically consistent with it.

    {"id": "av2-god-11-causal-forge", "category": "Agentic", "tier": 0,
     "difficulty": "god_mode",
     "artifact": {"file": "causal_forge.html", "kind": "app",
                  "prompt_id": "agent.causal_forge"},
     "prompt": ("You are working in a sandbox: the CURRENT DIRECTORY is your workdir. Act "
                "autonomously. Build causal_forge.html as ONE self-contained causal-inference "
                "workbench (all CSS/JS inline, no external resources, no libraries). It must: "
                "(1) render an editable causal DAG on a <canvas> with id=\"dag\" (drag nodes, "
                "draw directed edges, detect cycles); (2) implement d-separation and use it to "
                "find a valid BACKDOOR adjustment set, falling back to a FRONTDOOR criterion "
                "when no backdoor set exists; (3) generate a synthetic confounded dataset "
                "in-page from a fixed seed; (4) estimate the ATE with BOTH an IPW estimator "
                "using fitted PROPENSITY scores and a doubly-robust AIPW estimator, with "
                "BOOTSTRAP confidence intervals; (5) show a sensitivity analysis for unmeasured "
                "confounding. The estimates must update live when the DAG is edited. ALSO write "
                "IDENTIFICATION.json with keys \"estimand\": \"ate\", \"strategy\": "
                "\"backdoor\", \"adjustment_set\": [\"age\", \"severity\"], "
                "\"estimators\": [\"ipw\", \"aipw\"], and \"bootstrap\": 500. "
                "Answer exactly: GOD MODE CAUSAL FORGE IDENTIFIED."),
     "setup_files": {},
     "success": {
         "files": {"causal_forge.html": {"contains": ['id="dag"', "<canvas", "backdoor",
                                                      "frontdoor", "propensity", "aipw",
                                                      "bootstrap", "sensitivity"]},
                   "IDENTIFICATION.json": {"contains": ['"estimand": "ate"',
                                                        '"strategy": "backdoor"',
                                                        '"adjustment_set"', '"age"', '"severity"',
                                                        '"ipw"', '"aipw"', '"bootstrap": 500']}},
         "answer_contains": ["god mode causal forge identified"]},
     "timeout_s": GOD_TASK_TIMEOUT_S,
     "_expected": {"files": {
         "causal_forge.html": '<html><canvas id="dag"></canvas><script>const backdoor=1, '
                              'frontdoor=1, propensity=1, aipw=1, bootstrap=500, '
                              'sensitivity=1;</script></html>',
         "IDENTIFICATION.json": '{"estimand": "ate", "strategy": "backdoor", '
                                '"adjustment_set": ["age", "severity"], '
                                '"estimators": ["ipw", "aipw"], "bootstrap": 500}'},
         "answer": "GOD MODE CAUSAL FORGE IDENTIFIED"}},

    {"id": "av2-god-12-hex-dominion", "category": "Agentic", "tier": 0,
     "difficulty": "god_mode",
     "artifact": {"file": "hex_dominion.html", "kind": "game",
                  "prompt_id": "agent.hex_dominion"},
     "prompt": ("You are working in a sandbox: the CURRENT DIRECTORY is your workdir. Act "
                "autonomously. Build hex_dominion.html as ONE self-contained turn-based strategy "
                "game (all CSS/JS inline, no external resources). Requirements: a <canvas> hex "
                "board using AXIAL coordinates with at least 60 tiles and varied terrain; "
                "shortest-path movement over terrain costs; FOG of war that reveals by unit "
                "vision radius; a resource economy with capturable settlements; and an AI "
                "opponent that plays via MINIMAX with ALPHA-BETA pruning to at least depth 3, "
                "using a positional evaluation combining material, territory and mobility. The "
                "match must be deterministic from a fixed seed so a replay reproduces it "
                "exactly. ALSO write AI.json with keys \"search\": \"alpha-beta\", "
                "\"depth\": 3, \"eval_terms\": [\"material\", \"territory\", "
                "\"mobility\"], and \"seed\": 20260808. "
                "Answer exactly: GOD MODE HEX DOMINION VICTORY."),
     "setup_files": {},
     "success": {
         "files": {"hex_dominion.html": {"contains": ["<canvas", "axial", "alpha-beta",
                                                      "minimax", "fog", "terrain",
                                                      "requestAnimationFrame"]},
                   "AI.json": {"contains": ['"search": "alpha-beta"', '"depth": 3',
                                            '"material"', '"territory"', '"mobility"',
                                            '"seed": 20260808']}},
         "answer_contains": ["god mode hex dominion victory"]},
     "timeout_s": GOD_TASK_TIMEOUT_S,
     "_expected": {"files": {
         "hex_dominion.html": '<html><canvas></canvas><script>const axial=1, minimax=1, '
                              'fog=1, terrain=1; /* alpha-beta pruning */ '
                              "requestAnimationFrame(()=>{})</script></html>",
         "AI.json": '{"search": "alpha-beta", "depth": 3, "eval_terms": '
                    '["material", "territory", "mobility"], "seed": 20260808}'},
         "answer": "GOD MODE HEX DOMINION VICTORY"}},

    {"id": "av2-god-13-stable-fluids", "category": "Agentic", "tier": 0,
     "difficulty": "god_mode",
     "artifact": {"file": "stable_fluids.html", "kind": "animation",
                  "prompt_id": "agent.stable_fluids"},
     "prompt": ("You are working in a sandbox: the CURRENT DIRECTORY is your workdir. Act "
                "autonomously. Build stable_fluids.html as ONE self-contained real-time fluid "
                "simulation (all CSS/JS inline, no external resources, no libraries). Implement "
                "an actual incompressible Navier-Stokes solver on a grid of at least 128x128: "
                "SEMI-LAGRANGIAN advection of velocity and dye, a PRESSURE projection solved "
                "with at least 40 Jacobi (or multigrid) iterations to keep the field "
                "DIVERGENCE-free, VORTICITY confinement to restore the small-scale swirls "
                "numerical diffusion destroys, and viscous diffusion. Drive it with mouse or "
                "touch forcing that injects both velocity and dye, run on requestAnimationFrame "
                "with delta-time, and keep it interactive at 60fps. ALSO write SOLVER.json with "
                "keys \"grid\": 128, \"pressure_iterations\": 40, \"scheme\": "
                "\"semi-lagrangian\", and \"vorticity_confinement\": true. "
                "Answer exactly: GOD MODE STABLE FLUIDS CONVERGED."),
     "setup_files": {},
     "success": {
         "files": {"stable_fluids.html": {"contains": ["<canvas", "advect", "vorticity",
                                                       "divergence", "pressure", "jacobi",
                                                       "requestAnimationFrame"]},
                   "SOLVER.json": {"contains": ['"grid": 128', '"pressure_iterations": 40',
                                                '"scheme": "semi-lagrangian"',
                                                '"vorticity_confinement": true']}},
         "answer_contains": ["god mode stable fluids converged"]},
     "timeout_s": GOD_TASK_TIMEOUT_S,
     "_expected": {"files": {
         "stable_fluids.html": '<html><canvas></canvas><script>function advect(){}'
                               "const vorticity=1, divergence=0, pressure=1, jacobi=40;"
                               "requestAnimationFrame(()=>{})</script></html>",
         "SOLVER.json": '{"grid": 128, "pressure_iterations": 40, '
                        '"scheme": "semi-lagrangian", "vorticity_confinement": true}'},
         "answer": "GOD MODE STABLE FLUIDS CONVERGED"}},

    {"id": "av2-god-14-photon-cathedral", "category": "Agentic", "tier": 0,
     "difficulty": "god_mode",
     "artifact": {"file": "photon_cathedral.html", "kind": "animation",
                  "prompt_id": "agent.photon_cathedral"},
     "prompt": ("You are working in a sandbox: the CURRENT DIRECTORY is your workdir. Act "
                "autonomously. Build photon_cathedral.html as ONE self-contained PROGRESSIVE "
                "PATH TRACER running on a <canvas> in plain JavaScript (all inline, no external "
                "resources, no WebGL libraries). It must render a cathedral interior of at least "
                "200 triangles accelerated by a BVH; trace global illumination with COSINE-"
                "weighted IMPORTANCE sampling, next-event estimation against area lights, and "
                "RUSSIAN roulette path termination at up to 8 bounces; support a FRESNEL "
                "dielectric (stained glass) and a rough conductor material; ACCUMULATE samples "
                "across frames so the image converges and the noise visibly falls; and tone-map "
                "the accumulated buffer before display. ALSO write RENDER.json with keys "
                "\"integrator\": \"path\", \"accel\": \"bvh\", \"sampler\": "
                "\"cosine-weighted\", \"russian_roulette\": true, and \"max_bounces\": 8. "
                "Answer exactly: GOD MODE PHOTON CATHEDRAL CONVERGED."),
     "setup_files": {},
     "success": {
         "files": {"photon_cathedral.html": {"contains": ["<canvas", "bvh", "fresnel",
                                                          "russian", "importance", "accumulat",
                                                          "bounce"]},
                   "RENDER.json": {"contains": ['"integrator": "path"', '"accel": "bvh"',
                                                '"sampler": "cosine-weighted"',
                                                '"russian_roulette": true',
                                                '"max_bounces": 8']}},
         "answer_contains": ["god mode photon cathedral converged"]},
     "timeout_s": GOD_TASK_TIMEOUT_S,
     "_expected": {"files": {
         "photon_cathedral.html": '<html><canvas></canvas><script>const bvh=1, fresnel=1, '
                                  "russian=1, importance=1, bounce=8; function accumulate(){}"
                                  "</script></html>",
         "RENDER.json": '{"integrator": "path", "accel": "bvh", '
                        '"sampler": "cosine-weighted", "russian_roulette": true, '
                        '"max_bounces": 8}'},
         "answer": "GOD MODE PHOTON CATHEDRAL CONVERGED"}},

    {"id": "av2-god-15-cloth-requiem", "category": "Agentic", "tier": 0,
     "difficulty": "god_mode",
     "artifact": {"file": "cloth_requiem.html", "kind": "animation",
                  "prompt_id": "agent.cloth_requiem"},
     "prompt": ("You are working in a sandbox: the CURRENT DIRECTORY is your workdir. Act "
                "autonomously. Build cloth_requiem.html as ONE self-contained soft-body "
                "simulation (all CSS/JS inline, no external resources, no libraries). Implement "
                "XPBD (extended position-based dynamics) on a cloth of at least 64x64 particles "
                "with distance AND bending CONSTRAINTS, at least 8 SUBSTEPS per frame with "
                "compliance-correct stiffness, SELF-COLLISION resolved through a spatial hash, "
                "TEARING when a constraint exceeds a stress threshold, a turbulent WIND field, "
                "and two-way coupling with a rigid collider the cloth drapes over. It must stay "
                "stable (no explosion) and interactive at 60fps on requestAnimationFrame with "
                "delta-time. ALSO write CLOTH.json with keys \"solver\": \"xpbd\", "
                "\"substeps\": 8, \"grid\": 64, \"self_collision\": true, and "
                "\"tear_threshold\": 1.8. "
                "Answer exactly: GOD MODE CLOTH REQUIEM SETTLED."),
     "setup_files": {},
     "success": {
         "files": {"cloth_requiem.html": {"contains": ["<canvas", "xpbd", "substep",
                                                       "constraint", "tear", "wind",
                                                       "requestAnimationFrame"]},
                   "CLOTH.json": {"contains": ['"solver": "xpbd"', '"substeps": 8',
                                               '"grid": 64', '"self_collision": true',
                                               '"tear_threshold": 1.8']}},
         "answer_contains": ["god mode cloth requiem settled"]},
     "timeout_s": GOD_TASK_TIMEOUT_S,
     "_expected": {"files": {
         "cloth_requiem.html": '<html><canvas></canvas><script>const xpbd=1, substep=8, '
                               "constraint=1, tear=1.8, wind=1;"
                               "requestAnimationFrame(()=>{})</script></html>",
         "CLOTH.json": '{"solver": "xpbd", "substeps": 8, "grid": 64, '
                       '"self_collision": true, "tear_threshold": 1.8}'},
         "answer": "GOD MODE CLOTH REQUIEM SETTLED"}},
]
