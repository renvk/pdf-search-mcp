"""Unit tests for query.py — all pure functions, no fixtures needed.

The pipeline's contract is that prepare_query() output is valid FTS5 for
ANY input, so most tests assert two things: the expected transformation
and acceptance by a real FTS5 table (via _fts5_accepts).
"""

import sqlite3

import pytest

from pdf_search_mcp.query import (
    _balance_parens,
    _digraph_variants,
    _prepare_near,
    _preserve_near,
    _quote_term,
    _to_digraph,
    _token_variants,
    extract_terms,
    prepare_query,
)


def _fts5_accepts(query: str) -> bool:
    """True when a real FTS5 table parses the query without error."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(content)")
    try:
        conn.execute("SELECT count(*) FROM t WHERE t MATCH ?", (query,))
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


# --- _to_digraph ---


class TestToDigraph:
    def test_replaces_all_german_chars(self):
        assert _to_digraph("Größe") == "Groesse"

    def test_noop_on_plain_english(self):
        assert _to_digraph("hello") == "hello"

    def test_mixed_case_umlauts(self):
        result = _to_digraph("Ärger Über Öl")
        assert "Ae" in result
        assert "Ue" in result
        assert "Oe" in result

    def test_eszett(self):
        assert _to_digraph("Straße") == "Strasse"


# --- _digraph_variants ---


class TestDigraphVariants:
    def test_single_ss(self):
        # "Strasse" has one ss position → ["Straße"]
        variants = _digraph_variants("Strasse")
        assert "Straße" in variants

    def test_two_ss_positions(self):
        # "Aussendurchmesser" has two ss positions
        variants = _digraph_variants("Aussendurchmesser")
        assert "Außendurchmesser" in variants
        assert "Aussendurchmeßer" in variants
        assert len(variants) == 2

    def test_mixed_digraphs(self):
        # "Groesse" has oe and ss → ["Grösse", "Groeße"]
        variants = _digraph_variants("Groesse")
        assert "Grösse" in variants
        assert "Groeße" in variants

    def test_no_digraphs(self):
        assert _digraph_variants("hello") == []


# --- _token_variants ---


class TestTokenVariants:
    def test_forward_native_chars(self):
        # ä/ö/ü/ß → digraph form
        variants = _token_variants("Größe")
        assert "Groesse" in variants

    def test_reverse_digraphs(self):
        # "Groesse" → single-position replacements
        variants = _token_variants("Groesse")
        assert "Grösse" in variants
        assert "Groeße" in variants

    def test_no_reverse_when_native_chars_present(self):
        # "Schlüssel" has ü → forward only, no ss→ß
        variants = _token_variants("Schlüssel")
        assert "Schluessel" in variants
        # Should NOT contain a ß variant since native chars are present
        assert not any("ß" in v for v in variants)

    def test_no_variants(self):
        assert _token_variants("hello") == []


# --- _quote_term ---


class TestQuoteTerm:
    def test_hyphens_quoted(self):
        assert _quote_term("EN-13445") == '"EN-13445"'

    def test_dots_quoted(self):
        assert _quote_term("v2.1") == '"v2.1"'

    def test_commas_quoted(self):
        assert _quote_term("1,000") == '"1,000"'

    def test_colons_quoted(self):
        """Bug #Q1: unquoted '1:100' is FTS5 column-filter syntax and
        raised OperationalError('no such column: 1')."""
        assert _quote_term("1:100") == '"1:100"'

    def test_prefix_wildcard_preserved(self):
        assert _quote_term("EN-13445*") == '"EN-13445"*'

    def test_plain_word_unchanged(self):
        assert _quote_term("pressure") == "pressure"

    def test_german_word_unchanged(self):
        """Umlauts/eszett are word characters — no quoting needed."""
        assert _quote_term("Größe") == "Größe"

    def test_lone_star_dropped(self):
        """A bare '*' is an FTS5 'unknown special query' — must vanish."""
        assert _quote_term("*") == ""


# --- NEAR handling ---


class TestNearPreservation:
    def test_single_near_extracted(self):
        query, saved = _preserve_near("hello NEAR(foo bar, 5) world")
        assert "__NEAR0__" in query
        assert len(saved) == 1
        assert saved[0] == "NEAR(foo bar, 5)"

    def test_multiple_nears(self):
        query, saved = _preserve_near("NEAR(a b, 3) test NEAR(c d, 7)")
        assert "__NEAR0__" in query
        assert "__NEAR1__" in query
        assert len(saved) == 2

    def test_no_nears(self):
        query, saved = _preserve_near("pressure vessel")
        assert query == "pressure vessel"
        assert saved == []

    def test_whitespace_before_paren_extracted(self):
        """Bug #Q4b: 'NEAR (a b, 5)' was missed by preservation but caught
        by the relaxation skip — sanitization mangled it into literal terms."""
        query, saved = _preserve_near("NEAR (a b, 5)")
        assert saved == ["NEAR (a b, 5)"]


class TestPrepareNear:
    def test_without_german(self):
        assert _prepare_near("NEAR(bolt flange, 5)") == "NEAR(bolt flange, 5)"

    def test_german_variants_expanded(self):
        result = _prepare_near("NEAR(Größe Schlüssel, 10)")
        assert "OR" in result
        assert "Groesse" in result
        assert "Schluessel" in result

    def test_no_distance_param(self):
        result = _prepare_near("NEAR(Größe test)")
        assert "Groesse" in result

    def test_lowercase_keyword_uppercased(self):
        """Bug #Q4a: FTS5 only accepts uppercase NEAR; the old pipeline
        preserved 'near(...)' verbatim, guaranteeing a syntax error."""
        result = _prepare_near("near(bolt flange, 5)")
        assert result == "NEAR(bolt flange, 5)"
        assert _fts5_accepts(result)

    def test_inner_terms_quoted(self):
        """Bug #Q3: special-char terms inside NEAR were never auto-quoted,
        contradicting the tool docstring and failing with a syntax error."""
        result = _prepare_near("NEAR(13445-3 Anhang, 10)")
        assert '"13445-3"' in result
        assert _fts5_accepts(result)

    def test_empty_near_returns_empty(self):
        """No inner terms — the expression must vanish, not emit 'NEAR()'."""
        assert _prepare_near("NEAR(, 5)") == ""


# --- _balance_parens ---


class TestBalanceParens:
    def test_balanced_unchanged(self):
        assert _balance_parens(["(", "a", ")"]) == ["(", "a", ")"]

    def test_stray_closer_dropped(self):
        assert _balance_parens([")", "a"]) == ["a"]

    def test_unclosed_opener_closed(self):
        assert _balance_parens(["(", "a"]) == ["(", "a", ")"]

    def test_empty_group_removed(self):
        """'()' is an FTS5 syntax error — must be removed entirely."""
        assert _balance_parens(["(", ")"]) == []

    def test_nested_empty_groups_removed(self):
        assert _balance_parens(["(", "(", ")", ")"]) == []


# --- extract_terms (relaxation tokenizer) ---


class TestExtractTerms:
    def test_simple_terms(self):
        assert extract_terms("pressure vessels piping") == ["pressure", "vessels", "piping"]

    def test_quoted_phrase_kept_whole(self):
        """A quoted phrase counts as one droppable term."""
        assert extract_terms('"AD 2000" Merkblatt') == ['"AD 2000"', "Merkblatt"]

    def test_prefix_with_quotes(self):
        """Quoted prefix search preserved as single term."""
        assert extract_terms('"EN-13445"* design') == ['"EN-13445"*', "design"]

    def test_returns_none_for_and(self):
        """Explicit AND means the user structured the query — don't relax."""
        assert extract_terms("pressure AND vessels") is None

    def test_returns_none_for_or(self):
        assert extract_terms("pressure OR vessels") is None

    def test_returns_none_for_not(self):
        assert extract_terms("pressure NOT vessels") is None

    def test_returns_none_for_near(self):
        assert extract_terms("NEAR(pressure vessels, 5)") is None

    def test_returns_none_for_parens(self):
        """Parenthesized groups are structured queries — dropping a term
        from inside a group would change the user's intended logic."""
        assert extract_terms("(pressure OR vessels) design") is None

    def test_returns_none_for_empty(self):
        assert extract_terms("") is None

    def test_single_term(self):
        assert extract_terms("pressure") == ["pressure"]

    def test_case_sensitive_operators(self):
        """Lowercase 'and' is a regular word in FTS5, not an operator."""
        assert extract_terms("rock and roll") == ["rock", "and", "roll"]


