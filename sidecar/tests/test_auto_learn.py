"""Tests for routers.text_processing.extract_new_tokens — vocab auto-learner."""
import pytest

from routers.text_processing import extract_new_tokens


# ── Token extraction ─────────────────────────────────────────────────────────

def test_no_new_tokens_when_identical():
    assert extract_new_tokens("hello world", "hello world") == []


def test_no_new_tokens_when_only_punctuation_added():
    # LLM added punctuation only — same tokens, no learning
    assert extract_new_tokens("hello world", "Hello, world.") == []


def test_extracts_new_domain_term():
    raw = "lets use kubernetes for deployment"
    cleaned = "Let's use Kubernetes for deployment with Helm charts."
    out = extract_new_tokens(raw, cleaned)
    # 'helm' and 'charts' (and 'with') are new; 'with' is a stopword variant.
    # 'with' is not in our stopwords list — but its length (4) qualifies and
    # 'with' is in our stopword set. Verify the actual filter behavior.
    assert "helm" in out
    assert "charts" in out


def test_filters_stopwords():
    raw = "go"
    cleaned = "I will be there after the meeting"
    out = extract_new_tokens(raw, cleaned)
    # Excluded: i (too short), will (stopword), there (stopword),
    # after (stopword), the (stopword), meeting (8 chars, NOT stopword)
    assert "meeting" in out
    assert "will" not in out
    assert "the" not in out
    assert "after" not in out


def test_filters_short_tokens():
    raw = "hi"
    cleaned = "Hi to you and me at the bar today"
    out = extract_new_tokens(raw, cleaned)
    # All <4 chars or stopwords excluded; "today" qualifies
    assert "today" in out
    assert "you" not in out
    assert "me" not in out


def test_case_insensitive_dedup():
    raw = "kubernetes"
    cleaned = "Kubernetes KUBERNETES kubernetes"
    out = extract_new_tokens(raw, cleaned)
    # All forms of 'kubernetes' are in raw → no new tokens
    assert out == []


def test_preserves_order_in_cleaned():
    raw = "test"
    cleaned = "test alpha bravo charlie"
    out = extract_new_tokens(raw, cleaned)
    assert out == ["alpha", "bravo", "charlie"]


def test_deduplicates_within_cleaned():
    raw = "test"
    cleaned = "alpha beta alpha beta gamma"
    out = extract_new_tokens(raw, cleaned)
    assert out == ["alpha", "beta", "gamma"]


def test_punctuation_stripped():
    raw = "hello"
    cleaned = "hello, world! it's; great:"
    out = extract_new_tokens(raw, cleaned)
    # 'world', 'great' qualify (≥4 chars, not stopwords)
    assert "world" in out
    assert "great" in out
    # "it's" tokenizes to "it" + "s" — both too short
    assert "it" not in out


def test_empty_inputs():
    assert extract_new_tokens("", "") == []
    assert extract_new_tokens("", "hello world") == ["hello", "world"]
    assert extract_new_tokens("hello world", "") == []


def test_numbers_in_tokens():
    raw = ""
    cleaned = "version 2024 release"
    # _tokenize regex: [A-Za-z][A-Za-z0-9]+ — must start with letter
    out = extract_new_tokens(raw, cleaned)
    # 'version' qualifies. '2024' starts with digit — excluded by regex.
    # 'release' qualifies.
    assert "version" in out
    assert "release" in out
    assert "2024" not in out
