"""FTS5 query preparation pipeline.

Handles sanitization (auto-quoting dots/hyphens/commas), German character
expansion (ß↔ss, ä↔ae, ö↔oe, ü↔ue), and NEAR() expression preservation.
"""

import re
from itertools import product

# FTS5 operators that should not be modified during query expansion
_FTS5_OPERATORS = frozenset({"AND", "OR", "NOT"})

# Pattern matching NEAR(...) expressions — must be preserved verbatim
_NEAR_RE = re.compile(r"NEAR\([^)]+\)", re.IGNORECASE)

# German special character → digraph (forward direction, always unambiguous)
_CHAR_TO_DIGRAPH = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"}
_GERMAN_CHARS = frozenset(_CHAR_TO_DIGRAPH)

# Digraph → German character (reverse direction, applied per-position)
_DIGRAPH_TO_CHAR = {"ae": "ä", "oe": "ö", "ue": "ü", "Ae": "Ä", "Oe": "Ö", "Ue": "Ü", "ss": "ß"}


def _preserve_near(query: str) -> tuple[str, list[str]]:
    """Extract NEAR() expressions, replacing them with placeholders."""
    saved: list[str] = []

    def _save(m: re.Match) -> str:
        saved.append(m.group(0))
        return f"__NEAR{len(saved) - 1}__"

    return _NEAR_RE.sub(_save, query), saved


def _to_digraph(text: str) -> str:
    """Replace all German special characters with their digraph equivalents."""
    for char, digraph in _CHAR_TO_DIGRAPH.items():
        text = text.replace(char, digraph)
    return text


def _digraph_variants(word: str) -> list[str]:
    """Generate variants replacing each digraph with its German character independently.

    For 'Aussendurchmesser' (two 'ss' occurrences), returns:
      ['Außendurchmesser', 'Aussendurchmeßer']
    For 'Groesse' (oe and ss), returns:
      ['Grösse', 'Groeße']
    Each position is tried separately.
    """
    variants = []
    for digraph, char in _DIGRAPH_TO_CHAR.items():
        start = 0
        while True:
            pos = word.find(digraph, start)
            if pos == -1:
                break
            variant = word[:pos] + char + word[pos + len(digraph) :]
            if variant not in variants:
                variants.append(variant)
            start = pos + len(digraph)
    return variants


def _has_german_content(text: str) -> bool:
    """Check if text contains German special characters or their digraph equivalents."""
    if any(c in text for c in _GERMAN_CHARS):
        return True
    return any(d in text for d in _DIGRAPH_TO_CHAR)


def _token_variants(token: str) -> list[str]:
    """Generate all German spelling variants for a token.

    Forward direction (ä→ae, ß→ss, etc.) replaces all chars at once — always correct.
    Reverse direction (ae→ä, ss→ß, etc.) replaces each position individually,
    since digraphs like 'ss' may or may not represent a German special character.
    """
    variants = []
    has_german_chars = any(c in token for c in _GERMAN_CHARS)
    # Forward: all German chars → digraphs at once
    if has_german_chars:
        digraph_form = _to_digraph(token)
        if digraph_form != token:
            variants.append(digraph_form)
    # Reverse: each digraph position individually.
    # Skip if token already contains German chars — the author is using native
    # spelling, so digraphs like 'ss' in 'Schlüssel' are genuinely 'ss'.
    if not has_german_chars:
        for v in _digraph_variants(token):
            if v not in variants:
                variants.append(v)
    return variants


def _expand_near_german(near_expr: str) -> str:
    """Expand German character variants inside a NEAR() expression by OR-ing variant NEARs.

    NEAR(Größe Schlüssel, 10)
    → (NEAR(Größe Schlüssel, 10) OR NEAR(Groesse Schluessel, 10) OR ...)
    """
    # Parse: "NEAR(term1 term2 ..., N)"
    inner = near_expr[5:-1]  # strip NEAR( and )
    # Split off the distance parameter (last ", N" part)
    if "," in inner:
        terms_str, distance = inner.rsplit(",", 1)
        suffix = "," + distance
    else:
        terms_str = inner
        suffix = ""

    terms = terms_str.split()
    # Collect variants per term
    term_variants = []
    has_variants = False
    for t in terms:
        tvars = _token_variants(t)
        if tvars:
            term_variants.append([t] + tvars)
            has_variants = True
        else:
            term_variants.append([t])

    if not has_variants:
        return near_expr

    combos = list(product(*term_variants))
    nears = [f"NEAR({' '.join(combo)}{suffix})" for combo in combos]
    if len(nears) == 1:
        return nears[0]
    return "(" + " OR ".join(nears) + ")"


def _restore_near(query: str, saved: list[str]) -> str:
    """Put NEAR() expressions back, with German variants expanded."""
    for i, expr in enumerate(saved):
        query = query.replace(f"__NEAR{i}__", _expand_near_german(expr))
    return query


def _sanitize_query(query: str) -> str:
    """Quote tokens containing internal dots, hyphens, or commas for FTS5.

    Preserves trailing * for prefix search: EN-13445* becomes "EN-13445"*
    """
    tokens = re.findall(r'"[^"]*"|\S+', query)
    sanitized = []
    for t in tokens:
        if not (t.startswith('"') and t.endswith('"')) and re.search(r"[\.\-,]", t):
            suffix = ""
            if t.endswith("*"):
                t = t[:-1]
                suffix = "*"
            sanitized.append(f'"{t}"{suffix}')
        else:
            sanitized.append(t)
    return " ".join(sanitized)


def _expand_german(query: str) -> str:
    """Expand German character variants in query tokens so both spellings are found.

    FTS5 unicode61 does not treat ß/ss, ä/ae, ö/oe, ü/ue as equivalent,
    so searching for 'Größe' misses pages containing 'Groesse' and vice versa.
    This rewrites each affected token as (variant1 OR variant2 [OR ...]).
    """
    if not _has_german_content(query):
        return query

    # Tokenize preserving quoted phrases
    parts = re.findall(r'"[^"]*"|\S+', query)
    expanded = []
    for part in parts:
        # Skip FTS5 operators and NEAR placeholders
        if part in _FTS5_OPERATORS or part.startswith("__NEAR"):
            expanded.append(part)
            continue

        # Quoted phrase
        if part.startswith('"') and part.endswith('"'):
            inner = part[1:-1]
            variants = _token_variants(inner)
            if variants:
                all_forms = [part] + [f'"{v}"' for v in variants]
                expanded.append(f'({" OR ".join(all_forms)})')
            else:
                expanded.append(part)
            continue

        # Plain token (may include * for prefix search)
        variants = _token_variants(part)
        if variants:
            all_forms = [part] + variants
            expanded.append(f'({" OR ".join(all_forms)})')
        else:
            expanded.append(part)

    # FTS5 requires explicit AND when parenthesized groups are adjacent to
    # other terms.  Insert AND between a group and a non-operator neighbour.
    result = []
    for i, tok in enumerate(expanded):
        if i > 0 and expanded[i - 1] not in _FTS5_OPERATORS and tok not in _FTS5_OPERATORS:
            prev_is_group = expanded[i - 1].startswith("(")
            curr_is_group = tok.startswith("(")
            if prev_is_group or curr_is_group:
                result.append("AND")
        result.append(tok)

    return " ".join(result)


def prepare_query(query: str) -> str:
    """Full query preparation pipeline: preserve NEAR → sanitize → expand German chars."""
    query, saved_nears = _preserve_near(query)
    query = _sanitize_query(query)
    query = _expand_german(query)
    return _restore_near(query, saved_nears)
