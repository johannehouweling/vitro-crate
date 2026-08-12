"""Names and descriptions for licence URLs, derived from their structure.

RO-Crate wants a licence to be a described contextual entity, not a bare URL:
the profile asks a License entity for a ``name`` and a ``description``. We set
the licence ourselves, so the crate can carry that description instead of
shipping a URL and leaving the reader to resolve it.

The names here are DERIVED, not looked up per crate. Creative Commons encodes
the licence in the path — ``/licenses/by-nc-sa/4.0/`` is Attribution,
NonCommercial, ShareAlike at version 4.0 — so one parser covers the whole
family, including combinations no crate of ours has used yet. Licences outside
that family are matched by their canonical URL.

Descriptions state what the licence IS and point at the canonical text; they do
not paraphrase the terms. A summary of licence conditions that drifts from the
deed is worse than no summary, and the deed is one dereference away.
"""

from __future__ import annotations

import re

# The four Creative Commons condition codes, in the order CC itself names them.
_CC_CONDITIONS = {
    "by": "Attribution",
    "nc": "NonCommercial",
    "nd": "NoDerivatives",
    "sa": "ShareAlike",
}

# CC licence URLs: .../licenses/<codes>/<version>/ with optional jurisdiction and
# an optional /legalcode suffix. Both http and https are seen in the wild.
_CC_LICENSE = re.compile(
    r"^https?://creativecommons\.org/licenses/([a-z-]+)/(\d+\.\d+)/", re.IGNORECASE
)
_CC_ZERO = re.compile(
    r"^https?://creativecommons\.org/publicdomain/zero/(\d+\.\d+)/", re.IGNORECASE
)
_CC_MARK = re.compile(
    r"^https?://creativecommons\.org/publicdomain/mark/(\d+\.\d+)/", re.IGNORECASE
)

# Non-CC licences, keyed by the canonical URL with scheme and trailing slash
# stripped. Small on purpose: this is the tail, and a wrong licence name is a
# licensing error, so only exact well-known URLs are claimed.
_KNOWN: dict[str, str] = {
    "opensource.org/licenses/MIT": "MIT License",
    "opensource.org/license/mit": "MIT License",
    "www.apache.org/licenses/LICENSE-2.0": "Apache License 2.0",
    "opensource.org/licenses/Apache-2.0": "Apache License 2.0",
    "www.gnu.org/licenses/gpl-3.0.en.html": "GNU General Public License v3.0",
    "www.gnu.org/licenses/gpl-3.0": "GNU General Public License v3.0",
    "opensource.org/licenses/BSD-3-Clause": "BSD 3-Clause License",
    "www.eclipse.org/legal/epl-2.0": "Eclipse Public License 2.0",
    "opendatacommons.org/licenses/by/1-0": "Open Data Commons Attribution License v1.0",
    "opendatacommons.org/licenses/odbl/1-0": "Open Data Commons Open Database License v1.0",
}


def _cc_name(codes: str, version: str) -> str | None:
    """Build the CC display name from its codes.

    ``("by-nc-sa", "4.0")`` becomes "Creative Commons
    Attribution-NonCommercial-ShareAlike 4.0 International". Returns None when
    any code is not a CC condition, so an unknown path is declined rather than
    half-named.
    """
    parts = [c for c in codes.lower().split("-") if c]
    if not parts or any(p not in _CC_CONDITIONS for p in parts):
        return None
    # CC orders the conditions BY, NC, ND, SA regardless of how the URL spells them.
    ordered = [_CC_CONDITIONS[c] for c in _CC_CONDITIONS if c in parts]
    return f"Creative Commons {'-'.join(ordered)} {version} International"


def describe_license(url: str) -> dict[str, str] | None:
    """Return ``{"name": ..., "description": ...}`` for a licence URL, or None.

    None means "not recognised" — the caller should leave the licence as it is
    rather than invent a name for it. Anything that is not a URL (for instance
    the all-rights-reserved placeholder) is not a licence entity and returns
    None too.
    """
    value = (url or "").strip()
    if not value.lower().startswith(("http://", "https://")):
        return None

    name: str | None = None
    if match := _CC_LICENSE.match(value):
        name = _cc_name(match.group(1), match.group(2))
    elif match := _CC_ZERO.match(value):
        name = f"CC0 {match.group(1)} Universal Public Domain Dedication"
    elif match := _CC_MARK.match(value):
        name = f"Public Domain Mark {match.group(1)}"
    else:
        key = re.sub(r"^https?://", "", value).rstrip("/")
        name = _KNOWN.get(key)

    if not name:
        return None
    return {"name": name, "description": f"{name}. Full terms: {value}"}
