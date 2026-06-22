#!/usr/bin/env python3
"""Check that all container image references are accounted for in disconnected manifests.

Supports two patterns:
1. Static CSV relatedImages — images listed in the ClusterServiceVersion YAML
2. RELATED_IMAGE_* env vars — operator injects images via environment variables at runtime

If the repo uses RELATED_IMAGE_* env vars (the opendatahub-operator pattern), the rule
checks that every image reference maps to a RELATED_IMAGE_* variable. If the repo uses
a static CSV, it checks that every image appears in relatedImages.

Exclusions (test files, CI, etc.) are handled by the exception system in config/config.yaml.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    from rules.common import (
        Finding, RuleResult, get_tracked_files, is_file_in_production_scope,
        SKIP_DIRS, find_params_env_dirs, build_overlay_file_map,
        is_non_production_overlay_file, production_scope_relative_dirs,
    )
except ModuleNotFoundError:
    from common import (
        Finding, RuleResult, get_tracked_files, is_file_in_production_scope,
        SKIP_DIRS, find_params_env_dirs, build_overlay_file_map,
        is_non_production_overlay_file, production_scope_relative_dirs,
    )

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


def extract_related_image_vars(
    repo_root: Path,
    with_locations: bool = False,
) -> set[str] | dict[str, tuple[str, int]]:
    """Extract all RELATED_IMAGE_* env var names defined in Go source.

    When *with_locations* is True, returns a dict mapping var name to
    (relative_file, line_number) of its first occurrence.
    """
    env_vars: set[str] = set()
    var_locations: dict[str, tuple[str, int]] = {}
    for go_file in repo_root.rglob("*.go"):
        if any(d in go_file.parts for d in SKIP_DIRS):
            continue
        try:
            lines = go_file.read_text().splitlines()
            for i, line in enumerate(lines, 1):
                for match in RELATED_IMAGE_PATTERN.finditer(line):
                    var = match.group()
                    if var != "RELATED_IMAGE_*":
                        env_vars.add(var)
                        if with_locations and var not in var_locations:
                            var_locations[var] = (
                                str(go_file.relative_to(repo_root)), i,
                            )
        except (OSError, UnicodeDecodeError):
            continue
    if with_locations:
        return var_locations
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



def scan_for_image_refs(
    repo_root: Path,
    tracked: set[Path] | None = None,
    params_env_dirs: set[Path] | None = None,
    files_checked: list[str] | None = None,
    production_scope=None,
    non_image_prefixes: list[str] | None = None,
    scan_filters: dict | None = None,
) -> list[tuple[Path, int, str]]:
    """Scan source files for container image references."""
    extensions = {".go", ".py", ".yaml", ".yml", ".json", ".sh"}
    results = []

    if scan_filters is not None:
        scan_filters["globs"] = ["**/*"]
        scan_filters["extensions"] = sorted(extensions)
        scan_filters["skip_dirs"] = sorted(SKIP_DIRS)
        scan_filters["tracked_files_only"] = tracked is not None
        prod_dirs = production_scope_relative_dirs(production_scope, repo_root)
        if prod_dirs is not None:
            scan_filters["production_scope_dirs"] = prod_dirs
        if params_env_dirs:
            scan_filters["params_env_dirs_excluded"] = sorted(
                str(d.relative_to(repo_root.resolve())) + "/"
                for d in params_env_dirs
                if d.is_relative_to(repo_root.resolve())
            )

    for filepath in repo_root.rglob("*"):
        if tracked is not None and filepath.resolve() not in tracked:
            continue
        if any(d in filepath.parts for d in SKIP_DIRS):
            continue
        if params_env_dirs and any(filepath.resolve().is_relative_to(d) for d in params_env_dirs):
            continue
        if filepath.suffix not in extensions:
            continue
        if is_file_in_production_scope(filepath, production_scope) is False:
            continue

        try:
            lines = filepath.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue

        if files_checked is not None:
            files_checked.append(str(filepath.relative_to(repo_root)))

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("#"):
                continue
            seen: set[str] = set()
            for pattern in (IMAGE_REF_PATTERN, GO_IMAGE_ASSIGN_PATTERN):
                for match in pattern.finditer(line):
                    img = match.group(1).strip().strip('"').strip("'")
                    domain = img.split("/")[0].split(":")[0]
                    parts = img.split("/")
                    if (
                        "/" in img
                        and not img.startswith("#")
                        and domain not in NON_REGISTRY_DOMAINS
                        and img not in seen
                        and all(len(p.split(":")[0]) > 1 for p in parts)
                        and not (non_image_prefixes and any(img.startswith(p) for p in non_image_prefixes))
                    ):
                        seen.add(img)
                        results.append((filepath, i, img))

    return results


def check_env_var_pattern(
    repo_root: Path,
    manifest_env_vars: set[str] | None = None,
    tracked: set[Path] | None = None,
    production_scope=None,
    arch_data: dict | None = None,
    non_image_prefixes: list[str] | None = None,
) -> RuleResult:
    """Check repos that use RELATED_IMAGE_* env var pattern.

    When manifest_env_vars is provided (from operator_manifest), cross-references
    the target repo's env vars against the authoritative operator manifest.
    """
    result = RuleResult(rule="image-manifest-complete")
    overlay_file_map = build_overlay_file_map(arch_data, repo_root)
    var_locations = extract_related_image_vars(repo_root, with_locations=True)
    local_vars = set(var_locations.keys())

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

    pe_dirs = find_params_env_dirs(repo_root)
    image_refs = scan_for_image_refs(repo_root, tracked=tracked, params_env_dirs=pe_dirs, files_checked=result.files_checked, production_scope=production_scope, non_image_prefixes=non_image_prefixes, scan_filters=result.scan_filters)
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
                severity = "blocker"
                msg = (f"Image '{image}' has no RELATED_IMAGE_* mapping on this line. "
                       f"Will not be mirrored in disconnected environments.")
                if is_non_production_overlay_file(filepath, production_scope, overlay_file_map):
                    severity = "info"
                    msg += " [non-production overlay]"
                if severity == "blocker":
                    result.passed = False
                result.findings.append(Finding(
                    severity=severity,
                    file=relative,
                    line=line_num,
                    image=image,
                    message=msg,
                ))
        elif manifest_env_vars is not None:
            for var_name in related_vars:
                if var_name not in manifest_env_vars:
                    relative = str(filepath.relative_to(repo_root))
                    severity = "blocker"
                    msg = (f"Image references '{var_name}' which does not exist "
                           f"in the operator manifest. The operator will not inject "
                           f"this image in disconnected environments.")
                    if is_non_production_overlay_file(filepath, production_scope, overlay_file_map):
                        severity = "info"
                        msg += " [non-production overlay]"
                    if severity == "blocker":
                        result.passed = False
                    result.findings.append(Finding(
                        severity=severity,
                        file=relative,
                        line=line_num,
                        image=image,
                        message=msg,
                    ))

    if manifest_env_vars is not None:
        stale_vars = local_vars - manifest_env_vars
        for var in sorted(stale_vars):
            var_file, var_line = var_locations.get(var, ("", 0))
            result.passed = False
            result.findings.append(Finding(
                severity="blocker",
                file=var_file,
                line=var_line,
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


def check_static_csv_pattern(
    repo_root: Path,
    tracked: set[Path] | None = None,
    production_scope=None,
    arch_data: dict | None = None,
    non_image_prefixes: list[str] | None = None,
) -> RuleResult:
    """Check repos that use static CSV relatedImages."""
    result = RuleResult(rule="image-manifest-complete")
    overlay_file_map = build_overlay_file_map(arch_data, repo_root)
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

    pe_dirs = find_params_env_dirs(repo_root)
    image_refs = scan_for_image_refs(repo_root, tracked=tracked, params_env_dirs=pe_dirs, files_checked=result.files_checked, production_scope=production_scope, non_image_prefixes=non_image_prefixes, scan_filters=result.scan_filters)

    for filepath, line_num, image in image_refs:
        normalized = normalize_image(image)

        if normalized and normalized not in related_images:
            relative = str(filepath.relative_to(repo_root))
            severity = "blocker"
            msg = f"Image '{image}' not found in CSV relatedImages."
            if is_non_production_overlay_file(filepath, production_scope, overlay_file_map):
                severity = "info"
                msg += " [non-production overlay]"
            if severity == "blocker":
                result.passed = False
            result.findings.append(Finding(
                severity=severity,
                file=relative,
                line=line_num,
                image=image,
                message=msg,
            ))

    return result


def check_unmanaged_images(
    repo_root: Path,
    manifest_env_vars: set[str],
    tracked: set[Path] | None = None,
    production_scope=None,
    arch_data: dict | None = None,
    non_image_prefixes: list[str] | None = None,
) -> RuleResult:
    """Check repos with no RELATED_IMAGE pattern but known to be operator-managed.

    Scans for hardcoded image references that have no RELATED_IMAGE_* wiring.
    These images will not be injected by the operator in disconnected environments.
    """
    result = RuleResult(rule="image-manifest-complete")
    overlay_file_map = build_overlay_file_map(arch_data, repo_root)
    result.findings.append(Finding(
        severity="info",
        file="",
        line=0,
        image="",
        message=f"No RELATED_IMAGE_* pattern detected, but repo is operator-managed. "
                f"Scanning for hardcoded images not covered by operator manifest "
                f"({len(manifest_env_vars)} authoritative vars).",
    ))

    pe_dirs = find_params_env_dirs(repo_root)
    image_refs = scan_for_image_refs(repo_root, tracked=tracked, params_env_dirs=pe_dirs, files_checked=result.files_checked, production_scope=production_scope, non_image_prefixes=non_image_prefixes, scan_filters=result.scan_filters)

    for filepath, line_num, image in image_refs:
        relative = str(filepath.relative_to(repo_root))
        severity = "blocker"
        msg = (f"Hardcoded image '{image}' has no RELATED_IMAGE_* wiring. "
               f"The operator will not inject a mirrored version in disconnected environments.")

        if is_non_production_overlay_file(filepath, production_scope, overlay_file_map):
            severity = "info"
            msg += " [non-production overlay]"

        if severity == "blocker":
            result.passed = False

        result.findings.append(Finding(
            severity=severity,
            file=relative,
            line=line_num,
            image=image,
            message=msg,
        ))

    return result


def run(repo_root: str, manifest_env_vars: set[str] | None = None, production_scope=None, arch_data: dict | None = None, non_image_prefixes: list[str] | None = None, **_kwargs) -> RuleResult:
    """Run the image manifest completeness rule.

    When manifest_env_vars is provided, the env_var pattern check will
    cross-reference against the authoritative operator manifest.
    """
    root = Path(repo_root)
    tracked = get_tracked_files(root)
    pattern = detect_image_pattern(root)

    if pattern == "env_var":
        return check_env_var_pattern(root, manifest_env_vars=manifest_env_vars, tracked=tracked, production_scope=production_scope, arch_data=arch_data, non_image_prefixes=non_image_prefixes)
    elif pattern == "static_csv":
        return check_static_csv_pattern(root, tracked=tracked, production_scope=production_scope, arch_data=arch_data, non_image_prefixes=non_image_prefixes)
    elif manifest_env_vars is not None:
        return check_unmanaged_images(
            root, manifest_env_vars=manifest_env_vars,
            tracked=tracked, production_scope=production_scope, arch_data=arch_data, non_image_prefixes=non_image_prefixes,
        )
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
