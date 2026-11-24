# D3: The session state is the persistence format, the crate is its output

`sessions/<id>/crate_state.json` (a serialized `CrateState`) is what a session saves and what
`--resume` reads; nothing is rebuilt from a crate's `ro-crate-metadata.json`, and there is no
crate reader (D13). A partial crate is still valid output: a missing field is an unfilled field,
reported by validation and the maturity report, never filled in to look complete.
