"""
Chess Notation Converter
Detects algebraic chess notation in narrative text and converts it to
natural-language descriptions suitable for TTS audio generation.

Features:
- Detects move sequences like "1.e4 c5 2.Nf3 d5" and single moves like "13.Bd3"
- Converts piece symbols, squares, captures, castling, checks to spoken words
- Consolidates long move sequences into summaries to avoid tedious listening
- Integrates move numbering naturally, avoiding duplication with existing text
"""

import re
from typing import Optional


# ── Piece and symbol mappings ──────────────────────────────────────────────

_PIECE_NAMES = {
    "K": "king",
    "Q": "queen",
    "R": "rook",
    "B": "bishop",
    "N": "knight",
}

_FILE_NAMES = {
    "a": "ah", "b": "bee", "c": "see", "d": "dee",
    "e": "ee", "f": "eff", "g": "jee", "h": "aitch",
}

_RANK_NAMES = {
    "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight",
}

_ORDINALS = {
    1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth",
    6: "sixth", 7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth",
    11: "eleventh", 12: "twelfth", 13: "thirteenth", 14: "fourteenth",
    15: "fifteenth", 16: "sixteenth", 17: "seventeenth", 18: "eighteenth",
    19: "nineteenth", 20: "twentieth",
}

# ── Single move notation pattern ──────────────────────────────────────────

# Matches a single SAN move (no move number): e4, Nf3, cxd4, O-O, O-O-O, e8=Q, Nxh7+, etc.
# Also handles non-breaking hyphens (‑ = \u2011) in castling
_CASTLING = r'(?:O[\-\u2011\u2010]O[\-\u2011\u2010]O|O[\-\u2011\u2010]O)'
_SAN_MOVE = (
    r'(?:' + _CASTLING + r'|'
    r'[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?)'
)

# ── Sequence detection ────────────────────────────────────────────────────

# A numbered move: "1.e4 c5" or "13.Bd3" or "1...e6" or "1…e6"
# Captures: move_number, dot+ellipsis, white_move_or_black_move, optional_reply
_NUMBERED_MOVE = re.compile(
    r'(\d+)'                                # move number
    r'(\.{1,3}|…)'                          # dot(s) or ellipsis
    r'(' + _SAN_MOVE + r')'                 # first move (white or black)
    r'(?:\s+(' + _SAN_MOVE + r'))?'         # optional second move (black reply)
)

# A sequence of numbered moves (2+ consecutive numbered moves with optional text between)
_MOVE_SEQUENCE = re.compile(
    r'(\d+)(\.{1,3}|…)(' + _SAN_MOVE + r')'
    r'(?:\s+(' + _SAN_MOVE + r'))?'
    r'(?:'
    r'(?:\s*,?\s*)'
    r'(\d+)(\.{1,3}|…)(' + _SAN_MOVE + r')'
    r'(?:\s+(' + _SAN_MOVE + r'))?'
    r')+'
)

# Detect "move N" already in surrounding text (to avoid "on move 5, the fifth move...")
_MOVE_NUMBER_CONTEXT = re.compile(r'\bmoves?\s+(\d+)', re.IGNORECASE)

# ── Bare (unnumbered) move detection ──────────────────────────────────────

# Matches distinctive SAN moves in prose NOT preceded by a move number.
# Requires at least one strong chess indicator: piece prefix, capture (x),
# check/mate suffix, promotion, or ellipsis prefix (Black's move marker).
# Group 1: optional ellipsis prefix   Group 2: the SAN move
_BARE_CHESS_MOVE = re.compile(
    r'(?<![a-zA-Z0-9])'                     # not preceded by word char
    r'(\.{2,3}|\u2026)?'                    # optional ellipsis prefix (Black)
    r'('
    + _CASTLING + r'|'
    r'[KQRBN][a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?'  # piece move
    r'|'
    r'[a-h]x[a-h][1-8](?:=[QRBN])?[+#]?'   # pawn capture
    r'|'
    r'[a-h][1-8]=[QRBN][+#]?'               # pawn promotion
    r'|'
    r'[a-h][1-8][+#]'                        # pawn move with check/mate
    r')'
    r'(?![a-zA-Z0-9])'                       # not followed by word char
)


