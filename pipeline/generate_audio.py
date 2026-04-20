#!/usr/bin/env python3
"""
Phase 2: Audio Generation
- Reads merged_games.jsonl from prepare_data.py
- Generates TTS audio for each narrative segment using Coqui XTTS v2
- Outputs per-game: game.json, control.json, audio/segment_NN.mp3
- Outputs master index.json
- Supports --max-games, --resume, --start-from
"""

import argparse
import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import torch
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from pathlib import Path

from chess_notation_converter import convert_chess_notation
from tts_text_sanitizer import sanitize_for_tts

# Coqui TTS checkpoints contain custom classes; PyTorch 2.6+ defaults to weights_only=True
# which rejects them. Override so torch.load works with the XTTS v2 model.
torch.serialization.add_safe_globals([])  # no-op to ensure module is loaded
_orig_torch_load = torch.load
def _patched_torch_load(*args, **kwargs):
    kwargs.setdefault("weights_only", False)
    return _orig_torch_load(*args, **kwargs)
torch.load = _patched_torch_load

# torchaudio 2.11 hardcodes torchcodec as its only backend for torchaudio.load(),
# but torchcodec requires FFmpeg shared DLLs that are painful on Windows.
# Replace torchaudio.load with a soundfile-based implementation.
import torchaudio
import soundfile as sf
import numpy as np

def _soundfile_load(uri, frame_offset=0, num_frames=-1, normalize=True,
                    channels_first=True, format=None, buffer_size=4096, backend=None):
    data, sample_rate = sf.read(uri, dtype="float32",
                                start=frame_offset,
                                stop=frame_offset + num_frames if num_frames > 0 else None,
                                always_2d=True)
    # data shape: (frames, channels)
    tensor = torch.from_numpy(data)
    if channels_first:
        tensor = tensor.t()
    return tensor, sample_rate

torchaudio.load = _soundfile_load

# Suppress the attention-mask warning emitted by the transformers GPT-2 tokenizer
# used internally by XTTS v2.  The message is logged via the `transformers` logging
# system, not Python warnings, so we need to patch at that level.
import logging as _logging

_transformers_logger = _logging.getLogger("transformers.modeling_utils")
_transformers_logger.setLevel(_logging.ERROR)

# Suppress Coqui TTS verbose output ("Text splitted to sentences",
# "Processing time", "Real-time factor", etc.)
_logging.getLogger("TTS.tts.layers.xtts.stream_generator").setLevel(_logging.ERROR)
_logging.getLogger("TTS.tts.models.xtts").setLevel(_logging.ERROR)


# ── Graceful shutdown on Ctrl+C ────────────────────────────────────────────
_shutdown = threading.Event()


def _signal_handler(sig, frame):
    if _shutdown.is_set():
        print("\nForced exit.")
        sys.exit(1)
    print("\nInterrupt received \u2014 finishing current segment, then stopping...")
    _shutdown.set()


# ── Text chunking ──────────────────────────────────────────────────────────
_CHUNK_LIMIT = 230  # stay safely under the 250-char XTTS v2 ceiling


def _split_at(text: str, delimiters: str) -> list[str]:
    """Split *text* on any character in *delimiters*, keeping the delimiter
    attached to the preceding fragment."""
    pattern = "([" + re.escape(delimiters) + "])"
    parts = re.split(pattern, text)
    # Re-attach each delimiter to the chunk before it
    chunks: list[str] = []
    for p in parts:
        if not p:
            continue
        if chunks and len(p) == 1 and p in delimiters:
            chunks[-1] += p
        else:
            chunks.append(p)
    return [c.strip() for c in chunks if c.strip()]


def chunk_text(text: str, limit: int = _CHUNK_LIMIT) -> list[str]:
    """Break *text* into pieces that each fit within *limit* characters.

    Strategy (progressive):
    1. Split on sentence-ending punctuation (.!?)
    2. If a piece is still too long, split on commas / semicolons / colons
    3. If still too long, split on the last space before the limit
    """
    if len(text) <= limit:
        return [text]

    # Step 1 – sentence boundaries
    sentences = _split_at(text, ".!?")

    result: list[str] = []
    for sent in sentences:
        if len(sent) <= limit:
            result.append(sent)
            continue
        # Step 2 – clause boundaries
        clauses = _split_at(sent, ",;:")
        for clause in clauses:
            if len(clause) <= limit:
                result.append(clause)
                continue
            # Step 3 – hard word-boundary split
            while len(clause) > limit:
                idx = clause.rfind(" ", 0, limit)
                if idx == -1:
                    idx = limit  # no space; force split
                result.append(clause[:idx].strip())
                clause = clause[idx:].strip()
            if clause:
                result.append(clause)

    return result


