# Running the Pipeline

The pipeline has two phases: **data preparation** (fast, CPU-only) and **audio generation** (slow, GPU-required). Run them in order.

---

## Prerequisites 

Setup and activate the pipeline:

```bash
cd DeepRedStories
.venv\Scripts\activate 
```

## Phase 1: Data Preparation

For an intial 500 games set

```bash
python pipeline/prepare_data.py --max-games 500
```

or for the full dataset 

```bash
python pipeline/prepare_data.py
```

### What it does

1. **Stratified sampling** (when `--max-games` is set): first cleans the output folder (`pipeline/output/`) by removing `merged_games.jsonl`, `index.json`, `progress.json`, and the entire `games/` directory (use `--no-clean` to skip this). Then pre-scans `augmented_chess_games.jsonl` to collect all available keys, then scans `chess_games.jsonl` to collect candidates — only considering games that exist in the augmented file. Games are grouped into decade-based strata with a Russian/Soviet relevance score (0–3). Slots are allocated proportionally per stratum. Within each stratum, games are selected via weighted sampling — Russian-connected games are boosted by `russian_bias ^ score` (default 3.0×, so a game with two Russian players at a Soviet event is 27× more likely to be picked). The sampler over-selects by ~15% to compensate for downstream quality-filter attrition, with `--max-games` enforced as a hard cap on final output. Use `--no-stratify` to fall back to simple first-N selection. Then **indexes** `augmented_chess_games.jsonl` — building an in-memory map of `key → best English narrative`, exiting early once all selected candidates are found. Applies quality filters (English detection, minimum 500 chars, no gibberish/repetition). Prefers `prompt_variant == 1` (Russian style). Without `--max-games`, the entire augmented file is indexed.
2. **Joins** with `chess_games.jsonl` — streams game records, keeps only those with a matching narrative.
3. **Parses moves** from the game text into a structured list: `[{num: 1, white: "e4", black: "c5"}, ...]`
4. **Segments the narrative** into move-aligned paragraphs, each tagged with `start_move` / `end_move`.
5. **Estimates move timings** for each segment by locating move notations in the narrative text (character positions → fractional time anchors; unmatched moves interpolated). The raw anchors are then shaped by the **parameter-driven pacing algorithm** (blend toward uniform spacing + per-move gap clamping) so the playback is viewer-friendly. The result is a `move_timings` array of per-ply delay fractions (summing to 1.0). See [Move Pacing & Timing Quality](#move-pacing--timing-quality).
6. **Writes** the merged output to `pipeline/output/merged_games.jsonl`.

### CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--games` | `pipeline/input/chess_games.jsonl` | Path to the primary games JSONL |
| `--augmented` | `pipeline/input/augmented_chess_games.jsonl` | Path to the augmented narratives JSONL |
| `--output` | `pipeline/output/merged_games.jsonl` | Output path |
| `--max-games` | `0` (no limit) | Limit number of games to output |
| `--russian-bias` | `3.0` | Weight multiplier per Russian relevance point (higher = more Russian games) |
| `--seed` | `42` | Random seed for reproducible sampling |
| `--no-stratify` | off | Disable stratified sampling; use simple first-N selection |
| `--no-clean` | off | Skip cleaning the output folder before processing |
| `--verify` | off | Verify merged output (spot-check + validation + pacing metric) |
| `--verify-count` | `3` | Number of games to display in verify mode |
| `--pacing-sample` | `5000` | Games to score for the pacing metric in `--verify` (`0` = all) |
| `--evaluate` | off | Grid-search the move-timing parameters against the pacing metric |
| `--eval-games` | `100` | Games to evaluate in `--evaluate` mode (100 dev / 10000 production) |

### Expected Output

```
Cleaned output folder: removed N items from .../pipeline/output
Pre-scanning augmented file for available keys...
  ...processed 50000 lines, 18000 unique keys
  ...processed 100000 lines, 25000 unique keys
  ...processed 150000 lines, 30000 unique keys
  Done: 189707 lines, 32000 unique keys available

Pre-scanning all games for stratified sampling (target: 500, oversampling to 575)...
  ...scanned 100000 lines, 8000 candidates
  ...scanned 200000 lines, 16000 candidates
  ...scanned 300000 lines, 24000 candidates
  Scanned 355980 lines, 28000 candidates with moves and augmented data

  Stratum        Pool  Slots Selected
  ------------ ------ ------ --------
  1830s            12      1        1  (0 Russian)
  1840s            45      1        1  (1 Russian)
  ...
  1950s          5500     77       77  (62 Russian)
  1960s         13000    191      191  (148 Russian)

  Total selected: 575
  Russian-connected: 445  Non-Russian: 130  (77.4% Russian)

Indexing augmented narratives from .../augmented_chess_games.jsonl...
  Early exit: found all 575 candidate keys
  Done: ~1500 lines processed, ~560 unique keys accepted

Processing games from .../chess_games.jsonl...
  Reached --max-games limit (500)

Summary:
  Total games scanned: ~355980
  Skipped (no augmented narrative): ~355480
  Written to merged output: 500
  Output: pipeline/output/merged_games.jsonl

=== Runtime summary ===
  scan_augmented_keys          XX.Xs  (X.X min)
  stratified_sample            XX.Xs  (X.X min)
  build_augmented_index        XX.Xs  (X.X min)
  process_games                XX.Xs  (X.X min)
  total                       XXX.Xs  (X.X min)
```

Every run prints a **Runtime summary** of the major phases (augmented-key
pre-scan, stratified sampling, augmented index build, and game processing).
These timings are produced by `time.perf_counter()` instrumentation in
`prepare_data.py` and are intended to be recorded alongside dataset builds.
Measured runtimes for the locked-in full build are listed in
[Move Pacing & Timing Quality](#move-pacing--timing-quality) below.

When `--max-games` is set, the script first pre-scans the augmented file to know which keys have narrative data available, then performs stratified sampling only among those games. This ensures the selected games actually have augmented narratives. The sampler over-selects by ~15% to compensate for quality-filter attrition, and `--max-games` is enforced as a hard cap on the final output.

The `--russian-bias` flag controls how aggressively Russian games are preferred (default 3.0). Set to 1.0 for uniform sampling, or higher values (e.g. 5.0) for stronger Russian bias. Use `--no-stratify` to revert to the old simple first-N selection.

### Verification

Verify the merged output:

```bash
python pipeline/prepare_data.py --verify
```

This spot-checks the first 3 games (use `--verify-count N` to check more) and validates:
- Move numbers are contiguous (no gaps from 1 to N)
- Each game has at least 1 segment
- Segment move ranges are non-overlapping and cover the full game
- Each non-intro segment has a `move_timings` array with the correct number of plies
- Timing values sum to 1.0 (within tolerance) and contain no negative values

It then reports the **pacing-quality metric** (see below) over the first
`--pacing-sample` games (default 5000; use `0` to score the entire file). The
metric is computed from the timings **actually stored** in the merged output,
so it reflects exactly what the web app will play.

```
Verified 334439 games, 229 issue(s) found.

=== Pacing metric - first 20000 games (20000 games, 1477168 plies) ===
  Score (0-100, higher=better) : 72.79
  Fast penalty   (<1s)      : 0.0265  (6.9% of moves)
  Boring penalty (>6s)      : 0.0444  (13.1% of interior moves)
  Match penalty  (unlocated)   : 0.3413  (match rate 65.9%)
  Align penalty  (voice drift) : 0.1077
  Median move gap              : 3.39s
```

---

## Move Pacing & Timing Quality

The board replays moves in sync with the voiceover using the per-ply
`move_timings` fractions written into each segment. Early builds anchored each
move purely to where its notation appears in the narrative text. Because dense
notation blocks (e.g. `16.cxd3 e5 17.fxe5 dxe5 18.Bxf6 …`) pack many moves into
a few characters while prose stretches a single move across a sentence, this
produced **bursts of sub-second moves followed by long boring gaps** — hard for
a viewer to follow.

### Pacing-Quality Metric

The metric scores how watchable a game's pacing is on a **0–100 scale (higher is
better)**. For each half-move it reconstructs the real on-screen gap a viewer
would experience:

$$\text{gap}_i = \text{move\_timings}_i \times \text{spoken\_duration(segment)}$$

where `spoken_duration` estimates the TTS narration length by running the exact
notation-conversion + sanitization pipeline used before synthesis and dividing
the resulting character count by a calibrated **13.3 characters/second** (the
median measured across generated audio). It then penalizes four failure modes:

| Sub-metric | What it penalizes | Notes |
|------------|-------------------|-------|
| **Fast** (`<1s`) | Sub-second gaps that are too quick to follow | First move of the game is exempt (lead-in) |
| **Boring** (`>6s`) | Interior gaps with long dead air | First and last gaps exempt (intro / final lingering) |
| **Match** | Moves not locatable in the narrative text, so they can't be correlated with the voiceover | Reported as a match rate |
| **Align** | Drift of the final timing away from where moves are actually mentioned (mild regularizer keeping moves roughly voice-synced) | Area between cumulative timing curves |

The composite score is:

$$\text{score} = 100 \cdot \left(1 - \operatorname{clamp}\big(w_f\,\text{fast} + w_b\,\text{boring} + w_m\,\text{match} + w_a\,\text{align},\ 0,\ 1\big)\right)$$

with weights $w_f = 1.0$, $w_b = 1.0$, $w_m = 0.5$, $w_a = 0.3$. Thresholds and
weights live at the top of `prepare_data.py` (`GAP_FAST`, `GAP_BORING`,
`W_FAST`, `W_BORING`, `W_MATCH`, `W_ALIGN`).

### Parameter-Driven Timing Algorithm

Move timings are now shaped by three parameters (`DEFAULT_TIMING_PARAMS` in
`prepare_data.py`). The raw text-anchored fractions are:

1. **Blended** toward uniform spacing — `blend` of `0` keeps pure text anchoring,
   `1` is fully uniform.
2. Converted to seconds via the segment's estimated spoken duration and
   **clamped** so each per-move gap falls within `[min_gap, max_gap]` seconds
   (`0` disables a clamp).
3. Renormalized back to fractions summing to 1.0.

This smooths out notation bursts (via the floor and the blend) while still
trimming dead air (via the cap), keeping moves roughly aligned to the voice.

### Evaluation Mode (Grid Search)

`--evaluate` grid-searches the timing parameters against the metric. It loads
the source dataset **once**, precomputes the param-independent features
(notation conversion, text anchoring, duration estimates), then re-scores every
parameter combination cheaply:

```bash
# Dev / test sweep (fast)
python pipeline/prepare_data.py --evaluate --eval-games 100

# Production sweep
python pipeline/prepare_data.py --evaluate --eval-games 10000
```

It prints a ranked table of all combinations and the optimal set, plus the
baseline (`blend=0`, no clamps) for comparison. The search space is `EVAL_GRID`:
`blend ∈ {0, 0.3, 0.5, 0.6, 0.7, 0.85, 1.0}`, `min_gap ∈ {0, 0.6, 0.9, 1.2}`,
`max_gap ∈ {0, 6, 7, 8, 10}` (140 combinations).

### Final Result (locked-in parameters)

The production sweep over **10,000 games (739,013 plies)** selected:

| Parameter | Value |
|-----------|-------|
| `blend` | **0.85** |
| `min_gap` | **1.2 s** |
| `max_gap` | **6.0 s** |

These are locked into `DEFAULT_TIMING_PARAMS`. Impact vs the original
text-anchored baseline:

| Metric | Baseline (deployed) | Optimal (locked) |
|--------|--------------------:|-----------------:|
| **Pacing score** | 59.18 | **72.60** |
| Sub-1s moves | 36.8% | **7.0%** |
| Boring (>6s) interior moves | 14.3% | **13.1%** |
| Median move gap | 2.05 s | **3.43 s** |
| Match rate | 65.9% | 65.9% |

The 100-game dev sweep and the verify metric on the deployed dataset
(score ≈ 59.4, 36.5% sub-1s moves) corroborate the same baseline and optimum.

After regenerating the full dataset with the locked parameters, the verify
metric over the first 20,000 games confirms the improvement carries through to
the on-disk timings:

| Metric | Old build (text-anchored) | New build (locked params) |
|--------|--------------------------:|--------------------------:|
| **Pacing score** | 59.37 | **72.79** |
| Sub-1s moves | 36.5% | **6.9%** |
| Median move gap | 2.02 s | **3.39 s** |

```bash
python pipeline/prepare_data.py --verify --pacing-sample 20000
```

### Full-Build Runtimes

Measured on the full run (all 334,439 games, no `--max-games`, so the
stratified-sampling phases are skipped). Captured via the `time.perf_counter()`
instrumentation reported in the **Runtime summary** block:

| Phase | Runtime |
|-------|--------:|
| `build_augmented_index` | 107.8 s (1.8 min) |
| `process_games` (parse + segment + timing) | 454.6 s (7.6 min) |
| **Total** | **562.5 s (9.4 min)** |

The grid-search sweeps are fast because features are precomputed once: the
production `--evaluate --eval-games 10000` sweep loaded its 10,000 games in
9.4 s and scored all 140 parameter combinations in 91.3 s.

> **Note:** `pipeline/output/merged_games.jsonl` has been regenerated with the
> locked parameters. To apply the improved pacing on the website, re-run
> Phase 2 audio generation so each `control.json` is rebuilt from the new
> timings, then redeploy.

---


## Phase 2: Audio Generation

> **Prerequisite:** `pipeline/audio/voice*.wav` clips must exist. See [02-pipeline-setup.md](02-pipeline-setup.md).

### Chess Notation Preprocessing

Before text is sent to the TTS engine, `chess_notation_converter.py` automatically converts algebraic chess notation into natural language, and `tts_text_sanitizer.py` performs a final cleanup pass. This runs transparently during audio generation — no extra steps required.

| Input | Output |
|-------|--------|
| `1.e4 c5` | "on the first move, white pawn to ee four, black pawn to see five" |
| `13.Nd4` | "on the thirteenth move, white knight to dee four" |
| `13...e5` | "on the thirteenth move, black pawn to ee five" |
| `O-O` | "white castles kingside" |
| `e8=Q+` | "white pawn to ee eight promoting to queen with check" |
| Long sequences (>4 moves) | Summarized: first/last moves in full, middle as "a series of N moves follows" |

Square names are spelled out phonetically (e.g. "ee four" instead of "e4") to prevent the XTTS v2 model from drifting into non-English phonemes.

The converter also detects existing "move N" references in the surrounding prose and skips redundant numbering.

#### Text Sanitization

After notation conversion, `tts_text_sanitizer.py` applies additional cleanup:

- **Unicode normalization** — non-breaking hyphens, smart quotes, and ellipsis characters are replaced with ASCII equivalents
- **ECO code expansion** — codes like "B62" are spelled out as "B sixty-two" when preceded by context words ("opening", "ECO", "the")
- **Result expansion** — "1-0" → "one-zero", "1/2-1/2" → "draw"
- **Residual cleanup** — trailing bare move numbers and double spaces are removed

### Small test run (5 games)

```bash
python pipeline/generate_audio.py --max-games 5
```

This will:
1. Load the XTTS v2 model onto the GPU (~30 seconds first time, downloads ~1.8 GB model)
2. For each game, convert chess notation to natural language, sanitize text, and generate one MP3 per narrative segment
3. Write `game.json` and `control.json` per game
4. Write `pipeline/output/index.json`

### Full run (500 games)

```bash
python pipeline/generate_audio.py --max-games 500 --resume
```

The `--resume` flag skips games already listed in `pipeline/output/progress.json`. This allows restarting after interruptions without reprocessing.

### CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--merged` | `pipeline/output/merged_games.jsonl` | Path to merged data |
| `--output-dir` | `pipeline/output` | Base output directory |
| `--voice-dir` | `pipeline/audio` | Directory containing voice reference WAVs |
| `--max-games` | `100` | Max games to process |
| `--resume` | off | Resume from checkpoint |
| `--overwrite` | off | Wipe existing state (`games/`, `progress.json`, `index.json`) and regenerate from scratch (mutually exclusive with `--resume`) |
| `--index-only` | off | Regenerate `index.json` without processing audio |
| `--gpu` | on | Use CUDA GPU |
| `--workers` | `2` | Number of parallel TTS model replicas (GPU inference is serialized via lock; parallelism overlaps GPU work with CPU ffmpeg conversion) |
| `--speed` | `1.15` | Speech speed multiplier (no pitch change). Range `0.5`–`2.0`; values `1.1`–`1.3` recommended |

### Expected Output per Game

```
pipeline/output/games/{game_id}/
├── game.json          # Game metadata + structured moves
├── control.json       # Audio sync data (segment durations, move ranges)
└── audio/
    ├── segment_00.mp3
    ├── segment_01.mp3
    └── ...
```

### Output file formats

**game.json:**
```json
{
  "key": "Kortschnoj, Viktor-Zhukhovitsky, Samuel-1950.??.??-URS-ch sf-?",
  "game_id": "kortschnoj-viktor-zhukhovitsky-samuel-1950-urs-ch-sf-afaeef9c",
  "white": "Kortschnoj, Viktor",
  "black": "Zhukhovitsky, Samuel",
  "date": "1950.??.??",
  "event": "URS-ch sf",
  "eco": "B62",
  "result": "1-0",
  "moves": [
    {"num": 1, "white": "e4", "black": "c5"},
    {"num": 2, "white": "Nf3", "black": "Nc6"},
    ...
  ]
}
```

**control.json:**
```json
{
  "key": "...",
  "game_id": "...",
  "total_moves": 55,
  "total_half_moves": 109,
  "segments": [
    {
      "segment_index": 0,
      "audio_file": "audio/segment_00.mp3",
      "duration_seconds": 28.45,
      "start_move": 1,
      "end_move": 15,
      "text": "The encounter between Viktor Kortschnoj and...",
      "move_timings": [0.12, 0.01, 0.02, 0.03, ...]
    },
    ...
  ]
}
```

**index.json:**
```json
[
  {
    "game_id": "kortschnoj-viktor-...-afaeef9c",
    "white": "Kortschnoj, Viktor",
    "black": "Zhukhovitsky, Samuel",
    "date": "1950.??.??",
    "event": "URS-ch sf",
    "eco": "B62",
    "result": "1-0"
  },
  ...
]
```

### Performance Estimates

Measured with 2 parallel model replicas, cached speaker latents, serialized GPU inference, and threaded ffmpeg conversion:

| Metric | Estimate |
|--------|----------|
| Model load (2 replicas) | ~60s (first run downloads ~1.8 GB) |
| Per game (avg) | ~118s |
| 100 games | ~3.3 hours |
| Disk per game | ~2–5 MB |

### Quality Check

After a test run, listen to a few segments:

```bash
# Play a random segment (Windows)
start pipeline\output\games\{game_id}\audio\segment_00.mp3
```

Verify:
- Voice sounds like the reference clip (deep, male, Russian-accented)
- English speech is clear and intelligible — no Chinese-sounding or non-English drift
- Chess square names are spoken phonetically ("ee four", "bee three", etc.)
- No audio artifacts, clicks, or cutoffs
- Duration in `control.json` matches actual MP3 length

### Voice Reference Clips

The quality of the Russian accent depends on the reference voice clips in `pipeline/audio/`:

- Clips **must** be a Russian-accented English speaker (not Russian speech or unaccented English)
- Aim for **30+ seconds total** across all `voice*.wav` files
- Include varied intonation: declarative, emphatic, questioning
- Ensure clean recordings with no background noise or room reverb
- Speaker conditioning uses `max_ref_length=30` and `sound_norm_refs=True` to maximize accent capture

### TTS Inference Tuning

The XTTS v2 inference uses tuned parameters to prevent language drift and improve output quality:

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `temperature` | `0.4` | Lower randomness prevents non-English phoneme sampling |
| `top_k` | `25` | Constrains to more likely English tokens |
| `top_p` | `0.7` | Further constrains nucleus sampling |
| `repetition_penalty` | `5.0` | Reduces repeated sounds and "uhhhh" artifacts |

### Regenerating the Index Only

If you manually add or remove game directories, regenerate the master index without reprocessing audio:

```bash
python pipeline/generate_audio.py --index-only
```

---

## Deploying to the Web App

After the pipeline completes, use the deploy script to build the `site\` folder and launch a local server:

```powershell
.\scripts\deploy_and_serve.ps1
```

This will:
1. Clean and recreate the `site\` directory
2. Copy all web assets from `web\`
3. Copy `index.json` and game folders from `pipeline\output\` into `site\data\`
4. Start a local server at `http://localhost:8000`

Open `http://localhost:8000` in a browser. Press `Ctrl+C` to stop the server.
