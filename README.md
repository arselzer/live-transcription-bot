# Live Transcription Bot

A self-hosted Zoom and Proton Meet recording and transcription tool. It keeps
original audio, screen video, snapshots and speaker observations independently
of live captions, summaries and optional text-to-speech. Hermes and calendar
scheduling are optional integrations.

The code uses a Python `src/` layout. Historical script names are thin
compatibility entrypoints; maintained implementations live under
[`src/live_transcript`](src/live_transcript). The prepared Linux browser VM,
FFmpeg, local speech services and hardware configuration remain deployment
prerequisites. This refactored source copy has offline regression and packaging
checks; it has not replaced or been validated against a live deployment.

## Source layout

| Package | Responsibility |
| --- | --- |
| `core` | Validated configuration and meeting options |
| `runtime` | Session ownership, supervision, deployment and postprocessing |
| `providers` | Zoom/Proton browser adapters, admission and bounded UI recovery |
| `capture` | Independent recorder and speaker observation |
| `transcription` | GPU/CPU routing, streaming, journals, language and quality checks |
| `storage` | Archive mirroring, reconstruction, evidence export and recovery |
| `notes` | Evidence-grounded summaries and meeting board |
| `presentation` | Caption/summary camera rendering |
| `speech` | Explicit TTS and virtual microphone control |
| `integrations` | Optional calendar and Hermes entrypoints |

Tests live in `tests/`; explicit operator drills and benchmarks live in `tools/`.
[The module map](docs/module-map.json) lists the compatibility names and their
implementations. The provider adapters still contain procedural UI logic; moving
modules does not establish immunity to provider changes or rewrite every legacy
implementation.

## Development

Use Python 3.11+ and an editable checkout:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check src tools tests build_backend.py _bootstrap.py
.venv/bin/ruff format --check src tools tests build_backend.py _bootstrap.py
.venv/bin/python tools/run_tests.py
.venv/bin/python tools/check_public_source.py
```

The test runner creates isolated configuration and temporary runtime paths,
clears deployment-specific caption environment overrides, and avoids implicit
home-directory glossary loading. It
does not load the operator's normal deployment configuration. Unit and fixture
success does not prove live admission, remote camera visibility or word accuracy.
Browser dependencies are optional (`pip install -e '.[browser]'`). CPU recognition
and research dependencies have separate `cpu` and `research` extras.

## Deployment

Copy [the example configuration](config.example.toml) to a private location and
supply your own VM, model, storage and service settings. Set `paths.root` to this
checkout. Keep `paths.state`, logs, recordings, models, voices and safety copies
outside the source tree where possible. No weights, recordings, credentials or
operator configuration are supplied.

```bash
.venv/bin/live-transcript --config /path/to/private-config.toml --json doctor
.venv/bin/live-transcript --config /path/to/private-config.toml start MEETING_INVITATION
```

The CLI supports `start`, `stop`, `status`, `doctor`, `say`, `verify`, `recover`,
`watchdog` and `calendar`. Existing script entrypoints remain available from the
checkout. The launcher builds an explicit source bundle for a prepared guest,
including the package and bootstrap scripts; it excludes configuration and
runtime data. Packaging installs the Python API and CLI; a checkout remains
necessary for the legacy shell launcher and guest deployment assets.

[Deployment instructions](docs/deployment.md) describe the Linux host, browser
VM, managed audio services and optional Hermes integration. Never replace an
active deployment merely because a refactoring test suite passes. Validate a
candidate in an authorized test environment before switching operations.

## Reliability and accuracy

Capture survives failures in transcription, summaries or presentation. Original
files are retained, short recording segments have explicit finalization and
checksums, and independent archive/safety workers report their own status.
Whisper results are checked for malformed timestamps and sustained repetition;
suspect sections get independent short retries using the full model. Rejected
candidates and both views of live caption evidence remain available for review.

Experimental voice clustering is disabled by default; enabling
`postprocess.diarization_enabled` requires the optional `diarization` dependencies
and an explicitly configured local model. Speaker names remain hypotheses.

Quality gates are heuristics. A mixed audio feed cannot guarantee transcription
of overlapping voices. Speaker labels derive from UI observations and can be
ambiguous. The caption/control TCP protocol requires a trusted isolated network;
it is not an authenticated public API.

## Preparing a repository

[`PUBLIC_SOURCE_FILES.txt`](PUBLIC_SOURCE_FILES.txt) is an explicit source
allowlist. Do not add the entire deployment directory to Git. The privacy checker
supports an external private policy containing identifiers that must never enter
source history:

```bash
.venv/bin/python tools/check_public_source.py --private-policy /path/to/private-policy.json
# Once a repository exists, check the exact index bytes before committing:
.venv/bin/python tools/check_public_source.py --staged --private-policy /path/to/private-policy.json
```

Source distributions and wheel/editable builds enforce the source allowlist
before calling the packaging backend. Unexpected source files and symlinked
source directories fail validation. Guest bundles publish atomically with
normalized owner/time metadata. Release source archives also strip local owner,
group and timestamp headers. Editable-install wheels and virtual environments
contain machine paths by design and must remain private.

Private policy contents are not part of this project. Findings report locations
and categories without echoing sensitive values. Automated checks complement
human review; they are not proof that all possible secrets have been recognized.
Before the first commit, choose the intended per-repository Git author name and
email. Source scanning does not redact author/committer metadata or Git history.
No repository or commit is needed to run these checks.

A project license has not been selected. Review project and third-party licensing
before public release.

## Documentation

- [Architecture](docs/architecture.md)
- [Configuration](docs/configuration.md)
- [Operations and recovery](docs/operations.md)
- [Testing](docs/testing.md)
- [Zoom admission](docs/zoom-admission.md)
- [Research notes](docs/research.md)
