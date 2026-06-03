# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Claude Code skill (plugin) that scores a repository's readiness for disconnected / air-gapped OpenShift deployments. It is invoked as `/disconnected-score` from any RHOAI component repo. The skill definition lives in `skills/disconnected-score/SKILL.md`.

## Dependencies

No `requirements.txt` — install test/dev dependencies directly:

```bash
pip install pytest pytest-cov pyyaml jinja2
```

`pyyaml` and `jinja2` are optional at runtime (rules/report degrade gracefully without them) but required for full test coverage.

## Testing

```bash
python -m pytest tests/ -v                                 # all tests
python -m pytest tests/test_csv_relatedimages.py -v        # single test file
python -m pytest tests/test_main.py::TestParseArgs -v      # single test class
python -m pytest tests/ -v --cov=. --cov-report=term       # with coverage
```

CI runs on Python 3.9 and 3.12 (`.github/workflows/ci.yml`). Codecov enforces 80% patch coverage.

Tests use `tmp_path` fixtures to create disposable repo layouts (Go files, YAML manifests, etc.) and assert on `RuleResult` / `Finding` fields. No external network calls or fixtures needed.

## Running the Orchestrator

`main.py` runs all (or selected) rules and produces the aggregate score:

```bash
python3 main.py /path/to/target/repo                     # all default rules
python3 main.py . --rules csv,tags                        # subset of rules
python3 main.py . --report json                           # JSON output
python3 main.py . --operator-path /tmp/opendatahub-operator  # pre-cloned operator
```

Rule aliases: `csv`, `tags`, `egress`, `python`, `params_env`, `manifest`. Exit code is 0 for READY, 1 for NOT READY.

## Running Rules Individually

Each rule is a standalone Python script with a `run(repo_root: str) -> RuleResult` entry point. Run any rule directly:

```bash
python3 rules/csv_relatedimages.py /path/to/target/repo
python3 rules/no_image_tags.py .
python3 rules/no_runtime_egress.py .
python3 rules/python_imports.py .
python3 rules/operator_manifest.py /tmp/opendatahub-operator
```

All rules output JSON to stdout with `rule`, `passed`, and `findings` fields.

## Architecture

**Orchestrator (`main.py`):** CLI entry point that imports rules as modules, runs them, computes the aggregate score, and renders output (console summary + markdown or JSON report). Handles the operator manifest lifecycle — only clones when `csv` or `params_env` detect a pattern needing cross-referencing, or when `manifest` is explicitly selected. Supports `--exceptions` to load exception rules that downgrade matching findings to info severity.

**Shared types (`rules/common.py`):** `Finding` and `RuleResult` dataclasses used by all rules, plus `get_tracked_files()` which returns the set of git-tracked files (used by rules to skip untracked files). Each rule uses a dual-import pattern: `try: from rules.common import ...` / `except ModuleNotFoundError: from common import ...` so standalone execution (`python3 rules/foo.py .`) works without the package being installed. The catch is deliberately narrow (`ModuleNotFoundError` only, not `ImportError`) to avoid masking other import errors such as misspelled symbols or circular imports. Tests import via the package path (`from rules.common import ...`).

**Rule engine pattern:** Every rule module under `rules/` exports a `run(repo_root) -> RuleResult` function. `RuleResult` is a dataclass with `rule` (name), `passed` (bool), and `findings` (list of `Finding`). Each `Finding` has `severity` (blocker/info), `file`, `line`, `image`, and `message`. Severity is binary: blocker (will/may break disconnected) or info (excluded file, configurable pattern, or informational).

**Rules:**

