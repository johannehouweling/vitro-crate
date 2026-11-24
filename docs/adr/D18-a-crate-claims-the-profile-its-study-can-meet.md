# D18: A crate claims the profile its study can meet

ISA-Tox requires an Investigation to include an Exposure, so a study that exposes
nothing (culture conditions, the characterisation of a cell model) cannot conform to
it and must not claim to. `--profile isa` is the depositor saying so:
`metadata.profile`, kept with the session, `isa-tox` unless stated. The claim decides
three things, and nothing else decides them:

- **what the crate states**: the root's `conformsTo` lists the ISA profile alone, and
  the ISA-Tox profile is not described in the crate;
- **what is judged**: `CrateState.layers` is what `profile="all"` means in every
  validation call, so the verdict and the fix loop cover RO-Crate and
  ISA. A pass asked for by name (`profile="tox"`) still runs;
- **what is shown**: `CrateState.conformance()` reports the claimed layers, and the
  report's matrix and cards, the TUI, the dashboard, the session status and the build
  summaries list those layers and no other.

The ReAct brief tells the agent such a crate has no Exposure step. The claim is never inferred from the crate: in an ISA-Tox build
a missing Exposure is a gap the deposit has to answer (#781), and a crate that
dropped the claim by itself would read as conformant exactly when its evidence is
thinnest.
