# DEEP RED Stories — Project Overview

A static web application that replays historical chess games with synchronized AI-generated audio commentary. A deep-voiced, Russian-accented narrator guides the viewer through each game move by move.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  OFFLINE PIPELINE                   │
│                                                     │
│  pipeline/input/     │                              │
│    chess_games.jsonl ┤                              │
│                      ├─► prepare_data.py            │
│    augmented_chess_  │     ▼                        │
│      games.jsonl ────┘   merged_games.jsonl         │
│                              │                      │
│  reference_voice.wav ───►    ▼                      │
│                        generate_audio.py            │
│                         ▲    │                      │
│  chess_notation_        │    │                      │
│    converter.py ────────┘    ▼                      │
│                     pipeline/output/                │
│                       ├── index.json                │
│                       └── games/{id}/               │
│                             ├── game.json           │
│                             ├── control.json        │
│                             └── audio/              │
│                                  └── segment_NN.mp3 │
└─────────────────────────────────────────────────────┘
                         │
                    copy to web/data/
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│                   WEB APPLICATION                   │
│                                                     │
│  Static HTML/JS/CSS served from any HTTP server     │
│  No backend required                                │
│                                                     │
│  • Picks a random game from index.json              │
│  • Renders chessboard with animated piece moves     │
│  • Plays audio segments synchronized to move flow   │
│  • Controls: Play/Pause, New Game, Auto-continue    │
└─────────────────────────────────────────────────────┘
```

## Source Data

| File | Description | Size |
|------|-------------|------|
| `pipeline/input/chess_games.jsonl` | Historical chess games with metadata and moves in algebraic notation | ~300K lines |
| `pipeline/input/augmented_chess_games.jsonl` | AI-generated narrative commentary for each game | ~200K lines |

Both files share a `key` field (format: `"Player1-Player2-Date-Event-Round"`).

The augmented file contains a `prompt_variant` field (0–4) controlling writing style. Variant 1 is the preferred "Russian Comrades" English style. All narratives pass through quality filters that reject non-English texts, overly short texts (<500 chars), texts with nonsensical long words (>50 chars), and texts with excessive word repetition.

## Directory Structure

```
DeepRedStories/
├── docs/                          # This documentation
├── pipeline/
│   ├── input/                     # Source data (read-only)
│   │   ├── chess_games.jsonl
│   │   └── augmented_chess_games.jsonl
│   ├── prepare_data.py            # Phase 1: data joining, sampling & parsing
│   ├── generate_audio.py          # Phase 2: TTS audio generation
│   ├── chess_notation_converter.py # Notation-to-speech preprocessing
│   ├── requirements.txt           # Python dependencies
│   ├── reference_voice.wav        # Voice reference clip (user-provided)
│   └── output/                    # Generated content (gitignored)
│       ├── merged_games.jsonl
│       ├── index.json
│       ├── progress.json
│       └── games/{game_id}/
│           ├── game.json
│           ├── control.json
│           └── audio/segment_NN.mp3
└── web/
    ├── index.html
    ├── css/
    │   ├── style.css
    │   └── chessboard-1.0.0.min.css
    ├── js/
    │   ├── app.js
    │   ├── chess-0.10.3.min.js
    │   └── chessboard-1.0.0.min.js
    ├── img/chesspieces/wikipedia/  # 12 piece PNGs
    └── data/                       # Copied from pipeline/output/
        ├── index.json
        └── games/...
```

## Key Decisions

- **TTS engine:** Coqui XTTS v2 — voice cloning with ~5–6 GB VRAM, fits a 12 GB GPU
- **Voice:** Deep male, slight Russian accent via a reference WAV clip
- **Scope:** 500–1000 games, selected via stratified time-based sampling with Russian/Soviet bias
- **Sampling strategy:** Games are divided into decade-based strata for even time coverage; within each stratum, games involving Russian/Soviet players or events are weighted higher (configurable bias multiplier)
- **Audio format:** MP3 128 kbps
- **Chess rendering:** chess.js (game logic) + chessboard.js (board UI), both client-side
- **Narrative selection:** `prompt_variant == 1` preferred (Russian "Comrades" style). Fallback to variants 0, 3, 4. All variants pass through quality filters (English detection, minimum length, gibberish rejection).
- **Notation-to-speech:** Chess algebraic notation (e.g. `1.e4 c5 2.Nf3`) is converted to natural language before TTS generation. Long move sequences (>4 moves) are consolidated into summaries to keep audio engaging.
- **Move-audio synchronization:** Each segment includes a `move_timings` array of per-ply delay fractions (summing to 1.0) estimated from where move notations appear in the narrative text. The web app uses these to schedule moves non-linearly so that a move is displayed on the board at approximately the same time the narrator speaks it. Segments without embedded move references fall back to uniform spacing.
