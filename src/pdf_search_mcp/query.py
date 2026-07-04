"""FTS5 query preparation pipeline.

Tokenizes the raw query (quoted phrases, parentheses, barewords), then
transforms each token: apostrophe stripping, auto-quoting of terms with
non-word characters, German digraph expansion (ß↔ss, ä↔ae, ö↔oe, ü↔ue),
and NEAR() canonicalization (uppercase keyword, inner terms quoted and
expanded).  Digraph variants are always expanded — no language detection
heuristic.

Invariant: prepare_query() output is valid FTS5 MATCH syntax for any
input — stray quotes are dropped, parentheses are rebalanced, special
characters are quoted, and dangling AND/OR operators are trimmed.  One
deliberate exception: NOT without a left operand (FTS5's NOT is binary)
is passed through, because silently searching the term the user tried
to exclude would invert their intent — the downstream error is better.
"""

import re
from itertools import product

# FTS5 operators that should not be modified during query expansion.
# Case-sensitive: FTS5 only recognizes the uppercase forms; lowercase
# 'and'/'or'/'not' are ordinary search terms.
_FTS5_OPERATORS = frozenset({"AND", "OR", "NOT"})

# Pattern matching NEAR(...) expressions, tolerant of whitespace before
# the paren and lowercase keyword — both are canonicalized to NEAR(...)
# during preparation (FTS5 itself only accepts the uppercase, no-space form).
_NEAR_RE = re.compile(r"NEAR\s*\([^)]*\)", re.IGNORECASE)

# Query tokenizer: quoted phrase (optional trailing * for prefix search),
# a single parenthesis, or a bareword run.  Barewords cannot contain
# whitespace, parens, or quotes — so an unterminated quote is dropped
# rather than producing invalid FTS5.
_TOKEN_RE = re.compile(r'"[^"]*"\*?|[()]|[^\s()"]+')

# Apostrophe-like characters that FTS5's query parser rejects but its unicode61
# tokenizer silently strips during indexing.  Replacing with space aligns query
# tokenization with index tokenization.
_APOSTROPHES = str.maketrans({
    "'": " ",       # U+0027 ASCII apostrophe
    "’": " ",  # U+2019 right single quotation mark (common in PDFs)
})

# German special character → digraph (forward direction, always unambiguous)
_CHAR_TO_DIGRAPH = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"}
_GERMAN_CHARS = frozenset(_CHAR_TO_DIGRAPH)

# Digraph → German character (reverse direction, applied per-position)
_DIGRAPH_TO_CHAR = {"ae": "ä", "oe": "ö", "ue": "ü", "Ae": "Ä", "Oe": "Ö", "Ue": "Ü", "ss": "ß"}


def _preserve_near(query: str) -> tuple[str, list[str]]:
    """Extract NEAR() expressions, replacing them with placeholders.

    Placeholders (__NEAR0__, __NEAR1__, ...) are pure word characters, so
    the tokenizer and quoting pass leave them untouched.
    """
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


def _token_variants(token: str) -> list[str]:
    """Generate all German spelling variants for a token.

    Forward direction (ä→ae, ß→ss, etc.) replaces all chars at once — always
    correct.  Reverse direction (ae→ä, ss→ß, etc.) replaces each position
    individually, since digraphs like 'ss' may or may not represent a German
    special character.  Reverse is skipped when the token already contains
    German chars — the author is using native spelling, so digraphs like
    'ss' in 'Schlüssel' are genuinely 'ss'.
    """
    if any(c in token for c in _GERMAN_CHARS):
        return [_to_digraph(token)]
    return _digraph_variants(token)


def _quote_term(term: str) -> str:
    """Quote a bareword for FTS5 if it contains any non-word character.

    Inputs: a bareword without surrounding quotes; a trailing * is treated
    as the prefix operator and re-attached outside the quotes.
    Returns the term unchanged, quoted ("term"), or quoted-prefix ("term"*).
    Returns '' for terms that are empty after stripping the prefix star.

    Quoting any non-word character (not just dots/hyphens/commas/slashes)
    covers FTS5 metacharacters like ':' (column filter), '^' (initial
    match), and '*' in non-trailing position — unicode61 treats them all
    as token separators, so the quoted phrase matches the same pages.
    """
    suffix = ""
    if term.endswith("*"):
        term = term[:-1]
        suffix = "*"
    if not term:
        return ""
    if re.search(r"\W", term):
        return f'"{term}"{suffix}'
    return term + suffix


