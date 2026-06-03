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
    from rules.common import Finding, RuleResult, get_tracked_files
except ModuleNotFoundError:
    from common import Finding, RuleResult, get_tracked_files

IMAGE_REF_PATTERN = re.compile(
    r'(?:'
    r'image:\s*'
    r'|"image":\s*"'
    r'|FROM\s+'
    r'|newName:\s*'
    r'|imageUrl:\s*'
    r'|image_url:\s*'
    r')'
    r'((?:[\w.\-]+(?:\.[\w.\-]+)+(?::\d+)?/)?[\w.\-]+/[\w.\-]+(?:[:@][\w.\-:]+)?)'
)

GO_IMAGE_ASSIGN_PATTERN = re.compile(
    r'(?:'
    r'[:=]\s*"'
    r"|export\s+\w+=\s*"
    r')'
    r'([\w.\-]+\.[\w.\-]+(?::\d+)?/[\w.\-]+/[\w.\-]+[:@][\w.\-:]+)'
)

DIGEST_PATTERN = re.compile(r'@sha256:[a-f0-9]{64}')
TAG_PATTERN = re.compile(r':[\w][\w.\-]*$')
RELATED_IMAGE_PATTERN = re.compile(r'RELATED_IMAGE_[A-Z0-9_]+')

NON_REGISTRY_DOMAINS = {
    "github.com", "gitlab.com", "bitbucket.org",
    "golang.org", "google.golang.org", "gopkg.in",
    "k8s.io", "sigs.k8s.io",
    "openshift.io",
}

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


def _build_file_related_image_map(
    file_lines_cache: dict[Path, list[str]],
) -> tuple[dict[Path, set[str]], dict[Path, set[str]]]:
    """Build maps of RELATED_IMAGE vars at file and directory level.

    Returns:
        file_vars: filepath -> set of RELATED_IMAGE vars in that file
        dir_vars:  directory -> union of RELATED_IMAGE vars across all files in that dir
    """
    file_vars: dict[Path, set[str]] = {}
    dir_vars: dict[Path, set[str]] = {}
    for filepath, lines in file_lines_cache.items():
        vars_in_file: set[str] = set()
        full_content = "\n".join(lines)
        for match in RELATED_IMAGE_PATTERN.finditer(full_content):
            var = match.group()
            if var != "RELATED_IMAGE_*":
                vars_in_file.add(var)
        if vars_in_file:
            file_vars[filepath] = vars_in_file
            if not is_excluded_file(filepath):
                parent = filepath.parent
                if parent not in dir_vars:
                    dir_vars[parent] = set()
                dir_vars[parent] |= vars_in_file
    return file_vars, dir_vars


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


def scan_for_image_refs(repo_root: Path, tracked: set[Path] | None = None) -> list[tuple[Path, int, str]]:
    """Scan source files for container image references."""
    extensions = {".go", ".py", ".yaml", ".yml", ".json", ".sh"}
    results = []

    for filepath in repo_root.rglob("*"):
        if tracked is not None and filepath.resolve() not in tracked:
            continue
        if any(d in filepath.parts for d in SKIP_DIRS):
            continue
        if filepath.suffix not in extensions and filepath.name != "Dockerfile":
            continue

        try:
            lines = filepath.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue

        for i, line in enumerate(lines, 1):
            seen: set[str] = set()
            for pattern in (IMAGE_REF_PATTERN, GO_IMAGE_ASSIGN_PATTERN):
                for match in pattern.finditer(line):
                    img = match.group(1).strip().strip('"').strip("'")
                    domain = img.split("/")[0].split(":")[0]
                    if (
                        "/" in img
                        and not img.startswith("#")
                        and domain not in NON_REGISTRY_DOMAINS
                        and img not in seen
                    ):
                        seen.add(img)
                        results.append((filepath, i, img))

    return results


