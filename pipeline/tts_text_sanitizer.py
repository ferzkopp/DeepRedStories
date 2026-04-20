"""
TTS Text Sanitizer
Cleans and normalizes text before sending to XTTS v2 to prevent
language drift (e.g. Chinese-sounding output) and improve clarity.
"""

import re
import unicodedata


# ── Unicode normalization ──────────────────────────────────────────────────

# Non-breaking / special hyphens → ASCII hyphen
_HYPHENS = re.compile(r'[\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uFE58\uFE63\uFF0D]')

# Fancy quotes → ASCII
_LEFT_SINGLE = re.compile(r'[\u2018\u201B]')
_RIGHT_SINGLE = re.compile(r'[\u2019\u201A]')
_LEFT_DOUBLE = re.compile(r'[\u201C\u201F]')
_RIGHT_DOUBLE = re.compile(r'[\u201D\u201E]')

# Ellipsis
_ELLIPSIS = re.compile(r'\u2026')

# Non-breaking spaces
_NBSP = re.compile(r'[\u00A0\u202F\u2007\u2060]')


def _normalize_unicode(text: str) -> str:
    """Replace problematic Unicode characters with ASCII equivalents."""
    text = _HYPHENS.sub('-', text)
    text = _LEFT_SINGLE.sub("'", text)
    text = _RIGHT_SINGLE.sub("'", text)
    text = _LEFT_DOUBLE.sub('"', text)
    text = _RIGHT_DOUBLE.sub('"', text)
    text = _ELLIPSIS.sub('...', text)
    text = _NBSP.sub(' ', text)
    # Normalize remaining to NFC (composed form)
    text = unicodedata.normalize('NFC', text)
    return text


# ── ECO code expansion ────────────────────────────────────────────────────

_ECO_PATTERN = re.compile(
    r'\b([A-E])(\d{2})([a-z]?)\b'
)

_ECO_LETTER_NAMES = {
    'A': 'A', 'B': 'B', 'C': 'C', 'D': 'D', 'E': 'E',
}

_NUMBER_WORDS = {
    0: 'zero', 1: 'one', 2: 'two', 3: 'three', 4: 'four',
    5: 'five', 6: 'six', 7: 'seven', 8: 'eight', 9: 'nine',
    10: 'ten', 11: 'eleven', 12: 'twelve', 13: 'thirteen',
    14: 'fourteen', 15: 'fifteen', 16: 'sixteen', 17: 'seventeen',
    18: 'eighteen', 19: 'nineteen', 20: 'twenty', 30: 'thirty',
    40: 'forty', 50: 'fifty', 60: 'sixty', 70: 'seventy',
    80: 'eighty', 90: 'ninety',
}


def _number_to_words(n: int) -> str:
    """Convert a two-digit number to words."""
    if n in _NUMBER_WORDS:
        return _NUMBER_WORDS[n]
    tens = (n // 10) * 10
    ones = n % 10
    return f"{_NUMBER_WORDS[tens]}-{_NUMBER_WORDS[ones]}"


def _expand_eco(match: re.Match) -> str:
    """Expand an ECO code like B62 to 'B sixty-two'."""
    letter = match.group(1)
    number = int(match.group(2))
    suffix = match.group(3)  # optional lowercase variant letter
    result = f"{letter} {_number_to_words(number)}"
    if suffix:
        result += f" {suffix}"
    return result


def _spell_out_eco_codes(text: str) -> str:
    """Find and spell out ECO chess opening codes."""
    # Only replace when preceded by context suggesting it's an ECO code
    # (e.g., "opening A98", "code B62", "the A03", "ECO B46")
    # to avoid false positives on regular words
    eco_context = re.compile(
        r'(?:(?:ECO|opening|code|structure|classification|the|an?)\s+)'
        r'([A-E])(\d{2})([a-z]?)\b',
        re.IGNORECASE
    )

    def _replace_with_context(m: re.Match) -> str:
        prefix = m.group(0)[:m.start(1) - m.start(0)]
        letter = m.group(1)
        number = int(m.group(2))
        suffix = m.group(3)
        result = f"{prefix}{letter} {_number_to_words(number)}"
        if suffix:
            result += f" {suffix}"
        return result

    text = eco_context.sub(_replace_with_context, text)
    return text


# ── Game result normalization ─────────────────────────────────────────────

def _expand_results(text: str) -> str:
    """Expand chess result notations to spoken form."""
    text = re.sub(r'\b1-0\b', 'one-zero', text)
    text = re.sub(r'\b0-1\b', 'zero-one', text)
    text = re.sub(r'\b1/2-1/2\b', 'draw', text)
    return text


# ── Bare alphanumeric fragment cleanup ────────────────────────────────────

def _clean_residual_notation(text: str) -> str:
    """Remove or normalize residual chess-like fragments that survived
    the notation converter but could confuse TTS."""
    # Isolated move-number dots like "55." at end of text
    text = re.sub(r'\b(\d+)\.\s*$', '', text)
    # Double+ spaces
    text = re.sub(r'  +', ' ', text)
    return text.strip()


# ── Main entry point ──────────────────────────────────────────────────────

def sanitize_for_tts(text: str) -> str:
    """Full sanitization pipeline for text about to be sent to XTTS v2."""
    text = _normalize_unicode(text)
    text = _spell_out_eco_codes(text)
    text = _expand_results(text)
    text = _clean_residual_notation(text)
    return text