def _expand_token(token: str) -> str:
    """Quote a single term token and expand German variants into an OR group.

    Inputs: a bareword (optional trailing *) or a quoted phrase (optional
    trailing *).  Operators, parens, and NEAR placeholders are handled by
    the caller, never passed here.
    Returns the FTS5-safe form: the token itself, or '(form1 OR form2 ...)'
    when German variants exist.  Returns '' for empty/unquotable tokens.
    """
    if token.startswith('"'):
        # Quoted phrase, optionally with trailing * for prefix search
        if token.endswith('"*'):
            inner, suffix = token[1:-2], "*"
        else:
            inner, suffix = token[1:-1], ""
        if not inner:
            return ""
        variants = _token_variants(inner)
        if variants:
            forms = [f'"{inner}"{suffix}'] + [f'"{v}"{suffix}' for v in variants]
            return f'({" OR ".join(forms)})'
        return f'"{inner}"{suffix}'

    # Bareword: quote if needed, then expand variants of the raw word.
    # Variants are generated from the unquoted core so digraph positions
    # are found, then each variant is quoted independently.
    suffix = "*" if token.endswith("*") else ""
    core = token[:-1] if suffix else token
    if not core:
        return ""
    quoted = _quote_term(token)
    if not quoted:
        return ""
    variants = _token_variants(core)
    if variants:
        forms = [quoted] + [_quote_term(v + suffix) for v in variants]
        return f'({" OR ".join(forms)})'
    return quoted


def _prepare_near(near_expr: str) -> str:
    """Canonicalize and expand one NEAR() expression.

    Inputs: a string matched by _NEAR_RE — any case, optional whitespace
    before the paren, e.g. 'near (4200-3 Anhang, 10)'.
    Returns valid FTS5: keyword uppercased, inner terms quoted when they
    contain non-word characters, German variants expanded by OR-ing whole
    NEAR expressions:

      NEAR(Größe Schlüssel, 10)
      → (NEAR(Größe Schlüssel, 10) OR NEAR(Groesse Schluessel, 10) OR ...)

    Returns '' when no inner terms survive (e.g. 'NEAR(, 5)').
    """
    inner = near_expr[near_expr.index("(") + 1 : -1]
    # Split off the distance parameter — only when the tail is an integer,
    # so a comma inside a term is not mistaken for the distance separator.
    suffix = ""
    if "," in inner:
        terms_str, tail = inner.rsplit(",", 1)
        if tail.strip().isdigit():
            inner = terms_str
            suffix = ", " + tail.strip()

    terms = [_quote_term(t) for t in inner.split()]
    terms = [t for t in terms if t]
    if not terms:
        return ""

    # Variants per term: quote each variant like the original term and
    # keep the prefix star — 'Größe*' must expand to 'Groesse*', not 'Groesse'
    term_variants = []
    for t in terms:
        star = "*" if t.endswith("*") else ""
        bare = t.rstrip("*").strip('"')
        variants = [_quote_term(v + star) for v in _token_variants(bare)]
        term_variants.append([t] + [v for v in variants if v])

    combos = list(product(*term_variants))
    nears = [f"NEAR({' '.join(combo)}{suffix})" for combo in combos]
    if len(nears) == 1:
        return nears[0]
    return "(" + " OR ".join(nears) + ")"


def _restore_near(query: str, prepared: list[str]) -> str:
    """Put prepared NEAR() expressions back in place of their placeholders."""
    for i, expr in enumerate(prepared):
        query = query.replace(f"__NEAR{i}__", expr)
    return query


def _clean_operators(tokens: list[str]) -> list[str]:
    """Drop FTS5 operators that lack an operand on either side.

    Inputs: token list where operands are term tokens (including generated
    '(... OR ...)' groups and __NEARn__ placeholders) and parens are
    single-character tokens.
    Returns a new list where: AND/OR with no left operand (start of query,
    after another operator, after '(') are dropped — this also collapses
    adjacent operators; any operator with no right operand (end of query,
    before ')') is dropped.  NOT with a missing LEFT operand is kept on
    purpose: dropping it would search exactly the term the user excluded.
    """
    cleaned = []
    for tok in tokens:
        if tok in ("AND", "OR") and (
            not cleaned or cleaned[-1] in _FTS5_OPERATORS or cleaned[-1] == "("
        ):
            continue
        cleaned.append(tok)

    # Right-operand check runs reversed: kept[-1] is the token immediately
    # to the right of the current one
    kept = []
    for tok in reversed(cleaned):
        if tok in _FTS5_OPERATORS and (
            not kept or kept[-1] in _FTS5_OPERATORS or kept[-1] == ")"
        ):
            continue
        kept.append(tok)
    return list(reversed(kept))