def check_env_var_pattern(
    repo_root: Path,
    manifest_env_vars: set[str] | None = None,
    tracked: set[Path] | None = None,
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

    image_refs = scan_for_image_refs(repo_root, tracked=tracked)
    file_lines_cache: dict[Path, list[str]] = {}

    dirs_with_refs: set[Path] = set()
    for filepath, _ln, _img in image_refs:
        if filepath not in file_lines_cache:
            try:
                file_lines_cache[filepath] = filepath.read_text().splitlines()
            except (OSError, UnicodeDecodeError):
                file_lines_cache[filepath] = []
        dirs_with_refs.add(filepath.parent)

    for d in dirs_with_refs:
        for go_file in d.glob("*.go"):
            if go_file not in file_lines_cache:
                try:
                    file_lines_cache[go_file] = go_file.read_text().splitlines()
                except (OSError, UnicodeDecodeError):
                    relative = str(go_file.relative_to(repo_root))
                    result.findings.append(Finding(
                        severity="info",
                        file=relative,
                        line=0,
                        image="",
                        message=f"Could not read sibling file '{relative}'; "
                                f"its RELATED_IMAGE_* vars (if any) were not considered.",
                    ))

    file_related_vars, dir_related_vars = _build_file_related_image_map(
        file_lines_cache,
    )

    for filepath, line_num, image in image_refs:
        excluded = is_excluded_file(filepath)

        try:
            line_content = file_lines_cache[filepath][line_num - 1]
        except (IndexError, KeyError):
            line_content = ""

        related_vars = RELATED_IMAGE_PATTERN.findall(line_content)

        if not related_vars:
            file_vars = file_related_vars.get(filepath, set())
            dir_vars = (
                dir_related_vars.get(filepath.parent, set())
                if not file_vars
                else set()
            )

            relative = str(filepath.relative_to(repo_root))
            nearby_vars = file_vars or (dir_vars if filepath.suffix == ".go" else set())
            if manifest_env_vars is not None and nearby_vars:
                nearby_vars = nearby_vars & manifest_env_vars

            if nearby_vars:
                nearby_source = "file" if file_vars else "sibling"
                result.findings.append(Finding(
                    severity="info",
                    file=relative,
                    line=line_num,
                    image=image,
                    message=f"Image '{image}' has no same-line RELATED_IMAGE_* mapping, "
                            f"but {nearby_source} contains {', '.join(sorted(nearby_vars))}. "
                            f"Likely covered by env var injection.",
                ))
            else:
                severity = "info" if excluded else "blocker"
                if severity == "blocker":
                    result.passed = False
                result.findings.append(Finding(
                    severity=severity,
                    file=relative,
                    line=line_num,
                    image=image,
                    message=f"Image '{image}' has no RELATED_IMAGE_* mapping on this line. "
                            f"Will not be mirrored in disconnected environments.",
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
            result.passed = False
            result.findings.append(Finding(
                severity="blocker",
                file="",
                line=0,
                image="",
                message=f"Env var '{var}' found in repo but not in operator manifest. "
                        f"Operator will not inject this image in disconnected environments.",
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


def check_static_csv_pattern(repo_root: Path, tracked: set[Path] | None = None) -> RuleResult:
    """Check repos that use static CSV relatedImages."""
    result = RuleResult(rule="image-manifest-complete")
    related_images = extract_static_related_images(repo_root)

    if not related_images:
        result.passed = False
        result.findings.append(Finding(
            severity="blocker",
            file="",
            line=0,
            image="",
            message="CSV found but relatedImages section is empty or unparseable. "
                    "No images can be verified for disconnected mirroring.",
        ))

    image_refs = scan_for_image_refs(repo_root, tracked=tracked)

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
    tracked = get_tracked_files(root)
    pattern = detect_image_pattern(root)

    if pattern == "env_var":
        return check_env_var_pattern(root, manifest_env_vars=manifest_env_vars, tracked=tracked)
    elif pattern == "static_csv":
        return check_static_csv_pattern(root, tracked=tracked)
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
