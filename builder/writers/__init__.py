"""
Output writers for the ISA-Tox RO-Crate Builder.

Writers project a completed CrateState onto output formats. The agent does
not need to know about output formats — writers handle the conversion.

Modules:
    - rocrate_writer.py: Assembles ro-crate-metadata.json via ro-crate-py
    - arc_writer.py: Projects the ARC folder structure
"""

from __future__ import annotations