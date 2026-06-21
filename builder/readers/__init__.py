"""
Input readers for the ISA-Tox RO-Crate Builder.

Readers convert various input formats into a CrateState. The agent does not
need to know about input formats — readers handle the conversion.

Modules:
    - metadata_files.py: Reads README, .json, .yaml, .csv metadata files
    - existing_crate.py: Reconstructs CrateState from ro-crate-metadata.json
    - directory.py: Scans a directory and builds a CrateState from contents
"""

from __future__ import annotations
