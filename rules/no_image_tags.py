#!/usr/bin/env python3
"""Enforce digest-only image references — reject mutable tags."""

import re
from pathlib import Path
from typing import List

try:
    from rules.common import Finding, RuleResult, get_tracked_files
except ModuleNotFoundError:
    from common import Finding, RuleResult, get_tracked_files

IMAGE_REF_PATTERN = re.compile(
    r'(https?://)?'
    r'((?:[\w.\-]+(?:\.[\w.\-]+)+(?::\d+)?/)?[\w.\-]+/[\w.\-]+)'
    r'([:@][\w.\-:]+)'
)

SOURCE_EXTENSIONS = {".go", ".py", ".ts", ".tsx", ".sh"}
EXCLUDED_DIRS = {"test", "tests", "e2e", "hack", "testdata", ".github", ".tekton", "ci"}
TEST_SUFFIXES = {"_test.go", "_int_test.go", "_internal_test.go"}
SKIP_FILES = {"semgrep.yaml", "semgrep.yml", ".semgrep.yml", "params.env"}
BUILD_FILES = {"Dockerfile", "Containerfile"}


def is_excluded_file(filepath: Path) -> bool:
    """Files that should produce info instead of blocker findings."""
    if filepath.name in SKIP_FILES:
        return True
    if filepath.name in BUILD_FILES or filepath.name.endswith(".Dockerfile"):
        return True
    if any(filepath.name.endswith(s) for s in TEST_SUFFIXES):
        return True
    if any(d in filepath.parts for d in EXCLUDED_DIRS):
        return True
    return False


def is_source_code(filepath: Path) -> bool:
    """Source code files that hardcode image refs at runtime."""
    return filepath.suffix in SOURCE_EXTENSIONS


def scan_file(filepath: Path, root: Path) -> List[Finding]:
    findings = []
    try:
        lines = filepath.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return findings

    for i, line in enumerate(lines, 1):
        if line.strip().startswith("#") or line.strip().startswith("//"):
            continue

        for match in IMAGE_REF_PATTERN.finditer(line):
            if match.group(1):
                continue
            repo_part = match.group(2)
            ref_part = match.group(3)

            if "/" not in repo_part:
                continue
            if ref_part.startswith("@sha256:"):
                continue

            relative = str(filepath.relative_to(root))
            base_msg = (f"Image `{repo_part}{ref_part}` uses tag '{ref_part}' instead of digest. "
                        f"Tags cannot be reliably mirrored.")
            if is_excluded_file(filepath):
                severity = "info"
                msg = f"{base_msg} File is excluded (test/build/CI)."
            else:
                severity = "blocker"
                if is_source_code(filepath):
                    msg = f"{base_msg} Hardcoded in source code."
                else:
                    msg = f"{base_msg} Manifest file not managed by params.env."

            findings.append(Finding(
                severity=severity,
                file=relative,
                line=i,
                image=f"{repo_part}{ref_part}",
                message=msg,
            ))

    return findings


def _find_params_env_dirs(root: Path) -> set[Path]:
    """Find directories managed by params.env + kustomize, including all referenced bases."""
    dirs: set[Path] = set()
    for params_env in root.rglob("params.env"):
        overlay_dir = params_env.parent
        if (overlay_dir / "kustomization.yaml").exists():
            _collect_kustomize_tree(overlay_dir, dirs)
    return dirs


def _collect_kustomize_tree(overlay_dir: Path, dirs: set[Path]):
    """Walk kustomization.yaml resources recursively to collect the full directory tree."""
    resolved = overlay_dir.resolve()
    if resolved in dirs:
        return
    dirs.add(resolved)

    kustomization = overlay_dir / "kustomization.yaml"
    if not kustomization.exists():
        return

    try:
        content = kustomization.read_text()
    except (OSError, UnicodeDecodeError):
        return

    in_resources = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "resources:":
            in_resources = True
            continue
        if in_resources:
            if stripped.startswith("- "):
                ref = stripped[2:].strip()
                if ref.startswith("#"):
                    continue
                target = (overlay_dir / ref).resolve()
                if target.is_dir():
                    _collect_kustomize_tree(target, dirs)
            elif stripped and not stripped.startswith("#"):
                in_resources = False


def run(repo_root: str) -> RuleResult:
    root = Path(repo_root)
    result = RuleResult(rule="no-image-tags")
    skip_dirs = {".git", "vendor", "node_modules", "__pycache__"}
    extensions = {".go", ".py", ".yaml", ".yml", ".json", ".toml"}
    params_env_dirs = _find_params_env_dirs(root)
    tracked = get_tracked_files(root)

    for filepath in root.rglob("*"):
        if tracked is not None and filepath.resolve() not in tracked:
            continue
        if any(d in filepath.parts for d in skip_dirs):
            continue
        if params_env_dirs and any(filepath.resolve().is_relative_to(d) for d in params_env_dirs):
            continue
        if filepath.suffix not in extensions and filepath.name not in BUILD_FILES and not filepath.name.endswith(".Dockerfile"):
            continue

        for finding in scan_file(filepath, root):
            result.findings.append(finding)
            if finding.severity == "blocker":
                result.passed = False

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