def _spell_square(sq: str) -> str:
    """Spell out a chess square phonetically, e.g. 'e4' -> 'ee four'."""
    if len(sq) == 2 and sq[0] in _FILE_NAMES and sq[1] in _RANK_NAMES:
        return f"{_FILE_NAMES[sq[0]]} {_RANK_NAMES[sq[1]]}"
    # Partial disambiguation (just a file or rank)
    if len(sq) == 1:
        if sq in _FILE_NAMES:
            return _FILE_NAMES[sq]
        if sq in _RANK_NAMES:
            return _RANK_NAMES[sq]
    return sq


def _describe_san_move(san: str, color: str = "white") -> str:
    """Convert a single SAN move to natural language."""
    # Normalize non-breaking hyphens
    san = san.replace('\u2011', '-').replace('\u2010', '-')

    # Castling
    if san in ("O-O-O", "0-0-0"):
        return f"{color} castles queenside" if color else "castles queenside"
    if san in ("O-O", "0-0"):
        return f"{color} castles kingside" if color else "castles kingside"

    # Strip check/checkmate suffix
    suffix = ""
    if san.endswith('#'):
        suffix = " with checkmate"
        san = san[:-1]
    elif san.endswith('+'):
        suffix = " with check"
        san = san[:-1]

    # Promotion
    promotion = ""
    if '=' in san:
        prom_piece = san[san.index('=') + 1]
        promotion = f" promoting to {_PIECE_NAMES.get(prom_piece, prom_piece)}"
        san = san[:san.index('=')]

    # Capture
    is_capture = 'x' in san
    san_clean = san.replace('x', '')

    # Determine piece and square
    piece = "pawn"
    disambiguation = ""

    if san_clean and san_clean[0] in _PIECE_NAMES:
        piece = _PIECE_NAMES[san_clean[0]]
        san_clean = san_clean[1:]

    # Destination square is the last 2 characters
    if len(san_clean) >= 2:
        dest = _spell_square(san_clean[-2:])
        dis = san_clean[:-2]
        if dis:
            disambiguation = f" from {_spell_square(dis)}"
    else:
        dest = _spell_square(san_clean)

    action = "captures on" if is_capture else "to"

    color_prefix = f"{color} " if color else ""
    return f"{color_prefix}{piece}{disambiguation} {action} {dest}{promotion}{suffix}"


def _is_black_move(dots: str) -> bool:
    """Check if the dots/ellipsis indicator means it's Black's move."""
    return dots in ('...', '…') or len(dots) >= 3


def _convert_single_numbered_move(match: re.Match, context_before: str = "") -> str:
    """Convert a single numbered move match to natural language."""
    num = int(match.group(1))
    dots = match.group(2)
    first_san = match.group(3)
    second_san = match.group(4) if match.lastindex >= 4 else None

    parts = []

    # Determine move number prefix
    prefix = _move_number_prefix(num, context_before)

    if _is_black_move(dots):
        # Black-only move
        desc = _describe_san_move(first_san, "black")
        if prefix:
            parts.append(f"{prefix}, {desc}")
        else:
            parts.append(desc)
    else:
        # White move (+ optional black reply)
        white_desc = _describe_san_move(first_san, "white")
        if prefix:
            parts.append(f"{prefix}, {white_desc}")
        else:
            parts.append(white_desc)

        if second_san:
            black_desc = _describe_san_move(second_san, "black")
            parts.append(black_desc)

    return ", ".join(parts)


