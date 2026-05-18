# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Claude Code skill (plugin) that scores a repository's readiness for disconnected / air-gapped OpenShift deployments. It is invoked as `/disconnected-score` from any RHOAI component repo. The skill definition lives in `SKILL.md`.

## Running the Orchestrator

`main.py` runs all (or selected) rules and produces the aggregate score:

```bash
python3 main.py /path/to/target/repo                     # all default rules
python3 main.py . --rules csv,tags                        # subset of rules
python3 main.py . --report json                           # JSON output
python3 main.py . --operator-path /tmp/opendatahub-operator  # pre-cloned operator
```

Rule aliases: `csv`, `tags`, `egress`, `python`, `manifest`. Exit code is 0 for READY/WARNING, 1 for NOT READY.

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

**Orchestrator (`main.py`):** CLI entry point that imports rules as modules, runs them, computes the aggregate score, and renders output (console summary + markdown or JSON report). Handles the operator manifest lifecycle — only clones when the `csv` rule detects the env_var pattern or when `manifest` is explicitly selected.

**Shared types (`rules/common.py`):** `Finding` and `RuleResult` dataclasses used by all rules. Each rule has a `try/except ImportError` fallback so standalone `python3 rules/foo.py .` still works without the package import.

**Rule engine pattern:** Every rule module under `rules/` exports a `run(repo_root) -> RuleResult` function. `RuleResult` is a dataclass with `rule` (name), `passed` (bool), and `findings` (list of `Finding`). Each `Finding` has `severity` (blocker/warning/info), `file`, `line`, `image`, and `message`.

**Rules:**

- `csv_relatedimages.py` — Auto-detects whether the target repo uses `RELATED_IMAGE_*` env vars (opendatahub-operator pattern) or static CSV `relatedImages`, then checks image completeness against the detected pattern. Accepts optional `manifest_env_vars` parameter — when provided by the orchestrator, cross-references the target repo's env vars against the authoritative operator manifest (blocker for invalid vars, warning for stale vars).
- `operator_manifest.py` — Parses the opendatahub-operator source to build the authoritative image manifest. Clones the operator repo to `/tmp/opendatahub-operator` if needed. Returns a dict (not RuleResult); the orchestrator adapts it via `adapt_manifest_result()`.
- `no_image_tags.py` — Enforces `@sha256:` digest refs; rejects mutable tags. Production manifest dirs escalate to blocker severity.
- `no_runtime_egress.py` — Detects outbound HTTP calls in Go/Python/TS/shell source. Distinguishes hardcoded URLs (blocker) from configurable ones (info). Build-time usage (Dockerfiles, Makefiles, CI) is excluded.
- `python_imports.py` — Validates Python deps against the known-bundled list. Checks requirements files, `setup.py`, `pyproject.toml`, and runtime `pip install` calls.

**Manifest cross-referencing:** When the orchestrator runs, it detects the target repo's image pattern. If env_var, it clones the opendatahub-operator, builds the authoritative manifest via `operator_manifest.build_manifest()`, and passes the env var set to `csv_relatedimages.run()`. The cross-reference produces three check types: (A) image ref uses a RELATED_IMAGE var not in the manifest → blocker, (B) repo defines a var not in the manifest → warning (stale), (C) manifest vars not referenced in repo → info.

**Exclusion logic (shared across rules):** Test files (`*_test.go`, `test/`, `testdata/`, `e2e/`), CI config (`.github/`, `.tekton/`), and lint rules (`semgrep.yaml`) are excluded from blocker-level findings — they produce `info` severity instead.

**Config files in `config/`:**

- `known_mirrors.yaml` — Approved registries and PyPI mirrors. Rules treat pulls from these as safe.
- `exceptions.yaml` — Per-repo rule exceptions for known false positives.

**Report rendering:** `templates/report.md` uses Jinja2-style `{{ }}` placeholders. The orchestrator tries `import jinja2` first; falls back to a built-in micro-renderer that handles `{{ var }}`, `{{ var | upper }}`, and `{% for %}` blocks.

## Severity Levels

| Severity | Meaning |
|----------|---------|
| blocker | Fails the score — must be fixed for disconnected readiness |
| warning | Needs manual review but doesn't fail |
| info | Excluded file or configurable pattern — informational only |

## Key Design Decisions

- The `csv_relatedimages` rule detects the image management pattern (env var vs static CSV) automatically rather than requiring config. Threshold: 5+ `RELATED_IMAGE_*` occurrences in Go source → env var pattern.
- `operator_manifest.py` shells out to `git clone --depth 1` (list form, no shell) to fetch the operator source. When no `--operator-path` is given, the orchestrator creates a unique temporary directory via `tempfile.mkdtemp()`. The repo URL is hardcoded to the upstream operator; it is never user-supplied.
- Optional `yaml` import: rules that parse YAML gracefully degrade if PyYAML is not installed.
