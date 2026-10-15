"""Tests for config/settings.py's generic label-based service scoping
fields (ROADMAP P32, ADR 0043): tracked_label_keys and label_key_aliases.

Every test supplies ANTHROPIC_API_KEY/LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY
directly so resolve_api_keys' optional Secrets Manager fallbacks never
trigger — these tests are about the two new properties, not credential
resolution, and must not depend on AWS being reachable.
"""

from config.settings import Settings

_BASE_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "LANGFUSE_PUBLIC_KEY": "pk-test",
    "LANGFUSE_SECRET_KEY": "sk-test",
}


def _settings(monkeypatch, **extra_env):
    for k, v in {**_BASE_ENV, **extra_env}.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_tracked_label_keys_splits_and_strips_whitespace(monkeypatch):
    s = _settings(monkeypatch, TRACKED_LABEL_KEYS="apm-id, team , cost-center")
    assert s.tracked_label_keys == ["apm-id", "team", "cost-center"]


def test_tracked_label_keys_defaults_to_empty(monkeypatch):
    s = _settings(monkeypatch)
    assert s.tracked_label_keys == []


def test_tracked_label_keys_empty_string_is_empty_list_not_one_blank_entry(monkeypatch):
    s = _settings(monkeypatch, TRACKED_LABEL_KEYS="")
    assert s.tracked_label_keys == []


def test_label_key_aliases_parses_json_object(monkeypatch):
    s = _settings(monkeypatch, LABEL_KEY_ALIASES='{"apm": "apm-id"}')
    assert s.label_key_aliases == {"apm": "apm-id"}


def test_label_key_aliases_defaults_to_empty_dict(monkeypatch):
    s = _settings(monkeypatch)
    assert s.label_key_aliases == {}


def test_label_key_aliases_malformed_json_degrades_to_empty_dict(monkeypatch):
    # Never a startup failure (ADR 0043 Decision #6) -- this is a
    # convenience table for the Tier-1 fast path, not load-bearing config.
    s = _settings(monkeypatch, LABEL_KEY_ALIASES="not-json")
    assert s.label_key_aliases == {}


def test_label_key_aliases_non_object_json_degrades_to_empty_dict(monkeypatch):
    s = _settings(monkeypatch, LABEL_KEY_ALIASES="[1, 2, 3]")
    assert s.label_key_aliases == {}
