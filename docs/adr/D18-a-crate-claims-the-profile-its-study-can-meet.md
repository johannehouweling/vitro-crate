# D18: A crate claims the profile its study can meet

ISA-Tox requires an Investigation to include an Exposure, so a study that exposes nothing cannot
conform and must not claim to. `--profile isa` says so: `metadata.profile`, kept with the session,
default `isa-tox`. The claim alone decides:

- **what the crate states**: the root's `conformsTo` lists only the ISA profile, and ISA-Tox is not
  described;
- **what is judged**: `CrateState.layers` is what `profile="all"` means in every validation call;
  a pass named explicitly (`profile="tox"`) still runs;
- **what is shown**: `CrateState.conformance()` reports the claimed layers, and every surface (the
  report, TUI, dashboard, session status, build summaries) lists only those.

The ReAct brief tells the agent such a crate has no Exposure step. The claim is **never inferred**:
in an ISA-Tox build a missing Exposure is a gap the deposit must answer (#781), and a crate that
dropped the claim itself would read as conformant exactly when its evidence is thinnest.
