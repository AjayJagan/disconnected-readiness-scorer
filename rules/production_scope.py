"""Production-scope analysis using arch-analyzer original_sources.

Uses arch-analyzer's ``dockerfiles[].copy_instructions[].original_sources``
to identify production source directories, and the operator's
``get_all_manifests.sh`` to find production manifest folders.
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

# Extended skip set for scope computation — testdata/docs are never production code.
_SKIP_DIRS = {".git", "vendor", "node_modules", "__pycache__", "testdata", "docs"}

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
    production_dirs: Optional[set[Path]],
) -> set[Path]:
    """Find YAML files referenced by ``//go:embed`` in production .go files."""
    if not production_dirs:
        return set()

    embedded: set[Path] = set()

    # Collect all .go files under production_dirs
    production_go_files: set[Path] = set()
    for prod_dir in production_dirs:
        for dirpath, dirnames, filenames in prod_dir.walk():
            # Skip subdirs in-place
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fname in filenames:
                if fname.endswith(".go"):
                    production_go_files.add((dirpath / fname).resolve())

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
# arch-analyzer original_sources
# ---------------------------------------------------------------------------


_DOCKER_ARG_RE = re.compile(r'\$\{[^}]+\}')


def _is_glob_source(source_stripped: str) -> bool:
    """True if source contains ** wildcards or unresolved ${VAR} tokens."""
    return "**" in source_stripped or bool(_DOCKER_ARG_RE.search(source_stripped))


def _normalize_glob(source_stripped: str) -> str:
    """Convert ${VAR} tokens to glob wildcards.

    Use ** when the token is the entire path component (e.g. ${VAR}/foo),
    * when it appears mid-component (e.g. file.${EXT}.txt) since ** is
    only valid as a complete path segment in Path.glob().
    """
    def _replace(m):
        start, end = m.start(), m.end()
        at_start = start == 0 or source_stripped[start - 1] == "/"
        at_end = end == len(source_stripped) or source_stripped[end] == "/"
        if at_start and at_end:
            return "**"
        return "*"
    return _DOCKER_ARG_RE.sub(_replace, source_stripped)


def _glob_source(source_stripped: str, repo_root: Path, resolved_root: Path) -> list[Path]:
    """Glob a source pattern containing ** or ${VAR} tokens.

    Returns empty list if the pattern is too broad (pure **) or matches nothing.
    """
    glob_pat = _normalize_glob(source_stripped)
    if glob_pat == "**":
        return []
    results = []
    try:
        for match in repo_root.glob(glob_pat):
            resolved = match.resolve()
            if resolved != resolved_root:
                results.append(match)
    except ValueError:
        return []
    return results


def _find_go_module_dir(all_sources: list[str], repo_root: Path) -> Optional[Path]:
    """Find the Go module root by locating go.mod via glob across all sources."""
    for s in all_sources:
        pat = _normalize_glob(s.strip("/"))
        if "go.mod" not in pat:
            continue
        try:
            for match in repo_root.glob(pat):
                if match.is_file() and match.name == "go.mod":
                    return match.parent.resolve()
        except ValueError:
            continue
    return None


def _go_list_production_dirs(module_dir: Path) -> set[Path]:
    """Run go list -deps -json ./... and return set of source directories."""
    try:
        result = subprocess.run(
            ["go", "list", "-deps", "-json", "./..."],
            cwd=module_dir, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return set()
        raw = result.stdout.strip()
        if not raw:
            return set()
        # go list outputs multiple JSON objects; wrap into array
        raw_array = "[" + raw.replace("}\n{", "},\n{") + "]"
        packages = json.loads(raw_array)
        dirs: set[Path] = set()
        for pkg in packages:
            if pkg.get("Standard"):
                continue
            pkg_dir = pkg.get("Dir")
            if pkg_dir:
                dirs.add(Path(pkg_dir).resolve())
        return dirs
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError, KeyError):
        return set()


