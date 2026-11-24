# D4: Format-agnostic core

The core reads no input format of its own: a deposit is files, and the readers in
`builder/tools/file_readers.py` turn each into text the agent reads. Every metadata file is a
generic source whatever its schema; no repository dialect is special-cased
(`tests/test_document_discovery_ranking.py`). One writer, `export_crate`, turns `CrateState`
into the crate.