def _balance_parens(tokens: list[str]) -> list[str]:
    """Drop unmatched ')' and close unmatched '(' so parens always balance.

    Inputs: token list where parens are single-character tokens.
    Returns a new token list with balanced parens and empty '()' groups
    removed — both are FTS5 syntax errors.
    """
    balanced = []
    depth = 0
    for tok in tokens:
        if tok == "(":
            depth += 1
        elif tok == ")":
            if depth == 0:
                continue  # stray closer — dropping it keeps the rest valid
            depth -= 1
        balanced.append(tok)
    balanced.extend(")" * depth)

    # Remove empty '()' groups, repeating because removal can create new ones
    while True:
        for i in range(len(balanced) - 1):
            if balanced[i] == "(" and balanced[i + 1] == ")":
                del balanced[i : i + 2]
                break
        else:
            return balanced


def extract_terms(query: str) -> list[str] | None:
    """Split a raw query into droppable terms for search relaxation.

    Inputs: raw query string (before prepare_query).
    Returns a list of term strings (quoted phrases kept whole, including
    quotes), or None when the query is structured — explicit FTS5
    operators (AND, OR, NOT), NEAR() expressions, or parentheses — and
    must not be relaxed.
    """
    if _NEAR_RE.search(query):
        return None
    tokens = _TOKEN_RE.findall(query)
    if any(t in _FTS5_OPERATORS or t in "()" for t in tokens):
        return None
    return tokens or None


def prepare_query(query: str) -> str:
    """Full query preparation pipeline.

    Inputs: any user query string.
    Returns valid FTS5 MATCH syntax (see module invariant), or '' when no
    searchable term survives.

    Steps: strip apostrophes → extract NEAR() to placeholders → tokenize →
    quote/expand each term → rebalance parens and trim dangling operators
    (to a fixpoint — each step can expose work for the other, e.g. dropping
    an operator can empty a group) → insert explicit AND around groups
    (FTS5 implicit AND does not span parenthesized groups) → restore
    NEAR() expressions.
    """
    query = query.translate(_APOSTROPHES)
    query, saved_nears = _preserve_near(query)
    prepared_nears = [_prepare_near(expr) for expr in saved_nears]

    tokens = []
    for raw in _TOKEN_RE.findall(query):
        if raw in _FTS5_OPERATORS or raw in "()":
            tokens.append(raw)
            continue
        m = re.fullmatch(r"__NEAR(\d+)__", raw)
        if m:
            # Drop placeholders whose NEAR() had no searchable terms —
            # restoring '' mid-query would leave a dangling operator
            if int(m.group(1)) < len(prepared_nears) and prepared_nears[int(m.group(1))]:
                tokens.append(raw)
            continue
        expanded = _expand_token(raw)
        if expanded:
            tokens.append(expanded)

    # Fixpoint: trimming an operator can empty a group ('( OR )'), and
    # removing an empty group can make two operators adjacent — each pass
    # strictly shrinks the list, so this terminates
    tokens = _balance_parens(tokens)
    while True:
        cleaned = _balance_parens(_clean_operators(tokens))
        if cleaned == tokens:
            break
        tokens = cleaned

    # FTS5 requires explicit AND when a parenthesized group (literal paren
    # token, generated OR-group, or NEAR placeholder — restored NEARs may
    # become groups) is adjacent to another term.
    def _is_group(tok: str) -> bool:
        return tok.startswith("(") or tok == ")" or tok.startswith("__NEAR")

    result = []
    for i, tok in enumerate(tokens):
        if (
            i > 0
            and tokens[i - 1] not in _FTS5_OPERATORS
            and tokens[i - 1] != "("
            and tok not in _FTS5_OPERATORS
            and tok != ")"
            and (_is_group(tokens[i - 1]) or _is_group(tok))
        ):
            result.append("AND")
        result.append(tok)

    prepared = " ".join(result)
    return _restore_near(prepared, prepared_nears).strip()
