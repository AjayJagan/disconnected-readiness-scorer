#!/usr/bin/env python3
"""Enforce digest-only image references — reject mutable tags."""

import re
from pathlib import Path
from typing import List

try:
    from rules.common import (
        Finding, RuleResult, get_tracked_files, is_in_production_scope,
        is_yaml_in_production_scope, SKIP_DIRS, find_params_env_dirs,
    )
except ModuleNotFoundError:
    from common import (
        Finding, RuleResult, get_tracked_files, is_in_production_scope,
        is_yaml_in_production_scope, SKIP_DIRS, find_params_env_dirs,
    )

IMAGE_REF_PATTERN = re.compile(
    r'(https?://)?'
    r'((?:[\w.\-]+(?:\.[\w.\-]+)+(?::\d+)?/)?[\w.\-]+/[\w.\-]+)'
    r'([:@][\w.\-:]+)'
)

SOURCE_EXTENSIONS = {".go", ".py", ".ts", ".tsx", ".sh"}

_SKIP_FILENAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}


def is_excluded_file(filepath: Path) -> bool:
    """Files that should produce info instead of blocker findings."""
    return filepath.name == "params.env"


def is_source_code(filepath: Path) -> bool:
    """Source code files that hardcode image refs at runtime."""
    return filepath.suffix in SOURCE_EXTENSIONS


_MAX_FILE_SIZE = 512 * 1024  # 512 KB


def scan_file(filepath: Path, root: Path, production_scope=None) -> List[Finding]:
    findings = []
    try:
        file_size = filepath.stat().st_size
        if file_size > _MAX_FILE_SIZE:
            findings.append(Finding(
                severity="info",
                file=str(filepath.relative_to(root)),
                line=0,
                image="",
                message=f"Skipped large file ({file_size // 1024}KB > {_MAX_FILE_SIZE // 1024}KB limit).",
            ))
            return findings
        lines = filepath.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return findings

    in_prod_go = is_in_production_scope(filepath, production_scope)
    in_prod_yaml = is_yaml_in_production_scope(filepath, production_scope)

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
            if any(len(p) <= 1 for p in repo_part.split("/")):
                continue
            if ref_part.startswith("@sha256:"):
                continue

            relative = str(filepath.relative_to(root))
            base_msg = (f"Image `{repo_part}{ref_part}` uses tag '{ref_part}' instead of digest. "
                        f"Tags cannot be reliably mirrored.")
            if is_excluded_file(filepath):
                severity = "info"
                msg = f"{base_msg} File is excluded (params.env)."
            else:
                severity = "blocker"
                if is_source_code(filepath):
                    msg = f"{base_msg} Hardcoded in source code."
                else:
                    msg = f"{base_msg} Manifest file not managed by params.env."

            if severity in ("blocker", "warning"):
                if in_prod_go is False or in_prod_yaml is False:
                    severity = "info"
                    msg += " [out of production scope]"

            findings.append(Finding(
                severity=severity,
                file=relative,
                line=i,
                image=f"{repo_part}{ref_part}",
                message=msg,
            ))

    return findings


def run(repo_root: str, production_scope=None) -> RuleResult:
    root = Path(repo_root)
    result = RuleResult(rule="no-image-tags")
    skip_dirs = SKIP_DIRS
    extensions = {".go", ".py", ".yaml", ".yml", ".json", ".toml"}
    params_env_dirs = find_params_env_dirs(root)
    params_env_prefixes = tuple(str(d) + "/" for d in params_env_dirs)
    tracked = get_tracked_files(root)

    for filepath in root.rglob("*"):
        if filepath.name in _SKIP_FILENAMES:
            continue
        if filepath.suffix not in extensions:
            continue
        if any(d in filepath.parts for d in skip_dirs):
            continue
        resolved = filepath.resolve()
        if tracked is not None and resolved not in tracked:
            continue
        if params_env_prefixes and str(resolved).startswith(params_env_prefixes):
            continue

        for finding in scan_file(filepath, root, production_scope=production_scope):
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
