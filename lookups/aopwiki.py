"""
AOP-Wiki lookup.

Returns the full Adverse Outcome Pathway graph for an AOP id from the AOP-Wiki
JSON API: the pathway itself, each of its key events (molecular initiating
events, key events, adverse outcomes), and the key event relationships that
connect them. Every node is identified by its resolvable AOP-Wiki @id so the
graph is machine-actionable.
"""

from __future__ import annotations

import functools
from concurrent.futures import ThreadPoolExecutor

from lookups._http import NOT_FOUND, TransientLookupError, http_get_json

_BASE = "https://aopwiki.org"

# Bound on concurrent AOP-Wiki event-detail fetches. The per-host throttle in
# lookups._http keeps us polite no matter how many workers run; this just caps
# how many sockets we open at once (#62).
_EVENT_FETCH_WORKERS = 6

# AOP-Wiki event_type -> human-readable label.
_EVENT_TYPE_LABEL = {
    "MolecularInitiatingEvent": "Molecular Initiating Event",
    "KeyEvent": "Key Event",
    "AdverseOutcome": "Adverse Outcome",
}


def _event_url(eid) -> str:
    return f"{_BASE}/events/{eid}"


def _rel_url(rid) -> str:
    return f"{_BASE}/relationships/{rid}"


@functools.lru_cache(maxsize=2048)
def _event_details(event_id: str) -> dict:
    """Best-effort per-event enrichment (short name, biological organization).

    Falls back to {} on any failure, so a slow/unavailable event endpoint never
    breaks the surrounding AOP lookup.

    Politeness toward AOP-Wiki is enforced by the per-host throttle inside
    ``http_get_json`` (lookups._http), so this is safe to call concurrently.
    """
    try:
        d = http_get_json(f"{_event_url(event_id)}.json")
        if d is NOT_FOUND:
            return {}
        out: dict = {}
        if d.get("short_name"):
            out["short_name"] = d["short_name"]
        if d.get("biological_organization"):
            out["biologicalOrganization"] = d["biological_organization"]
        return out
    except Exception:
        return {}


@functools.lru_cache(maxsize=256)
def lookup_aop(aop_id: str) -> dict:
    """Return the full AOP graph for the given AOP id.

    Args:
        aop_id: numeric AOP identifier, e.g. "610" or "42".

    Returns:
        A dict with three keys (or ``{}`` on failure):

        - ``"aop"``: the AdverseOutcomePathway entity — name, identifier, url and
          ``has_molecular_initiating_event`` / ``has_key_event`` /
          ``has_adverse_outcome`` / ``has_key_event_relationship`` link references.
        - ``"events"``: one KeyEvent entity per MIE / KE / AO, discriminated by
          ``eventType``.
        - ``"relationships"``: one KeyEventRelationship entity per relationship,
          linking its upstream and downstream events by @id.
    """
    aop_id = str(aop_id).strip()
    aop_url = f"{_BASE}/aops/{aop_id}"
    try:
        data = http_get_json(f"{aop_url}.json", timeout=15)
        if data is NOT_FOUND:
            return {}
        # AOP-Wiki JSON may nest the payload under an "aop" key.
        if isinstance(data, dict) and "aop" in data:
            data = data["aop"]

        # Events: MIEs + KEs + AOs, de-duplicated, in pathway order. Build the
        # base entities first (deterministic, single-threaded) so the final
        # order never depends on which detail fetch returns first.
        events: list[dict] = []
        seen: set = set()
        for bucket in ("aop_mies", "aop_kes", "aop_aos"):
            for ev in data.get(bucket, []):
                eid = ev.get("event_id")
                if eid is None or eid in seen:
                    continue
                seen.add(eid)
                events.append(
                    {
                        "@id": _event_url(eid),
                        "@type": "KeyEvent",
                        "name": ev.get("event", f"Event {eid}"),
                        "eventType": _EVENT_TYPE_LABEL.get(
                            ev.get("event_type", ""), ev.get("event_type", "")
                        ),
                        "identifier": str(eid),
                        "url": f"{_event_url(eid)}.json",
                    }
                )

        # Enrich each event with its per-event details. These calls are
        # independent and have no data dependency, so fetch them concurrently
        # with a bounded pool; the per-host throttle in http_get_json keeps us
        # polite. Merge results back by identifier so order stays deterministic
        # and the outcome is identical to the serial path (#62).
        if events:
            event_ids = [e["identifier"] for e in events]
            with ThreadPoolExecutor(
                max_workers=min(_EVENT_FETCH_WORKERS, len(event_ids))
            ) as pool:
                detail_map = dict(zip(event_ids, pool.map(_event_details, event_ids)))
            for entity in events:
                entity.update(detail_map[entity["identifier"]])

        # Key event relationships, linking upstream -> downstream events by @id.
        relationships: list[dict] = []
        for rel in data.get("relationships", []):
            rid = rel.get("relation")
            if rid is None:
                continue
            up, down = rel.get("upstream_event_id"), rel.get("downstream_event_id")
            entity = {
                "@id": _rel_url(rid),
                "@type": "KeyEventRelationship",
                "name": f"{rel.get('upstream_event', '')} → {rel.get('downstream_event', '')}",
                "identifier": str(rid),
                "url": f"{_rel_url(rid)}.json",
            }
            if up is not None:
                entity["upstream_event"] = {"@id": _event_url(up)}
            if down is not None:
                entity["downstream_event"] = {"@id": _event_url(down)}
            relationships.append(entity)

        def _refs(bucket: str) -> list[dict]:
            return [
                {"@id": _event_url(e["event_id"])}
                for e in data.get(bucket, [])
                if e.get("event_id") is not None
            ]

        aop: dict = {
            "@id": aop_url,
            "@type": "AdverseOutcomePathway",
            "name": data.get("title") or data.get("short_name") or f"AOP {aop_id}",
            "url": aop_url,
            "identifier": aop_id,
        }
        if data.get("short_name"):
            aop["alternateName"] = data["short_name"]
        if _refs("aop_mies"):
            aop["has_molecular_initiating_event"] = _refs("aop_mies")
        if _refs("aop_kes"):
            aop["has_key_event"] = _refs("aop_kes")
        if _refs("aop_aos"):
            aop["has_adverse_outcome"] = _refs("aop_aos")
        kers = [
            {"@id": _rel_url(r["relation"])}
            for r in data.get("relationships", [])
            if r.get("relation") is not None
        ]
        if kers:
            aop["has_key_event_relationship"] = kers

        return {"aop": aop, "events": events, "relationships": relationships}
    except TransientLookupError:
        raise
    except Exception:
        return {}
