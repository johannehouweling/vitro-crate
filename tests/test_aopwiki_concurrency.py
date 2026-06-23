"""Concurrency tests for lookups/aopwiki.py (#62).

`lookup_aop` enriches each key event with an independent HTTP call. These calls
have no data dependency, so they are fetched with a bounded ThreadPoolExecutor
instead of a strictly-serial for-loop. This module asserts:

  * event details are fetched concurrently (calls overlap in time),
  * the assembled result is byte-identical to the deterministic serial path,
  * the per-host rate cap is still honoured (politeness preserved).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from lookups import _http, aopwiki

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _reset():
    aopwiki.lookup_aop.cache_clear()
    aopwiki._event_details.cache_clear()
    _http.reset_host_throttle()
    yield
    aopwiki.lookup_aop.cache_clear()
    aopwiki._event_details.cache_clear()
    _http.reset_host_throttle()


def _serial_assemble(aop_data: dict, event_details: dict[str, dict]) -> list[dict]:
    """Reference serial assembly used to prove results are identical.

    Mirrors the original strictly-serial logic: iterate buckets in order,
    de-dup by event id, then merge per-event details.
    """
    data = aop_data["aop"]
    events: list[dict] = []
    seen: set = set()
    for bucket in ("aop_mies", "aop_kes", "aop_aos"):
        for ev in data.get(bucket, []):
            eid = ev.get("event_id")
            if eid is None or eid in seen:
                continue
            seen.add(eid)
            entity = {
                "@id": aopwiki._event_url(eid),
                "@type": "KeyEvent",
                "name": ev.get("event", f"Event {eid}"),
                "eventType": aopwiki._EVENT_TYPE_LABEL.get(
                    ev.get("event_type", ""), ev.get("event_type", "")
                ),
                "identifier": str(eid),
                "url": f"{aopwiki._event_url(eid)}.json",
            }
            entity.update(event_details.get(str(eid), {}))
            events.append(entity)
    return events


class TestAopConcurrency:
    """lookup_aop fetches event details concurrently and deterministically."""

    def test_results_identical_to_serial(self, monkeypatch):
        """The concurrent assembly produces exactly the serial result/order."""
        aop_data = _load("aopwiki_aop610.json")

        # Per-event detail responses, keyed by id. Returned out of any order.
        details = {
            "888": {"short_name": "Binding", "biologicalOrganization": "Molecular"},
            "177": {"short_name": "Mito dysfunction", "biologicalOrganization": "Cellular"},
            "889": {"short_name": "DA degeneration", "biologicalOrganization": "Tissue"},
            "890": {"short_name": "Parkinsonism", "biologicalOrganization": "Individual"},
        }

        def fake_get_json(url, **kwargs):
            if url.endswith("/aops/610.json"):
                return aop_data
            # event detail url: .../events/<id>.json
            eid = url.rsplit("/", 1)[1].removesuffix(".json")
            return {
                "short_name": details[eid]["short_name"],
                "biological_organization": details[eid]["biologicalOrganization"],
            }

        monkeypatch.setattr(aopwiki, "http_get_json", fake_get_json)
        # Throttle off for speed; concurrency correctness is what we check here.
        monkeypatch.setattr(_http, "_HOST_MIN_INTERVAL", 0.0)

        result = aopwiki.lookup_aop("610")

        expected_events = _serial_assemble(aop_data, details)
        assert result["events"] == expected_events
        # Order is the deterministic pathway order: MIE, KEs, AO.
        assert [e["identifier"] for e in result["events"]] == ["888", "177", "889", "890"]
        # Per-event enrichment landed on the right event.
        by_id = {e["identifier"]: e for e in result["events"]}
        assert by_id["177"]["short_name"] == "Mito dysfunction"
        assert by_id["890"]["biologicalOrganization"] == "Individual"

    def test_event_details_fetched_concurrently(self, monkeypatch):
        """The 4 event-detail fetches overlap in time (not strictly serial)."""
        aop_data = _load("aopwiki_aop610.json")
        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_get_json(url, **kwargs):
            nonlocal active, max_active
            if url.endswith("/aops/610.json"):
                return aop_data
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.2)  # hold the "connection" open to force overlap
                eid = url.rsplit("/", 1)[1].removesuffix(".json")
                return {"short_name": f"e{eid}", "biological_organization": "x"}
            finally:
                with lock:
                    active -= 1

        monkeypatch.setattr(aopwiki, "http_get_json", fake_get_json)
        monkeypatch.setattr(_http, "_HOST_MIN_INTERVAL", 0.0)

        start = time.monotonic()
        result = aopwiki.lookup_aop("610")
        elapsed = time.monotonic() - start

        assert len(result["events"]) == 4
        # If serial, 4 events * 0.2s = 0.8s. Concurrent must be well under that.
        assert elapsed < 0.6, f"event details not concurrent, elapsed={elapsed:.3f}s"
        assert max_active >= 2, f"expected overlapping fetches, max_active={max_active}"

    def test_concurrent_fetch_respects_rate_cap(self, monkeypatch):
        """With the per-host throttle on, the event fetches are still spaced."""
        aop_data = _load("aopwiki_aop610.json")
        grant_times: list[float] = []
        lock = threading.Lock()

        def fake_get_json(url, **kwargs):
            if url.endswith("/aops/610.json"):
                return aop_data
            # Apply the real throttle, then record when this request "fires".
            _http.throttle_for_url(url)
            with lock:
                grant_times.append(time.monotonic())
            eid = url.rsplit("/", 1)[1].removesuffix(".json")
            return {"short_name": f"e{eid}", "biological_organization": "x"}

        monkeypatch.setattr(aopwiki, "http_get_json", fake_get_json)
        monkeypatch.setattr(_http, "_HOST_MIN_INTERVAL", 0.1)

        result = aopwiki.lookup_aop("610")
        assert len(result["events"]) == 4

        grant_times.sort()
        for earlier, later in zip(grant_times, grant_times[1:]):
            assert later - earlier >= 0.1 - 0.02, "event fetches violated the rate cap"
