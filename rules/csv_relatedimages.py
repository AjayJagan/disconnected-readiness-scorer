#!/usr/bin/env python3
"""Check that all container image references are accounted for in disconnected manifests.

Supports two patterns:
1. Static CSV relatedImages — images listed in the ClusterServiceVersion YAML
2. RELATED_IMAGE_* env vars — operator injects images via environment variables at runtime

If the repo uses RELATED_IMAGE_* env vars (the opendatahub-operator pattern), the rule
checks that every image reference maps to a RELATED_IMAGE_* variable. If the repo uses
a static CSV, it checks that every image appears in relatedImages.

Test files, CI config, and semgrep rules are excluded from blocker findings.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    from rules.common import Finding, RuleResult
except ModuleNotFoundError:
    from common import Finding, RuleResult

IMAGE_REF_PATTERN = re.compile(
    r'(?:image:\s*|"image":\s*"|FROM\s+)'
    r'((?:[\w.\-]+(?:\.[\w.\-]+)+(?::\d+)?/)?[\w.\-]+/[\w.\-]+(?:[:@][\w.\-:]+)?)'
)

DIGEST_PATTERN = re.compile(r'@sha256:[a-f0-9]{64}')
TAG_PATTERN = re.compile(r':[\w][\w.\-]*$')
RELATED_IMAGE_PATTERN = re.compile(r'RELATED_IMAGE_[A-Z0-9_]+')

TEST_DIRS = {"test", "tests", "testdata", "e2e", "hack"}
TEST_SUFFIXES = {"_test.go", "_int_test.go", "_internal_test.go"}
CI_DIRS = {".github", ".tekton", "ci"}
SKIP_DIRS = {".git", "vendor", "node_modules", "__pycache__", ".tox"}
SKIP_FILES = {"semgrep.yaml", "semgrep.yml", ".semgrep.yml"}


def is_excluded_file(filepath: Path) -> bool:
    """Check if a file should be excluded from blocker findings."""
    if filepath.name in SKIP_FILES:
        return True
    if any(filepath.name.endswith(s) for s in TEST_SUFFIXES):
        return True
    if any(d in filepath.parts for d in TEST_DIRS | CI_DIRS):
        return True
    return False


def detect_image_pattern(repo_root: Path) -> str:
    """Detect whether the repo uses RELATED_IMAGE env vars or static CSV."""
    related_image_count = 0
    for go_file in repo_root.rglob("*.go"):
        if any(d in go_file.parts for d in SKIP_DIRS):
            continue
        try:
            content = go_file.read_text()
            related_image_count += len(RELATED_IMAGE_PATTERN.findall(content))
        except (OSError, UnicodeDecodeError):
            continue
        if related_image_count >= 5:
            return "env_var"

    for yaml_file in repo_root.rglob("*.yaml"):
        if any(d in yaml_file.parts for d in SKIP_DIRS):
            continue
        try:
            content = yaml_file.read_text()
            if "relatedImages:" in content and "ClusterServiceVersion" in content:
                return "static_csv"
        except (OSError, UnicodeDecodeError):
            continue

    return "unknown"


def extract_related_image_vars(repo_root: Path) -> set[str]:
    """Extract all RELATED_IMAGE_* env var names defined in Go source."""
    env_vars = set()
    for go_file in repo_root.rglob("*.go"):
        if any(d in go_file.parts for d in SKIP_DIRS):
            continue
        if is_excluded_file(go_file):
            continue
        try:
            content = go_file.read_text()
            for match in RELATED_IMAGE_PATTERN.finditer(content):
                var = match.group()
                if var != "RELATED_IMAGE_*":
                    env_vars.add(var)
        except (OSError, UnicodeDecodeError):
            continue
    return env_vars


def extract_static_related_images(repo_root: Path) -> set[str]:
    """Extract image refs from CSV relatedImages section."""
    try:
        import yaml
    except ImportError:
        return set()

    images = set()
    for yaml_file in repo_root.rglob("*.yaml"):
        if any(d in yaml_file.parts for d in SKIP_DIRS):
            continue
        try:
            content = yaml_file.read_text()
            if "relatedImages:" not in content:
                continue
            with open(yaml_file) as f:
                docs = list(yaml.safe_load_all(f))
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                spec = doc.get("spec", {})
                for entry in spec.get("relatedImages", []):
                    img = entry.get("image", "")
                    if img:
                        images.add(normalize_image(img))
        except Exception:
            continue
    return images


def normalize_image(ref: str) -> str:
    """Strip tag/digest for comparison."""
    ref = ref.strip().strip('"').strip("'")
    ref = DIGEST_PATTERN.sub("", ref)
    ref = TAG_PATTERN.sub("", ref)
    return ref


def scan_for_image_refs(repo_root: Path) -> list[tuple[Path, int, str]]:
    """Scan source files for container image references."""
    extensions = {".go", ".py", ".yaml", ".yml", ".json"}
    results = []

    for filepath in repo_root.rglob("*"):
        if any(d in filepath.parts for d in SKIP_DIRS):
            continue
        if filepath.suffix not in extensions and filepath.name != "Dockerfile":
            continue

        try:
            lines = filepath.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue

        for i, line in enumerate(lines, 1):
            for match in IMAGE_REF_PATTERN.finditer(line):
                img = match.group(1).strip().strip('"').strip("'")
                if "/" in img and not img.startswith("#"):
                    results.append((filepath, i, img))

    return results


def check_env_var_pattern(
    repo_root: Path,
    manifest_env_vars: set[str] | None = None,
) -> RuleResult:
    """Check repos that use RELATED_IMAGE_* env var pattern.

    When manifest_env_vars is provided (from operator_manifest), cross-references
    the target repo's env vars against the authoritative operator manifest.
    """
    result = RuleResult(rule="image-manifest-complete")
    local_vars = extract_related_image_vars(repo_root)

    if manifest_env_vars is not None:
        result.findings.append(Finding(
            severity="info",
            file="",
            line=0,
            image="",
            message=f"Repo uses RELATED_IMAGE_* pattern. "
                    f"Found {len(local_vars)} env vars in repo, "
                    f"validated against {len(manifest_env_vars)} authoritative vars "
                    f"from operator manifest.",
        ))
    else:
        result.findings.append(Finding(
            severity="info",
            file="",
            line=0,
            image="",
            message=f"Repo uses RELATED_IMAGE_* pattern. Found {len(local_vars)} env vars.",
        ))

    image_refs = scan_for_image_refs(repo_root)
    file_lines_cache: dict[Path, list[str]] = {}

    for filepath, line_num, image in image_refs:
        excluded = is_excluded_file(filepath)

        try:
            if filepath not in file_lines_cache:
                file_lines_cache[filepath] = filepath.read_text().splitlines()
            line_content = file_lines_cache[filepath][line_num - 1]
        except (OSError, IndexError):
            line_content = ""

        related_vars = RELATED_IMAGE_PATTERN.findall(line_content)

        if not related_vars:
            relative = str(filepath.relative_to(repo_root))
            severity = "info" if excluded else "warning"
            result.findings.append(Finding(
                severity=severity,
                file=relative,
                line=line_num,
                image=image,
                message=f"Image '{image}' has no RELATED_IMAGE_* mapping on this line. "
                        f"Verify it is covered by an env var elsewhere.",
            ))
        elif manifest_env_vars is not None:
            for var_name in related_vars:
                if var_name not in manifest_env_vars:
                    relative = str(filepath.relative_to(repo_root))
                    severity = "info" if excluded else "blocker"
                    if severity == "blocker":
                        result.passed = False
                    result.findings.append(Finding(
                        severity=severity,
                        file=relative,
                        line=line_num,
                        image=image,
                        message=f"Image references '{var_name}' which does not exist "
                                f"in the operator manifest. The operator will not inject "
                                f"this image in disconnected environments.",
                    ))

    if manifest_env_vars is not None:
        stale_vars = local_vars - manifest_env_vars
        for var in sorted(stale_vars):
            result.findings.append(Finding(
                severity="warning",
                file="",
                line=0,
                image="",
                message=f"Env var '{var}' found in repo but not in operator manifest. "
                        f"May be stale or renamed.",
            ))

        unused_manifest_vars = manifest_env_vars - local_vars
        if unused_manifest_vars:
            result.findings.append(Finding(
                severity="info",
                file="",
                line=0,
                image="",
                message=f"{len(unused_manifest_vars)} operator manifest vars not referenced "
                        f"in this repo (expected if this component uses a subset of images).",
            ))

    return result


def check_static_csv_pattern(repo_root: Path) -> RuleResult:
    """Check repos that use static CSV relatedImages."""
    result = RuleResult(rule="image-manifest-complete")
    related_images = extract_static_related_images(repo_root)

    if not related_images:
        result.findings.append(Finding(
            severity="warning",
            file="",
            line=0,
            image="",
            message="CSV found but relatedImages section is empty or unparseable.",
        ))

    image_refs = scan_for_image_refs(repo_root)

    for filepath, line_num, image in image_refs:
        normalized = normalize_image(image)
        excluded = is_excluded_file(filepath)

        if normalized and normalized not in related_images:
            relative = str(filepath.relative_to(repo_root))
            severity = "info" if excluded else "blocker"
            if severity == "blocker":
                result.passed = False
            result.findings.append(Finding(
                severity=severity,
                file=relative,
                line=line_num,
                image=image,
                message=f"Image '{image}' not found in CSV relatedImages.",
            ))

    return result


def run(repo_root: str, manifest_env_vars: set[str] | None = None) -> RuleResult:
    """Run the image manifest completeness rule.

    When manifest_env_vars is provided, the env_var pattern check will
    cross-reference against the authoritative operator manifest.
    """
    root = Path(repo_root)
    pattern = detect_image_pattern(root)

    if pattern == "env_var":
        return check_env_var_pattern(root, manifest_env_vars=manifest_env_vars)
    elif pattern == "static_csv":
        return check_static_csv_pattern(root)
    else:
        result = RuleResult(rule="image-manifest-complete")
        result.findings.append(Finding(
            severity="info",
            file="",
            line=0,
            image="",
            message="No RELATED_IMAGE_* env vars or CSV relatedImages found. "
                    "Cannot determine image management pattern for this repo.",
        ))
        return result


if __name__ == "__main__":
    import sys
    import json

    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    r = run(repo)
    print(json.dumps({
        "rule": r.rule,
        "passed": r.passed,
        "findings": [
            {"severity": f.severity, "file": f.file, "line": f.line,
             "image": f.image, "message": f.message}
            for f in r.findings
        ],
    }, indent=2))