def concatenate_wavs(wav_paths: list[str], output_path: str):
    """Concatenate multiple WAV files (same sample rate / channels) into one."""
    if len(wav_paths) == 1:
        os.rename(wav_paths[0], output_path)
        return
    with wave.open(output_path, "wb") as out:
        params_set = False
        for wp in wav_paths:
            with wave.open(wp, "rb") as inp:
                if not params_set:
                    out.setparams(inp.getparams())
                    params_set = True
                out.writeframes(inp.readframes(inp.getnframes()))
    for wp in wav_paths:
        os.remove(wp)


def check_dependencies():
    """Verify required packages are available."""
    missing = []
    try:
        import torch
    except ImportError:
        missing.append("torch")
    try:
        from TTS.api import TTS
    except ImportError:
        missing.append("TTS")
    if missing:
        print(f"Error: Missing packages: {', '.join(missing)}")
        print("Install with: pip install -r pipeline/requirements.txt")
        sys.exit(1)


def wav_duration(path: str) -> float:
    """Get duration of a WAV file in seconds."""
    with wave.open(path, 'r') as w:
        return w.getnframes() / w.getframerate()


def convert_wav_to_mp3(wav_path: str, mp3_path: str, bitrate: str = "128k"):
    """Convert WAV to MP3 using ffmpeg."""
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", wav_path, "-b:a", bitrate, "-ar", "22050", mp3_path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[:500]}")
    os.remove(wav_path)


def normalize_voice_clips(voice_dir: str):
    """Convert all voice*.wav files in voice_dir to 22050 Hz mono."""
    from pydub import AudioSegment

    for path in sorted(glob.glob(os.path.join(voice_dir, "voice*.wav"))):
        audio = AudioSegment.from_wav(path)
        changed = False
        if audio.frame_rate != 22050:
            audio = audio.set_frame_rate(22050)
            changed = True
        if audio.channels != 1:
            audio = audio.set_channels(1)
            changed = True
        if changed:
            audio.export(path, format="wav")
            print(f"  Normalized: {os.path.basename(path)} -> 22050 Hz mono")


def load_progress(progress_path: str) -> set:
    """Load set of already-completed game_ids."""
    if os.path.exists(progress_path):
        with open(progress_path, 'r') as f:
            return set(json.load(f))
    return set()


def save_progress(progress_path: str, completed: set):
    """Save completed game_ids to checkpoint."""
    with open(progress_path, 'w') as f:
        json.dump(sorted(completed), f)


# ── Low-level TTS helpers ──────────────────────────────────────────────────

def _trim_trailing_silence(samples: np.ndarray, threshold: float = 0.01,
                           min_silence_samples: int = 1000,
                           sample_rate: int = 22050) -> np.ndarray:
    """Remove trailing silence from a waveform, keeping a short tail."""
    abs_samples = np.abs(samples)
    above = np.where(abs_samples > threshold)[0]
    if len(above) == 0:
        return samples
    last_loud = above[-1]
    # Keep a small buffer (~20ms) after the last loud sample for natural decay
    keep = min(last_loud + int(0.02 * sample_rate), len(samples))
    return samples[:keep]


def _write_wav(samples: np.ndarray, path: str, sample_rate: int = 22050):
    """Write a 1-D float32 numpy array to a 16-bit mono WAV file."""
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())


def compute_speaker_latents(tts, reference_voices: list[str]):
    """Pre-compute speaker conditioning latents once for the whole run."""
    gpt_cond_latent, speaker_embedding = (
        tts.synthesizer.tts_model.get_conditioning_latents(
            audio_path=reference_voices,
            gpt_cond_len=60,
            gpt_cond_chunk_len=4,
            max_ref_length=30,
            sound_norm_refs=True,
        )
    )
    return gpt_cond_latent, speaker_embedding


