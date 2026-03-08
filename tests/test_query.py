"""Unit tests for query.py — all pure functions, no fixtures needed."""

import pytest

from pdf_search_mcp.query import (
    _digraph_variants,
    _expand_german,
    _expand_near_german,
    _has_german_content,
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


# --- _has_german_content ---


class TestHasGermanContent:
    def test_native_chars(self):
        assert _has_german_content("Größe") is True

    def test_digraphs(self):
        assert _has_german_content("Groesse") is True

    def test_plain_english(self):
        assert _has_german_content("hello world") is False

    def test_bug_21_assess_false_positive(self):
        """'assess' contains 'ss' digraph → detected as German.
        Known false positive — harmless, just adds an unused OR variant."""
        assert _has_german_content("assess") is True

    def test_bug_21_true_false_positive(self):
        """'true' contains 'ue' digraph → detected as German. Same false positive."""
        assert _has_german_content("true") is True


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

    def test_no_german_content(self):
        assert _expand_german("bolt flange") == "bolt flange"

    def test_near_placeholder_skipped(self):
        result = _expand_german("__NEAR0__ Größe")
        assert "__NEAR0__" in result

    @pytest.mark.xfail(strict=True, reason="* detached from quoted token during German expansion")
    def test_bug_18_prefix_wildcard_with_german(self):
        """EN-13445* in a query with German triggers sanitize → '"EN-13445"*',
        then _expand_german tokenizes and the * gets detached."""
        query = _sanitize_query("EN-13445* Größe")
        result = _expand_german(query)
        # The * should stay attached to the quoted token
        assert '"EN-13445"*' in result


# --- prepare_query (public entry point) ---


class TestPrepareQuery:
    def test_plain_text_passthrough(self):
        assert prepare_query("bolt flange") == "bolt flange"

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
