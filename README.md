[![codecov](https://codecov.io/gh/opendatahub-io/disconnected-readiness-scorer/graph/badge.svg?token=XE1XU6SQPB)](https://codecov.io/gh/opendatahub-io/disconnected-readiness-scorer)

# Disconnected Readiness Scorer

Score a repository's readiness for deployment in disconnected / air-gapped OpenShift environments.

## Why this exists

Disconnected and air-gapped deployments are a core requirement for many Red Hat OpenShift AI customers — particularly in government, financial services, and regulated industries where clusters have no outbound internet access. Analysis of ~96 open JIRA issues across RHAIRFE, RHAISTRAT, and RHOAIENG revealed that **75% of disconnected failures fall into four patterns detectable by static analysis before code is merged**:

| Pattern | % of Issues | Rule |
|---------|-------------|------|
| Missing images from manifests | ~30% | `image-manifest-complete` |
| Hardcoded external dependencies | ~25% | `python-imports` |
| Image tags instead of digests | ~10% | `no-image-tags` |
| Runtime external URL calls | ~10% | `no-runtime-egress` |

Today, none of these are checked automatically — failures are only discovered during manual disconnected testing, often weeks or months after the breaking change was merged. This scanner catches them at PR time.

## Quick start

### As a Claude Code skill

```bash
/disconnected-score
```

Run from the root of any RHOAI component repository. The skill definition is in [SKILL.md](SKILL.md).

### As a CLI tool

```bash
python3 main.py /path/to/target/repo                        # all default rules
python3 main.py /path/to/target/repo --rules csv,tags        # subset of rules
python3 main.py /path/to/target/repo --report json           # JSON output
python3 main.py /path/to/target/repo --operator-path /tmp/opendatahub-operator  # pre-cloned operator
python3 main.py /path/to/target/repo --exceptions /path/to/exceptions.yaml      # custom exceptions
```

Exit code is `0` for READY, `1` for NOT READY.

### Individual rules

Each rule is a standalone script:

```bash
python3 rules/image_manifest_complete.py /path/to/target/repo
python3 rules/params_env.py /path/to/target/repo
python3 rules/no_image_tags.py /path/to/target/repo
python3 rules/no_runtime_egress.py /path/to/target/repo
python3 rules/python_imports.py /path/to/target/repo
python3 rules/operator_manifest.py /path/to/opendatahub-operator
```

All rules output JSON to stdout with `rule`, `passed`, and `findings` fields.

## Output

```
Disconnected Readiness Score: NOT READY

  FAIL     image-manifest-complete   2 blocker(s)
  PASS     no-image-tags             All checks passed
  PASS     no-runtime-egress         All checks passed
  PASS     python-imports-bundled    All checks passed

Blockers: 2 | Passed: 3
```

Reports are also generated as markdown (default) or JSON (`--report json`). Write to a file with `--output report.md`.

## Rules

### image-manifest-complete (alias: `csv`)

Checks that every container image referenced in code is accounted for in the disconnected manifest. Auto-detects whether the repo uses `RELATED_IMAGE_*` env vars (opendatahub-operator pattern) or static CSV `relatedImages`.

**Files scanned:** `.go`, `.py`, `.yaml`, `.yml`, `.json`, `.sh` (git-tracked only). Comments (`//`, `#`) are skipped. Directories managed by `params.env` + kustomize are skipped (covered by `params-env-wiring`).

**Severity logic:**

| Condition | Severity |
|-----------|----------|
| Image ref uses a `RELATED_IMAGE` var not in operator manifest | blocker |
| Hardcoded image with no `RELATED_IMAGE_*` wiring | blocker |
| Image near a related env var (same file/sibling) | info |
| Manifest vars not referenced in repo | info |

When the env var pattern is detected, the orchestrator clones the opendatahub-operator and cross-references against the authoritative manifest.

### params-env-wiring (alias: `params_env`)

Validates repos using the `params.env` + kustomize pattern. Checks the full wiring chain: `params.env` &rarr; kustomize configMap &rarr; rendered manifest &rarr; Go `os.Getenv`. Requires `kustomize` binary on PATH.

When `manifest_source` is available (from operator manifest mapping), uses the operator's kustomize folders as the source of truth: copies the entire manifest source folder to a temp dir, replaces all `params.env` image values with probe sentinels, then builds every `kustomization.yaml` in the copy. Any non-sentinel image in the rendered output is hardcoded and not wired through `params.env`. Falls back to `discover_overlays()` (scanning the repo for co-located `params.env` + `kustomization.yaml` dirs) when no manifest source mapping is available.

**Files scanned:** `params.env`, kustomize overlays, `.go` files (git-tracked only)

**Severity logic:**

| Condition | Severity |
|-----------|----------|
| Hardcoded image not sourced from params.env | blocker |
| Orphan Go `os.Getenv` call with no matching rendered manifest var | blocker |
| `RELATED_IMAGE_*` var mapped from params.env not in operator manifest | blocker |
| Unwired params.env key (defined but not consumed by kustomize) | info |
| Key listed in `.verify-params-env-ignore` | skipped |

Supports `.verify-params-env-ignore` for excluding keys. When the orchestrator provides operator manifest vars, cross-references mapped `RELATED_IMAGE_*` vars against the manifest.

### no-image-tags (alias: `tags`)

Enforces `@sha256:` digest refs; rejects mutable tags (`:latest`, `:v1.2.3`). Tags cannot be reliably mirrored in disconnected environments.

**Files scanned:** `.go`, `.py`, `.yaml`, `.yml`, `.json`, `.toml`, `Dockerfile`, `Containerfile` (git-tracked only). Directories managed by `params.env` + kustomize are skipped entirely.

**Severity logic:**

| Condition | Severity |
|-----------|----------|
| Tagged image in any scanned file | blocker |
| Image uses `@sha256:` digest | pass (not reported) |

HTTP/HTTPS URLs are excluded from image detection. `params.env` files are excluded.

### no-runtime-egress (alias: `egress`)

Detects outbound HTTP calls in runtime code that would fail in disconnected environments.

**Files scanned:** `.go`, `.py`, `.ts`, `.tsx`, `.sh` (git-tracked only). Comments (`//`, `#`) are skipped.

**Patterns detected:** `http.Get/Post/Do`, `requests.get`, `fetch()`, `axios`, `curl`, `wget`, `net.Dial`, and more.

**Severity logic:**

| Condition | Severity |
|-----------|----------|
| Hardcoded external URL (`https://api.example.com`) | blocker |
| Cluster-internal URL (`kubernetes.default.svc`, `*.svc.cluster.local`, `localhost`) | info |
| Configurable URL (via env var, config, `viper`, etc.) | info |
| Network call with no hardcoded URL (relative/variable) | info |

### python-imports (alias: `python`)

Validates Python dependencies against the known-bundled list. Packages not pre-installed in the disconnected environment will fail to install at runtime.

**Files scanned:** `requirements*.txt`, `setup.py`, `pyproject.toml`, `.py` files (for `pip install` calls) — git-tracked only

**Severity logic:**

| Condition | Severity |
|-----------|----------|
| Unbundled package in production requirements | blocker |
| Runtime `pip install` / `subprocess.run(["pip", ...])` call | blocker |
| Package from known PyPI mirror | pass |

### operator-manifest (alias: `manifest`)

Parses the opendatahub-operator source to build the authoritative image manifest (100+ `RELATED_IMAGE_*` env vars across 18 components). Not run by default — included when `csv` or `params_env` detect a pattern needing cross-referencing, or when explicitly selected with `--rules manifest`.

### production-scope

Not a rule itself, but a cross-cutting optimization for Go repos. Parses all Dockerfiles to find `go build` targets (supports multiple targets per Dockerfile), then runs `go list -deps -json` to compute the transitive dependency set. Files outside the production binary's import graph are downgraded from blocker to info. Only affects `.go` files; non-Go files use existing rule logic. Disabled with `--no-production-scope`.

When operator manifest source folder mapping is available, also scopes YAML files to the operator-referenced kustomize/helm graph.

## Scoring

| Score | Meaning |
|-------|---------|
| **READY** | All rules pass — no blocker findings |
| **NOT READY** | One or more blocker-level findings |

Severity levels for individual findings:

| Severity | Meaning |
|----------|---------|
| `blocker` | Will or may break disconnected — must be fixed or granted an exception |
| `info` | Excluded file, configurable pattern, or informational — does not block |

## Exception System

All policy-based exclusions (test dirs, CI dirs, build files, etc.) are configured in `config/exceptions.yaml` rather than hardcoded in rules. Exceptions downgrade matching blocker findings to **info** severity.

### Exception fields

| Field     | Required | Description |
|-----------|----------|-------------|
| `rule`    | yes      | Rule name, comma-separated list, or `*` for all rules |
| `reason`  | yes      | Why this exception exists |
| `path`    | no       | Glob pattern matched against finding file path |
| `paths`   | no       | List of glob patterns — matches if ANY pattern matches |
| `image`   | no       | Glob pattern matched against finding image ref |
| `message` | no       | Glob pattern matched against finding message |
| `repo`    | no       | Repository name filter (matches basename or `org/repo` form) |

Path patterns support `**/` prefix to match at any depth (e.g. `**/Dockerfile` matches both `Dockerfile` and `build/Dockerfile`).

### Default exceptions

The default `config/exceptions.yaml` includes:

- **Test directories:** `test/`, `tests/`, `testdata/`, `testing/`, `e2e/`, `mocks/`, `contract-tests/`, `hack/`
- **Test file suffixes:** `*_test.go`, `test_*.py`, `*.test.ts`, `*.spec.ts`
- **CI directories:** `.github/`, `.tekton/`, `ci/`
- **Non-production:** `docs/`, `examples/`
- **Build files:** `**/Dockerfile`, `**/*.Dockerfile`, `**/Containerfile`
- **Lint config:** `**/semgrep.yaml`, `**/semgrep.yml`
- **OLM scorecard:** `config/scorecard/` (image rules only)

### Custom exceptions

Override with `--exceptions /path/to/file.yaml`:

```yaml
exceptions:
  - rule: no-runtime-egress
    repo: opendatahub-io/odh-dashboard
    paths:
      - "frontend/src/services/**"
    reason: "Frontend services call the backend BFF, not external endpoints"

  - rule: "image-manifest-complete, no-image-tags"
    path: "config/scorecard/**"
    reason: "OLM scorecard config — test images, not production"
```

### Skipped directories

The following directories are always fully skipped (no findings at all): `.git`, `vendor`, `node_modules`, `__pycache__`, `.tox`, `.devcontainer`.

## Configuration

### `config/known_mirrors.yaml`

Approved internal registries and PyPI mirrors. Rules treat pulls from these as safe.

```yaml
registries:
  - registry.redhat.io
  - brew.registry.redhat.io
  - quay.io/opendatahub
  - quay.io/modh

pypi_mirrors:
  - https://pypi.corp.redhat.com/simple/
```

### `.verify-params-env-ignore`

Opt-out file for the `params_env` rule. Place it in the target repo root to exclude specific `params.env` keys from validation. Keys listed here skip probe checks and wiring validation.

```yaml
- key: odh_notebook_controller_image
  reason: "Managed externally by the operator, not needed in params.env wiring"

- key: odh_trustyai_service_image
  reason: "Component deprecated, will be removed in next release"
  reference: "https://issues.redhat.com/browse/RHOAIENG-12345"
```

Each entry requires `key` and `reason`. The optional `reference` field can link to a tracking issue.

## PR Integration

The primary use case for this tool is running it against Pull Requests in RHOAI component repositories, catching disconnected-breaking changes **before they merge** rather than weeks later during manual testing.

### GitHub Actions (recommended)

Add a workflow to each target repository that runs the scorer on every PR:

```yaml
# .github/workflows/disconnected-readiness.yml
name: Disconnected Readiness Check

on:
  pull_request:
    branches: [main]

jobs:
  disconnected-score:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Clone disconnected-readiness-scorer
        run: git clone --depth 1 https://github.com/opendatahub-io/disconnected-readiness-scorer.git /tmp/scorer

      - name: Install kustomize
        run: |
          curl -s "https://raw.githubusercontent.com/kubernetes-sigs/kustomize/master/hack/install_kustomize.sh" | bash
          sudo mv kustomize /usr/local/bin/

      - name: Install dependencies
        run: pip install pyyaml

      - name: Run disconnected readiness check
        run: python3 /tmp/scorer/main.py . --report json --output disconnected-report.json

      - name: Upload report
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: disconnected-readiness-report
          path: disconnected-report.json
```

The workflow exits with code `1` when blocker-level findings are present, which will fail the PR check.

### Targeted rules per repository

Not every rule applies to every repo. Use `--rules` to run only the relevant checks:

| Repository type                             | Recommended rules              |
|---------------------------------------------|--------------------------------|
| Operators using `RELATED_IMAGE_*` env vars  | `csv,tags,manifest`            |
| Components using `params.env` + kustomize   | `params_env,tags,egress`       |
| Python ML components (e.g., model serving)  | `python,tags,egress`           |
| Go services / controllers                   | `csv,tags,egress`              |
| Frontend / dashboard                        | `egress`                       |

Example for a Python-heavy repo:

```yaml
      - name: Run disconnected readiness check
        run: python3 /tmp/scorer/main.py . --rules python,tags,egress
```

### PR comment reporting

To post results as a PR comment instead of just failing the check, pipe the markdown report into the GitHub CLI:

```yaml
      - name: Run disconnected readiness check
        id: score
        run: |
          python3 /tmp/scorer/main.py . --report markdown --output disconnected-report.md || true

      - name: Post PR comment
        if: github.event_name == 'pull_request'
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          {
            echo "## Disconnected Readiness Report"
            echo ""
            cat disconnected-report.md
          } | gh pr comment ${{ github.event.pull_request.number }} --body-file -
```

### Running locally on a PR branch

To check a PR branch before pushing:

```bash
# From the component repo, on your PR branch
python3 /path/to/disconnected-readiness-scorer/main.py .
```

Or using the Claude Code skill from the component repo root:

```bash
/disconnected-score
```

## Development

### Dependencies

```bash
pip install pytest pytest-cov pyyaml jinja2
```

`pyyaml` and `jinja2` are optional at runtime (rules degrade gracefully) but required for full test coverage.

### Running tests

```bash
python -m pytest tests/ -v                                 # all tests
python -m pytest tests/test_image_manifest_complete.py -v  # single file
python -m pytest tests/test_main.py::TestParseArgs -v      # single class
python -m pytest tests/ -v --cov=. --cov-report=term       # with coverage
```

CI runs on Python 3.9 and 3.12. Codecov enforces 80% patch coverage.

## License

Internal Red Hat / AI First Initiative.
