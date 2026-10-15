"""Text normalisation shared by the matchers.

Everything here is used by both the slskd search-phase matcher
(`matching.candidates`, strict, judges a peer's file *name*) and the
local-phase matcher (`matching.tracks`, judges a file's *tags* against a DB
row): accent/punctuation folding, artist-credit splitting, and the version
keywords that make a recording a different version.

The two phases keep their own scoring on purpose (strict rules vs difflib
similarity) - see `matching/__init__.py`.
"""
import re
import unicodedata

# Neutral release words: not required when matching the expected title.
NEUTRAL_TITLE_WORDS = frozenset({
    "original", "extended", "radio", "album", "single", "mix", "edit",
    "version", "remastered", "remaster", "mono", "stereo", "and",
})


# An artist matched with spaces removed ("FormatB" for "Format:B") must be at
# least this long, so short names can't hide inside other words.
MIN_COMPACT_ARTIST_LEN = 6


FEAT_RE = re.compile(
    r"[\(\[]\s*(?:feat|ft|featuring)\b[^\)\]]*[\)\]]"
    r"|\b(?:feat|ft|featuring)\b.*?(?=\s[-–—]\s|[\(\[]|$)",
    re.IGNORECASE,
)
BRACKETS_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
# Credit separators. "/" is deliberately absent: it is part of names (AC/DC).
ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:,|;|&|\bfeat\b\.?|\bft\b\.?|\bfeaturing\b"
    r"|\s+x\s+|\s+vs\b\.?|\s+with\s+|\s+and\s+)\s*",
    re.IGNORECASE,
)


def fold(text: str | None) -> str:
    """Lowercase, strip accents, collapse every non-alphanumeric run to a
    space, and spell "rmx" as "remix"."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return " ".join("remix" if w == "rmx" else w for w in re.findall(r"[^\W_]+", t))


def expected_title_tokens(title: str | None) -> set[str]:
    """Words a file must contain to be this title. Featured artists, neutral
    release words and bare numbers (years) are not required."""
    folded = fold(FEAT_RE.sub(" ", title or "")).split()
    core = {t for t in folded if t not in NEUTRAL_TITLE_WORDS and not t.isdigit()}
    return core or {t for t in folded if not t.isdigit()} or set(folded)


def credited_names(artist_str: str | None) -> list[str]:
    """The full credit plus each credited artist, folded, "The " dropped."""
    names = []
    for part in [artist_str or ""] + ARTIST_SPLIT_RE.split(artist_str or ""):
        name = fold(part)
        if name.startswith("the "):
            name = name[4:]
        if name and name not in names:
            names.append(name)
    return names


def artist_in(name: str, folded_text: str) -> bool:
    """Whole-name match: the artist's words as a contiguous run, never a
    single shared word ("kris m" is not in "kris kross")."""
    if f" {name} " in f" {folded_text} ":
        return True
    compact = name.replace(" ", "")
    return len(compact) >= MIN_COMPACT_ARTIST_LEN and compact in folded_text.replace(" ", "")


# Words that make a recording a *different* version. Compared as a set on both
# sides, so "Dustup (Original Mix)" can't stand in for "Dustup (Mefjus Remix)"
# and vice versa. Neutral variants (original/extended mix, radio edit,
# remastered, album version) are deliberately absent — `version_tier` ranks
# those instead.
VERSION_KEYWORDS = (
    "remix", "dub", "vip", "club mix", "live", "acoustic", "unplugged",
    "instrumental", "karaoke", "cover", "sped up", "slowed", "nightcore", "8d",
    "bootleg", "rework", "remake", "reedit", "flip", "mashup", "acapella",
    "hotmix", "megamix",
)
_VERSION_WORDS = frozenset(w for kw in VERSION_KEYWORDS for w in kw.split())
# "<word> Mix" / "<word> Edit" is a named version ("Kris M Edit", "Strings
# Mix") unless the word is one of these neutral qualifiers.
_NEUTRAL_QUALIFIERS = frozenset({
    "original", "extended", "radio", "album", "single", "short", "clean",
    "explicit", "main", "full", "long",
})


def version_keywords(text: str | None, artist: str = "") -> frozenset[str]:
    """Version keywords in `text`, ignoring words of the credited artists
    (so an artist called "Dub FX" doesn't make every file a dub)."""
    t = " " + fold(text) + " "
    for name in credited_names(artist):
        t = t.replace(f" {name} ", " ")
    found = {kw for kw in VERSION_KEYWORDS if f" {kw} " in t}
    for word, kind in re.findall(r"(?<= )(\w+) (mix|edit)(?= )", t):
        if word not in _NEUTRAL_QUALIFIERS and word not in _VERSION_WORDS:
            found.add(f"named {kind}")
    return frozenset(found)


def has_version_marker(text: str) -> str | None:
    found = version_keywords(text)
    return next((kw for kw in VERSION_KEYWORDS if kw in found), None)


def version_mismatch(expected_title: str, file_title: str, artist: str = "") -> bool:
    """True when the two sides declare different sets of version keywords."""
    return version_keywords(expected_title, artist) != version_keywords(file_title, artist)


# DJ-oriented version preference. Lower = more preferred. Extended Mix is the
# DJ-friendly long cut; Original Mix is the producer's studio version; everything
# else (radio edits, plain titles) is the fallback. Note: this is intentionally
# orthogonal to `VERSION_KEYWORDS` / `version_mismatch` — those reject *wrong*
# versions (e.g. Live when expecting Studio); this only ranks compatible ones.
def version_tier(text: str) -> int:
    t = (text or "").lower()
    if re.search(r"\bextended\s+(mix|version|edit)\b", t):
        return 0
    if re.search(r"\boriginal\s+(mix|version)\b", t):
        return 1
    return 2
