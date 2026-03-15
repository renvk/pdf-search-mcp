"""Unit tests for query.py — all pure functions, no fixtures needed."""

from pdf_search_mcp.query import (
    _digraph_variants,
    _expand_german,
    _expand_near_german,
    _preserve_near,
    _restore_near,
    _sanitize_query,
    _to_digraph,
    _token_variants,
    prepare_query,
)


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


# --- _sanitize_query ---


class TestSanitizeQuery:
    def test_hyphens_quoted(self):
        assert _sanitize_query("EN-13445") == '"EN-13445"'

    def test_dots_quoted(self):
        assert _sanitize_query("v2.1") == '"v2.1"'

    def test_prefix_wildcard_preserved(self):
        assert _sanitize_query("EN-13445*") == '"EN-13445"*'

    def test_already_quoted_unchanged(self):
        assert _sanitize_query('"EN-13445"') == '"EN-13445"'

    def test_plain_words_unchanged(self):
        assert _sanitize_query("pressure vessel") == "pressure vessel"

    def test_commas_quoted(self):
        assert _sanitize_query("1,000") == '"1,000"'


# --- _preserve_near / _restore_near ---


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

    def test_roundtrip_without_german(self):
        original = "NEAR(bolt flange, 5)"
        query, saved = _preserve_near(original)
        restored = _restore_near(query, saved)
        assert restored == original

    def test_roundtrip_with_german(self):
        # NEAR terms with German content get expanded via _expand_near_german
        original = "NEAR(Größe test, 10)"
        query, saved = _preserve_near(original)
        restored = _restore_near(query, saved)
        assert "OR" in restored
        assert "Groesse" in restored


# --- _expand_near_german ---


class TestExpandNearGerman:
    def test_with_german_variants(self):
        result = _expand_near_german("NEAR(Größe Schlüssel, 10)")
        assert "OR" in result
        assert "Groesse" in result
        assert "Schluessel" in result

    def test_no_variants(self):
        result = _expand_near_german("NEAR(bolt flange, 5)")
        assert result == "NEAR(bolt flange, 5)"

    def test_no_distance_param(self):
        result = _expand_near_german("NEAR(Größe test)")
        assert "Groesse" in result


# --- _expand_german ---


class TestExpandGerman:
    def test_single_german_term(self):
        result = _expand_german("Größe")
        assert "Größe" in result
        assert "Groesse" in result
        assert "OR" in result

    def test_multiple_terms(self):
        result = _expand_german("Größe Schlüssel")
        assert "AND" in result

    def test_fts5_operators_preserved(self):
        result = _expand_german("Größe OR Schlüssel")
        # OR should be preserved as operator, not duplicated
        assert result.count(" OR ") >= 1

    def test_quoted_phrase(self):
        result = _expand_german('"Größe"')
        assert '"Größe"' in result
        assert '"Groesse"' in result
        assert "OR" in result

    def test_english_words_with_digraphs_expanded(self):
        """With always-expand, English words containing digraph substrings (ss, ue)
        get German variants even though they are not German words."""
        result = _expand_german("pressure vessel")
        # "pressure" contains 'ss' and 'ue', "vessel" contains 'ss'
        assert "OR" in result

    def test_plain_english_without_digraphs(self):
        """Words with no digraph substrings pass through unchanged."""
        assert _expand_german("bolt flange") == "bolt flange"

    def test_near_placeholder_skipped(self):
        result = _expand_german("__NEAR0__ Größe")
        assert "__NEAR0__" in result

    def test_bug_18_prefix_wildcard_with_german(self):
        """EN-13445* in a query with German triggers sanitize → '"EN-13445"*',
        then _expand_german must keep * attached to the quoted token."""
        query = _sanitize_query("EN-13445* Größe")
        result = _expand_german(query)
        # The * should stay attached to the quoted token
        assert '"EN-13445"*' in result


# --- prepare_query (public entry point) ---


class TestPrepareQuery:
    def test_plain_text_without_digraphs(self):
        """Words with no digraph substrings pass through unchanged."""
        assert prepare_query("bolt flange") == "bolt flange"

    def test_english_words_with_digraphs_expanded(self):
        """English words containing digraph substrings now get expanded."""
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
        result = prepare_query("Young\u2019s modulus")
        assert "\u2019" not in result
        assert "Young" in result

    def test_apostrophe_in_near(self):
        """Apostrophes inside NEAR expressions must also be stripped."""
        result = prepare_query("NEAR(Young's modulus, 5)")
        assert "'" not in result
        assert "NEAR" in result
