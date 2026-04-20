# Pipeline Setup & Prerequisites

## System Requirements

| Component | Requirement |
|-----------|-------------|
| Python | 3.10 or 3.11 (Coqui TTS does not support 3.12+) |
| GPU | NVIDIA with CUDA, 12 GB VRAM |
| CUDA toolkit | 11.8+ (must match PyTorch build) |
| ffmpeg | Installed and on PATH |
| Disk space | ~5 GB for model + ~2 GB per 100 games of audio output |

## Download Input Data

The two large input files are not included in the repository. Download them into `pipeline/input/`:

```powershell
# Windows (PowerShell)
New-Item -ItemType Directory -Force pipeline/input
Invoke-WebRequest -Uri https://www.ferzkopp.net/Data/chess_games.jsonl.gz -OutFile pipeline/input/chess_games.jsonl.gz
Invoke-WebRequest -Uri https://www.ferzkopp.net/Data/augmented_chess_games.jsonl.gz -OutFile pipeline/input/augmented_chess_games.jsonl.gz
```

```bash
# Linux / macOS
mkdir -p pipeline/input
curl -L -o pipeline/input/chess_games.jsonl.gz https://www.ferzkopp.net/Data/chess_games.jsonl.gz
curl -L -o pipeline/input/augmented_chess_games.jsonl.gz https://www.ferzkopp.net/Data/augmented_chess_games.jsonl.gz
```

| File | Description |
|------|-------------|
| `chess_games.jsonl.gz` | Primary chess game records (PGN headers, moves, metadata) |
| `augmented_chess_games.jsonl.gz` | AI-generated narrative annotations for each game |

## Install ffmpeg

ffmpeg is required for WAV → MP3 conversion.

**Windows (winget):**
```powershell
winget install --id Gyan.FFmpeg -e --source winget
```

Restart terminal and verify with: `ffmpeg -version`

## Install Python Dependencies

Create a virtual environment with Python 3.11:

cd DeepRedStories
py -3.11 -m venv .venv
.venv\Scripts\activate       # Windows
# source .venv/bin/activate  # Linux / macOS
python.exe -m pip install --upgrade pip
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
# or pip install --force-reinstall torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
pip install -r pipeline/requirements.txt
pip install --force-reinstall --no-deps "pandas>=2.2" "transformers>=4.50,<4.52" "tokenizers>=0.21,<0.22" "huggingface-hub>=0.34,<1.0"
```

> **Important:** Install PyTorch with CUDA **before** the other dependencies.
> `pip install TTS` will pull in CPU-only PyTorch if it isn't already installed.

> **Note:** The final line force-installs compatible versions of three packages.
> Coqui TTS 0.22 pins `pandas<2.0`, but modern numpy (shipped with PyTorch) requires
> pandas 2.x+. TTS also imports `BeamSearchScorer`, which was removed in transformers 4.52+.
> Transformers 4.50–4.51 requires `tokenizers>=0.21,<0.22` and `huggingface-hub<1.0`.
> The `--force-reinstall --no-deps` flags bypass pip's resolver to apply these overrides.
> pip will show a dependency warning — this is safe to ignore; TTS works fine at runtime.

This installs:
- **torch** — PyTorch with CUDA 12.8 support
- **TTS** (Coqui TTS) — the XTTS v2 text-to-speech engine
- **soundfile** — audio I/O backend for torchaudio (avoids torchcodec DLL issues on Windows)
- **pydub** — audio format utilities
- **tqdm** — progress bars

## Prepare the Reference Voice

The TTS engine clones a voice from a short reference audio clip. This clip defines the narrator's character: a deep male voice with a slight Russian accent speaking English.

### Requirements for the clips

| Property | Value |
|----------|-------|
| Duration | 6–10 seconds each |
| Format | WAV |
| Content | Continuous English speech, single speaker |
| Quality | Clean recording, minimal background noise |

> **Note:** The script automatically converts clips to 22050 Hz mono on first run, so sample rate and channel count don't matter.

### Recommended Sourcing Approach

Target speakers with distinctive voices and Russian accents:

| Speaker | Video Title | YouTube Link |
|--------|-------------|--------------|
| **Garry Kasparov** | Putin's attempts to restore Russia's lost empire destined to fail | https://www.youtube.com/watch?v=3gOpI3AieFo |
| **Peter Svidler** | The Glory Days of 1999 | https://www.youtube.com/watch?v=Oe0rmoxOPdg |
| **Vladimir Kramnik** | Tells about the legendary match with Kasparov — Second Interview | https://www.youtube.com/watch?v=O87f37AJDkM |
| **Vladimir Kramnik** | Talks about the legendary match — Fourth Interview | https://www.youtube.com/watch?v=1nJDL7uhk8c |
| **Crazy Russian Dad** | Explains and demonstrates Russian accent | https://www.youtube.com/watch?v=BbcBcmDmDUk |
| **Various**| Russians in Hollywood movies (collection) | https://www.youtube.com/watch?v=7c2tOzh0_tk |

Steps:
1. Find a suitable interview clip on YouTube
2. Download the audio (various tools) and convert to wav (Audacity) 
3. Isolate vocals from background, lower pitch, normalize (Audacity):
4. Trim to a clean 6–10 second segment (Audacity)

### Final placement

Save one or more prepared clips as:
```
pipeline/audio/voice1.wav
pipeline/audio/voice2.wav
...
```

Using multiple clips (from different sentences of the same speaker) improves voice cloning quality — XTTS v2 averages the speaker embeddings.

The audio generation script will refuse to run without at least one `voice*.wav` file in this directory.

## Verify GPU Availability

```python
import torch
print(torch.cuda.is_available())       # Should be True
print(torch.cuda.get_device_name(0))   # Should show your GPU
print(torch.cuda.mem_get_info()[0] / 1e9, "GB free")  # Should be >6 GB
```

XTTS v2 uses approximately 5–6 GB VRAM. With a 12 GB GPU, this leaves headroom for batch processing.
