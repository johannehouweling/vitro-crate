"""The checks read a folder the crate holds as a folder of the crate (#852).

A Study or an Assay that has a folder of its own is a directory Dataset in the
crate. A directory has no bytes of its own, so the payload check, which asks
whether the crate carries every file it describes, has nothing to look for. And
a directory in the crate is no directory on the web: the base profile asks one
on the web where an archive of it can be fetched, and a document validated in
memory has no place of its own, so the validator reads every folder of the
crate as one on the web. Neither may become a finding against the crate, and
the guidance may not ask a person for such an archive.
"""

from __future__ import annotations

from rocrate.model import Dataset
from rocrate.rocrate import ROCrate

from builder.tools.validation import verify_payload
from profiles.validator import validate_crate_dict


class TestThePayloadCheck:
    def test_a_directory_is_not_a_file_the_crate_fails_to_carry(self) -> None:
        crate = ROCrate()
        crate.add_dataset(dest_path="assay_01", properties={"name": "Assay"})

        assert verify_payload(crate) == []

    def test_a_file_without_content_in_that_directory_is_still_reported(self) -> None:
        crate = ROCrate()
        crate.add_dataset(dest_path="assay_01", properties={"name": "Assay"})
        crate.add_file(source=None, dest_path="assay_01/ghost.csv", properties={"name": "ghost"})

        assert [issue["entity_id"] for issue in verify_payload(crate)] == ["assay_01/ghost.csv"]


def _asked_for_a_distribution(issues: list, severity: str) -> list[str]:
    return sorted(
        str(issue.entity_id)
        for issue in issues
        if issue.severity == severity and str(issue.property).endswith("/distribution")
    )


class TestAFolderOfTheCrateIsNoDirectoryOnTheWeb:
    """A directory on the web SHOULD state where an archive of it can be fetched."""

    def _document(self) -> dict:
        crate = ROCrate()
        crate.root_dataset["name"] = "A crate"
        crate.add_dataset(dest_path="assay_01", properties={"name": "In the crate"})
        crate.add(Dataset(crate, "https://example.org/remote/", properties={"name": "On the web"}))
        return crate.metadata.generate()

    def test_only_the_directory_on_the_web_should_state_one(self) -> None:
        (result,) = validate_crate_dict(self._document(), severity="recommended", profile="base")

        assert _asked_for_a_distribution(result.issues, "recommended") == [
            "https://example.org/remote/"
        ]

    def test_a_folder_of_the_crate_still_may_state_one(self) -> None:
        """This finding is the same on disk, so it stays in the report."""
        (result,) = validate_crate_dict(self._document(), severity="optional", profile="base")

        assert "./assay_01/" in _asked_for_a_distribution(result.issues, "optional")