def _move_number_prefix(num: int, context: str, inline: bool = False) -> str:
    """
    Generate a move number prefix like 'on move 5' or 'the first move',
    avoiding duplication if context already mentions this move number.
    If inline is True, returns a shorter form suitable for mid-sentence use.
    """
    # Check if surrounding text already says "move N"
    for m in _MOVE_NUMBER_CONTEXT.finditer(context):
        if int(m.group(1)) == num:
            return ""  # already referenced

    if inline:
        # Shorter form for mid-sentence use (after "with", "played", etc.)
        if num in _ORDINALS:
            return f"the {_ORDINALS[num]} move"
        return f"move {num}"

    if num in _ORDINALS:
        return f"on the {_ORDINALS[num]} move"
    return f"on move {num}"


# Detect preceding words that indicate we're mid-sentence
_PRECEDING_MID_SENTENCE = re.compile(r'\b(?:with|after|played|plays|following|continued|then|and)\s*$', re.IGNORECASE)


# ── Consolidation constants ───────────────────────────────────────────────

# Sequences of more than this many consecutive numbered moves get consolidated
_CONSOLIDATION_THRESHOLD = 4


def _consolidate_long_sequence(moves_data: list[tuple]) -> str:
    """
    Consolidate a long sequence of moves into a summary.
    moves_data: list of (num, dots, san1, san2_or_None)
    """
    total = len(moves_data)
    first_num = moves_data[0][0]
    last_num = moves_data[-1][0]

    # Always describe the first move and last move in full
    first_dots, first_san1, first_san2 = moves_data[0][1], moves_data[0][2], moves_data[0][3]
    last_dots, last_san1, last_san2 = moves_data[-1][1], moves_data[-1][2], moves_data[-1][3]

    # Build first move description
    if _is_black_move(first_dots):
        first_desc = _describe_san_move(first_san1, "black")
    else:
        first_desc = _describe_san_move(first_san1, "white")
        if first_san2:
            first_desc += ", " + _describe_san_move(first_san2, "black")

    # Build last move description
    if _is_black_move(last_dots):
        last_desc = _describe_san_move(last_san1, "black")
    else:
        last_desc = _describe_san_move(last_san1, "white")
        if last_san2:
            last_desc += ", " + _describe_san_move(last_san2, "black")

    # Count half-moves in the middle
    middle_count = total - 2
    half_moves = 0
    for _, dots, san1, san2 in moves_data[1:-1]:
        half_moves += 1
        if san2:
            half_moves += 1

    if middle_count <= 0:
        return (
            f"starting on move {first_num}, {first_desc}, "
            f"followed by {last_desc} on move {last_num}"
        )

    return (
        f"starting on move {first_num}, {first_desc}, "
        f"a series of {middle_count} moves follows, "
        f"concluding on move {last_num} with {last_desc}"
    )


# ── Main entry point ─────────────────────────────────────────────────────

def _find_move_sequences(text: str) -> list[dict]:
    """
    Find all chess move sequences in the text.
    Returns list of dicts with 'start', 'end', 'moves_data' keys.
    moves_data: list of (num: int, dots: str, san1: str, san2: str|None)
    """
    sequences = []
    pos = 0

    while pos < len(text):
        m = _NUMBERED_MOVE.search(text, pos)
        if not m:
            break

        # Collect consecutive numbered moves starting from this match
        moves_data = []
        seq_start = m.start()
        seq_end = m.end()

        num = int(m.group(1))
        dots = m.group(2)
        san1 = m.group(3)
        san2 = m.group(4) if m.lastindex >= 4 else None
        moves_data.append((num, dots, san1, san2))

        # Try to extend: look for more numbered moves immediately following
        scan_pos = m.end()
        while scan_pos < len(text):
            # Allow comma, space between moves
            gap_match = re.match(r'[\s,]*', text[scan_pos:])
            gap_end = scan_pos + gap_match.end() if gap_match else scan_pos

            # The gap should be small (no long prose between moves)
            if gap_end - scan_pos > 5:
                break

            next_m = _NUMBERED_MOVE.match(text, gap_end)
            if not next_m:
                break

            next_num = int(next_m.group(1))
            # Must be sequential or close (allow small gaps for black-only notations)
            prev_num = moves_data[-1][0]
            if next_num < prev_num or next_num > prev_num + 2:
                break

            n_dots = next_m.group(2)
            n_san1 = next_m.group(3)
            n_san2 = next_m.group(4) if next_m.lastindex >= 4 else None
            moves_data.append((next_num, n_dots, n_san1, n_san2))
            seq_end = next_m.end()
            scan_pos = next_m.end()

        sequences.append({
            'start': seq_start,
            'end': seq_end,
            'moves_data': moves_data,
        })
        pos = seq_end

    return sequences


