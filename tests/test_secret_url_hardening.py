"""Security hardening tests for Issue #170 (LOW).

Two independent hardening fixes:

1. **Secret-at-rest.** ``builder.config.save_config`` writes the LLM provider
   API key to disk in plaintext. The file must be created with owner-only
   permissions (``0o600``) so the key is not group/world-readable, and the
   parent config directory must be ``0o700``.

2. **Lookup URL encoding.** User/lookup-derived values interpolated into a URL
   *path segment* must be percent-encoded so a value containing ``/``, spaces,
   ``#``, ``?``, ``&``, or ``..`` cannot break out of the intended path or
   inject extra query parameters. This pins the built request URL for each raw
   lookup client that interpolates a caller-supplied segment.

All HTTP is captured via the ``responses`` library — no network is touched.
"""

from __future__ import annotations

import stat
from urllib.parse import urlsplit

import responses

from builder import config
from lookups.aopwiki import lookup_aop
from lookups.cellosaurus import lookup_cellosaurus
from lookups.crossref import lookup_doi
from lookups.orcid import lookup_orcid


# ===========================================================================
# Part 1 — secret-at-rest: config file mode is 0o600
# ===========================================================================


class TestSecretAtRest:
    """``save_config`` must persist the secret with restrictive permissions."""

    def test_config_file_mode_is_0600_on_create(self, tmp_path, monkeypatch) -> None:
        """A freshly written config must be owner read/write only (0o600)."""
        cfg_dir = tmp_path / "vitro-crate"
        cfg_path = cfg_dir / "config.toml"
        monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
        monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)

        config.save_config({"openai": {"api_key": "sk-super-secret"}})

        assert cfg_path.exists()
        mode = stat.S_IMODE(cfg_path.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    def test_config_dir_mode_is_0700(self, tmp_path, monkeypatch) -> None:
        """The parent config dir must be owner-only too (no group/world)."""
        cfg_dir = tmp_path / "vitro-crate"
        cfg_path = cfg_dir / "config.toml"
        monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
        monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)

        config.save_config({"openai": {"api_key": "sk-super-secret"}})

        mode = stat.S_IMODE(cfg_dir.stat().st_mode)
        assert mode == 0o700, f"expected 0o700, got {oct(mode)}"

    def test_config_file_mode_is_0600_on_overwrite(self, tmp_path, monkeypatch) -> None:
        """An already-existing (loosely-permissioned) file is tightened on save."""
        cfg_dir = tmp_path / "vitro-crate"
        cfg_path = cfg_dir / "config.toml"
        cfg_dir.mkdir(parents=True)
        cfg_path.write_text('[openai]\napi_key = "old"\n')
        cfg_path.chmod(0o644)  # simulate a world-readable secret already on disk
        monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
        monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)

        config.save_config({"openai": {"api_key": "sk-new-secret"}})

        mode = stat.S_IMODE(cfg_path.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
        # Format/readers must still work — the new value round-trips.
        assert config.load_config()["openai"]["api_key"] == "sk-new-secret"


# ===========================================================================
# Part 2 — lookup URL encoding: reserved chars in path segments are encoded
# ===========================================================================


def _captured_url() -> str:
    """Return the URL of the single captured outbound request."""
    assert len(responses.calls) >= 1, "no HTTP request was captured"
    url = responses.calls[0].request.url
    assert url is not None, "captured request has no URL"
    return url


class TestCellosaurusURLEncoding:
    """A path-traversal-y accession must be percent-encoded into the path."""

    def setup_method(self) -> None:
        lookup_cellosaurus.cache_clear()

    @responses.activate
    def test_slash_and_dotdot_are_encoded(self) -> None:
        # Match any URL on the host; capture what was actually requested.
        responses.add(
            responses.GET,
            "https://api.cellosaurus.org/cell-line/CVCL_0027%2F..%2Fadmin",
            json={"Cellosaurus": {"cell-line-list": []}},
            status=200,
        )
        lookup_cellosaurus("CVCL_0027/../admin")

        url = _captured_url()
        # The slash must be percent-encoded, not left as a path separator.
        assert "/../" not in url
        assert "%2F" in url.upper()
        # The query must still be the single intended format param.
        assert urlsplit(url).query == "format=json"


class TestAOPWikiURLEncoding:
    """A non-numeric / reserved aop_id must not break out of the path."""

    def setup_method(self) -> None:
        lookup_aop.cache_clear()

    @responses.activate
    def test_reserved_aop_id_is_encoded(self) -> None:
        responses.add(
            responses.GET,
            "https://aopwiki.org/aops/610%2F..%2Fsecret.json",
            json={},
            status=200,
        )
        lookup_aop("610/../secret")

        url = _captured_url()
        assert "/../" not in url
        assert "%2F" in url.upper()
        assert url.endswith(".json")


class TestCrossrefURLEncoding:
    """DOIs keep their structural slashes but encode injection chars."""

    def setup_method(self) -> None:
        lookup_doi.cache_clear()

    @responses.activate
    def test_well_formed_doi_unchanged(self) -> None:
        # A normal DOI's slashes/dots are structural and must survive verbatim.
        responses.add(
            responses.GET,
            "https://api.crossref.org/works/10.1016/j.tox.2021.152898",
            json={"message": {"title": ["X"]}},
            status=200,
        )
        lookup_doi("10.1016/j.tox.2021.152898")

        url = _captured_url()
        assert url == "https://api.crossref.org/works/10.1016/j.tox.2021.152898"

    @responses.activate
    def test_query_injection_chars_are_encoded(self) -> None:
        responses.add(
            responses.GET,
            "https://api.crossref.org/works/10.1016/evil%3Fmailto%3Dx%40y.com",
            json={"message": {"title": ["X"]}},
            status=200,
        )
        lookup_doi("10.1016/evil?mailto=x@y.com")

        url = _captured_url()
        # No extra query param may be injected via the path segment.
        assert urlsplit(url).query == ""
        assert "?mailto" not in url
        assert "%3F" in url.upper()


class TestOrcidURLEncoding:
    """An ORCID iD with reserved chars must stay inside the /record path."""

    def setup_method(self) -> None:
        lookup_orcid.cache_clear()

    @responses.activate
    def test_reserved_orcid_id_is_encoded(self) -> None:
        responses.add(
            responses.GET,
            "https://pub.orcid.org/v3.0/0000%2F..%2Fadmin/record",
            json={"person": {}},
            status=200,
        )
        lookup_orcid("0000/../admin")

        url = _captured_url()
        assert "/../" not in url.replace("/record", "")
        assert "%2F" in url.upper()
        assert url.endswith("/record")