def synthesize_chunk(tts, text: str, gpt_cond_latent, speaker_embedding,
                     speed: float = 1.15,
                     cuda_lock: threading.Lock = None,
                     cuda_stream: "torch.cuda.Stream | None" = None) -> np.ndarray:
    """Run XTTS v2 inference for a single text chunk using cached latents.
    Returns the waveform as a 1-D float32 numpy array.
    When *cuda_lock* is provided, serializes GPU access across threads.
    When *cuda_stream* is provided, runs inference on that stream."""
    if cuda_lock is not None:
        cuda_lock.acquire()
    try:
        ctx = torch.cuda.stream(cuda_stream) if cuda_stream is not None else nullcontext()
        with ctx:
            out = tts.synthesizer.tts_model.inference(
                text=text,
                language="en",
                gpt_cond_latent=gpt_cond_latent,
                speaker_embedding=speaker_embedding,
                temperature=0.4,
                top_k=25,
                top_p=0.7,
                repetition_penalty=5.0,
                speed=speed,
            )
            if cuda_stream is not None:
                cuda_stream.synchronize()
    finally:
        if cuda_lock is not None:
            cuda_lock.release()
    wav = out["wav"]
    if isinstance(wav, torch.Tensor):
        wav = wav.cpu().numpy()
    return np.asarray(wav, dtype=np.float32).squeeze()


def generate_game_audio(
    tts,
    game: dict,
    output_dir: str,
    gpt_cond_latent,
    speaker_embedding,
    mp3_pool: ThreadPoolExecutor,
    speed: float = 1.15,
    chunk_bar=None,
    cuda_lock: threading.Lock = None,
    cuda_stream: "torch.cuda.Stream | None" = None,
) -> list[dict]:
    """
    Generate audio for all segments of a single game.
    Returns list of segment info dicts with duration.
    Uses cached speaker latents and submits ffmpeg conversions to *mp3_pool*.
    """
    audio_dir = os.path.join(output_dir, "audio")
    os.makedirs(audio_dir, exist_ok=True)

    segment_infos = []
    mp3_futures = []  # collect async ffmpeg jobs

    for seg in game["segments"]:
        if _shutdown.is_set():
            break
        idx = seg["segment_index"]
        original_text = seg["text"]

        # Convert chess notation to natural language for TTS
        tts_text = convert_chess_notation(original_text)
        # Sanitize text for TTS (normalize unicode, spell out codes, etc.)
        tts_text = sanitize_for_tts(tts_text)

        # Skip very short segments (< 10 chars)
        if len(tts_text.strip()) < 10:
            continue

        wav_path = os.path.join(audio_dir, f"segment_{idx:02d}.wav")
        mp3_path = os.path.join(audio_dir, f"segment_{idx:02d}.mp3")

        # Chunk text to stay within XTTS v2 character limit
        chunks = chunk_text(tts_text)

        if len(chunks) == 1:
            wav_data = synthesize_chunk(tts, chunks[0], gpt_cond_latent, speaker_embedding,
                                        speed=speed, cuda_lock=cuda_lock, cuda_stream=cuda_stream)
            _write_wav(wav_data, wav_path)
            if chunk_bar is not None:
                chunk_bar.update(1)
        else:
            # Multiple chunks – synthesize each, concatenate waveforms in memory
            wav_parts: list[np.ndarray] = []
            for ci, chunk in enumerate(chunks):
                part = synthesize_chunk(tts, chunk, gpt_cond_latent, speaker_embedding,
                                        speed=speed, cuda_lock=cuda_lock, cuda_stream=cuda_stream)
                # Trim trailing silence from intermediate chunks to avoid mid-sentence pauses
                if ci < len(chunks) - 1:
                    part = _trim_trailing_silence(part)
                wav_parts.append(part)
                if chunk_bar is not None:
                    chunk_bar.update(1)
            _write_wav(np.concatenate(wav_parts), wav_path)

        # Get duration before converting
        duration = wav_duration(wav_path)

        # Submit MP3 conversion to thread pool (CPU work, overlaps with next GPU chunk)
        mp3_futures.append(mp3_pool.submit(convert_wav_to_mp3, wav_path, mp3_path))

        segment_infos.append({
            "segment_index": idx,
            "audio_file": f"audio/segment_{idx:02d}.mp3",
            "duration_seconds": round(duration, 2),
            "start_move": seg["start_move"],
            "end_move": seg["end_move"],
            "text": original_text,
            "tts_text": tts_text,
            "move_timings": seg.get("move_timings", []),
        })

    # Wait for all MP3 conversions for this game to finish
    for fut in mp3_futures:
        fut.result()  # re-raises any ffmpeg errors

    return segment_infos