# --- prepare_query (public entry point) ---


class TestPrepareQuery:
    def test_plain_text_without_digraphs(self):
        """Words with no digraph substrings pass through unchanged."""
        assert prepare_query("bolt flange") == "bolt flange"

    def test_english_words_with_digraphs_expanded(self):
        """English words containing digraph substrings get expanded."""
        result = prepare_query("pressure vessel")
        assert "OR" in result

    def test_hyphenated_and_german(self):
        result = prepare_query("EN-13445 Größe")
        # Hyphen should be quoted
        assert '"EN-13445"' in result
        # German expansion
        assert "Groesse" in result

    def test_near_with_german(self):
        result = prepare_query("NEAR(Größe test, 10)")
        assert "Groesse" in result

    def test_empty_string(self):
        assert prepare_query("") == ""

    def test_fts5_operators_preserved(self):
        result = prepare_query("Größe OR pressure")
        assert " OR " in result

    def test_apostrophe_stripped(self):
        """Bare apostrophes cause FTS5 syntax errors — must be replaced with
        spaces to match unicode61 tokenizer behavior during indexing."""
        result = prepare_query("Young's modulus")
        assert "'" not in result
        assert "Young" in result
        assert "modulus" in result

    def test_unicode_right_quote_stripped(self):
        """U+2019 right single quotation mark, common in PDFs."""
        result = prepare_query("Young’s modulus")
        assert "’" not in result
        assert "Young" in result

    def test_apostrophe_in_near(self):
        """Apostrophes inside NEAR expressions must also be stripped."""
        result = prepare_query("NEAR(Young's modulus, 5)")
        assert "'" not in result
        assert "NEAR" in result

    def test_prefix_wildcard_with_german(self):
        """Bug #18 regression: the * must stay attached to the quoted token
        when the query also triggers German expansion."""
        result = prepare_query("EN-13445* Größe")
        assert '"EN-13445"*' in result

    def test_quoted_phrase_expansion(self):
        result = prepare_query('"Größe"')
        assert '"Größe"' in result
        assert '"Groesse"' in result
        assert "OR" in result


