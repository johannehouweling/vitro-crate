"""openpyxl's dropped-extension warnings never reach the user.

Reading a depositor workbook printed

    UserWarning: Data Validation extension is not supported and will be removed

once per affected sheet. It describes openpyxl's in-memory model, not the file
on disk, and we never write a workbook back — so it reports nothing that
happened to anyone's data, and neither the user nor the agent can act on it.
"""

from __future__ import annotations

import warnings

import pytest

from builder.tools.file_readers import (
    _silence_openpyxl_extension_warnings,
    read_excel,
    read_excel_rows,
)

# The whole family openpyxl can emit (openpyxl.xml.constants.EXT_TYPES), all of
# them Excel presentation features that cannot affect a cell value.
EXTENSION_NAMES = [
    "Data Validation",
    "Conditional Formatting",
    "Sparkline Group",
    "Slicer List",
    "Protected Range",
    "Ignored Error",
    "Web Extension",
    "Timeline Ref",
    "Unknown",
]


@pytest.mark.parametrize("extension", EXTENSION_NAMES)
def test_every_extension_warning_is_silenced(extension):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # catch_warnings(record=True) resets the filters, so re-apply ours
        # inside the sandbox — this tests the filter, not the import order.
        _silence_openpyxl_extension_warnings()
        warnings.warn(
            f"{extension} extension is not supported and will be removed",
            UserWarning,
            stacklevel=1,
        )
    assert caught == []


def test_importing_the_module_arms_the_filter():
    """Nothing has to call the helper — importing the readers is enough.

    Checked by reloading inside a `catch_warnings` sandbox rather than by
    inspecting the live filters: pytest's own warnings plugin installs a fresh
    filter set around every test, so the registration done at interpreter start
    is not visible here. It persists normally in the running app, which is the
    case that matters and the reason this is import-time at all.
    """
    import importlib

    import builder.tools.file_readers as readers

    with warnings.catch_warnings():
        warnings.resetwarnings()
        importlib.reload(readers)
        assert any(
            entry[0] == "ignore"
            and entry[1] is not None
            and entry[1].search("Data Validation extension is not supported and will be removed")
            for entry in warnings.filters
        )


def test_unrelated_warnings_still_get_through():
    """The filter is a scalpel, not a blanket."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _silence_openpyxl_extension_warnings()
        warnings.warn("something the user really should see", UserWarning, stacklevel=1)
        warnings.warn("a deprecation that matters", DeprecationWarning, stacklevel=1)
    assert len(caught) == 2


def test_openpyxl_warnings_about_other_things_still_get_through():
    """Only the dropped-extension sentence is silenced."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _silence_openpyxl_extension_warnings()
        warnings.warn("Workbook contains no default style", UserWarning, stacklevel=1)
    assert len(caught) == 1


class TestRealWorkbooksStayQuiet:
    @pytest.fixture
    def workbook(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        path = tmp_path / "conditions.xlsx"
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "Conditions"
        sheet.append(["compound", "dose_uM", "timepoint_h"])
        sheet.append(["Amiodarone", 10, 24])
        sheet.append(["Chlorpyrifos", 25, 48])
        # A dropdown is the feature that produced the report in the first place.
        validation = openpyxl.worksheet.datavalidation.DataValidation(
            type="list", formula1='"Amiodarone,Chlorpyrifos"', allow_blank=True
        )
        sheet.add_data_validation(validation)
        validation.add("A2:A3")
        book.save(path)
        return path

    def test_read_excel_rows_emits_nothing(self, workbook):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _silence_openpyxl_extension_warnings()
            sheets = read_excel_rows(str(workbook))
        assert sheets is not None
        assert [w for w in caught if "extension is not supported" in str(w.message)] == []

    def test_read_excel_still_returns_the_values(self, workbook):
        """Silencing the warning must not silence the data."""
        text = read_excel(str(workbook))
        assert text is not None
        assert "Amiodarone" in text
        assert "Chlorpyrifos" in text
