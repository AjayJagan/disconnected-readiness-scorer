# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Claude Code skill (plugin) that scores a repository's readiness for disconnected / air-gapped OpenShift deployments. It is invoked as `/disconnected-score` from any RHOAI component repo. The skill definition lives in `skills/disconnected-score/SKILL.md`.

## Dependencies

Managed via `pyproject.toml` + [uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra dev          # install all dev dependencies
```

Runtime deps: `pyyaml` (required). Optional: `jinja2` (for Jinja2-based report rendering, falls back to built-in renderer).

## Testing

```bash
python -m pytest tests/ -v                                 # all tests
python -m pytest tests/test_image_manifest_complete.py -v        # single test file
python -m pytest tests/test_main.py::TestParseArgs -v      # single test class
python -m pytest tests/ -v --cov=. --cov-report=term       # with coverage
```

CI runs on Python 3.12 (`.github/workflows/ci.yml`). Codecov enforces 80% patch coverage.

Tests use `tmp_path` fixtures to create disposable repo layouts (Go files, YAML manifests, etc.) and assert on `RuleResult` / `Finding` fields. No external network calls or fixtures needed.

## Running the Orchestrator

`main.py` runs all (or selected) rules and produces the aggregate score:

```bash
python3 main.py /path/to/target/repo                     # all default rules
python3 main.py . --rules csv,tags                        # subset of rules
python3 main.py . --report json                           # JSON output
python3 main.py . --operator-path /tmp/opendatahub-operator  # pre-cloned operator
python3 main.py . --verbose                               # diagnostics + timing + files_checked in JSON
```

Rule aliases: `csv`, `tags`, `egress`, `python`, `params_env`, `manifest`. Exit code is 0 for READY, 1 for NOT READY.

## Running Rules Individually

Each rule is a standalone Python script with a `run(repo_root: str) -> RuleResult` entry point. Run any rule directly:

```bash
python3 rules/image_manifest_complete.py /path/to/target/repo
python3 rules/no_image_tags.py .
python3 rules/no_runtime_egress.py .
python3 rules/python_imports.py .
python3 rules/operator_manifest.py /tmp/opendatahub-operator
```

All rules output JSON to stdout with `rule`, `passed`, and `findings` fields.

## Architecture

**Orchestrator (`main.py`):** CLI entry point that imports rules as modules, runs them, computes the aggregate score, and renders output (console summary + markdown or JSON report). Handles the operator manifest lifecycle — only clones when `csv` or `params_env` detect a pattern needing cross-referencing, or when `manifest` is explicitly selected. Supports `--exceptions` to load exception rules that downgrade matching findings to info severity. Computes production scope once via `production_scope.compute_production_scope()` and passes it to all rules. Use `--no-production-scope` to disable. Supports `--verbose` / `-v` for detailed diagnostic output (per-step timing, file scan progress, config loading, production scope decisions). When combined with `--report json`, includes `files_checked` per rule in the JSON output. `ArchAnalyzerError` is raised when the arch-analyzer binary is missing or fails, caught in `main()` for clean error reporting.

**Shared types (`rules/common.py`):** `Finding`, `RuleResult`, `ProductionScope`, `Severity` enum, and `ConfigError` exception used by all rules, plus `get_tracked_files()`, `is_file_in_production_scope()`, `is_yaml_in_production_scope()`, and `is_non_production_overlay_file()`. The `Severity` enum (`blocker`/`info`) validates severity strings at `Finding` construction time via `__post_init__` — invalid severities raise `ValueError`. `ConfigError` is raised when a config file exists but cannot be read or parsed. Each rule uses a dual-import pattern: `try: from rules.common import ...` / `except ModuleNotFoundError: from common import ...` so standalone execution (`python3 rules/foo.py .`) works without the package being installed. The catch is deliberately narrow (`ModuleNotFoundError` only, not `ImportError`) to avoid masking other import errors such as misspelled symbols or circular imports. Tests import via the package path (`from rules.common import ...`).

**Rule engine pattern:** Every rule module under `rules/` exports a `run(repo_root) -> RuleResult` function. `RuleResult` is a dataclass with `rule` (name), `passed` (bool), and `findings` (list of `Finding`). Each `Finding` has `severity` (blocker/info), `file`, `line`, `image`, and `message`. Severity is binary: blocker (will/may break disconnected) or info (excluded file, configurable pattern, or informational).

**Rules:**

- `image_manifest_complete.py` — Auto-detects whether the target repo uses `RELATED_IMAGE_*` env vars (opendatahub-operator pattern) or static CSV `relatedImages`, then checks image completeness against the detected pattern. Accepts optional `manifest_env_vars` parameter — when provided by the orchestrator, cross-references the target repo's env vars against the authoritative operator manifest (blocker for invalid or stale vars). Filters scanned files to git-tracked only.
- `params_env.py` — Validates repos using the `params.env` + kustomize pattern. Requires kustomize binary. Validates the full wiring chain: params.env → kustomize configMap → rendered manifest → Go os.Getenv. Detects hardcoded images not sourced from params.env (blocker), unwired params.env keys (blocker), and orphan Go os.Getenv calls (blocker). Accepts optional `manifest_env_vars` for operator manifest cross-referencing.
- `params_env_utils.py` — Utility functions for params.env + kustomize validation, adapted from `verify-params-env-images.py`. Handles params.env parsing, overlay discovery, kustomize build, probe-based hardcoded image detection, configMapKeyRef wiring, and Go env var cross-referencing. Used by `params_env.py`.
- `operator_manifest.py` — Parses the opendatahub-operator source to build the authoritative image manifest via `build_manifest()`. Returns a dict (not RuleResult); the orchestrator adapts it via `adapt_manifest_result()`. When no `--operator-path` is provided, `main.py` uses `tempfile.TemporaryDirectory(prefix="odh-operator-")` and clones the operator repo there. Also extracts overlay paths from operator Go source (`parse_component_overlay_paths()`) to determine which kustomize overlays are actually deployed per platform — used by `params_env` to filter out non-production overlays.
- `no_image_tags.py` — Enforces `@sha256:` digest refs; rejects mutable tags. Detects three patterns: qualified images (`registry/org/name:tag`), `oci://` URIs without digest pin, and unqualified k8s images (`image: name:tag` in YAML `image:` fields). Source code and manifest files produce blocker severity. Skips directories managed by params.env + kustomize. Filters to git-tracked files only. HTTP/HTTPS URLs are excluded from image detection. Skips `package.json` files to avoid false positives from npm package references.
- `no_runtime_egress.py` — Detects outbound HTTP calls in Go/Python/TS/shell source. Distinguishes hardcoded URLs (blocker) from configurable ones (info). Also detects HuggingFace model downloads (`hf download`, `huggingface-cli download`) as always-blocker. Filters to git-tracked files only.
- `python_imports.py` — Validates Python deps against the known-bundled list. Checks requirements files, `setup.py`, `pyproject.toml`, and runtime `pip install` calls.

**Manifest cross-referencing:** When the orchestrator runs, it detects the target repo's image pattern. If env_var or params_env, it clones the opendatahub-operator, builds the authoritative manifest via `operator_manifest.build_manifest()`, and passes the env var set to `image_manifest_complete.run()`. For env_var: (A) image ref uses a RELATED_IMAGE var not in the manifest → blocker, (B) repo defines a var not in the manifest → blocker, (C) manifest vars not referenced in repo → info. For params_env: validates that RELATED_IMAGE vars mapped from params.env keys exist in the operator manifest.

**Production scope (`rules/production_scope.py`):** Reduces false positives by narrowing scanning to production source directories. Uses arch-analyzer's `dockerfiles[].copy_instructions[].original_sources` to identify which directories are COPYed into Docker images. Returns a `ProductionScope` with `production_dirs` (set of resolved directory paths). Rules downgrade findings from files outside `production_dirs` from blocker/warning to info. Applies to ALL file types (Go, Python, YAML, shell, etc). Returns `None` (full scan) when arch_data has no original_sources.

**Exclusion logic:** Test-path exclusion is handled centrally by `config/config.yaml`, not by individual rules. The exceptions mechanism downgrades matching blocker findings to info severity based on file path globs, covering test directories, CI config, build files, and lint rules. Rules emit findings at their natural severity; the orchestrator applies exceptions post-hoc. Exceptions may have an optional `expires` date (ISO 8601 `YYYY-MM-DD`); expired exceptions are skipped in `apply_exceptions()`, and exceptions expiring within 14 days are flagged in the report output. Use `--list-expiring` to list soon-to-expire exceptions without running a scan.

**Central config (`config/config.yaml`):** Single unified YAML with exception rules that apply to all scanned repos. Loaded by the orchestrator via `--exceptions` or defaults to `config/config.yaml`. JSON schema at `schemas/config.schema.json`.

**Configuration:** All configuration is managed centrally through `config/config.yaml`. No per-repository configuration files are supported. Repo-specific exceptions use the `repo` field in the central config to scope them to a single component.

**Report rendering:** `templates/report.md` uses Jinja2-style `{{ }}` placeholders. The orchestrator tries `import jinja2` first; falls back to a built-in micro-renderer that handles `{{ var }}`, `{{ var | upper }}`, and `{% for %}` blocks.

## Severity Levels

| Severity | Meaning                                                                    |
|----------|----------------------------------------------------------------------------|
| blocker  | Will or may break disconnected — must be fixed or granted an exception     |
| info     | Excluded file, configurable pattern, or informational — does not block     |

## Post-Change Checklist

After modifying rules, config, or architecture, update `README.md` and `AGENTS.md` to reflect the changes. Both files document rule behavior, config options, exceptions, and architecture — they must stay in sync with the code.

## Key Design Decisions

- The `image_manifest_complete` rule detects the image management pattern (env var vs static CSV) automatically rather than requiring config. Threshold: 5+ `RELATED_IMAGE_*` occurrences in Go source → env var pattern.
- `operator_manifest.py` shells out to `git clone --depth 1` (list form, no shell) to fetch the operator source. When no `--operator-path` is given, the orchestrator uses `tempfile.TemporaryDirectory()` for automatic cleanup. The repo URL is hardcoded to the upstream operator; it is never user-supplied.
- Optional `yaml` import: rules that parse YAML skip their checks if PyYAML is not installed.
