"""Tests for the Frictionless payload/data-content validation layer (Issue #95).

This is a SEPARATE layer from the SHACL metadata validator:

- SHACL (``profiles/validator.py``) checks the *metadata descriptor* — the
  structure/semantics of ``ro-crate-metadata.json``.
- Frictionless (``validate_table``) checks the *payload* — do the rows in a CSV
  actually match the declared CSVW/Frictionless ``tableSchema`` (column types,
  constraints, foreign keys to ``MolecularEntity`` / ``Sample`` ids)?

Issues come back in the same routable shape as #87
(``{entity_id, property, message, fix, severity, profile}``) so the agent can
route a fix the same way it routes a SHACL issue.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from builder.tools.data_content import validate_table

# A Frictionless/CSVW table schema mirroring the condition table's typed columns
# (cell_line: string, compound: string, concentration: number).
_CONDITION_SCHEMA = {
    "fields": [
        {"name": "cell_line", "type": "string"},
        {"name": "compound", "type": "string"},
        {"name": "concentration", "type": "number"},
    ]
}


def _write_csv(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


class TestTypeViolation:
    def test_type_violating_cell_yields_structured_issue(self, tmp_path):
        """A non-numeric value in a `number` column yields a data-content issue."""
        csv = _write_csv(
            tmp_path,
            "cond.csv",
            "cell_line,compound,concentration\n"
            "HepG2,Aspirin,1.5\n"
            "HepG2,Aspirin,notanumber\n",
        )

        result = validate_table(csv, _CONDITION_SCHEMA)

        assert result["ok"] is False
        assert result["issues"], "expected at least one data-content issue"
        issue = result["issues"][0]
        # Same routable shape as #87.
        assert set(issue) >= {
            "entity_id",
            "property",
            "message",
            "fix",
            "severity",
            "profile",
        }
        # The data-content layer is its own profile, distinct from base/isa/tox.
        assert issue["profile"] == "data"
        # Routed to the offending column.
        assert issue["property"] == "concentration"
        assert "notanumber" in issue["message"]

    def test_conformant_table_passes(self, tmp_path):
        """A table whose rows all match the schema produces no issues."""
        csv = _write_csv(
            tmp_path,
            "cond.csv",
            "cell_line,compound,concentration\n"
            "HepG2,Aspirin,1.5\n"
            "A549,Paracetamol,2.0\n",
        )

        result = validate_table(csv, _CONDITION_SCHEMA)

        assert result["ok"] is True
        assert result["issues"] == []


class TestForeignKeys:
    def test_unknown_foreign_key_value_flagged(self, tmp_path):
        """A compound id not in the allowed MolecularEntity ids is flagged."""
        csv = _write_csv(
            tmp_path,
            "cond.csv",
            "cell_line,compound,concentration\n"
            "sample_hepg2,chem_aspirin,1.5\n"
            "sample_hepg2,chem_unknown,2.0\n",
        )

        result = validate_table(
            csv,
            _CONDITION_SCHEMA,
            foreign_keys={"compound": ["chem_aspirin"]},
        )

        assert result["ok"] is False
        assert any(
            "chem_unknown" in issue["message"] for issue in result["issues"]
        ), "the dangling foreign-key value should be reported"

    def test_known_foreign_key_values_pass(self, tmp_path):
        csv = _write_csv(
            tmp_path,
            "cond.csv",
            "cell_line,compound,concentration\n"
            "sample_hepg2,chem_aspirin,1.5\n",
        )

        result = validate_table(
            csv,
            _CONDITION_SCHEMA,
            foreign_keys={
                "compound": ["chem_aspirin"],
                "cell_line": ["sample_hepg2"],
            },
        )

        assert result["ok"] is True
        assert result["issues"] == []


class TestRobustness:
    def test_missing_file_returns_error_not_raises(self, tmp_path):
        result = validate_table(str(tmp_path / "nope.csv"), _CONDITION_SCHEMA)
        assert result["ok"] is False
        assert result["issues"] or result.get("error")

    def test_invalid_schema_returns_error_not_raises(self, tmp_path):
        csv = _write_csv(tmp_path, "cond.csv", "a\n1\n")
        # A non-dict schema is a programming error surfaced as a tool error,
        # never an unhandled exception that would crash the agent loop.
        result = validate_table(csv, "not a schema")  # ty: ignore[invalid-argument-type]
        assert result["ok"] is False


class TestRegistration:
    def test_validate_table_registered_as_tool(self):
        import builder.tools.data_content  # noqa: F401  (triggers registration)
        from builder.tools.registry import TOOL_REGISTRY

        assert "validate_table" in TOOL_REGISTRY
        spec = TOOL_REGISTRY.get_spec("validate_table")
        assert spec is not None
        # It validates a file on disk against a schema; it does not need CrateState.
        assert spec.takes_state is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
