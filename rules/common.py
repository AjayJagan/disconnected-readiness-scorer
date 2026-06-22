import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class Severity(str, Enum):
    BLOCKER = "blocker"
    INFO = "info"


# ---------------------------------------------------------------------------
# Arch-analyzer typed output
# ---------------------------------------------------------------------------

@dataclass
class CopyInstruction:
    original_sources: list[str] = field(default_factory=list)
    manifest_hint: bool = False


@dataclass
class BuildCommand:
    entry_point: str = ""


@dataclass
class DockerfileInfo:
    path: str = ""
    copy_instructions: list[CopyInstruction] = field(default_factory=list)
    build_commands: list[BuildCommand] = field(default_factory=list)


@dataclass
class KustomizeOverlayRef:
    overlay_path: str = ""
    file_path: str = ""


@dataclass
class KustomizeComponent:
    support_file: str = ""
    overlay_paths: list[str] = field(default_factory=list)


@dataclass
class ArchAnalyzerResult:
    dockerfiles: list[DockerfileInfo] = field(default_factory=list)
    kustomize_overlay_refs: list[KustomizeOverlayRef] = field(default_factory=list)
    kustomize_components: list[KustomizeComponent] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "ArchAnalyzerResult":
        dockerfiles = [
            DockerfileInfo(
                path=df.get("path", ""),
                copy_instructions=[
                    CopyInstruction(
                        original_sources=ci.get("original_sources", []),
                        manifest_hint=ci.get("manifest_hint", False),
                    )
                    for ci in df.get("copy_instructions", [])
                ],
                build_commands=[
                    BuildCommand(entry_point=bc.get("entry_point", ""))
                    for bc in df.get("build_commands", [])
                ],
            )
            for df in data.get("dockerfiles", [])
        ]
        overlay_refs = [
            KustomizeOverlayRef(
                overlay_path=ref.get("overlay_path", ""),
                file_path=ref.get("file_path", ""),
            )
            for ref in data.get("kustomize_overlay_refs", [])
        ]
        components = [
            KustomizeComponent(
                support_file=comp.get("support_file", ""),
                overlay_paths=comp.get("overlay_paths", []),
            )
            for comp in data.get("kustomize_components", [])
        ]
        return cls(
            dockerfiles=dockerfiles,
            kustomize_overlay_refs=overlay_refs,
            kustomize_components=components,
        )


@dataclass
class Finding:
    severity: str
    file: str
    line: int
    image: str
    message: str

    def __post_init__(self):
        try:
            self.severity = Severity(self.severity)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Invalid severity '{self.severity}'. "
                f"Must be one of: {[s.value for s in Severity]}"
            ) from exc


@dataclass
class RuleResult:
    rule: str
    passed: bool = True
    findings: list[Finding] = field(default_factory=list)
    files_checked: list[str] = field(default_factory=list)
    scan_filters: dict = field(default_factory=dict)


@dataclass
class ProductionScope:
    method: str            # e.g. "arch-analyzer-original-sources"
    manifest_files: Optional[set] = None  # resolved YAML paths in kustomize/helm graph
    manifest_source: Optional[str] = None  # e.g. "config" (source folder)
    overlay_paths: Optional[list] = None   # operator-deployed overlay dirs
    production_dirs: Optional[set] = None  # resolved dirs from original_sources (all file types)
    production_files: Optional[set] = None # resolved individual file paths (e.g. go.mod at repo root)


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


def is_file_in_production_scope(filepath: Path, production_scope: Optional[ProductionScope]) -> Optional[bool]:
    """Check whether ANY file type is inside the production scope.

    Returns True (in scope), False (out of scope), or None (unknown / scope not computed).

    Checks production_dirs (any file under a production directory) and
    production_files (individual files like go.mod at repo root).
    """
    if production_scope is None:
        return None
    has_dirs = bool(production_scope.production_dirs)
    has_files = bool(production_scope.production_files)
    if not has_dirs and not has_files:
        return None

    resolved = filepath.resolve()

    if has_files and resolved in production_scope.production_files:
        return True

    if has_dirs:
        for prod_dir in production_scope.production_dirs:
            try:
                resolved.relative_to(prod_dir)
                return True
            except ValueError:
                continue

    return False


def build_overlay_file_map(
    arch_data: "ArchAnalyzerResult | None",
    repo_root: Path,
) -> dict[str, set[Path]]:
    """Build overlay path → files map from kustomize_overlay_refs.

    Returns dict mapping overlay path (e.g., 'overlays/odh') to set of resolved file paths.
    """
    if not arch_data:
        return {}

    overlay_map: dict[str, set[Path]] = {}
    for ref in arch_data.kustomize_overlay_refs:
        if ref.overlay_path and ref.file_path:
            resolved = (repo_root / ref.file_path).resolve()
            overlay_map.setdefault(ref.overlay_path, set()).add(resolved)

    return overlay_map


def is_non_production_overlay_file(
    filepath: Path,
    production_scope,
    overlay_file_map: dict[str, set[Path]],
) -> bool:
    """Check if file is in a non-production overlay.

    Returns True if file is in an overlay that's not in production_scope.overlay_paths.
    """
    if not overlay_file_map or not production_scope or not production_scope.overlay_paths:
        return False

    resolved = filepath.resolve()
    production_overlays = set(production_scope.overlay_paths)

    for overlay_path, files in overlay_file_map.items():
        if resolved in files:
            return overlay_path not in production_overlays

    return False


# General source-scanning exclusions. Rules scanning the target repo import this set.
# Other modules define their own variants:
#   - operator_manifest.py: minimal subset (operator repo has no .tox/.devcontainer)
#   - production_scope.py: adds testdata/docs (irrelevant for production scope computation)
SKIP_DIRS = {".git", "vendor", "node_modules", "__pycache__", ".tox", ".devcontainer"}


def production_scope_relative_dirs(production_scope: Optional[ProductionScope], repo_root: Path) -> list[str] | None:
    if production_scope is None or not production_scope.production_dirs:
        return None
    resolved_root = repo_root.resolve()
    dirs = []
    for d in sorted(production_scope.production_dirs):
        try:
            dirs.append(str(d.relative_to(resolved_root)) + "/")
        except ValueError:
            continue
    return dirs if dirs else None


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





class ConfigError(Exception):
    """Raised when a config file exists but cannot be read or parsed."""


def load_config_file(config_path: Path) -> dict:
    """Load a YAML config file, returning empty dict if missing.

    Raises ConfigError if the file exists but cannot be read or parsed.
    """
    import yaml

    if not config_path.exists():
        return {}
    try:
        text = config_path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Cannot read {config_path}: {exc}") from exc
    try:
        result = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse {config_path}: {exc}") from exc
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise ConfigError(
            f"{config_path} must be a YAML mapping, got {type(result).__name__}"
        )
    return result


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
