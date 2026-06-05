#!/usr/bin/env python3
"""
Phase 1: Data Preparation
- Joins chess_games.jsonl and augmented_chess_games.jsonl on shared key
- Filters narratives for quality: English-only, minimum length, no gibberish
- Parses structured move lists from game text
- Segments narratives into move-aligned chunks
- Stratified sampling across time periods with Russian/Soviet bias
- Outputs merged_games.jsonl for audio generation
"""

import argparse
import gzip
import hashlib
import itertools
import json
import math
import os
import random
import re
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path

# Local imports for spoken-duration estimation (used by the pacing metric and
# the parameter-driven timing algorithm). These mirror the exact text
# transformation applied before TTS synthesis in generate_audio.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from chess_notation_converter import convert_chess_notation
from tts_text_sanitizer import sanitize_for_tts


# ===== Pacing / Timing Configuration =====

# Effective spoken-text rate, calibrated against generated audio durations:
# len(sanitize(convert(text))) / duration_seconds has median ~13.3 char/s
# (p25 12.4, p75 14.1) across the existing 500-game build.
SPOKEN_CHARS_PER_SEC = 13.3

# Pacing metric thresholds (seconds per half-move as experienced by a viewer).
GAP_FAST = 1.0   # gaps below this are too quick to follow
GAP_BORING = 6.0  # interior gaps above this are boring dead air

# Metric penalty weights (composite score = 100 * (1 - clamped penalty)).
# Followability (fast/boring) is the primary objective; alignment is a mild
# regularizer that keeps moves roughly synced to the voice instead of
# collapsing to pure uniform spacing.
W_FAST = 1.0
W_BORING = 1.0
W_MATCH = 0.5
W_ALIGN = 0.3

# Default parameter-driven timing settings. These values are locked in after
# the grid-search evaluation over 10,000 games (see --evaluate mode and
# docs/03-running-the-pipeline.md): blend=0.85, min_gap=1.2, max_gap=6.0
# lifted the pacing score from 59.2 (baseline) to 72.6.
DEFAULT_TIMING_PARAMS = {
    "blend": 0.85,    # 0 = pure text-anchored, 1 = pure uniform spacing
    "min_gap": 1.2,   # floor (seconds) on per-move gap; 0 disables
    "max_gap": 6.0,   # cap (seconds) on per-move gap; 0 disables
}


def _open_jsonl(path, mode="r"):
    """Open a JSONL file, transparently decompressing .gz files."""
    path = str(path)
    if path.endswith(".gz"):
        return gzip.open(path, mode + "t", encoding="utf-8")
    return open(path, mode, encoding="utf-8")


def slugify(key: str) -> str:
    """Convert a game key to a filesystem-safe ID."""
    slug = re.sub(r'[^a-zA-Z0-9]+', '-', key).strip('-').lower()
    slug = slug[:80]
    h = hashlib.md5(key.encode()).hexdigest()[:8]
    return f"{slug}-{h}"


def parse_moves_from_text(text: str) -> list[dict]:
    """
    Extract structured move list from game text.
    Moves appear on lines like: 1.e4 c5 2.Nf3 Nc6 3.d4 cxd4 ...
    Returns: [{"num": 1, "white": "e4", "black": "c5"}, ...]
    """
    # Collect all lines that contain move notation (may start mid-line)
    all_move_text = []
    for line in text.split('\n'):
        line = line.strip()
        # Include any line that contains move numbering (e.g., "23.Rc6")
        if re.search(r'\d+\.', line):
            all_move_text.append(line)

    if not all_move_text:
        return []

    combined = ' '.join(all_move_text)
    # Normalize unicode dashes to ASCII
    combined = combined.replace('\u2011', '-').replace('\u2010', '-')

    # Pattern: move_number.white_move [black_move]
    # Handles: 1.e4 c5  or  55.Nd5  (no black move at end)
    move_pattern = re.compile(
        r'(\d+)\.'
        r'(O-O-O|O-O|[A-Za-z][a-z0-9x]*[a-h]?[1-8]?[=]?[QRBN]?[+#]?)'
        r'(?:\s+'
        r'(O-O-O|O-O|[A-Za-z][a-z0-9x]*[a-h]?[1-8]?[=]?[QRBN]?[+#]?)'
        r')?'
    )

    moves = []
    for m in move_pattern.finditer(combined):
        entry = {"num": int(m.group(1)), "white": m.group(2)}
        if m.group(3):
            entry["black"] = m.group(3)
        moves.append(entry)

    # Drop leading spurious entries (stray numbers parsed before the real
    # move list).  The real sequence starts at move 1 and counts up.
    while moves and moves[0]["num"] != 1 and len(moves) > 1 and moves[1]["num"] == 1:
        moves.pop(0)

    return moves


def count_half_moves(moves: list[dict]) -> int:
    """Count total half-moves (plies) in a move list."""
    count = 0
    for m in moves:
        count += 1  # white move
        if "black" in m:
            count += 1
    return count


def segment_narrative(narrative: str, moves: list[dict]) -> list[dict]:
    """
    Split narrative into move-aligned segments.
    Each segment covers a range of move numbers referenced in its text.
    Segments are forced to be continuous and non-overlapping.
    """
    paragraphs = [p.strip() for p in narrative.split('\n\n') if p.strip()]

    if not paragraphs:
        return []

    total_moves = max(m["num"] for m in moves) if moves else 0

    # Pattern to find move number references like "1.e4" or "23.Nxh7"
    move_ref_pattern = re.compile(r'\b(\d+)\.')

    segments = []
    for i, para in enumerate(paragraphs):
        # Find all move numbers referenced in this paragraph
        refs = [int(x) for x in move_ref_pattern.findall(para)]
        # Filter to plausible move numbers (1..total_moves)
        refs = [r for r in refs if 1 <= r <= total_moves]

        if refs:
            start_move = min(refs)
            end_move = max(refs)
        else:
            # No move references — intro or summary paragraph
            if not segments:
                start_move = 0
                end_move = 0
            else:
                # Attach to previous segment's range
                start_move = segments[-1]["start_move"]
                end_move = segments[-1]["end_move"]

        segments.append({
            "segment_index": i,
            "text": para,
            "start_move": start_move,
            "end_move": end_move,
        })

    # Merge consecutive intro segments (all 0,0) at the start into one
    while len(segments) > 1 and segments[0]["start_move"] == 0 and segments[0]["end_move"] == 0 \
            and segments[1]["start_move"] == 0 and segments[1]["end_move"] == 0:
        segments[0]["text"] += "\n\n" + segments[1]["text"]
        segments.pop(1)

    # If no segment has any move references, distribute moves evenly
    has_any_refs = any(s["start_move"] > 0 or s["end_move"] > 0 for s in segments)
    if not has_any_refs and total_moves > 0:
        # First segment stays as intro (0,0), rest share the move range
        # If only one segment, it covers all moves
        content_segs = segments if len(segments) == 1 else segments[1:]
        moves_per_seg = total_moves / len(content_segs)
        for j, seg in enumerate(content_segs):
            seg["start_move"] = int(j * moves_per_seg) + 1
            seg["end_move"] = int((j + 1) * moves_per_seg)
        # Ensure last content segment reaches game end
        content_segs[-1]["end_move"] = total_moves

    # Post-process: enforce continuous, non-overlapping move ranges
    if len(segments) > 1:
        for i in range(1, len(segments)):
            prev_end = segments[i - 1]["end_move"]
            # Fill gaps: pull start back to continue from previous segment
            if segments[i]["start_move"] > prev_end + 1 and prev_end > 0:
                segments[i]["start_move"] = prev_end + 1
            # Fix overlaps: push start forward past previous segment
            if segments[i]["start_move"] <= prev_end and prev_end > 0:
                segments[i]["start_move"] = prev_end + 1
            # If start overshot end (e.g. summary paragraph), attach to game end
            if segments[i]["start_move"] > segments[i]["end_move"]:
                if prev_end >= total_moves:
                    # Previous segment already covers to game end — summary segment
                    segments[i]["start_move"] = total_moves
                    segments[i]["end_move"] = total_moves
                else:
                    segments[i]["start_move"] = prev_end + 1
                    segments[i]["end_move"] = total_moves

    # Ensure last segment extends to game end
    if segments and total_moves > 0:
        if segments[-1]["end_move"] < total_moves:
            segments[-1]["end_move"] = total_moves

    # Merge trailing summary segments that overlap with the previous segment
    while len(segments) > 1:
        last = segments[-1]
        prev = segments[-2]
        if last["start_move"] <= prev["end_move"] and prev["end_move"] > 0:
            prev["text"] += "\n\n" + last["text"]
            prev["end_move"] = max(prev["end_move"], last["end_move"])
            segments.pop()
        else:
            break

    # Re-index segments after any merges
    for i, seg in enumerate(segments):
        seg["segment_index"] = i

    return segments


