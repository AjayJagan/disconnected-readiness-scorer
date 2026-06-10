"""Production-scope analysis for Go repositories and manifest files.

Go scope: Parses Dockerfiles to find Go build targets, then uses
``go list -deps`` to compute the set of source files compiled into the
production binary.

Manifest scope: Uses the operator's ``get_all_manifests.sh`` to find
which source folder contains production manifests, then walks the
kustomize graph (or includes all helm chart files) to identify which
YAML files are actually deployed.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

try:
    from rules.common import ProductionScope
except (ImportError, ModuleNotFoundError):
    from common import ProductionScope

# ---------------------------------------------------------------------------
# Dockerfile parsing
# ---------------------------------------------------------------------------

_FROM_RE = re.compile(r"^FROM\s+\S+(?:\s+AS\s+(\S+))?", re.IGNORECASE)
_COPY_FROM_RE = re.compile(
    r"^COPY\s+--from=(\S+)\s+", re.IGNORECASE,
)
_RUN_GO_BUILD_RE = re.compile(
    r"go\s+build\b.*?(\.(?:/\S+)?)", re.IGNORECASE,
)


def _join_continuations(lines: list[str]) -> list[str]:
    """Merge backslash-continued lines."""
    merged: list[str] = []
    buf = ""
    for line in lines:
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
        else:
            buf += stripped
            merged.append(buf)
            buf = ""
    if buf:
        merged.append(buf)
    return merged


def _parse_dockerfile(path: Path) -> list[str]:
    """Return all Go build packages from *path*.

    Scans for ``RUN go build ... ./cmd/foo`` in all stages, collecting every
    unique package target found.
    """
    try:
        raw_lines = path.read_text().splitlines()
    except OSError:
        return []

    lines = _join_continuations(raw_lines)

    packages: list[str] = []
    seen: set[str] = set()

    stage_idx = -1
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        m_from = _FROM_RE.match(stripped)
        if m_from:
            stage_idx += 1
            continue

        if stripped.upper().startswith("RUN "):
            for m_build in _RUN_GO_BUILD_RE.finditer(stripped):
                pkg = m_build.group(1)
                if stage_idx >= 0 and pkg not in seen:
                    seen.add(pkg)
                    packages.append(pkg)

    return packages


# ---------------------------------------------------------------------------
# Dockerfile discovery
# ---------------------------------------------------------------------------

_DOCKERFILE_NAMES = {"Dockerfile", "Containerfile"}
_SKIP_DIRS = {".git", "vendor", "node_modules", "__pycache__", "testdata", "docs"}


def _find_all_dockerfiles(repo_root: Path) -> list[Path]:
    """Return all Dockerfiles in the repo, sorted by relevance (root first)."""
    found: list[Path] = []
    for filepath in repo_root.rglob("*"):
        if any(d in filepath.parts for d in _SKIP_DIRS):
            continue
        if filepath.name in _DOCKERFILE_NAMES or filepath.name.endswith(".Dockerfile"):
            found.append(filepath)

    def _sort_key(p: Path) -> tuple:
        depth = len(p.relative_to(repo_root).parts)
        return (depth, str(p))

    return sorted(found, key=_sort_key)


# ---------------------------------------------------------------------------
# Go entrypoint heuristic
# ---------------------------------------------------------------------------


def _find_go_entrypoints_heuristic(repo_root: Path) -> list[tuple[str, Path]]:
    """Find ``cmd/*/main.go`` patterns, including in subdirectories.

    Returns list of ``(package_path, go_mod_dir)`` tuples.
    """
    results: list[tuple[str, Path]] = []
    for main_go in sorted(repo_root.rglob("main.go")):
        if any(d in main_go.parts for d in _SKIP_DIRS):
            continue
        parent = main_go.parent
        if parent.name == "cmd" or (parent.parent and parent.parent.name == "cmd"):
            go_mod_dir = _find_go_mod_dir(main_go)
            if go_mod_dir is None:
                continue
            rel = str(parent.relative_to(go_mod_dir))
            pkg = f"./{rel}" if not rel.startswith(".") else rel
            results.append((pkg, go_mod_dir))
    return results


def _find_go_mod_dir(filepath: Path) -> Optional[Path]:
    """Walk up from *filepath* to find the nearest ``go.mod``."""
    current = filepath.parent
    while current != current.parent:
        if (current / "go.mod").is_file():
            return current
        current = current.parent
    return None


# ---------------------------------------------------------------------------
# go list -deps
# ---------------------------------------------------------------------------


def _go_list_deps(repo_root: Path, package: str) -> Optional[set[Path]]:
    """Run ``go list -deps -json <package>`` and collect production ``.go`` files."""
    try:
        result = subprocess.run(
            ["go", "list", "-deps", "-json", package],
            capture_output=True, text=True, timeout=60,
            cwd=str(repo_root),
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        return None

    production_files: set[Path] = set()
    repo_resolved = repo_root.resolve()

    for obj in _iter_json_objects(result.stdout):
        pkg_dir = obj.get("Dir")
        if not pkg_dir:
            continue

        pkg_path = Path(pkg_dir).resolve()
        try:
            pkg_path.relative_to(repo_resolved)
        except ValueError:
            continue

        for fname in obj.get("GoFiles", []):
            production_files.add((pkg_path / fname).resolve())
        for fname in obj.get("CgoFiles", []):
            production_files.add((pkg_path / fname).resolve())

    return production_files if production_files else None


def _iter_json_objects(text: str):
    """Yield decoded JSON objects from a concatenated JSON stream."""
    decoder = json.JSONDecoder()
    idx = 0
    length = len(text)
    while idx < length:
        while idx < length and text[idx] in " \t\r\n":
            idx += 1
        if idx >= length:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
            yield obj
            idx = end
        except json.JSONDecodeError:
            break


# ---------------------------------------------------------------------------
# Manifest (kustomize / helm) scope
# ---------------------------------------------------------------------------

_YAML_SUFFIXES = frozenset((".yaml", ".yml"))


def _collect_kustomize_dirs(root_dir: Path) -> set[Path]:
    """Walk kustomization.yaml ``resources:`` recursively, collecting directories."""
    dirs: set[Path] = set()

    for kustomization in root_dir.rglob("kustomization.yaml"):
        _walk_kustomize_resources(kustomization.parent, dirs)

    return dirs


def _walk_kustomize_resources(overlay_dir: Path, dirs: set[Path]):
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
                    _walk_kustomize_resources(target, dirs)
                elif target.is_file() and target.suffix in _YAML_SUFFIXES:
                    dirs.add(target)
            elif stripped and not stripped.startswith("#"):
                in_resources = False


_GO_EMBED_RE = re.compile(r'//go:embed\s+(.+)')


def _collect_go_embedded_yamls(
    repo_root: Path,
    production_go_files: Optional[set[Path]],
) -> set[Path]:
    """Find YAML files referenced by ``//go:embed`` in production Go files."""
    if not production_go_files:
        return set()

    embedded: set[Path] = set()
    for go_file in production_go_files:
        try:
            content = go_file.read_text()
        except (OSError, UnicodeDecodeError):
            continue

        for match in _GO_EMBED_RE.finditer(content):
            for pattern in match.group(1).split():
                if "*" in pattern:
                    parent = go_file.parent / Path(pattern).parent
                    if parent.is_dir():
                        for f in parent.rglob(Path(pattern).name):
                            if f.suffix in _YAML_SUFFIXES:
                                embedded.add(f.resolve())
                else:
                    target = (go_file.parent / pattern).resolve()
                    if target.is_file() and target.suffix in _YAML_SUFFIXES:
                        embedded.add(target)

    return embedded


def collect_manifest_scope_files(source_dir: Path) -> Optional[set[Path]]:
    """Collect production YAML files from a source directory.

    Auto-detects kustomize (walk graph) vs helm (include all chart files).
    Returns ``None`` if the directory does not exist.
    """
    if not source_dir.is_dir():
        return None

    has_chart = (source_dir / "Chart.yaml").is_file()
    has_kustomize = any(source_dir.rglob("kustomization.yaml"))

    if not has_chart and not has_kustomize:
        return None

    files: set[Path] = set()

    _HELM_SKIP_PARTS = {"tests", "test", "examples"}
    if has_chart:
        for f in source_dir.rglob("*"):
            if f.is_file() and f.suffix in _YAML_SUFFIXES:
                rel = f.relative_to(source_dir)
                if _HELM_SKIP_PARTS.intersection(rel.parts):
                    continue
                files.add(f.resolve())

    if has_kustomize:
        kustomize_dirs = _collect_kustomize_dirs(source_dir)
        for d in kustomize_dirs:
            if not d.is_dir():
                if d.is_file():
                    files.add(d)
                continue
            for f in d.iterdir():
                if f.is_file() and f.suffix in _YAML_SUFFIXES:
                    files.add(f.resolve())

    return files if files else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_production_scope(
    repo_root: Path,
    manifest_source_folders: Optional[list] = None,
    overlay_paths: Optional[list] = None,
) -> Optional[ProductionScope]:
    """Compute the production file scope for a repository.

    *manifest_source_folders* is an optional list of relative directories
    (e.g. ``["config"]``) containing production manifests.  When provided,
    the kustomize / helm graph is walked to populate ``manifest_files``.

    *overlay_paths* is an optional list of overlay dirs (relative to the
    manifest source folder) that the operator actually deploys.  Passed
    through to ``ProductionScope`` for use by ``params_env``.

    Returns ``None`` only when neither Go scope nor manifest scope can be
    determined.
    """
    repo_root = Path(repo_root)

    # --- Go scope (all Dockerfiles + heuristic entrypoints) ---
    production_files: Optional[set[Path]] = None
    seen_entrypoints: set[tuple[str, str]] = set()

    is_go_repo = (repo_root / "go.mod").exists()

    dockerfiles = _find_all_dockerfiles(repo_root) if is_go_repo else []
    for dockerfile in dockerfiles:
        packages = _parse_dockerfile(dockerfile)
        if not packages:
            continue
        go_mod_dir = _find_go_mod_dir(dockerfile)
        if go_mod_dir is None:
            go_mod_dir = repo_root
        for pkg in packages:
            key = (pkg, str(go_mod_dir))
            if key in seen_entrypoints:
                continue
            seen_entrypoints.add(key)

            deps = _go_list_deps(go_mod_dir, pkg)
            if deps:
                if production_files is None:
                    production_files = set()
                production_files.update(deps)

    for pkg, go_mod_dir in (_find_go_entrypoints_heuristic(repo_root) if is_go_repo else []):
        key = (pkg, str(go_mod_dir))
        if key in seen_entrypoints:
            continue
        seen_entrypoints.add(key)

        deps = _go_list_deps(go_mod_dir, pkg)
        if deps:
            if production_files is None:
                production_files = set()
            production_files.update(deps)

    # --- Manifest scope ---
    manifest_files: Optional[set[Path]] = None
    manifest_source_str: Optional[str] = None

    if manifest_source_folders:
        manifest_source_str = ",".join(manifest_source_folders)
        all_manifest_files: set[Path] = set()
        for folder in manifest_source_folders:
            source_dir = repo_root / folder
            folder_files = collect_manifest_scope_files(source_dir)
            if folder_files:
                all_manifest_files.update(folder_files)
        if all_manifest_files:
            manifest_files = all_manifest_files

    # --- Go-embedded YAMLs ---
    embedded_yamls = _collect_go_embedded_yamls(repo_root, production_files)
    if embedded_yamls:
        if manifest_files is None:
            manifest_files = set()
        manifest_files.update(embedded_yamls)

    if production_files is None and manifest_files is None:
        return None

    return ProductionScope(
        production_files=production_files or set(),
        method="go-import-graph" if production_files else "manifest-only",
        manifest_files=manifest_files,
        manifest_source=manifest_source_str,
        overlay_paths=overlay_paths,
    )