class TestPrepareQueryAlwaysValidFts5:
    """Regression suite for the module invariant: prepare_query() output
    must parse as FTS5 for ANY input. Each case is a query that crashed
    or silently failed before the rewrite (review findings Q1-Q6, S1-S2).
    """

    CASES = [
        "Maßstab 1:100",                    # Q1: colon → 'no such column'
        "(pressure OR pipe)",               # Q2: group + one expandable token → unbalanced parens
        "(pressure OR vessel)",             # Q2: group + two expandable tokens
        "NEAR(13445-3 Anhang, 10)",         # Q3: unquoted special chars inside NEAR
        "near(Anhang Groesse, 5)",          # Q4a: lowercase near rejected by FTS5
        "NEAR (Anhang Groesse, 5)",         # Q4b: whitespace before paren skipped preservation
        '"Größe"* test',                    # Q5: quoted-prefix phrase + German expansion detached the star
        "nozzle NEAR(Schlüssel weite, 5)",  # Q6: restored group adjacent to term without AND
        'foo"',                             # S2: unterminated string
        "foo:bar",                          # S2: column filter
        "*foo",                             # S2: 'unknown special query'
        "EN 13445 (",                       # S1: stray paren was masked as 'no results'
        "Test \"unbalanced",                # stray quote mid-query
        "NEAR(, 5) test",                   # empty NEAR must not leave a dangling AND
        "((a) (b",                          # nested unbalanced groups
        ")(",                               # pure noise
        "a -b +c ^d",                       # FTS5 metacharacters
        # PR-review round 2: dangling operators (each rejected by FTS5 before)
        "pressure AND",                     # trailing operator after expansion group
        "OR pressure",                      # leading operator
        "a OR (",                           # operator left dangling by paren repair
        "a OR NEAR(, 5)",                   # operator left dangling by empty-NEAR drop
        "xyznonexistent *",                 # term that prepares to nothing -> dangling OR in relaxation
        "a AND OR b",                       # adjacent operators
        "( OR )",                           # operator-only group
        "a NOT",                            # NOT with no right operand
        "a AND ( OR ) AND b",               # group emptied by operator trim -> adjacent ANDs
        ") OR a",                           # stray closer exposing a leading operator
    ]

    @pytest.mark.parametrize("raw", CASES)
    def test_output_is_valid_fts5(self, raw):
        prepared = prepare_query(raw)
        if prepared:  # '' means nothing searchable — callers handle that
            assert _fts5_accepts(prepared), f"{raw!r} -> {prepared!r}"

    def test_quoted_prefix_phrase_star_stays_attached(self):
        """Bug #Q5: '"Größe"* test' must expand to quoted prefix variants,
        not detach the star into a bare token."""
        result = prepare_query('"Größe"* test')
        assert '"Größe"*' in result
        assert '"Groesse"*' in result

    def test_group_adjacent_to_term_gets_and(self):
        """Bug #Q6: FTS5 rejects 'term (group)' — explicit AND required."""
        result = prepare_query("nozzle NEAR(Schlüssel weite, 5)")
        assert "nozzle AND (" in result

    def test_trailing_operator_trimmed(self):
        """'pressure AND' previously expanded to '(...) AND' — rejected."""
        assert prepare_query("pressure AND") == "(pressure OR preßure)"

    def test_leading_operator_trimmed(self):
        assert prepare_query("OR bolt") == "bolt"

    def test_adjacent_operators_collapsed(self):
        assert prepare_query("bolt AND OR flange") == "bolt AND flange"

    def test_leading_not_passed_through(self):
        """NOT without a left operand is the documented invariant exception:
        trimming it would search exactly the term the user excluded, so it
        is passed through and rejected downstream with a clear error."""
        result = prepare_query("NOT bolt")
        assert result.startswith("NOT")
        assert not _fts5_accepts(result)

    def test_near_prefix_star_kept_in_variants(self):
        """Review minor: German variants inside NEAR dropped the prefix
        star ('Größe*' expanded to 'Groesse' instead of 'Groesse*')."""
        result = _prepare_near("NEAR(Größe* test, 5)")
        assert "Groesse*" in result