def _build_plies(game_moves: list[dict], start_move: int, end_move: int) -> list[dict]:
    """Flatten a move list into half-moves (plies) within [start_move, end_move]."""
    plies = []
    for m in game_moves:
        if m['num'] < start_move or m['num'] > end_move:
            continue
        plies.append({'moveNum': m['num'], 'color': 'w'})
        if 'black' in m:
            plies.append({'moveNum': m['num'], 'color': 'b'})
    return plies


_MOVE_ANCHOR_PAT = re.compile(
    r'(\d+)\.'
    r'(O-O-O|O-O|[A-Za-z][a-z0-9x]*[a-h]?[1-8]?[=]?[QRBN]?[+#]?)'
    r'(?:\s+'
    r'(O-O-O|O-O|[A-Za-z][a-z0-9x]*[a-h]?[1-8]?[=]?[QRBN]?[+#]?)'
    r')?'
)


def estimate_spoken_duration(text: str, chars_per_sec: float = SPOKEN_CHARS_PER_SEC) -> float:
    """
    Estimate how long the TTS engine will take to speak a segment, in seconds.

    Applies the same notation-conversion + sanitization used before synthesis,
    then divides the resulting character count by a calibrated speaking rate.
    """
    if not text:
        return 0.0
    spoken = sanitize_for_tts(convert_chess_notation(text))
    return len(spoken) / chars_per_sec if chars_per_sec > 0 else 0.0


def anchored_fractions(segment_text: str, game_moves: list[dict],
                       start_move: int, end_move: int) -> tuple[list[float], int]:
    """
    Compute text-anchored fractional delays for each ply in a segment.

    Returns ``(fractions, n_anchored)`` where ``fractions`` sum to ~1.0 and
    ``n_anchored`` is the number of plies that were located directly in the
    narrative text (the rest are interpolated). This is the param-independent
    core of the timing algorithm — callers shape it via :func:`apply_timing_params`.
    """
    plies = _build_plies(game_moves, start_move, end_move)
    n = len(plies)
    if n == 0:
        return [], 0
    if n == 1:
        return [1.0], 0

    text_len = len(segment_text)
    if text_len == 0:
        return _uniform_delays(n), 0

    # Normalize unicode dashes for matching
    normalized = segment_text.replace('\u2011', '-').replace('\u2010', '-')

    # Collect anchors: (move_num, color, char_position)
    text_anchors = []
    for match in _MOVE_ANCHOR_PAT.finditer(normalized):
        move_num = int(match.group(1))
        if move_num < start_move or move_num > end_move:
            continue
        text_anchors.append((move_num, 'w', match.start()))
        if match.group(3):
            text_anchors.append((move_num, 'b', match.start(3)))

    # Match anchors to plies in order (greedy, preserving sequence)
    timestamps = [None] * n
    next_ply = 0
    for ta_num, ta_color, ta_pos in text_anchors:
        for i in range(next_ply, n):
            if plies[i]['moveNum'] == ta_num and plies[i]['color'] == ta_color:
                timestamps[i] = ta_pos / text_len
                next_ply = i + 1
                break

    n_anchored = sum(1 for t in timestamps if t is not None)

    # If no anchors found, return uniform spacing
    if n_anchored == 0:
        return _uniform_delays(n), 0

    # Interpolate missing timestamps
    first_anchor = next(i for i in range(n) if timestamps[i] is not None)
    last_anchor = next(i for i in range(n - 1, -1, -1) if timestamps[i] is not None)

    # Before first anchor: distribute evenly from 0 to first anchor time
    if first_anchor > 0:
        t0 = timestamps[first_anchor]
        for i in range(first_anchor):
            timestamps[i] = t0 * i / first_anchor

    # After last anchor: distribute evenly from last anchor time to 1.0
    if last_anchor < n - 1:
        t_last = timestamps[last_anchor]
        gap = n - 1 - last_anchor
        for i in range(last_anchor + 1, n):
            timestamps[i] = t_last + (1.0 - t_last) * (i - last_anchor) / gap

    # Between anchors: linear interpolation
    prev = None
    for i in range(n):
        if timestamps[i] is not None:
            if prev is not None and i - prev > 1:
                for j in range(prev + 1, i):
                    frac = (j - prev) / (i - prev)
                    timestamps[j] = timestamps[prev] + frac * (timestamps[i] - timestamps[prev])
            prev = i

    # Enforce monotonically increasing
    for i in range(1, n):
        if timestamps[i] <= timestamps[i - 1]:
            timestamps[i] = timestamps[i - 1] + 0.001

    # Convert to delays (differences between consecutive timestamps)
    delays = [timestamps[0]]
    for i in range(1, n):
        delays.append(timestamps[i] - timestamps[i - 1])

    delays = [max(d, 0.0) for d in delays]
    total = sum(delays)
    if total <= 0:
        return _uniform_delays(n), n_anchored
    delays = [d / total for d in delays]
    return delays, n_anchored


