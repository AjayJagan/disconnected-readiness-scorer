#!/usr/bin/env python3
"""Validate params.env + kustomize image wiring for disconnected readiness.

Requires kustomize binary. Validates the full chain:
params.env → kustomize configMap → rendered manifest → Go os.Getenv.
Optionally cross-references against the operator manifest.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

try:
    from rules.common import Finding, RuleResult, SKIP_DIRS, load_repo_config
except ModuleNotFoundError:
    from common import Finding, RuleResult, SKIP_DIRS, load_repo_config

try:
    from rules.params_env_utils import (
        kustomize_available, kustomize_build, discover_overlays,
        find_params_env_files, parse_params_env, load_ignore_keys,
        create_probe_overlay, extract_all_images,
        write_probe_params_env,
        extract_configmap_key_refs, extract_kustomize_replacement_keys,
        extract_env_configmap_mappings, find_go_related_image_envs,
        PROBE_SENTINEL,
    )
except ModuleNotFoundError:
    from params_env_utils import (
        kustomize_available, kustomize_build, discover_overlays,
        find_params_env_files, parse_params_env, load_ignore_keys,
        create_probe_overlay, extract_all_images,
        write_probe_params_env,
        extract_configmap_key_refs, extract_kustomize_replacement_keys,
        extract_env_configmap_mappings, find_go_related_image_envs,
        PROBE_SENTINEL,
    )

RULE_NAME = "params-env-wiring"

_IMAGE_KEY_INDICATORS = ("image", "img", "registry", "repository")


def _looks_like_image_key(key: str) -> bool:
    lower = key.lower().replace("-", "_")
    return any(ind in lower for ind in _IMAGE_KEY_INDICATORS)


OPERATOR_CONFIG_FILE = "component-params-env.yaml"


def _is_operator_repo(root: Path) -> bool:
    return (root / OPERATOR_CONFIG_FILE).is_file()


def _build_ignored_image_patterns(
    ignored_keys: set[str], overlay_params: dict[str, str],
) -> list[str]:
    patterns: list[str] = []
    for k in ignored_keys:
        if k in overlay_params:
            repo_part = overlay_params[k].split("@")[0].rsplit(":", 1)[0]
            patterns.append(f"{repo_part}:*")
            patterns.append(f"{repo_part}@*")
    return patterns


def _check_wiring(
    kdir: Path,
    root: Path,
    rendered: str,
    dir_params: dict[str, str],
    ignored_keys: set[str],
    result: RuleResult,
    all_manifest_related_vars: set[str],
):
    dir_active_keys = {k for k in dir_params if k not in ignored_keys}

    ref_keys = extract_configmap_key_refs(rendered)
    replacement_keys = extract_kustomize_replacement_keys(kdir)
    wired_keys = ref_keys | replacement_keys

    for key in sorted(dir_active_keys - wired_keys):
        result.findings.append(Finding(
            severity="info",
            file=str(kdir.relative_to(root) / "params.env"),
            line=0, image="",
            message=f"params.env key '{key}' is not consumed by kustomize "
                    f"(no configMapKeyRef or replacement). Unused key — "
                    f"image not referenced in rendered manifests.",
        ))

    for key in sorted(ref_keys):
        if key not in dir_params and _looks_like_image_key(key):
            result.findings.append(Finding(
                severity="info",
                file=str(kdir.relative_to(root)),
                line=0, image="",
                message=f"configMapKeyRef references '{key}' which is not a "
                        f"params.env image key.",
            ))

    env_mappings = extract_env_configmap_mappings(rendered)
    for env_name, cm_key, _ in env_mappings:
        if env_name.startswith("RELATED_IMAGE_") and cm_key in dir_active_keys:
            all_manifest_related_vars.add(env_name)


def _process_manifest_source_folder(
    source_dir: Path,
    root: Path,
    ignored_keys: set[str],
    result: RuleResult,
    all_repo_params: dict[str, str],
    all_manifest_related_vars: set[str],
    env_mappings_set: set[str],
    overlay_dirs: list[str] | None = None,
) -> int:
    """Process all kustomization dirs under a manifest_source folder.

    If overlay_dirs is set, only those dirs (relative to root) are probed
    for hardcoded images. Otherwise all kustomization.yaml dirs are scanned.

    Returns the number of overlays with params.env found.
    """
    all_params_env = sorted(source_dir.rglob("params.env"))
    kustomization_files = sorted(source_dir.rglob("kustomization.yaml"))

    if not kustomization_files:
        return 0

    overlay_params: dict[str, str] = {}
    for params_path in all_params_env:
        overlay_params.update(parse_params_env(params_path))
    all_repo_params.update(overlay_params)

    ignored_image_patterns = _build_ignored_image_patterns(ignored_keys, overlay_params)
    has_params = len(all_params_env) > 0

    # --- Probe: copy source dir, replace all params.env with sentinels ---
    try:
        with tempfile.TemporaryDirectory(prefix="verify-params-env-") as tmp:
            tmp_source = Path(tmp) / source_dir.name
            shutil.copytree(str(source_dir), str(tmp_source))

            if has_params:
                for params_path in all_params_env:
                    rel = params_path.relative_to(source_dir)
                    write_probe_params_env(params_path, tmp_source / rel, ignored_keys)

            if overlay_dirs is not None:
                probe_dirs = []
                source_name = source_dir.name
                for od in overlay_dirs:
                    od_path = Path(od)
                    if od_path.parts and od_path.parts[0] == source_name:
                        rel = Path(*od_path.parts[1:])
                    else:
                        rel = od_path
                    candidate = tmp_source / rel
                    if ".." in rel.parts:
                        continue
                    if (candidate / "kustomization.yaml").exists():
                        probe_dirs.append(candidate)
            else:
                probe_dirs = sorted(
                    k.parent for k in tmp_source.rglob("kustomization.yaml")
                    if not any(d in k.parts for d in SKIP_DIRS)
                )

            for kdir in probe_dirs:
                try:
                    probe_rendered = kustomize_build(kdir)
                except RuntimeError:
                    continue

                orig_kdir = source_dir / kdir.relative_to(tmp_source)
                images = extract_all_images(
                    probe_rendered,
                    ignored_image_patterns if has_params else [],
                )
                for img, locations in images.items():
                    if img == PROBE_SENTINEL:
                        continue
                    result.passed = False
                    loc_str = ", ".join(locations) if locations else "unknown"
                    result.findings.append(Finding(
                        severity="blocker",
                        file=str(orig_kdir.relative_to(root)),
                        line=0, image=img,
                        message=f"Hardcoded image '{img}' in operator-managed kustomize "
                                f"dir without params.env wiring (found in {loc_str}). "
                                f"Will not be mirrored in disconnected.",
                    ))
    except RuntimeError as e:
        result.findings.append(Finding(
            severity="info",
            file=str(source_dir.relative_to(root)),
            line=0, image="",
            message=f"kustomize build failed for manifest source: {e}",
        ))

    if not has_params:
        return 0

    # --- Wiring check + RELATED_IMAGE collection (on original dirs) ---
    overlays_with_params = 0
    for kustomization in kustomization_files:
        kdir = kustomization.parent
        if any(d in kustomization.parts for d in SKIP_DIRS):
            continue

        dir_params_files = find_params_env_files(kdir)
        dir_params: dict[str, str] = {}
        for p in dir_params_files:
            dir_params.update(parse_params_env(p))
        if not dir_params:
            continue

        overlays_with_params += 1

        try:
            original_rendered = kustomize_build(kdir)
        except RuntimeError:
            continue

        _check_wiring(
            kdir, root, original_rendered, dir_params,
            ignored_keys, result, all_manifest_related_vars,
        )

        # Collect env mappings for operator manifest cross-ref
        dir_active_keys = {k for k in dir_params if k not in ignored_keys}
        for env_name, cm_key, _ in extract_env_configmap_mappings(original_rendered):
            if cm_key in dir_active_keys:
                env_mappings_set.add(env_name)

    return overlays_with_params


def _process_discover_overlays(
    overlays: list[Path],
    root: Path,
    ignored_keys: set[str],
    result: RuleResult,
    all_repo_params: dict[str, str],
    all_manifest_related_vars: set[str],
    env_mappings_set: set[str],
) -> int:
    """Fallback: process overlays found by discover_overlays() (no manifest_source)."""
    total_overlays = 0

    for overlay_dir in overlays:
        params_files = find_params_env_files(overlay_dir)
        overlay_params: dict[str, str] = {}
        image_params_files = []
        for p in params_files:
            parsed = parse_params_env(p)
            if parsed:
                overlay_params.update(parsed)
                image_params_files.append(p)
        if not image_params_files:
            continue

        total_overlays += 1
        all_repo_params.update(overlay_params)
        active_keys = {k for k in overlay_params if k not in ignored_keys}
        ignored_image_patterns = _build_ignored_image_patterns(ignored_keys, overlay_params)

        try:
            with tempfile.TemporaryDirectory(prefix="verify-params-env-") as tmp:
                tmp_overlay = create_probe_overlay(
                    overlay_dir, image_params_files, Path(tmp), ignored_keys
                )
                if tmp_overlay is None:
                    continue
                probe_rendered = kustomize_build(tmp_overlay)
        except RuntimeError as e:
            result.findings.append(Finding(
                severity="info",
                file=str(overlay_dir.relative_to(root)),
                line=0, image="",
                message=f"kustomize build failed for overlay: {e}",
            ))
            continue

        images_with_locations = extract_all_images(probe_rendered, ignored_image_patterns)
        for img, locations in images_with_locations.items():
            if img == PROBE_SENTINEL:
                continue
            result.passed = False
            loc_str = ", ".join(locations) if locations else "unknown"
            result.findings.append(Finding(
                severity="blocker",
                file=str(overlay_dir.relative_to(root)),
                line=0,
                image=img,
                message=f"Hardcoded image '{img}' not sourced from params.env "
                        f"(found in {loc_str}). Will not be mirrored in disconnected.",
            ))

        try:
            original_rendered = kustomize_build(overlay_dir)
        except RuntimeError as e:
            result.findings.append(Finding(
                severity="info",
                file=str(overlay_dir.relative_to(root)),
                line=0, image="",
                message=f"kustomize build failed for original overlay, "
                        f"using probe output for wiring analysis: {e}",
            ))
            original_rendered = probe_rendered

        _check_wiring(
            overlay_dir, root, original_rendered, overlay_params,
            ignored_keys, result, all_manifest_related_vars,
        )

        # Collect env mappings for operator manifest cross-ref
        for env_name, cm_key, _ in extract_env_configmap_mappings(original_rendered):
            if cm_key in active_keys:
                env_mappings_set.add(env_name)

    return total_overlays


def run(repo_root: str, manifest_env_vars: set[str] | None = None,
        production_scope=None, **_kwargs) -> RuleResult:
    root = Path(repo_root)
    result = RuleResult(rule=RULE_NAME)

    if _is_operator_repo(root):
        result.findings.append(Finding(
            severity="info", file="", line=0, image="",
            message="Operator repo detected. params-env-wiring checks are not applicable — "
                    "use validate-related-images.sh for operator-level validation.",
        ))
        return result

    manifest_source = (
        production_scope.manifest_source
        if production_scope and production_scope.manifest_source
        else None
    )

    repo_config = load_repo_config(root)
    kustomize_overlay_dirs = repo_config.get("kustomize_overlays") or None

    if not kustomize_overlay_dirs and production_scope and production_scope.overlay_paths:
        kustomize_overlay_dirs = production_scope.overlay_paths

    overlays = discover_overlays(root)
    if not overlays and not manifest_source:
        return result

    if not kustomize_available():
        result.findings.append(Finding(
            severity="info", file="", line=0, image="",
            message="kustomize not found on PATH. Skipping params.env wiring checks. "
                    "Install kustomize for full validation.",
        ))
        return result

    ignored_keys = load_ignore_keys(repo_config)
    if ignored_keys:
        result.findings.append(Finding(
            severity="info", file="", line=0, image="",
            message=f"{len(ignored_keys)} params.env key(s) excluded via "
                    f"config: {', '.join(sorted(ignored_keys))}",
        ))

    all_repo_params: dict[str, str] = {}
    all_manifest_related_vars: set[str] = set()
    env_mappings_set: set[str] = set()
    total_overlays = 0

    if manifest_source:
        for folder in manifest_source.split(","):
            source_dir = root / folder
            if not source_dir.is_dir():
                continue
            total_overlays += _process_manifest_source_folder(
                source_dir, root, ignored_keys, result,
                all_repo_params, all_manifest_related_vars,
                env_mappings_set,
                overlay_dirs=kustomize_overlay_dirs,
            )
    else:
        if kustomize_overlay_dirs:
            filtered = []
            for o in overlays:
                rel = o.relative_to(root)
                if any(rel == Path(d) or rel.is_relative_to(Path(d))
                       for d in kustomize_overlay_dirs):
                    filtered.append(o)
            overlays = filtered
        total_overlays = _process_discover_overlays(
            overlays, root, ignored_keys, result,
            all_repo_params, all_manifest_related_vars,
            env_mappings_set,
        )

    # --- Go wiring check (repo-global) ---
    go_env_vars = find_go_related_image_envs(root)

    for var in sorted(all_manifest_related_vars - go_env_vars):
        result.findings.append(Finding(
            severity="info",
            file="", line=0, image="",
            message=f"RELATED_IMAGE var '{var}' is in rendered manifests but Go code "
                    f"never calls os.Getenv for it. Controller may ignore this image.",
        ))

    for var in sorted(go_env_vars - all_manifest_related_vars):
        result.passed = False
        result.findings.append(Finding(
            severity="blocker",
            file="", line=0, image="",
            message=f"Go code calls os.Getenv(\"{var}\") but this var is not in "
                    f"rendered manifests. Controller expects an image that won't "
                    f"be injected in disconnected environments.",
        ))

    # --- Operator manifest cross-reference ---
    if manifest_env_vars is not None:
        for env_name in sorted(env_mappings_set):
            if env_name.startswith("RELATED_IMAGE_") and env_name not in manifest_env_vars:
                result.passed = False
                result.findings.append(Finding(
                    severity="blocker",
                    file="", line=0, image="",
                    message=f"RELATED_IMAGE var '{env_name}' mapped from params.env is not "
                            f"in the operator manifest. Operator won't inject this image "
                            f"in disconnected environments.",
                ))

        for env_name in sorted(env_mappings_set - manifest_env_vars):
            if not env_name.startswith("RELATED_IMAGE_"):
                result.findings.append(Finding(
                    severity="info",
                    file="", line=0, image="",
                    message=f"params.env-mapped var '{env_name}' not in operator manifest. "
                            f"May be stale or renamed.",
                ))

    # --- Summary ---
    result.findings.insert(0, Finding(
        severity="info", file="", line=0, image="",
        message=f"Repo uses params.env pattern. Found {total_overlays} overlay(s) with "
                f"{len(all_repo_params)} image key(s)."
                + (f" Cross-referenced against {len(manifest_env_vars)} operator manifest vars."
                   if manifest_env_vars is not None else ""),
    ))

    return result


def detect_params_env(repo_root: Path) -> bool:
    overlays = discover_overlays(repo_root)
    for overlay_dir in overlays:
        if parse_params_env(overlay_dir / "params.env"):
            return True
    return False


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
