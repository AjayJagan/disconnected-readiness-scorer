#!/usr/bin/env python3
"""Detect outbound HTTP calls in runtime code that would fail disconnected."""

import re
import sys
from pathlib import Path

try:
    from rules.common import (
        Finding, RuleResult, get_tracked_files, is_file_in_production_scope,
        SKIP_DIRS, production_scope_relative_dirs,
    )
except ModuleNotFoundError:
    from common import (
        Finding, RuleResult, get_tracked_files, is_file_in_production_scope,
        SKIP_DIRS, production_scope_relative_dirs,
    )

EGRESS_PATTERNS = {
    ".go": [
        (re.compile(r'http\.(Get|Post|Head|Do|NewRequest)\s*\('), "http.{method} call", False),
        (re.compile(r'net\.Dial\s*\('), "net.Dial call", False),
        (re.compile(r'http\.DefaultClient'), "http.DefaultClient usage", False),
        (re.compile(r'exec\.Command\s*\(\s*"git"'), "git subprocess call", False),
    ],
    ".py": [
        (re.compile(r'requests\.(get|post|put|delete|head|patch)\s*\('), "requests.{method} call", False),
        (re.compile(r'urllib\.request\.(urlopen|Request)\s*\('), "urllib.request call", False),
        (re.compile(r'httpx\.(get|post|put|delete|AsyncClient)\s*\('), "httpx call", False),
        (re.compile(r'aiohttp\.ClientSession\s*\('), "aiohttp session", False),
        (re.compile(r'subprocess.*(?:curl|wget)'), "curl/wget via subprocess", False),
        (re.compile(r'subprocess.*(?:hf|huggingface.cli).*download'), "HuggingFace download via subprocess", True),
        (re.compile(r'\bfrom_pretrained\s*\('), "HuggingFace from_pretrained() model download", True),
        (re.compile(r'\bsnapshot_download\s*\('), "HuggingFace snapshot_download() model download", True),
        (re.compile(r'\bload_dataset\s*\('), "HuggingFace load_dataset() download", True),
        (re.compile(r'\bSentenceTransformer\s*\('), "SentenceTransformer model load", True),
        (re.compile(r'\btorch\.hub\.load\s*\('), "torch.hub.load() model download", True),
    ],
    ".ts": [
        (re.compile(r'fetch\s*\('), "fetch() call", False),
        (re.compile(r'axios\.(get|post|put|delete|request)\s*\('), "axios.{method} call", False),
        (re.compile(r'http\.request\s*\('), "http.request call", False),
    ],
    ".tsx": [
        (re.compile(r'fetch\s*\('), "fetch() call", False),
        (re.compile(r'axios\.(get|post|put|delete|request)\s*\('), "axios.{method} call", False),
    ],
    ".sh": [
        (re.compile(r'\bcurl\s+'), "curl invocation", False),
        (re.compile(r'\bwget\s+'), "wget invocation", False),
        (re.compile(r'\b(?:hf|huggingface-cli)\s+download\b'), "HuggingFace model download", True),
    ],
}

INTERNAL_URL_PATTERNS = [
    "kubernetes.default.svc",
    ".svc.cluster.local",
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
]


def has_configurable_url(line: str) -> bool:
    """Check if the URL in this line appears configurable (env var, config, etc)."""
    indicators = ["os.Getenv", "os.environ", "config.", "settings.", "env.",
                   "process.env", "viper.", "${", "getenv"]
    return any(ind in line for ind in indicators)


def run(repo_root: str, production_scope=None, **_kwargs) -> RuleResult:
    root = Path(repo_root)
    result = RuleResult(rule="no-runtime-egress")
    try:
        return _run_impl(root, result, production_scope)
    except Exception as exc:
        import traceback
        print(traceback.format_exc(), file=sys.stderr)
        result.passed = False
        result.findings.append(Finding(
            severity="blocker",
            file="",
            line=0,
            image="",
            message=f"Rule crashed: {type(exc).__name__}: {exc}",
        ))
        return result


def _run_impl(root: Path, result: RuleResult, production_scope) -> RuleResult:
    tracked = get_tracked_files(root)

    result.scan_filters = {
        "globs": ["**/*"],
        "extensions": sorted(EGRESS_PATTERNS.keys()),
        "skip_dirs": sorted(SKIP_DIRS),
        "tracked_files_only": tracked is not None,
    }
    prod_dirs = production_scope_relative_dirs(production_scope, root)
    if prod_dirs is not None:
        result.scan_filters["production_scope_dirs"] = prod_dirs

    for filepath in root.rglob("*"):
        if tracked is not None and filepath.resolve() not in tracked:
            continue
        if any(d in filepath.parts for d in SKIP_DIRS):
            continue

        if is_file_in_production_scope(filepath, production_scope) is False:
            continue

        suffix = filepath.suffix
        if suffix not in EGRESS_PATTERNS:
            continue

        try:
            lines = filepath.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue

        result.files_checked.append(str(filepath.relative_to(root)))
        patterns = EGRESS_PATTERNS[suffix]
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("#"):
                continue

            for pattern, desc, always_network in patterns:
                match = pattern.search(line)
                if not match:
                    continue

                if always_network:
                    severity = "blocker"
                    msg = f"{desc} — requires network access, will fail disconnected."
                else:
                    configurable = has_configurable_url(line)
                    hardcoded_url = bool(re.search(r'https?://', line))

                    internal_url = hardcoded_url and any(
                        p in line for p in INTERNAL_URL_PATTERNS
                    )

                    if hardcoded_url and not configurable and not internal_url:
                        severity = "blocker"
                        msg = f"{desc} with hardcoded external URL — will fail disconnected."
                    elif internal_url:
                        severity = "info"
                        msg = f"{desc} — cluster-internal URL, reachable in disconnected environments."
                    elif configurable:
                        severity = "info"
                        msg = f"{desc} — URL appears configurable. Verify mirror support."
                    elif not hardcoded_url:
                        severity = "info"
                        msg = f"{desc} — no hardcoded URL, likely internal/relative API call."
                    else:
                        severity = "blocker"
                        msg = f"{desc} — endpoint may not be reachable in disconnected environments."

                if severity == "blocker":
                    result.passed = False

                result.findings.append(Finding(
                    severity=severity,
                    file=str(filepath.relative_to(root)),
                    line=i,
                    image="",
                    message=msg,
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
             "message": f.message}
            for f in r.findings
        ],
    }, indent=2))
