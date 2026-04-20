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
import json
import math
import os
import random
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path


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


def estimate_move_timings(segment_text: str, game_moves: list[dict],
                          start_move: int, end_move: int) -> list[float]:
    """
    Estimate relative timing for each half-move in a segment based on
    where move notations appear in the narrative text.

    Returns a list of fractional delays (summing to ~1.0) for each ply.
    Each value represents the fraction of the segment's audio duration
    to wait before showing that move.  Moves referenced in the text are
    anchored to their approximate text position; unmatched moves are
    linearly interpolated between anchors.
    """
    # Build flat ply list for this segment
    plies = []
    for m in game_moves:
        if m['num'] < start_move or m['num'] > end_move:
            continue
        plies.append({'moveNum': m['num'], 'color': 'w', 'san': m['white']})
        if 'black' in m:
            plies.append({'moveNum': m['num'], 'color': 'b', 'san': m['black']})

    n = len(plies)
    if n == 0:
        return []
    if n == 1:
        return [1.0]

    text_len = len(segment_text)
    if text_len == 0:
        return _uniform_delays(n)

    # Normalize unicode dashes for matching
    normalized = segment_text.replace('\u2011', '-').replace('\u2010', '-')

    # Find move patterns with character positions
    move_pat = re.compile(
        r'(\d+)\.'
        r'(O-O-O|O-O|[A-Za-z][a-z0-9x]*[a-h]?[1-8]?[=]?[QRBN]?[+#]?)'
        r'(?:\s+'
        r'(O-O-O|O-O|[A-Za-z][a-z0-9x]*[a-h]?[1-8]?[=]?[QRBN]?[+#]?)'
        r')?'
    )

    # Collect anchors: (move_num, color, char_position)
    text_anchors = []
    for match in move_pat.finditer(normalized):
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

    # If no anchors found, return uniform spacing
    if all(t is None for t in timestamps):
        return _uniform_delays(n)

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

    # Ensure no negative delays
    delays = [max(d, 0.0) for d in delays]

    # Normalize to sum to 1.0
    total = sum(delays)
    if total > 0:
        delays = [d / total for d in delays]
    else:
        return _uniform_delays(n)

    # Round and adjust to sum exactly to 1.0
    delays = [round(d, 4) for d in delays]
    delays[-1] = round(max(0.0, delays[-1] + (1.0 - sum(delays))), 4)

    return delays


def _uniform_delays(n: int) -> list[float]:
    """Return n equal fractional delays summing to 1.0."""
    d = round(1.0 / n, 4)
    result = [d] * n
    result[-1] = round(result[-1] + (1.0 - sum(result)), 4)
    return result


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


def verify_output(output_path: str, count: int):
    """Spot-check the merged output file for data quality issues."""
    if not os.path.exists(output_path):
        print(f"Error: {output_path} not found. Run prepare_data.py first.")
        sys.exit(1)

    total = 0
    issues = 0

    with open(output_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total += 1
            game = json.loads(line)

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
    args = parser.parse_args()

    # Resolve paths relative to workspace root
    root = Path(__file__).resolve().parent.parent
    games_path = root / args.games
    aug_path = root / args.augmented
    output_path = root / args.output

    if not games_path.exists():
        print(f"Error: {games_path} not found")
        sys.exit(1)
    if not aug_path.exists():
        print(f"Error: {aug_path} not found")
        sys.exit(1)

    if args.verify:
        verify_output(str(output_path), args.verify_count)
        return

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
        else:
            # Pre-scan augmented file to know which keys actually have data
            available_keys = scan_augmented_keys(str(aug_path))
            # Stratified sampling with Russian bias (only from available keys)
            candidate_keys = stratified_sample(
                str(games_path),
                max_games=args.max_games,
                available_keys=available_keys,
                russian_bias=args.russian_bias,
                seed=args.seed,
            )

    # Step 1: Build augmented index (streaming, memory = one entry per unique key)
    aug_index = build_augmented_index(str(aug_path), candidate_keys=candidate_keys)

    # Step 2+3: Process games, parse moves, segment narratives, write output
    process_games(str(games_path), aug_index, str(output_path), args.max_games)


if __name__ == "__main__":
    main()
