"""Utility functions for params.env + kustomize image wiring validation.

Adapted from verify-params-env-images.py for use as a scorer rule.
Kustomize binary is required for probe and rendered-manifest checks.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


PROBE_SENTINEL = "probe.test/verify-params-env:check"
IMAGE_PLACEHOLDERS = frozenset({"REPLACE_IMAGE"})
SKIP_DIRS = {".git", "vendor", "node_modules", "__pycache__"}

_IMAGE_RE = re.compile(
    r"[a-zA-Z0-9._-]+/[a-zA-Z0-9._/-]+(?::[a-zA-Z0-9._-]+|@sha256:[a-f0-9]+)"
)
_UNQUALIFIED_IMAGE_RE = re.compile(
    r"(?:^|\s)image:\s*([a-zA-Z][\w._-]+(?::[a-zA-Z0-9._-]+|@sha256:[a-f0-9]+))",
    re.MULTILINE,
)
_CONFIGMAP_KEY_REF_RE = re.compile(
    r"configMapKeyRef:\s*\n"
    r"\s+key:\s+(\S+)\s*\n"
    r"\s+name:\s+(\S+)",
    re.MULTILINE,
)
_CONFIGMAP_KEY_REF_ALT_RE = re.compile(
    r"configMapKeyRef:\s*\n"
    r"\s+name:\s+\S+\s*\n"
    r"\s+key:\s+(\S+)",
    re.MULTILINE,
)
_ENV_NAME_BEFORE_CONFIGMAP_RE = re.compile(
    r"-\s+name:\s+(\S+)\s*\n"
    r"\s+valueFrom:\s*\n"
    r"\s+configMapKeyRef:\s*\n"
    r"\s+key:\s+(\S+)\s*\n"
    r"\s+name:\s+(\S+)",
    re.MULTILINE,
)
_ENV_NAME_BEFORE_CONFIGMAP_ALT_RE = re.compile(
    r"-\s+name:\s+(\S+)\s*\n"
    r"\s+valueFrom:\s*\n"
    r"\s+configMapKeyRef:\s*\n"
    r"\s+name:\s+(\S+)\s*\n"
    r"\s+key:\s+(\S+)",
    re.MULTILINE,
)
_KUSTOMIZE_REPLACEMENT_RE = re.compile(
    r"fieldPath:\s*data\.(\S+)",
    re.IGNORECASE,
)
_GO_GETENV_RE = re.compile(r'os\.Getenv\("(RELATED_IMAGE_[^"]+)"\)')


def kustomize_available() -> bool:
    try:
        subprocess.run(
            ["kustomize", "version"],
            capture_output=True,
            check=True,
            timeout=10,
        )
        return True
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def kustomize_build(overlay_dir: Path) -> str:
    result = subprocess.run(
        ["kustomize", "build", str(overlay_dir)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"kustomize build failed for {overlay_dir}:\n{result.stderr}"
        )
    return result.stdout


def _looks_like_image(value: str) -> bool:
    if value.startswith("/") or value.startswith("./"):
        return False
    if "/" in value:
        return True
    if ":" in value or "@" in value:
        return True
    return False


def parse_params_env(params_path: Path) -> Dict[str, str]:
    entries = {}
    if not params_path.exists():
        return entries
    for line in params_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if _looks_like_image(value):
            entries[key] = value
    return entries


def find_params_env_files(overlay_dir: Path) -> List[Path]:
    params_files: List[Path] = []
    visited: Set[Path] = set()
    _collect_params_env(overlay_dir, params_files, visited)
    return params_files


def _collect_params_env(overlay_dir: Path, result: List[Path], visited: Set[Path]):
    resolved = overlay_dir.resolve()
    if resolved in visited:
        return
    visited.add(resolved)

    params = overlay_dir / "params.env"
    if params.exists():
        result.append(params)

    kustomization = overlay_dir / "kustomization.yaml"
    if not kustomization.exists():
        return

    in_resources = False
    for line in kustomization.read_text().splitlines():
        stripped = line.strip()
        if stripped == "resources:":
            in_resources = True
            continue
        if in_resources:
            if stripped.startswith("- "):
                ref = stripped[2:].strip()
                if ref.startswith("#"):
                    continue
                parent = (overlay_dir / ref).resolve()
                if parent.is_dir():
                    _collect_params_env(parent, result, visited)
            elif stripped and not stripped.startswith("#"):
                in_resources = False


def discover_overlays(repo_root: Path) -> List[Path]:
    overlays = []
    for params_env in sorted(repo_root.rglob("params.env")):
        if any(d in params_env.parts for d in SKIP_DIRS):
            continue
        overlay_dir = params_env.parent
        if (overlay_dir / "kustomization.yaml").exists():
            overlays.append(overlay_dir)
    return overlays




def write_probe_params_env(params_path: Path, dest_path: Path, ignored_keys: Set[str]):
    lines = []
    for line in params_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            lines.append(line)
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key not in ignored_keys and _looks_like_image(value.strip()):
            lines.append(f"{key}={PROBE_SENTINEL}")
        else:
            lines.append(line)
    dest_path.write_text("\n".join(lines) + "\n")


def create_probe_overlay(
    overlay_dir: Path,
    params_files: List[Path],
    tmp_base: Path,
    ignored_keys: Set[str],
) -> Optional[Path]:
    """Returns None if params_files fall outside the overlay's config root."""
    overlay_resolved = overlay_dir.resolve()

    config_root = overlay_resolved
    while config_root.name != "config" and config_root.parent != config_root:
        config_root = config_root.parent
    if config_root.name != "config":
        config_root = overlay_resolved.parent

    local_params = [
        p for p in params_files
        if p.resolve().is_relative_to(config_root)
    ]
    if not local_params:
        return None

    resolved_root = config_root.resolve()
    for dirpath, dirnames, filenames in os.walk(config_root, followlinks=False):
        for name in dirnames + filenames:
            entry = Path(dirpath) / name
            if entry.is_symlink():
                target = entry.resolve()
                if not target.is_relative_to(resolved_root):
                    raise ValueError(
                        f"Symlink {entry} targets {target} outside config root {resolved_root}"
                    )

    tmp_config = tmp_base / config_root.name
    shutil.copytree(str(config_root), str(tmp_config))

    for params_file in local_params:
        rel = params_file.resolve().relative_to(config_root)
        tmp_params = tmp_config / rel
        write_probe_params_env(params_file, tmp_params, ignored_keys)

    rel_overlay = overlay_resolved.relative_to(config_root)
    return tmp_config / rel_overlay