def write_game_files(game: dict, segment_infos: list[dict], output_dir: str):
    """Write game.json and control.json for a processed game."""

    # game.json — metadata + moves
    game_data = {
        "key": game["key"],
        "game_id": game["game_id"],
        "white": game["white"],
        "black": game["black"],
        "date": game["date"],
        "event": game["event"],
        "eco": game["eco"],
        "opening": game.get("opening", ""),
        "result": game["result"],
        "moves": game["moves"],
    }
    with open(os.path.join(output_dir, "game.json"), 'w', encoding='utf-8') as f:
        json.dump(game_data, f, ensure_ascii=False, indent=2)

    # control.json — audio sync info
    control_data = {
        "key": game["key"],
        "game_id": game["game_id"],
        "total_moves": max(m["num"] for m in game["moves"]) if game["moves"] else 0,
        "total_half_moves": game["total_half_moves"],
        "segments": segment_infos,
    }
    with open(os.path.join(output_dir, "control.json"), 'w', encoding='utf-8') as f:
        json.dump(control_data, f, ensure_ascii=False, indent=2)


def write_index(games_dir: str, index_path: str):
    """Generate master index.json from all processed game directories."""
    entries = []
    games_root = Path(games_dir)

    for game_dir in sorted(games_root.iterdir()):
        game_json = game_dir / "game.json"
        if game_json.exists():
            with open(game_json, 'r', encoding='utf-8') as f:
                data = json.load(f)
            entries.append({
                "game_id": data["game_id"],
                "white": data["white"],
                "black": data["black"],
                "date": data["date"],
                "event": data["event"],
                "eco": data["eco"],
                "result": data["result"],
            })

    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)

    print(f"Index written: {len(entries)} games -> {index_path}")


