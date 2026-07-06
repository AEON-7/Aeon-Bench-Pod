# Speech assets — attribution

The `.wav` files in this directory are recordings from the
**Free Spoken Digit Dataset (FSDD)** — https://github.com/Jakobovski/free-spoken-digit-dataset —
by Zohar Jackson, César Souza, Jason Flaks, Yuxin Pan, Hereman Nicolas, and Adhish Thite,
licensed under **Creative Commons Attribution-ShareAlike 4.0**
(https://creativecommons.org/licenses/by-sa/4.0/).

Files are unmodified original recordings (8 kHz mono WAV), selected one per digit
(speakers `jackson` and `theo` alternating), and pinned by sha256 in
`aeon/audio_suite.py` — the suite hash covers the exact bytes, so a changed or
substituted recording fails validation instead of silently testing different audio.

Ground truth: the spoken digit is encoded in each FSDD filename (`<digit>_<speaker>_<take>.wav`);
scoring is Tier-0 exact-match against a closed set — no ASR, no judge model.