def apply_timing_params(raw_frac: list[float], est_dur: float,
                        blend: float, min_gap: float, max_gap: float) -> list[float]:
    """
    Shape raw text-anchored fractions into viewer-friendly fractions.

    1. Blend the anchored fractions toward uniform spacing (``blend`` 0..1).
    2. Convert to seconds via the segment's estimated spoken duration.
    3. Clamp each per-move gap to ``[min_gap, max_gap]`` seconds (0 = disabled).
    4. Renormalize back to fractions summing to 1.0.

    Returns a list of fractional delays (sum ~1.0). The web app multiplies
    each value by the real segment audio duration to schedule the move.
    """
    n = len(raw_frac)
    if n == 0:
        return []
    if n == 1:
        return [1.0]

    uniform = 1.0 / n
    blended = [(1.0 - blend) * r + blend * uniform for r in raw_frac]

    # Without a usable duration estimate the absolute clamps are meaningless;
    # fall back to the blended fractions directly.
    if est_dur > 0 and (min_gap > 0 or max_gap > 0):
        gaps = [f * est_dur for f in blended]
        if min_gap > 0:
            gaps = [max(g, min_gap) for g in gaps]
        if max_gap > 0:
            gaps = [min(g, max_gap) for g in gaps]
        total = sum(gaps)
        if total <= 0:
            return _uniform_delays(n)
        frac = [g / total for g in gaps]
    else:
        total = sum(blended)
        if total <= 0:
            return _uniform_delays(n)
        frac = [b / total for b in blended]

    frac = [round(f, 4) for f in frac]
    frac[-1] = round(max(0.0, frac[-1] + (1.0 - sum(frac))), 4)
    return frac


def estimate_move_timings(segment_text: str, game_moves: list[dict],
                          start_move: int, end_move: int,
                          params: dict = None) -> list[float]:
    """
    Estimate per-ply fractional delays for a segment using the
    parameter-driven pacing algorithm.

    Moves referenced in the narrative are anchored to their text position;
    the result is then blended toward uniform spacing and clamped to a
    viewer-friendly gap range (see :func:`apply_timing_params`).
    """
    if params is None:
        params = DEFAULT_TIMING_PARAMS
    raw_frac, _ = anchored_fractions(segment_text, game_moves, start_move, end_move)
    if not raw_frac:
        return []
    est_dur = estimate_spoken_duration(segment_text)
    return apply_timing_params(
        raw_frac, est_dur,
        blend=params["blend"],
        min_gap=params["min_gap"],
        max_gap=params["max_gap"],
    )


def _uniform_delays(n: int) -> list[float]:
    """Return n equal fractional delays summing to 1.0."""
    d = round(1.0 / n, 4)
    result = [d] * n
    result[-1] = round(result[-1] + (1.0 - sum(result)), 4)
    return result


# ===== Pacing Quality Metric =====
#
# The metric scores how watchable a game's move pacing is for a viewer, on a
# 0-100 scale (higher = better). It reconstructs the real per-move gaps a
# viewer would experience — gap_seconds = move_timings[i] * spoken_duration —
# and penalizes four failure modes:
#
#   * fast    — sub-1s gaps that are too quick to follow (rapid move bursts)
#   * boring  — interior gaps with long dead air (first/last move exempt)
#   * match   — moves that could not be located in the narrative text and so
#               cannot be correlated in time with the voiceover
#   * align   — drift of the final timing away from where moves are actually
#               mentioned in the text (keeps moves roughly synced to the voice)
#
# score = 100 * (1 - clamp(W_FAST*fast + W_BORING*boring
#                          + W_MATCH*match + W_ALIGN*align, 0, 1))


def precompute_pacing_features(game: dict) -> list[dict]:
    """
    Precompute param-independent pacing features for a game's content segments.

    Returns one dict per content segment holding the text-anchored fractions,
    estimated spoken duration, anchored-ply count and ply count. This isolates
    the expensive work (notation conversion, regex anchoring) so the grid
    search can re-score thousands of parameter combinations cheaply.
    """
    moves = game.get("moves", [])
    feats = []
    for seg in game.get("segments", []):
        if seg.get("end_move", 0) <= 0:
            continue
        start = max(seg.get("start_move", 1), 1)
        end = seg["end_move"]
        raw, n_anchored = anchored_fractions(seg.get("text", ""), moves, start, end)
        if not raw:
            continue
        est = estimate_spoken_duration(seg.get("text", ""))
        feats.append({
            "raw": raw,
            "est_dur": max(est, 1.0),
            "n_anchored": n_anchored,
            "n": len(raw),
        })
    return feats


def score_game_pacing(feats: list[dict], params: dict) -> dict | None:
    """
    Score one game's pacing for a given parameter set (used by --evaluate).

    Applies the timing parameters to each segment's anchored fractions, then
    scores the resulting per-move gaps. Returns a dict of sub-metrics plus a
    composite ``score`` (0-100), or None if the game has no timed plies.
    """
    seg_frac = []
    for f in feats:
        frac = apply_timing_params(
            f["raw"], f["est_dur"],
            blend=params["blend"], min_gap=params["min_gap"], max_gap=params["max_gap"],
        )
        seg_frac.append((frac, f["raw"], f["est_dur"], f["n_anchored"]))
    return _score_segments(seg_frac)


def score_stored_pacing(game: dict) -> dict | None:
    """
    Score a game's pacing from the timings already stored in the merged output
    (used by --verify). Reflects exactly what the web app will play, without
    recomputing the algorithm.
    """
    moves = game.get("moves", [])
    seg_frac = []
    for seg in game.get("segments", []):
        if seg.get("end_move", 0) <= 0:
            continue
        frac = seg.get("move_timings") or []
        if not frac:
            continue
        start = max(seg.get("start_move", 1), 1)
        raw, n_anchored = anchored_fractions(seg.get("text", ""), moves, start, seg["end_move"])
        if len(raw) != len(frac):
            # Stored timings out of sync with parsed plies; fall back to raw.
            raw = raw if raw else frac
        est = max(estimate_spoken_duration(seg.get("text", "")), 1.0)
        seg_frac.append((frac, raw, est, n_anchored))
    return _score_segments(seg_frac)


