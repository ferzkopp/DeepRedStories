# Web Application

The web app is a fully static site — HTML, CSS, JavaScript, and pre-generated data files. No server-side processing is required. Any HTTP file server works.

## Serving Locally

For quick testing against the `web/` folder directly:

```bash
python -m http.server 8000 --directory web
```

Then open `http://localhost:8000`.

### Deploy and Serve Script

The `scripts/deploy_and_serve.ps1` script builds a self-contained `site/` folder and starts a local server in one step:

```powershell
.\scripts\deploy_and_serve.ps1
```

It cleans any previous `site/` build, copies the `web/` assets, then copies `pipeline/output/` data (index + game folders) into `site/data/`, and launches `python -m http.server 8000` from the `site/` directory.

## Serving Remotely

To deploy to a remote web server, run the full pipeline and then copy the built site:

1. **Run the pipeline** (if not already done):

   ```bash
   python pipeline/prepare_data.py --max-games 500
   python pipeline/generate_audio.py --max-games 500 --resume
   ```

2. **Build the site:**

   ```powershell
   .\scripts\deploy_and_serve.ps1
   ```

   Stop the local server (`Ctrl+C`) once the build completes — only the `site/` folder is needed.

3. **Copy to the remote server:**

   ```bash
   scp -r site/* user@host:/var/www/deepred/
   ```

   Or use `rsync` for incremental updates:

   ```bash
   rsync -avz --delete site/ user@host:/var/www/deepred/
   ```

The site is fully static — any HTTP server (Nginx, Apache, Caddy, S3, GitHub Pages, etc.) can serve it with no additional configuration.

## How It Works

1. **Startup:** Fetches `data/index.json` to get the list of available games.
2. **Game selection:** Picks a random game from the index.
3. **Data loading:** Fetches `data/games/{game_id}/game.json` (moves + metadata) and `control.json` (audio segments + sync info).
4. **Board setup:** Initializes chess.js at starting position and renders the board with chessboard.js.
5. **Playback:** Iterates through audio segments in order:
   - Plays each segment's MP3 via HTML5 `Audio`
   - Schedules board moves using per-ply `move_timings` fractions from `control.json`, so moves appear when the narrator speaks them. Falls back to uniform spacing (`segment_duration / moves_in_segment`) for segments without timing data.
   - Displays the narrative text as a subtitle
   - Transitions to the next segment when audio ends

## Controls

| Control | Function |
|---------|----------|
| **▶ / ⏸** | Play or pause playback (pauses both audio and move advancement) |
| **⏩ Fast Forward** | Skip to the next audio segment |
| **⟳ New Game** | Stop current game and load a new random game |
| **Auto-Continue** | Checkbox — automatically play the next game when the current one ends |
| **Progress bar** | Visual indicator of game progress |
| **Game counter** | Shows current game position in the shuffled playlist (e.g. Game: 3/12) |
| **Move counter** | Shows current move / total moves (e.g. Move: 41/81) |

## UI Layout

```
┌───────────────────────┬──────────────────────────────┐
│  DEEP RED             │  ♔ White Name  vs           │
│  Dispatches from the  │  ♚ Black Name               │  ← Header bar
│  Deep Red Continuum   │  Event │ Year │ ECO │ Result │
├───────────────────────┴───────────┬──────────────────┤
│                                   │  MOVES           │
│    ♜ ♞ ♝ ♛ ♚ ♝ ♞ ♜          │  1. e4    c5     │
│    ♟ ♟ ♟ ♟ ♟ ♟ ♟ ♟          │  2. Nf3   Nc6    │
│                                   │  3. d4    cxd4   │  ← Main area
│    ♙ ♙ ♙ ♙ ♙ ♙ ♙ ♙          │  4. Nxd4  ...    │
│    ♖ ♘ ♗ ♕ ♔ ♗ ♘ ♖          │                  │
├───────────────────────────────────┴──────────────────┤
│  ▶  ⏭  ⟳ New Game  ████████░░░░░░░░  Move: 41/81    │  ← Controls
├──────────────────────────────────────────────────────┤
│  // "The position became a battlefield of rooks..."  │  ← Narrative subtitle
└──────────────────────────────────────────────────────┘
```

- **Dark theme** (#0f0f1a background) for atmosphere
- **Responsive** — stacks vertically on small screens
- Move list auto-scrolls to highlight the current move

## Libraries

| Library | Version | Purpose |
|---------|---------|---------|
| [jQuery](https://jquery.com/) | 3.7.1 | DOM manipulation (required by chessboard.js) |
| [chess.js](https://github.com/jhlywa/chess.js) | 0.10.3 | Game logic, move validation, FEN generation |
| [chessboard.js](https://chessboardjs.com/) | 1.0.0 | Board rendering with animated piece movement |

Both are vendored locally in `web/js/` and `web/css/` — no CDN dependency.

## Data Directory Structure

The `web/data/` directory is copied from `pipeline/output/` after audio generation:

```
web/data/
├── index.json                    # Array of all available games
└── games/
    └── {game_id}/
        ├── game.json             # Metadata + structured moves
        ├── control.json          # Audio segment sync info
        └── audio/
            ├── segment_00.mp3
            ├── segment_01.mp3
            └── ...
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Board doesn't render | Check browser console; verify piece images exist in `web/img/chesspieces/wikipedia/` |
| "Error loading game index" | Ensure `web/data/index.json` exists and the HTTP server root is `web/` |
| Audio doesn't play | Check that MP3 files exist in the game's `audio/` directory; some browsers block autoplay before user interaction |
| Moves out of sync | Sync uses per-ply `move_timings` from `control.json`. If timings are missing, falls back to uniform spacing based on `duration_seconds`. Re-run `prepare_data.py` then `generate_audio.py` to regenerate timing data |
| CORS errors | Must serve via HTTP server, not `file://` protocol |
