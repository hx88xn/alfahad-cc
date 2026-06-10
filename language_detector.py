"""
Deterministic language detector for the Al Fardan Exchange voice agent's six
supported languages: Najdi Arabic (ar, default), Pakistani Urdu (ur), Hindi (hi),
Tamil (ta), Tagalog/Filipino (tl), and English (en).

Runs on the realtime model's input_audio_transcription text and acts as a second,
authoritative signal that silently corrects the model's own language pick via the
`set_response_language` tool dispatcher — the same pattern used in
../bankislami-callcenter. This is what stops the model from carrying the default
Najdi accent into Urdu/Hindi/Tamil/Tagalog: when the classifier is confident the
caller used another language, the tool result forces that language + its accent.

Pure-Python, no external dependencies.

Strategy:
  - Tamil script (U+0B80–U+0BFF)        → ta  (decisive: unique script)
  - Devanagari script (U+0900–U+097F)   → hi  (decisive: unique script)
  - Arabic script (U+0600–U+06FF):
        Urdu-specific Perso-Arabic letters (ٹ ڈ ڑ ں ہ ھ ے گ چ پ ژ ک ی)
        or Urdu anchor words → ur; otherwise → ar.
  - Latin script: anchor-word scoring across en / ur (Roman) / tl.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional


# Letters that exist in Urdu's Perso-Arabic alphabet but NOT in standard Arabic.
# Any of these is decisive evidence the Arabic-script text is Urdu, not Arabic.
URDU_ONLY_LETTERS = set("ٹڈڑںہھےگچپژکی")

# Roman-script anchor words per language. Lowercased, punctuation-stripped.
LEXICON: dict[str, set[str]] = {
    "en": {
        "the", "is", "are", "was", "were", "am", "be", "been",
        "a", "an", "of", "to", "in", "on", "at", "for", "with", "from",
        "i", "me", "my", "you", "your", "we", "us", "our", "he", "she", "it",
        "they", "what", "when", "where", "why", "how", "which", "who",
        "want", "need", "have", "has", "had", "do", "does", "did", "can", "could",
        "would", "should", "will", "tell", "give", "get", "make", "take",
        "please", "thank", "thanks", "hello", "hi", "yes", "no", "okay", "ok",
        "rate", "transfer", "branch", "money", "exchange", "send", "account",
    },
    "ur": {
        # Roman Urdu function words / verb forms (Pakistani usage)
        "hai", "hain", "ho", "hoga", "hogi", "tha", "thi",
        "kya", "kyun", "kyon", "kaise", "kaisay", "kaisa", "kitna", "kitni",
        "mujhe", "mujhko", "mera", "meri", "mere", "main", "hum", "hamara",
        "aap", "apni", "apna", "apne", "tum", "tumhara",
        "ko", "ka", "ke", "se", "par", "mein",
        "shukriya", "meherbani", "meharbani", "baraye", "theek", "acha", "achha", "bilkul",
        "kar", "karna", "karta", "karti", "karte", "karen", "karein",
        "sakta", "sakti", "sakte", "raha", "rahi", "rahe",
        "samajh", "batao", "bataye", "bataiye", "chahiye", "chahta", "chahti",
        "nahin", "nahi", "haan", "ji", "kahan", "yahan", "wahan", "abhi", "phir",
        "paisa", "paise", "rupay", "rupaye", "bhejna", "bhejo",
    },
    "tl": {
        # Tagalog / Filipino
        "po", "opo", "salamat", "magkano", "paano", "kailangan", "kumusta",
        "ano", "padala", "palitan", "ang", "ng", "sa", "ako", "ikaw", "siya",
        "kami", "kayo", "sila", "ito", "iyan", "iyon", "hindi", "oo", "pwede",
        "puwede", "gusto", "mga", "naman", "lang", "kasi", "para", "may",
        "meron", "wala", "bakit", "saan", "kelan", "kanino", "magkakano",
        "pera", "bangko", "padalhan", "ipadala",
    },
}

# Distinctive Roman markers — essentially never appear in the other languages.
# Worth weight 3 instead of weight 1; any one usually decides.
DISTINCTIVE: dict[str, set[str]] = {
    "en": set(),
    "ur": {"kya", "kyun", "mujhe", "shukriya", "meherbani", "meharbani", "kaise",
           "kaisay", "chahiye", "samajh", "kitna", "kitni", "bilkul"},
    "tl": {"po", "opo", "salamat", "magkano", "paano", "kailangan", "kumusta",
           "padala", "palitan", "pwede", "puwede", "magkakano"},
}

# Tokens shared across multiple languages / brand terms — excluded from scoring.
SHARED_AMBIGUOUS: set[str] = {
    "ji", "haan", "ok", "okay", "hmm", "uh", "um",
    "al", "fardan", "exchange", "alfardan", "alfapay", "alfanow", "aani",
    "rate", "atm", "pin", "otp", "cnic",
    "ek", "do", "teen",
}

# Token splitter that preserves Latin, Arabic/Urdu, Devanagari and Tamil letters.
_TOKEN_RE = re.compile(r"[^\sa-zA-Z'؀-ۿݐ-ݿऀ-ॿ஀-௿]+",
                       re.UNICODE)


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    text = unicodedata.normalize("NFC", text)
    cleaned = _TOKEN_RE.sub(" ", text)
    return [t.lower() for t in cleaned.split() if t]


def _has_range(text: str, lo: int, hi: int) -> int:
    return sum(1 for ch in text if lo <= ord(ch) <= hi)


def detect_language(text: str) -> tuple[Optional[str], float, dict]:
    """Classify text into one of {ar, ur, hi, ta, tl, en} or None.

    Returns (lang, confidence, debug) where confidence is in [0, 1].
    Script-based hits (Tamil, Devanagari, Urdu-only letters) are decisive
    (confidence 1.0). Latin/Arabic ambiguity falls back to anchor scoring.
    """
    if not text or not text.strip():
        return None, 0.0, {}

    norm = unicodedata.normalize("NFC", text)

    # --- Decisive unique-script checks ---
    tamil = _has_range(norm, 0x0B80, 0x0BFF)
    deva = _has_range(norm, 0x0900, 0x097F)
    if tamil:
        return "ta", 1.0, {"script": "tamil", "chars": tamil}
    if deva:
        return "hi", 1.0, {"script": "devanagari", "chars": deva}

    # --- Arabic-script: disambiguate ar vs ur ---
    arabic = _has_range(norm, 0x0600, 0x06FF) + _has_range(norm, 0x0750, 0x077F)
    if arabic:
        urdu_letters = sum(1 for ch in norm if ch in URDU_ONLY_LETTERS)
        tokens = _tokenize(norm)
        urdu_word_hits = sum(1 for t in tokens if t in LEXICON["ur"])
        if urdu_letters > 0 or urdu_word_hits >= 1:
            return "ur", 1.0 if urdu_letters else 0.8, {
                "script": "arabic", "urdu_letters": urdu_letters,
                "urdu_words": urdu_word_hits,
            }
        return "ar", 0.9, {"script": "arabic", "urdu_letters": 0}

    # --- Latin script: anchor-word scoring across en / ur(Roman) / tl ---
    tokens = _tokenize(norm)
    scores = {"en": 0, "ur": 0, "tl": 0}
    if not tokens:
        return None, 0.0, {}
    for tok in tokens:
        if tok in SHARED_AMBIGUOUS:
            continue
        for lang in scores:
            if tok in LEXICON.get(lang, set()):
                scores[lang] += 3 if tok in DISTINCTIVE.get(lang, set()) else 1

    total = sum(scores.values())
    if total == 0:
        return None, 0.0, {"scores": scores}

    best = max(scores, key=scores.get)
    best_score = scores[best]
    non_shared = [t for t in tokens if t not in SHARED_AMBIGUOUS]
    # Require >=2 points unless it's a single-token utterance.
    if best_score < 2 and len(non_shared) > 1:
        return None, 0.0, {"scores": scores}

    return best, best_score / total, {"scores": scores}