def _score_segments(seg_frac: list[tuple]) -> dict | None:
    """
    Core pacing scorer. ``seg_frac`` is a list of
    ``(fractions, raw_anchored_fractions, est_duration, n_anchored)`` tuples,
    one per content segment. Returns the sub-metrics + composite score.
    """
    gaps = []  # per-ply gap in seconds, in move order across the whole game
    total_plies = 0
    total_anchored = 0
    align_err_sum = 0.0  # ply-weighted alignment error vs text anchors
    for frac, raw, est, n_anchored in seg_frac:
        gaps.extend(fr * est for fr in frac)
        total_plies += len(frac)
        total_anchored += n_anchored
        if len(raw) == len(frac):
            align_err_sum += _alignment_error(raw, frac) * len(frac)

    n = len(gaps)
    if n == 0:
        return None

    # Exclude the game's opening lead-in (first gap) from the fast penalty,
    # and both the opening and the final lingering gap from the boring penalty.
    fast_src = gaps[1:] if n > 1 else gaps
    interior = gaps[1:-1] if n > 2 else []

    fast_pen = (sum(max(0.0, (GAP_FAST - g) / GAP_FAST) for g in fast_src)
                / len(fast_src)) if fast_src else 0.0
    boring_pen = (sum(min(1.0, max(0.0, (g - GAP_BORING) / GAP_BORING)) for g in interior)
                  / len(interior)) if interior else 0.0
    match_rate = total_anchored / total_plies if total_plies else 0.0
    match_pen = 1.0 - match_rate
    align_pen = align_err_sum / total_plies if total_plies else 0.0

    penalty = min(1.0, max(0.0, W_FAST * fast_pen + W_BORING * boring_pen
                           + W_MATCH * match_pen + W_ALIGN * align_pen))
    score = 100.0 * (1.0 - penalty)

    pct_fast = (100.0 * sum(1 for g in fast_src if g < GAP_FAST) / len(fast_src)) if fast_src else 0.0
    pct_boring = (100.0 * sum(1 for g in interior if g > GAP_BORING) / len(interior)) if interior else 0.0
    median_gap = sorted(gaps)[n // 2]

    return {
        "score": score,
        "fast_pen": fast_pen,
        "boring_pen": boring_pen,
        "match_pen": match_pen,
        "align_pen": align_pen,
        "pct_fast": pct_fast,
        "pct_boring": pct_boring,
        "median_gap": median_gap,
        "match_rate": match_rate,
        "n_plies": n,
    }


def _alignment_error(raw: list[float], frac: list[float]) -> float:
    """
    Mean absolute area between the cumulative timing curves of the raw
    text-anchored fractions and the final fractions (0 = perfectly aligned
    to the voice, →1 = maximally drifted). Both inputs sum to ~1.0.
    """
    n = len(raw)
    if n <= 1:
        return 0.0
    cr = cf = acc = 0.0
    for i in range(n):
        cr += raw[i]
        cf += frac[i]
        acc += abs(cr - cf)
    return acc / n


def aggregate_pacing(per_game: list[dict]) -> dict:
    """Average the per-game pacing sub-metrics into a dataset-level summary."""
    if not per_game:
        return {}
    keys = ["score", "fast_pen", "boring_pen", "match_pen", "align_pen",
            "pct_fast", "pct_boring", "median_gap", "match_rate"]
    agg = {k: sum(g[k] for g in per_game) / len(per_game) for k in keys}
    agg["games"] = len(per_game)
    agg["total_plies"] = sum(g["n_plies"] for g in per_game)
    return agg


def print_pacing_summary(agg: dict, label: str = "Pacing metric"):
    """Pretty-print an aggregated pacing summary block."""
    if not agg:
        print("  (no games scored)")
        return
    print(f"\n=== {label} ({agg['games']} games, {agg['total_plies']} plies) ===")
    print(f"  Score (0-100, higher=better) : {agg['score']:.2f}")
    print(f"  Fast penalty   (<{GAP_FAST:.0f}s)      : {agg['fast_pen']:.4f}  "
          f"({agg['pct_fast']:.1f}% of moves)")
    print(f"  Boring penalty (>{GAP_BORING:.0f}s)      : {agg['boring_pen']:.4f}  "
          f"({agg['pct_boring']:.1f}% of interior moves)")
    print(f"  Match penalty  (unlocated)   : {agg['match_pen']:.4f}  "
          f"(match rate {agg['match_rate']*100:.1f}%)")
    print(f"  Align penalty  (voice drift) : {agg['align_pen']:.4f}")
    print(f"  Median move gap              : {agg['median_gap']:.2f}s")


# ===== Grid-Search Evaluation Mode =====
#
# Search space for the parameter-driven timing algorithm. Each combination is
# scored against the pacing metric on a fixed sample of games. Features are
# precomputed once, so all combinations re-score from cached anchors/durations.
EVAL_GRID = {
    "blend": [0.0, 0.3, 0.5, 0.6, 0.7, 0.85, 1.0],
    "min_gap": [0.0, 0.6, 0.9, 1.2],
    "max_gap": [0.0, 6.0, 7.0, 8.0, 10.0],
}


def _iter_param_grid(grid: dict):
    """Yield param dicts for the cartesian product of a grid spec."""
    keys = list(grid.keys())
    for combo in itertools.product(*(grid[k] for k in keys)):
        yield dict(zip(keys, combo))


def evaluate_gridsearch(output_path: str, max_games: int, grid: dict = None):
    """
    Grid-search the timing parameters against the pacing metric.

    Loads at most ``max_games`` games from the merged output once, precomputes
    param-independent pacing features, then scores every parameter combination
    and reports the ranking and the optimal set.
    """
    if not os.path.exists(output_path):
        print(f"Error: {output_path} not found. Run prepare_data.py first.")
        sys.exit(1)
    if grid is None:
        grid = EVAL_GRID

    # Phase 1: load games and precompute features (the expensive step, done once)
    t0 = time.perf_counter()
    print(f"Loading up to {max_games} games and precomputing pacing features...")
    all_feats = []
    loaded = 0
    with open(output_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            game = json.loads(line)
            feats = precompute_pacing_features(game)
            if feats:
                all_feats.append(feats)
            loaded += 1
            if loaded % 1000 == 0:
                print(f"  ...loaded {loaded} games")
            if loaded >= max_games:
                break
    t_load = time.perf_counter() - t0
    print(f"  Loaded {loaded} games ({len(all_feats)} with timed plies) in {t_load:.1f}s")

    combos = list(_iter_param_grid(grid))
    print(f"\nScoring {len(combos)} parameter combinations...")

    # Phase 2: score every combination
    t1 = time.perf_counter()
    results = []
    for params in combos:
        per_game = []
        for feats in all_feats:
            res = score_game_pacing(feats, params)
            if res is not None:
                per_game.append(res)
        agg = aggregate_pacing(per_game)
        results.append((params, agg))
    t_score = time.perf_counter() - t1

    # Rank by composite score (desc)
    results.sort(key=lambda r: r[1].get("score", 0.0), reverse=True)

    print(f"\n  {'blend':>6} {'min':>5} {'max':>5} {'score':>7} "
          f"{'fast%':>6} {'boring%':>8} {'align':>6} {'medGap':>7}")
    print(f"  {'-'*6} {'-'*5} {'-'*5} {'-'*7} {'-'*6} {'-'*8} {'-'*6} {'-'*7}")
    for params, agg in results:
        print(f"  {params['blend']:>6.2f} {params['min_gap']:>5.1f} {params['max_gap']:>5.1f} "
              f"{agg['score']:>7.2f} {agg['pct_fast']:>6.1f} {agg['pct_boring']:>8.1f} "
              f"{agg['align_pen']:>6.3f} {agg['median_gap']:>7.2f}")

    best_params, best_agg = results[0]
    print(f"\nScored {len(combos)} combinations over {best_agg['games']} games in {t_score:.1f}s")
    print(f"\n*** Optimal parameters ***")
    print(f"  blend   = {best_params['blend']}")
    print(f"  min_gap = {best_params['min_gap']}")
    print(f"  max_gap = {best_params['max_gap']}")
    print_pacing_summary(best_agg, label="Optimal pacing")

    # Show the baseline (pure text-anchored, no clamps) for comparison
    baseline = {"blend": 0.0, "min_gap": 0.0, "max_gap": 0.0}
    base_per_game = [r for r in (score_game_pacing(fe, baseline) for fe in all_feats) if r]
    base_agg = aggregate_pacing(base_per_game)
    print_pacing_summary(base_agg, label="Baseline (blend=0, no clamps)")

    return best_params, best_agg


# ===== Data Quality Filters =====

MIN_TEXT_LENGTH = 500  # characters
MAX_WORD_LENGTH = 50   # words longer than this are likely gibberish
MAX_REPEAT_RATIO = 0.4 # if >40% of words are repeats of the same word, reject

# Common English words — if none are present, the text is likely non-English
_ENGLISH_MARKERS = re.compile(
    r'\b(the|and|of|is|was|with|that|for|this|after|from|move|position|white|black|king|queen|pawn|bishop|knight|rook|game|opening|endgame)\b',
    re.IGNORECASE,
)


def strip_markdown(text: str) -> str:
    """Remove markdown formatting from narrative text.

    Strips bold/italic markers, heading prefixes, horizontal rules,
    and markdown links, keeping the plain-text content.
    """
    # Bold/italic: **text**, __text__, *text*, _text_
    text = re.sub(r'\*{2,3}(.+?)\*{2,3}', r'\1', text)
    text = re.sub(r'_{2,3}(.+?)_{2,3}', r'\1', text)
    # Single markers only between word-boundary contexts to avoid
    # touching move notation like 1.e4*
    text = re.sub(r'(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)', r'\1', text)
    text = re.sub(r'(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)', r'\1', text)
    # Heading markers at line starts
    text = re.sub(r'(?m)^#{1,6}\s+', '', text)
    # Horizontal rules (---, ***, ___)
    text = re.sub(r'(?m)^[-*_]{3,}\s*$', '', text)
    # Markdown links [text](url) → text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Collapse any resulting double-blank-lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def passes_quality_filter(text: str) -> bool:
    """
    Return True if the narrative text passes all quality checks:
    - At least MIN_TEXT_LENGTH characters
    - Detected as English (contains common English words)
    - No nonsensical long "words" (>MAX_WORD_LENGTH chars)
    - No excessive word repetition
    """
    if not text or len(text) < MIN_TEXT_LENGTH:
        return False

    # English detection: require at least 3 distinct English marker words
    markers_found = set(m.group().lower() for m in _ENGLISH_MARKERS.finditer(text))
    if len(markers_found) < 3:
        return False

    words = text.split()
    if not words:
        return False

    # Reject texts containing nonsensically long "words" (>50 chars)
    long_words = [w for w in words if len(w) > MAX_WORD_LENGTH]
    if len(long_words) > 2:  # allow a couple (e.g. long URLs or move sequences)
        return False

    # Reject any single absurdly long token (>200 chars) — repetitive glitch output
    if any(len(w) > 200 for w in words):
        return False

    # Reject excessive repetition: if any single word accounts for >40% of all words
    from collections import Counter
    word_counts = Counter(w.lower() for w in words)
    most_common_count = word_counts.most_common(1)[0][1]
    if most_common_count / len(words) > MAX_REPEAT_RATIO:
        return False

    return True


def build_augmented_index(aug_path: str, candidate_keys: set = None) -> dict:
    """
    Stream augmented file and build index: key -> best narrative entry.
    Applies quality filters: English-only, minimum length, no gibberish.
    Prefers prompt_variant 1 (Russian style), then 0, 3, 4.
    If candidate_keys is provided, only index those keys and stop early
    once all have been found.
    """
    VARIANT_PRIORITY = {1: 0, 0: 1, 3: 2, 4: 3}  # lower = better
    index = {}
    count = 0
    rejected = {"short": 0, "non_english": 0, "gibberish": 0}

    print(f"Indexing augmented narratives from {aug_path}...")
    with _open_jsonl(aug_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            key = obj.get("key")
            if not key:
                continue

            if candidate_keys is not None and key not in candidate_keys:
                count += 1
                if count % 50000 == 0:
                    print(f"  ...processed {count} augmented lines, {len(index)} unique keys")
                continue

            text = obj.get("text", "")

            # Quality filter
            if not passes_quality_filter(text):
                if len(text) < MIN_TEXT_LENGTH:
                    rejected["short"] += 1
                else:
                    # Distinguish non-English from gibberish for reporting
                    markers = set(m.group().lower() for m in _ENGLISH_MARKERS.finditer(text))
                    if len(markers) < 3:
                        rejected["non_english"] += 1
                    else:
                        rejected["gibberish"] += 1
                count += 1
                if count % 50000 == 0:
                    print(f"  ...processed {count} augmented lines, {len(index)} unique keys")
                continue

            variant = obj.get("prompt_variant", 99)
            priority = VARIANT_PRIORITY.get(variant, 99)

            if key not in index or priority < index[key]["_priority"]:
                index[key] = {
                    "text": text,
                    "prompt_variant": variant,
                    "_priority": priority,
                }

            count += 1
            if count % 50000 == 0:
                print(f"  ...processed {count} augmented lines, {len(index)} unique keys")

            if candidate_keys is not None and len(index) >= len(candidate_keys):
                print(f"  Early exit: found all {len(candidate_keys)} candidate keys")
                break

    print(f"  Done: {count} lines processed, {len(index)} unique keys accepted")
    print(f"  Rejected: {rejected['short']} too short, "
          f"{rejected['non_english']} non-English, "
          f"{rejected['gibberish']} gibberish/repetitive")
    return index


def process_games(games_path: str, aug_index: dict, output_path: str, max_games: int):
    """
    Stream chess_games.jsonl, join with augmented index, parse moves,
    segment narratives, and write merged output.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    written = 0
    skipped_no_aug = 0
    skipped_no_moves = 0
    total = 0

    print(f"\nProcessing games from {games_path}...")
    with _open_jsonl(games_path) as fin, \
         open(output_path, 'w', encoding='utf-8') as fout:

        for line in fin:
            line = line.strip()
            if not line:
                continue

            total += 1
            try:
                game = json.loads(line)
            except json.JSONDecodeError:
                continue

            key = game.get("key")
            if not key or key not in aug_index:
                skipped_no_aug += 1
                continue

            # Parse moves
            moves = parse_moves_from_text(game.get("text", ""))
            if not moves:
                skipped_no_moves += 1
                continue

            # Get narrative, strip any markdown formatting, and segment it
            aug = aug_index[key]
            narrative = strip_markdown(aug["text"])
            segments = segment_narrative(narrative, moves)

            if not segments:
                skipped_no_moves += 1
                continue

            # Estimate move timings for each segment
            for seg in segments:
                if seg["end_move"] > 0:
                    start = max(seg["start_move"], 1)
                    seg["move_timings"] = estimate_move_timings(
                        seg["text"], moves, start, seg["end_move"]
                    )
                else:
                    seg["move_timings"] = []

            game_id = slugify(key)

            merged = {
                "key": key,
                "game_id": game_id,
                "white": game.get("white", ""),
                "black": game.get("black", ""),
                "date": game.get("date", ""),
                "event": game.get("event", ""),
                "eco": game.get("eco", ""),
                "opening": game.get("opening", ""),
                "result": game.get("result", ""),
                "prompt_variant": aug["prompt_variant"],
                "moves": moves,
                "total_half_moves": count_half_moves(moves),
                "segments": segments,
            }

            fout.write(json.dumps(merged, ensure_ascii=False) + '\n')
            written += 1

            if written % 1000 == 0:
                print(f"  ...written {written} merged games")

            if max_games and written >= max_games:
                print(f"  Reached --max-games limit ({max_games})")
                break

    print(f"\nSummary:")
    print(f"  Total games scanned: {total}")
    print(f"  Skipped (no augmented narrative): {skipped_no_aug}")
    print(f"  Skipped (no parseable moves): {skipped_no_moves}")
    print(f"  Written to merged output: {written}")
    print(f"  Output: {output_path}")


def verify_output(output_path: str, count: int, pacing_sample: int = 5000):
    """Spot-check the merged output file for data quality issues and report
    the pacing-quality metric over the first ``pacing_sample`` games
    (0 = all games)."""
    if not os.path.exists(output_path):
        print(f"Error: {output_path} not found. Run prepare_data.py first.")
        sys.exit(1)

    total = 0
    issues = 0
    pacing_scores = []  # per-game metric dicts (limited to pacing_sample)

    with open(output_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total += 1
            game = json.loads(line)

            # Accumulate pacing metric over the sample window (scores the
            # timings actually stored in the merged output)
            if pacing_sample == 0 or len(pacing_scores) < pacing_sample:
                res = score_stored_pacing(game)
                if res is not None:
                    pacing_scores.append(res)

            if total <= count:
                print(f"{game['white']} vs {game['black']}")
                print(f"  Moves: {len(game['moves'])}  Half-moves: {game['total_half_moves']}")
                print(f"  Segments: {len(game['segments'])}")
                for s in game['segments']:
                    timings = s.get('move_timings', [])
                    t_info = f"  timings: {len(timings)} plies, sum={sum(timings):.4f}" if timings else "  timings: none (intro)"
                    print(f"    seg {s['segment_index']}: moves {s['start_move']}-{s['end_move']}{t_info}")
                print()

            # Validate move contiguity
            move_nums = [m['num'] for m in game['moves']]
            expected = list(range(1, len(move_nums) + 1))
            if move_nums != expected:
                if total <= count:
                    print(f"  WARNING: non-contiguous moves")
                issues += 1

            # Validate segments exist
            if not game['segments']:
                if total <= count:
                    print(f"  WARNING: no segments")
                issues += 1

            # Validate segment continuity (non-overlapping, increasing)
            segs = game['segments']
            for j in range(1, len(segs)):
                prev = segs[j - 1]
                curr = segs[j]
                if prev["end_move"] > 0 and curr["start_move"] <= prev["end_move"]:
                    if total <= count:
                        print(f"  WARNING: seg {curr['segment_index']} overlaps with previous "
                              f"({curr['start_move']}-{curr['end_move']} vs {prev['start_move']}-{prev['end_move']})")
                    issues += 1
                    break
                if prev["end_move"] > 0 and curr["start_move"] > prev["end_move"] + 1:
                    if total <= count:
                        print(f"  WARNING: gap between seg {prev['segment_index']} and {curr['segment_index']} "
                              f"(moves {prev['end_move']+1}-{curr['start_move']-1} uncovered)")
                    issues += 1
                    break

            # Validate segment coverage
            if game['segments'] and game['moves']:
                max_move = max(m['num'] for m in game['moves'])
                seg_end = max(s['end_move'] for s in game['segments'])
                if seg_end < max_move:
                    if total <= count:
                        print(f"  WARNING: segments only cover up to move {seg_end}, game has {max_move}")
                    issues += 1

            # Validate move timings
            for s in game['segments']:
                timings = s.get('move_timings', [])
                if s['start_move'] == 0 and s['end_move'] == 0:
                    if timings:
                        if total <= count:
                            print(f"  WARNING: seg {s['segment_index']} is intro but has timings")
                        issues += 1
                    continue

                # Count expected plies for this segment
                expected_plies = 0
                seg_start = max(s['start_move'], 1)
                for m in game['moves']:
                    if m['num'] < seg_start or m['num'] > s['end_move']:
                        continue
                    expected_plies += 1  # white
                    if 'black' in m:
                        expected_plies += 1

                if not timings:
                    if total <= count:
                        print(f"  WARNING: seg {s['segment_index']} has no move_timings")
                    issues += 1
                elif len(timings) != expected_plies:
                    if total <= count:
                        print(f"  WARNING: seg {s['segment_index']} has {len(timings)} timings "
                              f"but {expected_plies} plies")
                    issues += 1
                else:
                    t_sum = sum(timings)
                    if abs(t_sum - 1.0) > 0.01:
                        if total <= count:
                            print(f"  WARNING: seg {s['segment_index']} timings sum to {t_sum:.4f}, expected 1.0")
                        issues += 1
                    if any(t < 0 for t in timings):
                        if total <= count:
                            print(f"  WARNING: seg {s['segment_index']} has negative timing values")
                        issues += 1

    print(f"Verified {total} games, {issues} issue(s) found.")

    # Report the pacing-quality metric over the sampled games
    sample_label = "all games" if pacing_sample == 0 else f"first {len(pacing_scores)} games"
    agg = aggregate_pacing(pacing_scores)
    print_pacing_summary(agg, label=f"Pacing metric - {sample_label}")


# ===== Russian/Soviet Detection =====

# Well-known Russian/Soviet players whose surnames don't match suffix patterns.
# Last names only (case-insensitive match against the last-name part before comma).
_KNOWN_RUSSIAN_SOVIET_LASTNAMES = {
    "alekhine", "alekhin", "botvinnik", "tal", "keres", "korchnoi",
    "kortschnoj", "korchnoj", "geller", "averbakh", "tolush", "flohr",
    "lilienthal", "bogoljubow", "bogoljubov", "bogoljuboff", "furman",
    "gufeld", "bannik", "nimzowitsch", "nimzovich", "nimzowich",
    "chigorin", "tchigorin", "stein", "levenfish", "bondarevsky",
    "ragozin", "boleslavsky", "boleslavski", "simagin", "nezhmetdinov",
    "cherepkov", "chistiakov", "goldenov", "ilivitzki", "kasparian",
    "kholmov", "lutikov", "estrin", "suetin", "vasiukov", "sakharov",
    "polugaevsky", "polugayevsky", "gulko", "yusupov", "yudasin",
    "sveshnikov", "nikitin", "reshevsky", "lasker", "bogatyrchuk",
    "alatortsev", "lisitsin", "lisitsyn", "ilyin", "ilivitsky",
    "smyslov", "spassky", "gipslis", "mikenas", "nei", "zurakhov",
    "ratmir", "lein", "liberzon", "tseshkovsky", "tseitlin",
    "dorfman", "lputian", "vaganian", "beliavsky", "belyavsky",
    "kasparov", "kramnik", "shirov", "bronstein",
}

# Russian/Soviet event keywords (case-insensitive substring match).
_RUSSIAN_EVENT_KEYWORDS = [
    "urs", "ussr", "moscow", "moskou", "leningrad", "kiev", "tbilisi",
    "baku", "riga", "tallinn", "vilnius", "minsk", "tashkent",
    "sverdlovsk", "novosibirsk", "odessa", "kharkov", "kislovodsk",
    "sochi", "rostov", "stalingrad", "volgograd", "gorky", "yerevan",
    "alma-ata", "alma ata", "chigorin", "russian", "soviet",
    "ch-urs", "ch urs",
]


def _extract_lastname(name: str) -> str:
    """Extract lowercase last name from 'LastName, FirstName' format."""
    parts = name.split(",")
    return parts[0].strip().lower() if parts else ""


def _lastname_looks_russian(lastname: str) -> bool:
    """Heuristic: does this last name look Russian/Soviet by suffix?"""
    if not lastname or lastname == "?":
        return False
    # Known players
    if lastname in _KNOWN_RUSSIAN_SOVIET_LASTNAMES:
        return True
    # Strong Russian surname endings
    if re.search(r'(ov|ev|kov|nov|rov|lov|sov|zov|dov|tov)$', lastname):
        return True
    if re.search(r'(off|eff)$', lastname):
        return True
    if re.search(r'(sk[yi]y?|skij|skii)$', lastname):
        return True
    if re.search(r'(enko|chenko|yenko)$', lastname):
        return True
    if re.search(r'(ovich|evich|ovna|evna)$', lastname):
        return True
    if re.search(r'(ova|eva)$', lastname):
        return True
    # Armenian/Georgian (Soviet)
    if re.search(r'(ian|yan)$', lastname) and len(lastname) >= 6:
        return True
    if re.search(r'(dze|shvili|adze)$', lastname):
        return True
    # Russian -in/-yn endings (only for longer names to avoid English false positives)
    if re.search(r'(nin|lin|min|kin|din|tin|shin|chin|zin|rin|gin|vin|pin|sin)$', lastname) and len(lastname) >= 5:
        return True
    return False


def _event_looks_russian(event: str) -> bool:
    """Check if event name suggests a Russian/Soviet location or tournament."""
    if not event or event == "?":
        return False
    ev_lower = event.lower()
    return any(kw in ev_lower for kw in _RUSSIAN_EVENT_KEYWORDS)


def score_russian_relevance(white: str, black: str, event: str) -> int:
    """
    Score a game's Russian/Soviet relevance (0-3).
      0 = no Russian connection
      1 = one Russian player OR Russian event
      2 = one Russian player AND Russian event, OR two Russian players
      3 = two Russian players AND Russian event
    """
    w_russian = _lastname_looks_russian(_extract_lastname(white))
    b_russian = _lastname_looks_russian(_extract_lastname(black))
    e_russian = _event_looks_russian(event)
    player_score = int(w_russian) + int(b_russian)
    return player_score + int(e_russian)


def _extract_year(date_str: str) -> int | None:
    """Extract year from date string like '1950.??.??'. Returns None if invalid."""
    if not date_str:
        return None
    parts = date_str.split(".")
    if parts and parts[0].isdigit():
        y = int(parts[0])
        if 1800 <= y <= 2100:
            return y
    return None


def scan_augmented_keys(aug_path: str) -> set[str]:
    """
    Quick pre-scan of the augmented file to collect all available keys.
    This is a fast key-only extraction — no quality filtering.
    """
    keys = set()
    count = 0
    print(f"Pre-scanning augmented file for available keys...")
    with _open_jsonl(aug_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            count += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = obj.get("key")
            if key:
                keys.add(key)
            if count % 50000 == 0:
                print(f"  ...processed {count} lines, {len(keys)} unique keys")
    print(f"  Done: {count} lines, {len(keys)} unique keys available")
    return keys


def stratified_sample(games_path: str, max_games: int, available_keys: set[str],
                      russian_bias: float = 3.0, seed: int = 42) -> set[str]:
    """
    Pre-scan all games and return a set of keys chosen via stratified
    time-based sampling with Russian/Soviet bias.

    Only games whose key exists in available_keys (from the augmented
    file) are considered.  Selects ~10% more than max_games to
    compensate for quality-filter attrition downstream.

    Strategy:
      1. Scan all games, extract (key, year, russian_score).
      2. Divide into year-based strata (buckets).
      3. Assign each game a sampling weight: base=1, multiplied by
         russian_bias for each point of russian_score.
      4. Allocate slots to each stratum proportional to stratum size,
         then weighted-sample within each stratum.

    Args:
        games_path: Path to chess_games.jsonl
        max_games: Target number of games to select
        available_keys: Set of keys present in the augmented file
        russian_bias: Weight multiplier per Russian relevance point
        seed: Random seed for reproducibility

    Returns:
        Set of selected game keys
    """
    rng = random.Random(seed)

    # Over-select to compensate for quality-filter attrition
    oversample_target = math.ceil(max_games * 1.15)

    # Phase 1: Pre-scan all games (only those with augmented data)
    print(f"Pre-scanning all games for stratified sampling "
          f"(target: {max_games}, oversampling to {oversample_target})...")
    all_games = []  # list of (key, year, russian_score)
    total = 0
    skipped_no_aug = 0

    with _open_jsonl(games_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                game = json.loads(line)
            except json.JSONDecodeError:
                continue

            key = game.get("key")
            if not key:
                continue

            # Only consider games that exist in the augmented file
            if key not in available_keys:
                skipped_no_aug += 1
                continue

            # Quick check: must have move notation
            text = game.get("text", "")
            if not re.search(r'\d+\.', text):
                continue

            year = _extract_year(game.get("date", ""))
            r_score = score_russian_relevance(
                game.get("white", ""),
                game.get("black", ""),
                game.get("event", ""),
            )
            all_games.append((key, year, r_score))

            if total % 100000 == 0:
                print(f"  ...scanned {total} lines, {len(all_games)} candidates")

    print(f"  Scanned {total} lines, {len(all_games)} candidates "
          f"with moves and augmented data ({skipped_no_aug} skipped, no augmented)")

    if len(all_games) <= oversample_target:
        print(f"  Fewer candidates than target — selecting all {len(all_games)}")
        return {g[0] for g in all_games}

    # Phase 2: Build year strata
    strata = defaultdict(list)  # year_bucket -> [(key, russian_score), ...]
    no_year = []
    for key, year, r_score in all_games:
        if year is not None:
            # 10-year buckets
            bucket = (year // 10) * 10
            strata[bucket].append((key, r_score))
        else:
            no_year.append((key, r_score))

    # Add unknown-year games as their own stratum
    if no_year:
        strata["unknown"] = no_year

    # Phase 3: Allocate slots per stratum proportional to size
    total_candidates = len(all_games)
    stratum_slots = {}
    allocated = 0
    sorted_buckets = sorted(strata.keys(), key=lambda b: (isinstance(b, str), b))

    for bucket in sorted_buckets:
        proportion = len(strata[bucket]) / total_candidates
        slots = max(1, round(proportion * oversample_target))
        stratum_slots[bucket] = slots
        allocated += slots

    # Adjust to hit exact oversample target
    while allocated > oversample_target:
        # Remove slots from largest strata first
        biggest = max(sorted_buckets, key=lambda b: stratum_slots[b])
        if stratum_slots[biggest] > 1:
            stratum_slots[biggest] -= 1
            allocated -= 1
        else:
            break
    while allocated < oversample_target:
        # Add slots to largest strata
        biggest = max(sorted_buckets, key=lambda b: len(strata[b]))
        stratum_slots[biggest] += 1
        allocated += 1

    # Phase 4: Weighted sampling within each stratum
    selected = set()
    stats = {"total_russian": 0, "total_non_russian": 0}

    print(f"\n  {'Stratum':<12} {'Pool':>6} {'Slots':>6} {'Selected':>8}")
    print(f"  {'-'*12} {'-'*6} {'-'*6} {'-'*8}")

    for bucket in sorted_buckets:
        pool = strata[bucket]
        slots = min(stratum_slots[bucket], len(pool))

        # Compute weights: base=1, scaled by russian_bias^score
        weights = []
        for key, r_score in pool:
            w = russian_bias ** r_score
            weights.append(w)

        # Weighted sampling without replacement
        chosen_indices = set()
        if slots >= len(pool):
            chosen_indices = set(range(len(pool)))
        else:
            # Reservoir-style weighted sampling
            remaining = list(range(len(pool)))
            remaining_weights = list(weights)
            for _ in range(slots):
                total_w = sum(remaining_weights)
                r = rng.random() * total_w
                cumulative = 0.0
                pick_idx = 0
                for j, w in enumerate(remaining_weights):
                    cumulative += w
                    if cumulative >= r:
                        pick_idx = j
                        break
                chosen_indices.add(remaining[pick_idx])
                remaining.pop(pick_idx)
                remaining_weights.pop(pick_idx)

        russian_in_stratum = 0
        for idx in chosen_indices:
            key, r_score = pool[idx]
            selected.add(key)
            if r_score > 0:
                russian_in_stratum += 1
                stats["total_russian"] += 1
            else:
                stats["total_non_russian"] += 1

        label = str(bucket) + "s" if isinstance(bucket, int) else str(bucket)
        print(f"  {label:<12} {len(pool):>6} {slots:>6} {len(chosen_indices):>8}"
              f"  ({russian_in_stratum} Russian)")

    print(f"\n  Total selected: {len(selected)}")
    print(f"  Russian-connected: {stats['total_russian']}  "
          f"Non-Russian: {stats['total_non_russian']}  "
          f"({100*stats['total_russian']/len(selected):.1f}% Russian)")

    return selected


def main():
    parser = argparse.ArgumentParser(description="Prepare merged chess game data")
    parser.add_argument("--games", default="pipeline/input/chess_games.jsonl.gz",
                        help="Path to chess_games.jsonl(.gz)")
    parser.add_argument("--augmented", default="pipeline/input/augmented_chess_games.jsonl.gz",
                        help="Path to augmented_chess_games.jsonl(.gz)")
    parser.add_argument("--output", default="pipeline/output/merged_games.jsonl",
                        help="Output merged JSONL path")
    parser.add_argument("--max-games", type=int, default=0,
                        help="Limit number of output games (0 = no limit)")
    parser.add_argument("--russian-bias", type=float, default=3.0,
                        help="Weight multiplier per Russian relevance point (default: 3.0)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for sampling reproducibility (default: 42)")
    parser.add_argument("--no-stratify", action="store_true",
                        help="Disable stratified sampling; use simple first-N selection")
    parser.add_argument("--no-clean", action="store_true",
                        help="Skip cleaning the output folder before processing")
    parser.add_argument("--verify", action="store_true",
                        help="Verify merged output and print summary of first N games")
    parser.add_argument("--verify-count", type=int, default=3,
                        help="Number of games to display in --verify mode (default: 3)")
    parser.add_argument("--pacing-sample", type=int, default=5000,
                        help="Games to score for the pacing metric in --verify mode "
                             "(0 = all; default: 5000)")
    parser.add_argument("--evaluate", action="store_true",
                        help="Grid-search the timing parameters against the pacing metric")
    parser.add_argument("--eval-games", type=int, default=100,
                        help="Games to evaluate in --evaluate mode "
                             "(100 dev / 10000 production; default: 100)")
    args = parser.parse_args()

    # Resolve paths relative to workspace root
    root = Path(__file__).resolve().parent.parent
    games_path = root / args.games
    aug_path = root / args.augmented
    output_path = root / args.output

    if args.verify:
        verify_output(str(output_path), args.verify_count, pacing_sample=args.pacing_sample)
        return

    if args.evaluate:
        evaluate_gridsearch(str(output_path), max_games=args.eval_games)
        return

    if not games_path.exists():
        print(f"Error: {games_path} not found")
        sys.exit(1)
    if not aug_path.exists():
        print(f"Error: {aug_path} not found")
        sys.exit(1)

    # Track runtimes of each phase for reporting
    timings = {}
    t_total = time.perf_counter()

    # Step 0: Clean output folder
    output_dir = output_path.parent
    if not args.no_clean:
        games_dir = output_dir / "games"
        removed = 0
        for path in [output_path, output_dir / "index.json", output_dir / "progress.json"]:
            if path.exists():
                path.unlink()
                removed += 1
        if games_dir.exists():
            count = sum(1 for _ in games_dir.iterdir())
            shutil.rmtree(games_dir)
            removed += count
        if removed:
            print(f"Cleaned output folder: removed {removed} items from {output_dir}")
    else:
        print("Skipping output folder cleanup (--no-clean)")

    # Step 1: Select candidate games
    candidate_keys = None
    if args.max_games:
        if args.no_stratify:
            # Legacy behaviour: take first N*5 keys from file
            t = time.perf_counter()
            candidate_keys = set()
            with _open_jsonl(games_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        game = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    key = game.get("key")
                    if key and parse_moves_from_text(game.get("text", "")):
                        candidate_keys.add(key)
                        if len(candidate_keys) >= args.max_games * 5:
                            break
            print(f"Pre-scanned {len(candidate_keys)} candidate keys (simple mode)")
            timings["candidate_selection"] = time.perf_counter() - t
        else:
            # Pre-scan augmented file to know which keys actually have data
            t = time.perf_counter()
            available_keys = scan_augmented_keys(str(aug_path))
            timings["scan_augmented_keys"] = time.perf_counter() - t
            # Stratified sampling with Russian bias (only from available keys)
            t = time.perf_counter()
            candidate_keys = stratified_sample(
                str(games_path),
                max_games=args.max_games,
                available_keys=available_keys,
                russian_bias=args.russian_bias,
                seed=args.seed,
            )
            timings["stratified_sample"] = time.perf_counter() - t

    # Step 1: Build augmented index (streaming, memory = one entry per unique key)
    t = time.perf_counter()
    aug_index = build_augmented_index(str(aug_path), candidate_keys=candidate_keys)
    timings["build_augmented_index"] = time.perf_counter() - t

    # Step 2+3: Process games, parse moves, segment narratives, write output
    t = time.perf_counter()
    process_games(str(games_path), aug_index, str(output_path), args.max_games)
    timings["process_games"] = time.perf_counter() - t

    timings["total"] = time.perf_counter() - t_total

    # Runtime report
    print(f"\n=== Runtime summary ===")
    for name in ["scan_augmented_keys", "stratified_sample", "candidate_selection",
                 "build_augmented_index", "process_games", "total"]:
        if name in timings:
            secs = timings[name]
            print(f"  {name:<24} {secs:>8.1f}s  ({secs/60:.1f} min)")


if __name__ == "__main__":
    main()