def main():
    parser = argparse.ArgumentParser(description="Generate TTS audio for chess narratives")
    parser.add_argument("--merged", default="pipeline/output/merged_games.jsonl",
                        help="Path to merged_games.jsonl")
    parser.add_argument("--output-dir", default="pipeline/output",
                        help="Base output directory")
    parser.add_argument("--voice-dir", default="pipeline/audio",
                        help="Directory containing reference voice WAV files (voice*.wav)")
    parser.add_argument("--max-games", type=int, default=100,
                        help="Max games to process (default: 100)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from last checkpoint")
    parser.add_argument("--index-only", action="store_true",
                        help="Only regenerate index.json from existing game dirs")
    parser.add_argument("--gpu", action="store_true", default=True,
                        help="Use GPU (default: True)")
    parser.add_argument("--workers", type=int, default=2,
                        help="Number of parallel TTS model replicas (default: 2)")
    parser.add_argument("--speed", type=float, default=1.15,
                        help="TTS speech speed multiplier (default: 1.15, no pitch change)")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    merged_path = root / args.merged
    output_dir = root / args.output_dir
    games_dir = output_dir / "games"
    index_path = output_dir / "index.json"
    progress_path = output_dir / "progress.json"
    voice_dir = root / args.voice_dir

    # Index-only mode
    if args.index_only:
        write_index(str(games_dir), str(index_path))
        return

    # Validate inputs
    if not merged_path.exists():
        print(f"Error: {merged_path} not found. Run prepare_data.py first.")
        sys.exit(1)
    ref_voices = sorted(glob.glob(str(voice_dir / "voice*.wav")))
    if not ref_voices:
        print(f"Error: No voice*.wav files found in {voice_dir}")
        print("Provide one or more 6-10 second WAV clips of the target voice.")
        sys.exit(1)
    print(f"Reference voices: {len(ref_voices)} file(s) from {voice_dir}")
    normalize_voice_clips(str(voice_dir))

    check_dependencies()

    # Load TTS model(s)
    import torch
    from TTS.api import TTS

    device = "cuda" if args.gpu and torch.cuda.is_available() else "cpu"
    num_workers = max(1, args.workers) if device == "cuda" else 1

    print(f"Loading {num_workers} XTTS v2 replica(s) on {device}...")
    replicas: list[TTS] = []
    latents: list[tuple] = []  # (gpt_cond_latent, speaker_embedding) per replica
    for i in range(num_workers):
        tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)
        gpt_cond, spk_emb = compute_speaker_latents(tts, ref_voices)
        replicas.append(tts)
        latents.append((gpt_cond, spk_emb))
        if num_workers > 1:
            print(f"  Replica {i + 1}/{num_workers} ready.")
    print(f"Model(s) loaded — speaker latents cached.")

    # Load progress checkpoint
    completed = load_progress(str(progress_path)) if args.resume else set()
    if completed:
        print(f"Resuming: {len(completed)} games already completed")

    # Install Ctrl+C handler for graceful shutdown
    signal.signal(signal.SIGINT, _signal_handler)

    # Process games
    from tqdm import tqdm

    processed = 0
    total_segments = 0
    start_time = time.time()

    # Pre-read all lines so we know the total count (file is small)
    with open(merged_path, 'r', encoding='utf-8') as f:
        all_lines = [l.strip() for l in f if l.strip()]

    games_to_run = []
    for line in all_lines:
        game = json.loads(line)
        if game["game_id"] in completed:
            processed += 1
            continue
        games_to_run.append(game)
        if len(games_to_run) + processed >= args.max_games:
            break

    # Thread-pool for CPU-bound ffmpeg conversions (shared by all workers)
    mp3_pool = ThreadPoolExecutor(max_workers=4)

    # ── Single-worker fast path ────────────────────────────────────────────
    if num_workers == 1:
        tts = replicas[0]
        gpt_cond, spk_emb = latents[0]

        game_bar = tqdm(games_to_run, desc="Games", unit="game", position=0)
        for game in game_bar:
            if _shutdown.is_set():
                break
            game_id = game["game_id"]
            game_dir = games_dir / game_id
            os.makedirs(str(game_dir), exist_ok=True)

            label = f"{game['white']} vs {game['black']}"
            game_bar.set_postfix_str(label, refresh=True)

            total_chunks = sum(
                len(chunk_text(sanitize_for_tts(convert_chess_notation(seg["text"]))))
                for seg in game["segments"]
                if len(sanitize_for_tts(convert_chess_notation(seg["text"])).strip()) >= 10
            )
            chunk_bar = tqdm(total=total_chunks, desc="  Chunks", unit="chunk",
                             position=1, leave=False)

            try:
                segment_infos = generate_game_audio(
                    tts, game, str(game_dir), gpt_cond, spk_emb,
                    mp3_pool, speed=args.speed, chunk_bar=chunk_bar,
                )
                chunk_bar.close()

                if _shutdown.is_set():
                    break

                write_game_files(game, segment_infos, str(game_dir))
                completed.add(game_id)
                save_progress(str(progress_path), completed)
                total_segments += len(segment_infos)
                processed += 1

            except Exception as e:
                chunk_bar.close()
                tqdm.write(f"  ERROR on {game_id}: {e} — skipping")
                continue

    # ── Multi-worker path ──────────────────────────────────────────────────
    else:
        # Each worker thread owns one TTS replica with its own CUDA stream.
        # A lock serializes GPU inference to avoid device-side assertion errors
        # from concurrent CUDA access.  Parallelism comes from overlapping one
        # worker's GPU inference with another's CPU-bound ffmpeg conversions.
        cuda_lock = threading.Lock()
        cuda_streams = [torch.cuda.Stream(device=device) for _ in range(num_workers)]
        progress_lock = threading.Lock()
        game_bar = tqdm(total=len(games_to_run), desc="Games", unit="game", position=0)

        def _process_game(worker_idx: int, game: dict):
            nonlocal processed, total_segments
            if _shutdown.is_set():
                return
            tts_w = replicas[worker_idx]
            gc, se = latents[worker_idx]
            stream = cuda_streams[worker_idx]
            game_id = game["game_id"]
            gdir = games_dir / game_id
            os.makedirs(str(gdir), exist_ok=True)

            try:
                segment_infos = generate_game_audio(
                    tts_w, game, str(gdir), gc, se, mp3_pool,
                    speed=args.speed, cuda_lock=cuda_lock, cuda_stream=stream,
                )
                if _shutdown.is_set():
                    return
                write_game_files(game, segment_infos, str(gdir))

                with progress_lock:
                    completed.add(game_id)
                    save_progress(str(progress_path), completed)
                    total_segments += len(segment_infos)
                    processed += 1
                    game_bar.set_postfix_str(
                        f"{game['white']} vs {game['black']}", refresh=True
                    )
                    game_bar.update(1)

            except Exception as e:
                with progress_lock:
                    game_bar.update(1)
                tqdm.write(f"  ERROR on {game_id}: {e} — skipping")

        with ThreadPoolExecutor(max_workers=num_workers) as gpu_pool:
            futs = []
            for i, game in enumerate(games_to_run):
                if _shutdown.is_set():
                    break
                worker_idx = i % num_workers
                futs.append(gpu_pool.submit(_process_game, worker_idx, game))
            # Wait for all (poll with timeout so Ctrl+C signals can be delivered)
            for fut in futs:
                while not fut.done():
                    try:
                        fut.result(timeout=1.0)
                    except TimeoutError:
                        continue

        game_bar.close()

    mp3_pool.shutdown(wait=True)

    # Generate master index
    write_index(str(games_dir), str(index_path))

    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"Complete: {processed} games, {total_segments} audio segments")
    print(f"Time: {elapsed/60:.1f} minutes")
    print(f"Output: {output_dir}")


if __name__ == "__main__":
    main()