def _nearest_package_json_dir(dockerfile_dir: Path, repo_root: Path) -> Path:
    """Walk up from dockerfile_dir to find nearest package.json (before repo_root)."""
    current = dockerfile_dir.resolve()
    resolved_root = repo_root.resolve()
    while current != resolved_root and current != current.parent:
        if (current / "package.json").exists():
            return current
        current = current.parent
    return dockerfile_dir.resolve()


def _is_js_monorepo(repo_root: Path) -> bool:
    """True if repo root has a package.json with a workspaces field."""
    pkg_json = repo_root / "package.json"
    if not pkg_json.exists():
        return False
    try:
        data = json.loads(pkg_json.read_text())
        return bool(data.get("workspaces"))
    except (OSError, json.JSONDecodeError):
        return False


def _extract_production_sources_from_arch_data(
    arch_data: dict, repo_root: Path,
    docker_contexts: Optional[dict] = None,
) -> tuple[set[Path], set[Path], set[Path], list[str]]:
    """Extract production sources from arch-analyzer dockerfiles.

    Handles three source cases in order:
    1. Sources with ``${VAR}`` tokens — resolved by replacing with ``**`` glob.
    2. Sources resolving to repo_root (e.g. ``COPY . .``) — scoped via Go/JS heuristics.
    3. Literal paths — resolved directly.

    Returns:
        production_dirs: resolved directory paths
        production_files: resolved individual file paths
        manifest_dirs: resolved directory paths with manifest_hint=true
        manifest_source_folders: folder names (str) for manifest sources
    """
    production_dirs: set[Path] = set()
    production_files: set[Path] = set()
    manifest_dirs: set[Path] = set()
    manifest_source_folders: list[str] = []
    resolved_root = repo_root.resolve()
    js_monorepo = _is_js_monorepo(repo_root)
    docker_contexts = docker_contexts or {}

    def _inside_repo(path: Path) -> bool:
        try:
            path.resolve().relative_to(resolved_root)
            return True
        except ValueError:
            return False

    for dockerfile in arch_data.get("dockerfiles", []):
        dockerfile_path = dockerfile.get("path", "")
        dockerfile_dir = (repo_root / dockerfile_path).parent

        # Add build_commands entry_points as additional production sources
        for bc in dockerfile.get("build_commands", []):
            ep = bc.get("entry_point", "")
            if ep:
                ep_path = repo_root / ep.strip("./")
                if not _inside_repo(ep_path):
                    continue
                if ep_path.is_dir():
                    production_dirs.add(ep_path.resolve())
                elif ep_path.is_file():
                    production_files.add(ep_path.resolve())

        # Collect all original_sources across all copy instructions for this Dockerfile
        all_sources: list[str] = [
            s
            for ci in dockerfile.get("copy_instructions", [])
            for s in ci.get("original_sources", [])
        ]

        for copy_instr in dockerfile.get("copy_instructions", []):
            original_sources = copy_instr.get("original_sources", [])
            is_manifest = copy_instr.get("manifest_hint", False)

            for source in original_sources:
                source_stripped = source.strip("/")

                # Case 1: source contains ** or ${VAR} — resolve via glob
                if _is_glob_source(source_stripped):
                    for match in _glob_source(source_stripped, repo_root, resolved_root):
                        if not _inside_repo(match):
                            continue
                        resolved = match.resolve()
                        if is_manifest:
                            if match.is_dir():
                                manifest_dirs.add(resolved)
                                if match.parent.resolve() == resolved_root:
                                    manifest_source_folders.append(match.name)
                        else:
                            if match.is_dir():
                                production_dirs.add(resolved)
                            elif match.is_file():
                                production_files.add(resolved)
                    continue

                source_path = repo_root / source_stripped

                # Case 2: source resolves to repo_root — apply scoping heuristics
                if source_path.resolve() == resolved_root and not is_manifest:
                    # Config override: explicit context for this Dockerfile
                    if dockerfile_path in docker_contexts:
                        ctx_dir = repo_root / docker_contexts[dockerfile_path]
                        if ctx_dir.is_dir() and _inside_repo(ctx_dir):
                            production_dirs.add(ctx_dir.resolve())
                        continue

                    # Heuristic A: Go Dockerfile → go list -deps
                    go_module_dir = _find_go_module_dir(all_sources, repo_root)
                    if go_module_dir:
                        go_dirs = _go_list_production_dirs(go_module_dir)
                        production_dirs.update(
                            d for d in go_dirs if _inside_repo(d)
                        )
                        continue

                    # Heuristic B: JS monorepo → nearest package.json
                    if js_monorepo:
                        pkg_dir = _nearest_package_json_dir(dockerfile_dir, repo_root)
                        if pkg_dir.resolve() != resolved_root and _inside_repo(pkg_dir):
                            production_dirs.add(pkg_dir.resolve())
                    continue

                # Case 3: literal path
                if not _inside_repo(source_path):
                    continue
                if source_path.is_dir():
                    resolved = source_path.resolve()
                    if resolved == resolved_root:
                        continue  # skip bare repo root without heuristic context
                    if is_manifest:
                        manifest_dirs.add(resolved)
                        if source_path.parent.resolve() == resolved_root:
                            manifest_source_folders.append(source_path.name)
                    else:
                        production_dirs.add(resolved)
                elif source_path.is_file():
                    resolved = source_path.resolve()
                    if is_manifest:
                        manifest_dirs.add(source_path.parent.resolve())
                    else:
                        production_files.add(resolved)

    manifest_source_folders = sorted(set(manifest_source_folders))
    return production_dirs, production_files, manifest_dirs, manifest_source_folders


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_production_scope(
    repo_root: Path,
    manifest_source_folders: Optional[list] = None,
    overlay_paths: Optional[list] = None,
    arch_data: Optional[dict] = None,
    docker_contexts: Optional[dict] = None,
) -> Optional[ProductionScope]:
    """Compute the production file scope for a repository using arch-analyzer.

    *manifest_source_folders* is an optional list of relative directories
    (e.g. ``["config"]``) containing production manifests.  When provided,
    the kustomize / helm graph is walked to populate ``manifest_files``.

    *overlay_paths* is an optional list of overlay dirs (relative to the
    manifest source folder) that the operator actually deploys.  Passed
    through to ``ProductionScope`` for use by ``params_env``.

    *arch_data* is architecture data from arch-analyzer. Uses
    ``dockerfiles[].copy_instructions[].original_sources`` for production scope.

    Returns ``None`` when neither production dirs nor manifest scope can be determined.
    """
    repo_root = Path(repo_root)

    production_dirs: Optional[set[Path]] = None
    production_files: Optional[set[Path]] = None
    method = ""

    # --- arch-analyzer original_sources ---
    if arch_data:
        arch_prod_dirs, arch_prod_files, arch_manifest_dirs, arch_manifest_folders = (
            _extract_production_sources_from_arch_data(
                arch_data, repo_root, docker_contexts=docker_contexts,
            )
        )
        if arch_prod_dirs or arch_prod_files:
            production_dirs = arch_prod_dirs or None
            production_files = arch_prod_files or None
            method = "arch-analyzer-original-sources"
        if arch_manifest_folders and not manifest_source_folders:
            manifest_source_folders = arch_manifest_folders

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
    embedded_yamls = _collect_go_embedded_yamls(repo_root, production_dirs)
    if embedded_yamls:
        if manifest_files is None:
            manifest_files = set()
        manifest_files.update(embedded_yamls)

    if production_dirs is None and production_files is None and manifest_files is None:
        return None

    if not method:
        method = "manifest-only"

    return ProductionScope(
        method=method,
        manifest_files=manifest_files,
        manifest_source=manifest_source_str,
        overlay_paths=overlay_paths,
        production_dirs=production_dirs,
        production_files=production_files,
    )
