import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Finding:
    severity: str
    file: str
    line: int
    image: str
    message: str


@dataclass
class RuleResult:
    rule: str
    passed: bool = True
    findings: list[Finding] = field(default_factory=list)


@dataclass
class ProductionScope:
    production_files: set  # resolved absolute Paths of production .go files
    method: str            # e.g. "go-import-graph"
    manifest_files: Optional[set] = None  # resolved YAML paths in kustomize/helm graph
    manifest_source: Optional[str] = None  # e.g. "config" (source folder)


def is_in_production_scope(filepath: Path, production_scope: Optional[ProductionScope]) -> Optional[bool]:
    """Check whether a ``.go`` file is inside the production scope.

    Returns True (in scope), False (out of scope), or None (unknown / scope
    not computed).  Only ``.go`` files are evaluated; all other extensions
    return None so that existing rule logic handles them.
    """
    if production_scope is None:
        return None
    if not str(filepath).endswith(".go"):
        return None
    if not production_scope.production_files:
        return None
    return filepath.resolve() in production_scope.production_files


def is_yaml_in_production_scope(filepath: Path, production_scope: Optional[ProductionScope]) -> Optional[bool]:
    """Check whether a YAML file is inside the manifest production scope.

    Returns True (in scope), False (out of scope), or None (unknown / scope
    not computed).  Only ``.yaml`` / ``.yml`` files are evaluated.
    """
    if production_scope is None or production_scope.manifest_files is None:
        return None
    suffix = filepath.suffix.lower()
    if suffix not in (".yaml", ".yml"):
        return None
    return filepath.resolve() in production_scope.manifest_files


SKIP_DIRS = {".git", "vendor", "node_modules", "__pycache__", ".tox", ".devcontainer"}


def find_params_env_dirs(root: Path) -> set[Path]:
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


def get_tracked_files(repo_root: Path) -> Optional[set[Path]]:
    """Return git-tracked files as resolved Paths, or None if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        files = set()
        for rel in result.stdout.split("\0"):
            if rel:
                files.add((repo_root / rel).resolve())
        return files
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