- `csv_relatedimages.py` — Auto-detects whether the target repo uses `RELATED_IMAGE_*` env vars (opendatahub-operator pattern) or static CSV `relatedImages`, then checks image completeness against the detected pattern. Accepts optional `manifest_env_vars` parameter — when provided by the orchestrator, cross-references the target repo's env vars against the authoritative operator manifest (blocker for invalid or stale vars). Filters scanned files to git-tracked only.
- `params_env.py` — Validates repos using the `params.env` + kustomize pattern. Requires kustomize binary. Validates the full wiring chain: params.env → kustomize configMap → rendered manifest → Go os.Getenv. Detects hardcoded images not sourced from params.env (blocker), unwired params.env keys (blocker), and orphan Go os.Getenv calls (blocker). Supports `.verify-params-env-ignore` for excluding keys. Accepts optional `manifest_env_vars` for operator manifest cross-referencing.
- `params_env_utils.py` — Utility functions for params.env + kustomize validation, adapted from `verify-params-env-images.py`. Handles params.env parsing, overlay discovery, kustomize build, probe-based hardcoded image detection, configMapKeyRef wiring, and Go env var cross-referencing. Used by `params_env.py`.
- `operator_manifest.py` — Parses the opendatahub-operator source to build the authoritative image manifest via `build_manifest()`. Returns a dict (not RuleResult); the orchestrator adapts it via `adapt_manifest_result()`. When no `--operator-path` is provided, `main.py` uses `tempfile.TemporaryDirectory(prefix="odh-operator-")` and clones the operator repo there.
- `no_image_tags.py` — Enforces `@sha256:` digest refs; rejects mutable tags. Source code and manifest files produce blocker severity; test/build/CI files produce info. Skips directories managed by params.env + kustomize. Filters to git-tracked files only. HTTP/HTTPS URLs are excluded from image detection.
- `no_runtime_egress.py` — Detects outbound HTTP calls in Go/Python/TS/shell source. Distinguishes hardcoded URLs (blocker) from configurable ones (info). Test files produce info severity. Build-time usage (Dockerfiles, Makefiles, CI) is excluded. Filters to git-tracked files only.
- `python_imports.py` — Validates Python deps against the known-bundled list. Checks requirements files, `setup.py`, `pyproject.toml`, and runtime `pip install` calls.

**Manifest cross-referencing:** When the orchestrator runs, it detects the target repo's image pattern. If env_var or params_env, it clones the opendatahub-operator, builds the authoritative manifest via `operator_manifest.build_manifest()`, and passes the env var set to `csv_relatedimages.run()`. For env_var: (A) image ref uses a RELATED_IMAGE var not in the manifest → blocker, (B) repo defines a var not in the manifest → blocker, (C) manifest vars not referenced in repo → info. For params_env: validates that RELATED_IMAGE vars mapped from params.env keys exist in the operator manifest.

**Exclusion logic (shared across rules):** Test files (`*_test.go`, `test/`, `testdata/`, `e2e/`), CI config (`.github/`, `.tekton/`), and lint rules (`semgrep.yaml`) are excluded from blocker-level findings — they produce `info` severity instead.

**Config files in `config/`:**

- `known_mirrors.yaml` — Approved registries and PyPI mirrors. Rules treat pulls from these as safe.
- `exceptions.yaml` — Exception rules that downgrade matching blocker findings to info severity. Supports matching by rule name (comma-separated), file path glob, image glob, message substring, and repo name. Each exception requires a reason. Loaded by the orchestrator via `--exceptions` or defaults to `config/exceptions.yaml`.

**Report rendering:** `templates/report.md` uses Jinja2-style `{{ }}` placeholders. The orchestrator tries `import jinja2` first; falls back to a built-in micro-renderer that handles `{{ var }}`, `{{ var | upper }}`, and `{% for %}` blocks.

## Severity Levels

| Severity | Meaning |
|----------|---------|
| blocker | Will or may break disconnected — must be fixed or granted an exception |
| info | Excluded file, configurable pattern, or informational — does not block |

## Key Design Decisions

- The `csv_relatedimages` rule detects the image management pattern (env var vs static CSV) automatically rather than requiring config. Threshold: 5+ `RELATED_IMAGE_*` occurrences in Go source → env var pattern.
- `operator_manifest.py` shells out to `git clone --depth 1` (list form, no shell) to fetch the operator source. When no `--operator-path` is given, the orchestrator uses `tempfile.TemporaryDirectory()` for automatic cleanup. The repo URL is hardcoded to the upstream operator; it is never user-supplied.
- Optional `yaml` import: rules that parse YAML gracefully degrade if PyYAML is not installed.
