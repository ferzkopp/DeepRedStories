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

```bash
python pipeline/prepare_data.py --max-games 500
```

### What it does

1. **Stratified sampling** (when `--max-games` is set): first cleans the output folder (`pipeline/output/`) by removing `merged_games.jsonl`, `index.json`, `progress.json`, and the entire `games/` directory (use `--no-clean` to skip this). Then pre-scans `augmented_chess_games.jsonl` to collect all available keys, then scans `chess_games.jsonl` to collect candidates — only considering games that exist in the augmented file. Games are grouped into decade-based strata with a Russian/Soviet relevance score (0–3). Slots are allocated proportionally per stratum. Within each stratum, games are selected via weighted sampling — Russian-connected games are boosted by `russian_bias ^ score` (default 3.0×, so a game with two Russian players at a Soviet event is 27× more likely to be picked). The sampler over-selects by ~15% to compensate for downstream quality-filter attrition, with `--max-games` enforced as a hard cap on final output. Use `--no-stratify` to fall back to simple first-N selection. Then **indexes** `augmented_chess_games.jsonl` — building an in-memory map of `key → best English narrative`, exiting early once all selected candidates are found. Applies quality filters (English detection, minimum 500 chars, no gibberish/repetition). Prefers `prompt_variant == 1` (Russian style). Without `--max-games`, the entire augmented file is indexed.
2. **Joins** with `chess_games.jsonl` — streams game records, keeps only those with a matching narrative.
3. **Parses moves** from the game text into a structured list: `[{num: 1, white: "e4", black: "c5"}, ...]`
4. **Segments the narrative** into move-aligned paragraphs, each tagged with `start_move` / `end_move`.
5. **Estimates move timings** for each segment by locating move notations in the narrative text. Character positions are converted to fractional time anchors; moves not mentioned in the text are linearly interpolated between anchors. The result is a `move_timings` array of per-ply delay fractions (summing to 1.0).
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
| `--verify` | off | Verify merged output (spot-check + validation) |
| `--verify-count` | `3` | Number of games to display in verify mode |

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
```

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
