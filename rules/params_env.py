#!/usr/bin/env python3
"""Validate params.env + kustomize image wiring for disconnected readiness.

Requires kustomize binary. Validates the full chain:
params.env → kustomize configMap → rendered manifest → Go os.Getenv.
Optionally cross-references against the operator manifest.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

try:
    from rules.common import Finding, RuleResult
except ModuleNotFoundError:
    from common import Finding, RuleResult

try:
    from rules.params_env_utils import (
        kustomize_available, kustomize_build, discover_overlays,
        find_params_env_files, parse_params_env, load_ignore_file,
        create_probe_overlay, extract_all_images,
        extract_configmap_key_refs, extract_kustomize_replacement_keys,
        extract_env_configmap_mappings, find_go_related_image_envs,
        PROBE_SENTINEL,
    )
except ModuleNotFoundError:
    from params_env_utils import (
        kustomize_available, kustomize_build, discover_overlays,
        find_params_env_files, parse_params_env, load_ignore_file,
        create_probe_overlay, extract_all_images,
        extract_configmap_key_refs, extract_kustomize_replacement_keys,
        extract_env_configmap_mappings, find_go_related_image_envs,
        PROBE_SENTINEL,
    )

RULE_NAME = "params-env-wiring"
OPERATOR_CONFIG_FILE = "component-params-env.yaml"


def _is_operator_repo(root: Path) -> bool:
    return (root / OPERATOR_CONFIG_FILE).is_file()


def run(repo_root: str, manifest_env_vars: set[str] | None = None) -> RuleResult:
    root = Path(repo_root)
    result = RuleResult(rule=RULE_NAME)

    if _is_operator_repo(root):
        result.findings.append(Finding(
            severity="info", file="", line=0, image="",
            message="Operator repo detected. params-env-wiring checks are not applicable — "
                    "use validate-related-images.sh for operator-level validation.",
        ))
        return result

    overlays = discover_overlays(root)
    if not overlays:
        return result

    if not kustomize_available():
        result.findings.append(Finding(
            severity="info", file="", line=0, image="",
            message="kustomize not found on PATH. Skipping params.env wiring checks. "
                    "Install kustomize for full validation.",
        ))
        return result
    ignored_keys = load_ignore_file(root)

    if ignored_keys:
        result.findings.append(Finding(
            severity="info", file="", line=0, image="",
            message=f"{len(ignored_keys)} params.env key(s) excluded via "
                    f".verify-params-env-ignore: {', '.join(sorted(ignored_keys))}",
        ))

    all_repo_params: dict[str, str] = {}
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

        # --- Build ignored image patterns for probe ---
        ignored_image_patterns = []
        for k in ignored_keys:
            if k in overlay_params:
                repo_part = overlay_params[k].split("@")[0].rsplit(":", 1)[0]
                ignored_image_patterns.append(f"{repo_part}:*")
                ignored_image_patterns.append(f"{repo_part}@*")

        # --- Probe check ---
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

        # --- Wiring check ---
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

        ref_keys = extract_configmap_key_refs(original_rendered)
        replacement_keys = extract_kustomize_replacement_keys(overlay_dir)
        wired_keys = ref_keys | replacement_keys

        for key in sorted(active_keys - wired_keys):
            result.passed = False
            result.findings.append(Finding(
                severity="blocker",
                file=str(overlay_dir.relative_to(root) / "params.env"),
                line=0, image="",
                message=f"params.env key '{key}' is not consumed by kustomize "
                        f"(no configMapKeyRef or replacement). Image will not be injected "
                        f"in disconnected environments.",
            ))

        for key in sorted(ref_keys):
            if key not in overlay_params:
                result.findings.append(Finding(
                    severity="info",
                    file=str(overlay_dir.relative_to(root)),
                    line=0, image="",
                    message=f"configMapKeyRef references '{key}' which is not a "
                            f"params.env image key.",
                ))

        # --- Go wiring check ---
        env_mappings = extract_env_configmap_mappings(original_rendered)
        manifest_related_vars = {
            env_name for env_name, cm_key, _ in env_mappings
            if env_name.startswith("RELATED_IMAGE_") and cm_key in active_keys
        }
        go_env_vars = find_go_related_image_envs(root)

        for var in sorted(manifest_related_vars - go_env_vars):
            result.findings.append(Finding(
                severity="info",
                file="", line=0, image="",
                message=f"RELATED_IMAGE var '{var}' is in rendered manifests but Go code "
                        f"never calls os.Getenv for it. Controller may ignore this image.",
            ))

        for var in sorted(go_env_vars - manifest_related_vars):
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
        active_params = {k for k in all_repo_params if k not in ignored_keys}
        env_mappings_set: set[str] = set()
        for overlay_dir in overlays:
            try:
                rendered = kustomize_build(overlay_dir)
                for env_name, cm_key, _ in extract_env_configmap_mappings(rendered):
                    if cm_key in active_params:
                        env_mappings_set.add(env_name)
            except RuntimeError:
                continue

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
