#!/usr/bin/env python3
"""Enforce digest-only image references — reject mutable tags."""

import re
from pathlib import Path

try:
    from rules.common import Finding, RuleResult
except ImportError:
    from common import Finding, RuleResult

IMAGE_REF_PATTERN = re.compile(
    r'((?:[\w.\-]+(?:\.[\w.\-]+)+(?::\d+)?/)?[\w.\-]+/[\w.\-]+)([:@][\w.\-:]+)'
)

PRODUCTION_DIRS = {"manifests", "deploy", "config", "bundle", "helm", "chart", "kustomize"}
TEST_DIRS = {"test", "tests", "e2e", "hack", "testdata"}
CI_DIRS = {".github", ".tekton", "ci"}
TEST_SUFFIXES = {"_test.go", "_int_test.go", "_internal_test.go"}
SKIP_FILES = {"semgrep.yaml", "semgrep.yml", ".semgrep.yml"}


def is_excluded_file(filepath: Path) -> bool:
    """Files that should never produce blocker findings."""
    if filepath.name in SKIP_FILES:
        return True
    if any(filepath.name.endswith(s) for s in TEST_SUFFIXES):
        return True
    if any(d in filepath.parts for d in TEST_DIRS | CI_DIRS):
        return True
    return False


def is_production_file(filepath: Path) -> bool:
    return any(d in filepath.parts for d in PRODUCTION_DIRS) and not is_excluded_file(filepath)


def scan_file(filepath: Path, root: Path) -> list[Finding]:
    findings = []
    try:
        lines = filepath.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return findings

    for i, line in enumerate(lines, 1):
        if line.strip().startswith("#") or line.strip().startswith("//"):
            continue

        for match in IMAGE_REF_PATTERN.finditer(line):
            repo_part = match.group(1)
            ref_part = match.group(2)

            if "/" not in repo_part:
                continue
            if ref_part.startswith("@sha256:"):
                continue

            relative = str(filepath.relative_to(root))
            if is_excluded_file(filepath):
                severity = "info"
            elif is_production_file(filepath):
                severity = "blocker"
            else:
                severity = "warning"

            findings.append(Finding(
                severity=severity,
                file=relative,
                line=i,
                image=f"{repo_part}{ref_part}",
                message=f"Image uses tag '{ref_part}' instead of digest. "
                        f"Tags cannot be reliably mirrored.",
            ))

    return findings


def run(repo_root: str) -> RuleResult:
    root = Path(repo_root)
    result = RuleResult(rule="no-image-tags")
    skip_dirs = {".git", "vendor", "node_modules", "__pycache__"}
    extensions = {".go", ".py", ".yaml", ".yml", ".json", ".toml"}

    for filepath in root.rglob("*"):
        if any(d in filepath.parts for d in skip_dirs):
            continue
        if filepath.suffix not in extensions and filepath.name != "Dockerfile":
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