def _convert_bare_moves(text: str) -> str:
    """
    Second pass: convert bare (unnumbered) SAN moves in prose to natural
    language.  Only matches moves with strong chess indicators (piece prefix,
    capture, check/mate, promotion) or an ellipsis prefix for Black's moves.
    """
    matches = list(_BARE_CHESS_MOVE.finditer(text))
    if not matches:
        return text

    result = text
    for m in reversed(matches):
        ellipsis = m.group(1)   # optional '...' / '…'
        san = m.group(2)        # the SAN move

        # Determine color: ellipsis prefix → Black; otherwise omit
        color = "black" if ellipsis else ""
        desc = _describe_san_move(san, color)

        result = result[:m.start()] + desc + result[m.end():]

    return result


def convert_chess_notation(text: str) -> str:
    """
    Convert all chess algebraic notation in text to natural language.

    - Single/short move references (<=4 moves): converted individually
    - Long sequences (>4 moves): consolidated into summaries
    - Move numbering integrated smartly, avoiding duplication
    """
    sequences = _find_move_sequences(text)
    if not sequences:
        # No numbered sequences; still check for bare moves
        return _convert_bare_moves(text)

    # Process sequences in reverse order to preserve string indices
    result = text
    for seq in reversed(sequences):
        moves_data = seq['moves_data']
        original = result[seq['start']:seq['end']]
        context_before = result[max(0, seq['start'] - 80):seq['start']]

        if len(moves_data) > _CONSOLIDATION_THRESHOLD:
            replacement = _consolidate_long_sequence(moves_data)
            # Capitalize if NOT preceded by a preposition (i.e. starts new clause)
            if not _PRECEDING_MID_SENTENCE.search(context_before):
                replacement = replacement[0].upper() + replacement[1:]
        elif len(moves_data) == 1:
            # Single move - use simple conversion
            num, dots, san1, san2 = moves_data[0]
            inline = bool(_PRECEDING_MID_SENTENCE.search(context_before))
            prefix = _move_number_prefix(num, context_before, inline=inline)
            if _is_black_move(dots):
                desc = _describe_san_move(san1, "black")
                replacement = f"{prefix}, {desc}" if prefix else desc
            else:
                desc = _describe_san_move(san1, "white")
                if san2:
                    desc += ", " + _describe_san_move(san2, "black")
                replacement = f"{prefix}, {desc}" if prefix else desc
        else:
            # Short sequence (2-4 moves) - describe each move
            parts = []
            for i, (num, dots, san1, san2) in enumerate(moves_data):
                ctx = context_before if i == 0 else " ".join(parts)
                inline = bool(_PRECEDING_MID_SENTENCE.search(ctx)) if i == 0 else False
                prefix = _move_number_prefix(num, ctx, inline=inline)
                if _is_black_move(dots):
                    desc = _describe_san_move(san1, "black")
                    parts.append(f"{prefix}, {desc}" if prefix else desc)
                else:
                    desc = _describe_san_move(san1, "white")
                    if san2:
                        desc += ", " + _describe_san_move(san2, "black")
                    parts.append(f"{prefix}, {desc}" if prefix else desc)
            replacement = ", then ".join(parts)

        result = result[:seq['start']] + replacement + result[seq['end']:]

    # Second pass: convert bare (unnumbered) SAN moves remaining in prose
    result = _convert_bare_moves(result)

    return result