def extract_all_images(rendered: str, exclude_patterns: List[str]) -> Dict[str, List[str]]:
    images: Dict[str, List[str]] = {}
    for doc in rendered.split("\n---\n"):
        kind = ""
        name = ""
        for line in doc.splitlines():
            kind_match = re.match(r"^kind:\s+(\S+)", line)
            if kind_match:
                kind = kind_match.group(1)
            name_match = re.match(r"^\s+name:\s+(\S+)", line)
            if name_match and not name:
                name = name_match.group(1)
            if kind and name:
                break
        resource_id = f"{kind}/{name}" if kind and name else kind or "unknown"

        for m in _IMAGE_RE.findall(doc):
            if m in IMAGE_PLACEHOLDERS:
                continue
            if any(fnmatch.fnmatch(m, pat) for pat in exclude_patterns):
                continue
            images.setdefault(m, [])
            if resource_id not in images[m]:
                images[m].append(resource_id)
        for m in _UNQUALIFIED_IMAGE_RE.findall(doc):
            if m in IMAGE_PLACEHOLDERS:
                continue
            if any(fnmatch.fnmatch(m, pat) for pat in exclude_patterns):
                continue
            images.setdefault(m, [])
            if resource_id not in images[m]:
                images[m].append(resource_id)
    return images


def extract_configmap_key_refs(rendered: str) -> Set[str]:
    keys = {m.group(1) for m in _CONFIGMAP_KEY_REF_RE.finditer(rendered)}
    keys |= {m.group(1) for m in _CONFIGMAP_KEY_REF_ALT_RE.finditer(rendered)}
    return keys


def extract_kustomize_replacement_keys(overlay_dir: Path) -> Set[str]:
    keys: Set[str] = set()
    visited: Set[Path] = set()
    _collect_replacement_keys(overlay_dir, keys, visited)
    return keys


def _collect_replacement_keys(overlay_dir: Path, keys: Set[str], visited: Set[Path]):
    resolved = overlay_dir.resolve()
    if resolved in visited:
        return
    visited.add(resolved)

    kustomization = overlay_dir / "kustomization.yaml"
    if kustomization.exists():
        content = kustomization.read_text()
        keys.update(_KUSTOMIZE_REPLACEMENT_RE.findall(content))

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
                    parent = (overlay_dir / ref).resolve()
                    if parent.is_dir():
                        _collect_replacement_keys(parent, keys, visited)
                elif stripped and not stripped.startswith("#"):
                    in_resources = False


def extract_env_configmap_mappings(rendered: str) -> List[Tuple[str, str, str]]:
    results = [
        (m.group(1), m.group(2), m.group(3))
        for m in _ENV_NAME_BEFORE_CONFIGMAP_RE.finditer(rendered)
    ]
    results += [
        (m.group(1), m.group(3), m.group(2))
        for m in _ENV_NAME_BEFORE_CONFIGMAP_ALT_RE.finditer(rendered)
    ]
    return results


def find_go_related_image_envs(repo_root: Path) -> Set[str]:
    envs: Set[str] = set()
    if not repo_root.is_dir():
        return envs
    for go_file in repo_root.rglob("*.go"):
        if any(d in go_file.parts for d in SKIP_DIRS):
            continue
        try:
            content = go_file.read_text()
            envs.update(_GO_GETENV_RE.findall(content))
        except (OSError, UnicodeDecodeError):
            continue
    return envs
