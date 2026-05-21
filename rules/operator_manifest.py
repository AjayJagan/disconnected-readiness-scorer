#!/usr/bin/env python3
"""Parse the opendatahub-operator to extract the authoritative image manifest.

This is the foundation rule — it reads the operator source code to build
the definitive list of RELATED_IMAGE_* env vars, which component each
belongs to, and which Go file defines it. This manifest is the source of
truth for what images must be mirrorable in disconnected environments.
"""

import re
import json
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Union

try:
    from rules.common import Finding, RuleResult
except ModuleNotFoundError:
    from common import Finding, RuleResult

RELATED_IMAGE_PATTERN = re.compile(r'"(RELATED_IMAGE_[A-Z0-9_]+)"')
IMAGE_MAP_PATTERN = re.compile(r'"([^"]+)":\s*"(RELATED_IMAGE_[A-Z0-9_]+)"')
KNOWN_ISSUES_PATTERN = re.compile(r'- image:\s*(RELATED_IMAGE_[A-Z0-9_]+)')

OPERATOR_REPO = "https://github.com/opendatahub-io/opendatahub-operator.git"
COMPONENTS_PATH = "internal/controller/components"

SKIP_DIRS = {".git", "vendor", "node_modules", "__pycache__"}
TEST_SUFFIXES = {"_test.go", "_int_test.go", "_internal_test.go"}


@dataclass
class ImageEntry:
    env_var: str
    component: str
    manifest_key: str
    source_file: str
    source_line: int


@dataclass
class Manifest:
    images: list = field(default_factory=list)
    components: dict = field(default_factory=dict)
    known_issues: list = field(default_factory=list)
    rhai_helm_components: list = field(default_factory=list)


def clone_operator(target_dir: Path) -> Path:
    """Clone the operator repo if not already present."""
    if target_dir.exists() and (target_dir / ".git").exists():
        return target_dir
    subprocess.run(
        ["git", "clone", "--depth", "1", OPERATOR_REPO, str(target_dir)],
        capture_output=True, check=True,
    )
    return target_dir


def parse_component_images(component_dir: Path, component_name: str) -> List[ImageEntry]:
    """Parse a component's Go files for RELATED_IMAGE mappings."""
    entries = []

    for go_file in component_dir.rglob("*.go"):
        if any(go_file.name.endswith(s) for s in TEST_SUFFIXES):
            continue

        try:
            lines = go_file.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue

        for i, line in enumerate(lines, 1):
            map_match = IMAGE_MAP_PATTERN.search(line)
            if map_match:
                entries.append(ImageEntry(
                    env_var=map_match.group(2),
                    component=component_name,
                    manifest_key=map_match.group(1),
                    source_file=str(go_file),
                    source_line=i,
                ))
                continue

            for match in RELATED_IMAGE_PATTERN.finditer(line):
                env_var = match.group(1)
                if env_var == "RELATED_IMAGE_*":
                    continue
                if not any(e.env_var == env_var and e.component == component_name for e in entries):
                    entries.append(ImageEntry(
                        env_var=env_var,
                        component=component_name,
                        manifest_key="",
                        source_file=str(go_file),
                        source_line=i,
                    ))

    return entries


def parse_known_issues(operator_root: Path) -> Tuple[List[str], List[str]]:
    """Parse component-params-env.yaml for known issues and helm components."""
    params_file = operator_root / "component-params-env.yaml"
    known_issues = []
    rhai_helm = []

    if not params_file.exists():
        return known_issues, rhai_helm

    try:
        content = params_file.read_text()
    except (OSError, UnicodeDecodeError):
        return known_issues, rhai_helm

    in_known_issues = False
    in_rhai_helm = False

    for line in content.splitlines():
        stripped = line.strip()

        if stripped.startswith("# known_issues:"):
            in_known_issues = True
            in_rhai_helm = False
            continue
        if stripped.startswith("# rhai_helm_components:"):
            in_rhai_helm = True
            in_known_issues = False
            continue
        if stripped.startswith("#") and not stripped.startswith("# -"):
            in_known_issues = False
            in_rhai_helm = False

        match = KNOWN_ISSUES_PATTERN.match(stripped)
        if match:
            known_issues.append(match.group(1))

    return known_issues, rhai_helm


def build_manifest(operator_root: Union[str, Path]) -> Manifest:
    """Build the complete image manifest from the operator source."""
    root = Path(operator_root)
    manifest = Manifest()

    components_dir = root / COMPONENTS_PATH
    if not components_dir.exists():
        return manifest

    for component_dir in sorted(components_dir.iterdir()):
        if not component_dir.is_dir():
            continue
        if component_dir.name.startswith(".") or component_dir.name == "registry":
            continue

        component_name = component_dir.name
        entries = parse_component_images(component_dir, component_name)
        manifest.images.extend(entries)

        if entries:
            manifest.components[component_name] = {
                "image_count": len(entries),
                "env_vars": sorted(set(e.env_var for e in entries)),
            }

    # Also scan top-level files for RELATED_IMAGE refs not in components
    for go_file in root.rglob("*.go"):
        if COMPONENTS_PATH in str(go_file):
            continue
        if any(d in go_file.parts for d in SKIP_DIRS):
            continue
        if any(go_file.name.endswith(s) for s in TEST_SUFFIXES):
            continue

        try:
            lines = go_file.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue

        for i, line in enumerate(lines, 1):
            for match in RELATED_IMAGE_PATTERN.finditer(line):
                env_var = match.group(1)
                if env_var == "RELATED_IMAGE_*":
                    continue
                if not any(e.env_var == env_var for e in manifest.images):
                    manifest.images.append(ImageEntry(
                        env_var=env_var,
                        component="operator-core",
                        manifest_key="",
                        source_file=str(go_file),
                        source_line=i,
                    ))

    known_issues, rhai_helm = parse_known_issues(root)
    manifest.known_issues = known_issues
    manifest.rhai_helm_components = rhai_helm

    return manifest


def run(operator_path: str) -> RuleResult:
    """Run the manifest builder and return a RuleResult."""
    manifest = build_manifest(operator_path)
    all_env_vars = sorted(set(e.env_var for e in manifest.images))

    result = RuleResult(rule="operator-manifest")
    result.findings.append(Finding(
        severity="info",
        file="",
        line=0,
        image="",
        message=f"Operator manifest: {len(all_env_vars)} unique RELATED_IMAGE env vars "
                f"across {len(manifest.components)} components.",
    ))
    for issue in manifest.known_issues:
        result.findings.append(Finding(
            severity="warning",
            file="component-params-env.yaml",
            line=0,
            image=issue,
            message=f"Known issue in operator manifest: {issue}",
        ))
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: operator_manifest.py <path-to-operator-repo>", file=sys.stderr)
        sys.exit(1)
    path = sys.argv[1]
    r = run(path)
    print(json.dumps({
        "rule": r.rule,
        "passed": r.passed,
        "findings": [
            {"severity": f.severity, "file": f.file, "line": f.line,
             "image": f.image, "message": f.message}
            for f in r.findings
        ],
    }, indent=2))
