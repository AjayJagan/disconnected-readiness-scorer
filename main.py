#!/usr/bin/env python3
"""Disconnected Readiness Scorer — orchestrator.

Runs all (or selected) rules against a target repo and produces
an aggregate READY / WARNING / NOT READY score.
"""

import argparse
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import date
from fnmatch import fnmatch
from pathlib import Path

import yaml

from rules.common import (
    Finding, RuleResult,
)
from rules.production_scope import compute_production_scope

SEVERITY_ORDER = {"blocker": 0, "info": 1}
CENTRAL_CONFIG_PATH = "config/config.yaml"

RULE_REGISTRY = {
    "csv": {
        "module": "rules.image_manifest_complete",
        "name": "image-manifest-complete",
        "needs_manifest": True,
    },
    "tags": {
        "module": "rules.no_image_tags",
        "name": "no-image-tags",
    },
    "egress": {
        "module": "rules.no_runtime_egress",
        "name": "no-runtime-egress",
    },
    "python": {
        "module": "rules.python_imports",
        "name": "python-imports-bundled",
    },
    "params_env": {
        "module": "rules.params_env",
        "name": "params-env-wiring",
        "needs_manifest": True,
    },
    "manifest": {
        "module": "rules.operator_manifest",
        "name": "operator-manifest",
        "is_manifest_rule": True,
    },
}

DEFAULT_RULES = [k for k, v in RULE_REGISTRY.items() if not v.get("is_manifest_rule")]


def _load_yaml_file(config_path):
    """Load a YAML file, returning parsed dict or None if missing."""
    if not Path(config_path).exists():
        return None
    try:
        text = Path(config_path).read_text()
    except OSError as exc:
        raise ValueError(f"Cannot read {config_path}: {exc}") from exc
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(
            f"Failed to parse {config_path}: {exc}"
        ) from exc


def load_central_config(config_path):
    """Load unified central config (config/config.yaml).

    Returns dict with keys: registries, known_mirrors, exceptions.
    """
    raw = _load_yaml_file(config_path)
    if raw is None:
        return {"registries": [], "known_mirrors": {}, "exceptions": []}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{config_path} must be a YAML mapping, got {type(raw).__name__}"
        )
    exceptions = raw.get("exceptions") or []
    _validate_exceptions(exceptions, config_path)
    return {
        "registries": raw.get("registries") or [],
        "known_mirrors": raw.get("known_mirrors") or {},
        "exceptions": exceptions,
    }


def _validate_exceptions(exceptions, config_path):
    """Validate exception entries have required fields."""
    for i, exc in enumerate(exceptions):
        if not isinstance(exc, dict):
            raise ValueError(
                f"Exception entry {i + 1} in {config_path} "
                f"must be a mapping, got {type(exc).__name__}"
            )
        if not exc.get("rule"):
            raise ValueError(
                f"Exception entry {i + 1} in {config_path} "
                f"is missing required 'rule' field"
            )
        if not exc.get("reason"):
            raise ValueError(
                f"Exception entry {i + 1} (rule={exc.get('rule', '?')}) "
                f"in {config_path} is missing required 'reason' field"
            )


def _path_matches(filepath: str, pattern: str) -> bool:
    """Match a file path against a glob pattern.

    Handles ``**/X`` patterns by also matching ``X`` at the root level
    (fnmatch does not expand ``**`` as a recursive wildcard).
    Patterns ending with ``/**`` also match the directory itself (without
    trailing content), so ``**/config/scorecard/**`` matches both
    ``config/scorecard`` and ``config/scorecard/foo.yaml``.
    Also matches against the filename alone for suffix patterns like ``*_test.go``.
    """
    if fnmatch(filepath, pattern):
        return True
    if pattern.startswith("**/"):
        if fnmatch(filepath, pattern[3:]):
            return True
    if pattern.endswith("/**"):
        dir_pattern = pattern[:-3]
        if fnmatch(filepath, dir_pattern):
            return True
        if dir_pattern.startswith("**/") and fnmatch(filepath, dir_pattern[3:]):
            return True
    return fnmatch(filepath.rsplit("/", 1)[-1], pattern)


def apply_exceptions(results, exceptions, repo_name):
    """Downgrade blocker findings that match configured exceptions to info severity."""
    for result in results:
        for finding in result.findings:
            if finding.severity != "blocker":
                continue
            for exc in exceptions:
                exc_rule = exc.get("rule", "")
                if exc_rule != "*":
                    exc_rules = [r.strip() for r in exc_rule.split(",")]
                    if result.rule not in exc_rules:
                        continue
                exc_repo = exc.get("repo")
                if exc_repo:
                    if "/" in exc_repo:
                        if exc_repo != repo_name:
                            continue
                    else:
                        if exc_repo != repo_name.rsplit("/", 1)[-1]:
                            continue
                exc_paths = exc.get("paths") or []
                exc_path = exc.get("path")
                if exc_path:
                    exc_paths = exc_paths + [exc_path]
                if exc_paths:
                    if not any(_path_matches(finding.file, p) for p in exc_paths):
                        continue
                exc_image = exc.get("image")
                if exc_image:
                    if not fnmatch(finding.image, exc_image):
                        continue
                exc_message = exc.get("message")
                if exc_message:
                    if not fnmatch(finding.message, exc_message):
                        continue
                reason = exc.get("reason", "configured exception")
                finding.message += f" [Exception: {reason}]"
                finding.severity = "info"
                break
        if not any(f.severity == "blocker" for f in result.findings):
            result.passed = True




def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Score a repo's disconnected / air-gapped readiness.",
    )
    parser.add_argument(
        "repo_root", nargs="?", default=".",
        help="Path to the target repository (default: current directory)",
    )
    parser.add_argument(
        "--rules", default="all",
        help="Comma-separated rule aliases, 'all', or empty (default: all). "
             "'all' or empty runs every registered rule. "
             f"Available: {', '.join(RULE_REGISTRY)}",
    )
    parser.add_argument(
        "--report", choices=["markdown", "json"], default="markdown",
        help="Output format (default: markdown)",
    )
    parser.add_argument(
        "--operator-path",
        help="Path to a pre-cloned opendatahub-operator. "
             "If omitted, clones to a temporary directory when needed.",
    )
    parser.add_argument(
        "--output", "-o",
        help="Write the report to a file instead of stdout.",
    )
    parser.add_argument(
        "--config",
        help=f"Path to central config.yaml (default: {CENTRAL_CONFIG_PATH}).",
    )
    parser.add_argument(
        "--no-production-scope", action="store_true",
        help="Disable production-scope analysis (Dockerfile + go list). "
             "All files are scanned at full severity.",
    )
    parser.add_argument(
        "--timing", action="store_true",
        help="Print per-step wall time to stderr for performance debugging.",
    )
    return parser.parse_args(argv)


def resolve_rules(rules_arg):
    if not rules_arg or rules_arg == "all":
        return list(DEFAULT_RULES)
    keys = [k.strip() for k in rules_arg.split(",")]
    for k in keys:
        if k not in RULE_REGISTRY:
            raise SystemExit(f"Unknown rule '{k}'. Available: {', '.join(RULE_REGISTRY)}")
    return keys


def load_manifest(operator_path):
    mod = importlib.import_module("rules.operator_manifest")
    target = Path(operator_path)
    if not (target / ".git").exists():
        print("  Cloning opendatahub-operator (shallow)...", file=sys.stderr)
        try:
            mod.clone_operator(target)
        except Exception as exc:
            raise SystemExit(
                f"Failed to clone opendatahub-operator: {exc}\n"
                f"Use --operator-path to provide a pre-cloned copy."
            ) from exc
    manifest = mod.build_manifest(str(target))
    env_vars = set()
    for e in manifest.images:
        env_vars.add(e.env_var)
        if e.manifest_key:
            env_vars.add(e.manifest_key)
    return manifest, env_vars


def adapt_manifest_result(manifest):
    # passed stays True: manifest issues are informational/warning only,
    # not blockers — the csv rule handles actual disconnected-readiness failures.
    result = RuleResult(rule="operator-manifest")
    all_vars = sorted(set(e.env_var for e in manifest.images))
    result.findings.append(Finding(
        severity="info",
        file="",
        line=0,
        image="",
        message=f"Parsed {len(all_vars)} RELATED_IMAGE vars "
                f"across {len(manifest.components)} components.",
    ))
    if manifest.known_issues:
        for issue in manifest.known_issues:
            result.findings.append(Finding(
                severity="info",
                file="",
                line=0,
                image="",
                message=f"Known issue in operator manifest: {issue}",
            ))
    return result


def compute_score(results):
    if any(not r.passed for r in results):
        return "NOT READY"
    return "READY"


def print_summary(score, results):
    print(f"\nDisconnected Readiness Score: {score}\n", file=sys.stderr)
    for r in results:
        blockers = sum(1 for f in r.findings if f.severity == "blocker")

        if blockers:
            tag = "FAIL"
            summary_msg = f"{blockers} blocker(s)"
        else:
            tag = "PASS"
            summary_msg = "All checks passed"

        print(f"  {tag:<9} {r.rule:<25} {summary_msg}", file=sys.stderr)

    total_blockers = sum(1 for r in results for f in r.findings if f.severity == "blocker")
    total_passed = sum(1 for r in results if r.passed)
    print(f"\nBlockers: {total_blockers} | Passed: {total_passed}", file=sys.stderr)


def render_json(score, results, repo_name):
    snippets = _build_exception_snippets(results)
    data = {
        "repo": repo_name,
        "date": date.today().isoformat(),
        "score": score,
        "rules": [
            {
                "name": r.rule,
                "passed": r.passed,
                "blockers": sum(1 for f in r.findings if f.severity == "blocker"),
                "infos": sum(1 for f in r.findings if f.severity == "info"),
                "findings": [
                    {"severity": f.severity, "file": f.file, "line": f.line,
                     "image": f.image, "message": f.message}
                    for f in sorted(r.findings,
                                    key=lambda f: SEVERITY_ORDER.get(f.severity, 99))
                ],
            }
            for r in results
        ],
    }
    if snippets:
        data["false_positive_help"] = {
            "exception_snippets": snippets,
        }
    return json.dumps(data, indent=2)


def _render_template_simple(template_str, context):
    """Minimal Jinja2-compatible renderer for the report template.

    Handles {{ var }}, {{ var | upper }}, and {% for x in y %}...{% endfor %}.
    """
    def resolve(expr, local_ctx):
        expr = expr.strip()
        filt = None
        if "|" in expr:
            expr, filt = expr.rsplit("|", 1)
            expr = expr.strip()
            filt = filt.strip()
        parts = expr.split(".")
        val = local_ctx
        for p in parts:
            if isinstance(val, dict):
                val = val.get(p, "")
            else:
                val = getattr(val, p, "")
        val = str(val)
        if filt == "upper":
            val = val.upper()
        return val

    for_pattern = re.compile(
        r'\{%\s*for\s+(\w+)\s+in\s+(\w+)\s*%\}(.*?)\{%\s*endfor\s*%\}',
        re.DOTALL,
    )

    def expand_for(m):
        var_name = m.group(1)
        collection_name = m.group(2)
        body = m.group(3).strip("\n")
        if re.search(r'\{%\s*for\s+', body):
            raise ValueError("Nested {% for %} blocks are not supported by the built-in template renderer.")
        collection = context.get(collection_name, [])
        pieces = []
        for item in collection:
            local = {**context, var_name: item}
            rendered = re.sub(
                r'\{\{\s*(.+?)\s*\}\}',
                lambda mv: resolve(mv.group(1), local),
                body,
            )
            pieces.append(rendered)
        return "\n".join(pieces)

    output = for_pattern.sub(expand_for, template_str)
    output = re.sub(
        r'\{\{\s*(.+?)\s*\}\}',
        lambda mv: resolve(mv.group(1), context),
        output,
    )
    return output


def _escape_md_cell(value):
    """Escape a string for use inside a Markdown table cell."""
    s = str(value).replace("|", "\\|").replace("\n", " ")
    return s.replace("<", "&lt;").replace(">", "&gt;")


def _build_exception_snippets(results):
    """Build pre-filled exception YAML entries from blocker findings."""
    snippets = []
    for r in results:
        for f in r.findings:
            if f.severity != "blocker":
                continue
            snippet = {"rule": r.rule, "file": f.file, "line": f.line}
            if f.image:
                snippet["image"] = f.image
            if f.message:
                snippet["message"] = f.message
            snippets.append(snippet)
    return snippets


def _build_false_positive_section(snippets):
    """Build the Reporting False Positives markdown section from blocker snippets."""
    if not snippets:
        return ""

    count = len(snippets)
    noun = "finding" if count == 1 else "findings"
    readme_url = (
        "https://github.com/opendatahub-io/disconnected-readiness-scorer"
        "#reporting-false-positives"
    )
    lines = [
        "## Reporting False Positives",
        "",
        f"{count} blocker {noun} above may be false positives.",
        f"To unblock your PR, add an exception to the central config file.",
        f"See [{readme_url}]({readme_url}) for the format and required fields.",
        "",
    ]

    return "\n".join(lines)


def render_markdown(score, results, repo_name):
    template_path = Path(__file__).parent / "templates" / "report.md"
    try:
        template_str = template_path.read_text()
    except OSError:
        return f"# Disconnected Readiness Report\n\n**Score:** {score}\n"

    blocker_rows = []
    for r in results:
        for f in r.findings:
            if f.severity == "blocker":
                blocker_rows.append({
                    "rule": _escape_md_cell(r.rule),
                    "file": _escape_md_cell(f.file),
                    "line": f.line if f.line else "",
                    "message": _escape_md_cell(f.message),
                })

    context = {
        "repo_name": repo_name,
        "date": date.today().isoformat(),
        "score": score,
        "rules": [
            {
                "name": r.rule,
                "result": "PASS" if r.passed else "FAIL",
                "blockers": sum(1 for f in r.findings if f.severity == "blocker"),
            }
            for r in results
        ],
        "blockers": blocker_rows,
        "false_positive_section": _build_false_positive_section(
            _build_exception_snippets(results)
        ),
    }

    try:
        import jinja2
        env = jinja2.Environment(autoescape=False, trim_blocks=True, lstrip_blocks=True)
        tmpl = env.from_string(template_str)
        return tmpl.render(**context)
    except ImportError:
        return _render_template_simple(template_str, context)


def _get_repo_name(repo_root):
    """Derive org/name from git remote, fall back to directory basename."""
    try:
        url = subprocess.check_output(
            ["git", "-C", repo_root, "remote", "get-url", "origin"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        url = re.sub(r"\.git$", "", url)
        # Normalize SSH git@host:org/repo → org/repo
        ssh_match = re.match(r"[^@]+@[^:]+:(.+)", url)
        if ssh_match:
            url = ssh_match.group(1)
        parts = url.rstrip("/").rsplit("/", 2)
        if len(parts) >= 2:
            return f"{parts[-2]}/{parts[-1]}"
    except (subprocess.CalledProcessError, OSError):
        pass
    return os.path.basename(repo_root)


def _load_central_exceptions(args):
    """Load central exceptions.

    Returns (exceptions, error_result_or_None).
    """
    config_path = args.config or str(
        Path(__file__).parent / CENTRAL_CONFIG_PATH
    )
    central = load_central_config(config_path)
    exceptions = central["exceptions"]

    return exceptions, None


def _run(args, operator_path):
    repo_root = os.path.abspath(args.repo_root)
    repo_name = _get_repo_name(repo_root)
    selected = resolve_rules(args.rules)
    timing = getattr(args, "timing", False)

    def _tlog(label, elapsed):
        if timing:
            print(f"  [timing] {label}: {elapsed:.1f}s", file=sys.stderr)

    t_total = time.monotonic()

    manifest = None
    manifest_env_vars = None

    need_manifest = "manifest" in selected
    for key in selected:
        if not RULE_REGISTRY[key].get("needs_manifest"):
            continue
        mod = importlib.import_module(RULE_REGISTRY[key]["module"])
        if hasattr(mod, "detect_image_pattern"):
            pattern = mod.detect_image_pattern(Path(repo_root))
            if pattern == "env_var":
                need_manifest = True
                break
        elif hasattr(mod, "detect_params_env"):
            if mod.detect_params_env(Path(repo_root)):
                need_manifest = True
                break

    if need_manifest:
        t0 = time.monotonic()
        manifest, manifest_env_vars = load_manifest(operator_path)
        _tlog("load_manifest", time.monotonic() - t0)


    prod_scope = None
    if not getattr(args, "no_production_scope", False):
        t0 = time.monotonic()
        manifest_source_folders = None
        overlay_paths = None
        try:
            op_manifest_mod = importlib.import_module("rules.operator_manifest")
            repo_basename = os.path.basename(repo_root)
            if hasattr(op_manifest_mod, "parse_component_manifest_mapping"):
                mapping = op_manifest_mod.parse_component_manifest_mapping(operator_path)
                manifest_source_folders = mapping.get(repo_basename)
                if manifest_source_folders:
                    print(
                        f"  Operator mapping: {repo_basename} → {manifest_source_folders}",
                        file=sys.stderr,
                    )
            if hasattr(op_manifest_mod, "parse_component_overlay_paths"):
                    key_map = op_manifest_mod.parse_repo_component_key(operator_path)
                    component_key = key_map.get(repo_basename)
                    if component_key:
                        overlay_paths = op_manifest_mod.parse_component_overlay_paths(
                            operator_path, component_key,
                        )
                        if overlay_paths:
                            print(
                                f"  Overlay paths (auto-detected for {component_key}): {overlay_paths}",
                                file=sys.stderr,
                            )
        except Exception as e:
            repo_basename = repo_basename if 'repo_basename' in dir() else os.path.basename(repo_root)
            print(
                f"  WARNING: operator manifest handling failed for "
                f"{operator_path}/{repo_basename}: {e}",
                file=sys.stderr,
            )

        prod_scope = compute_production_scope(
            Path(repo_root),
            manifest_source_folders=manifest_source_folders,
            overlay_paths=overlay_paths,
        )
        _tlog("production_scope", time.monotonic() - t0)
        if prod_scope:
            parts = []
            if prod_scope.production_files:
                parts.append(f"go={len(prod_scope.production_files)} files")
            if prod_scope.manifest_files:
                parts.append(f"manifests={len(prod_scope.manifest_files)} files")
            print(
                f"  Production scope: {prod_scope.method} ({', '.join(parts)})",
                file=sys.stderr,
            )

    results = []
    for key in selected:
        entry = RULE_REGISTRY[key]
        mod = importlib.import_module(entry["module"])

        if entry.get("is_manifest_rule"):
            t0 = time.monotonic()
            if manifest is None:
                manifest, manifest_env_vars = load_manifest(operator_path)
            results.append(adapt_manifest_result(manifest))
            _tlog(f"rule {key}", time.monotonic() - t0)
            continue

        kwargs = {}
        if key in ("csv", "params_env") and manifest_env_vars is not None:
            kwargs["manifest_env_vars"] = manifest_env_vars
        if prod_scope is not None:
            kwargs["production_scope"] = prod_scope
        t0 = time.monotonic()
        result = mod.run(repo_root, **kwargs)
        _tlog(f"rule {key}", time.monotonic() - t0)
        results.append(result)

    exceptions, error_result = _load_central_exceptions(args)
    if error_result:
        results.insert(0, error_result)
    if exceptions:
        apply_exceptions(results, exceptions, repo_name)

    score = compute_score(results)
    print_summary(score, results)

    if args.report == "json":
        report = render_json(score, results, repo_name)
    else:
        report = render_markdown(score, results, repo_name)

    if args.output:
        Path(args.output).write_text(report + "\n")
        print(f"\nReport written to {args.output}", file=sys.stderr)
    else:
        print(report)

    _tlog("total", time.monotonic() - t_total)
    return 0 if score != "NOT READY" else 1


def main(argv=None):
    args = parse_args(argv)

    if args.operator_path:
        return _run(args, args.operator_path)

    with tempfile.TemporaryDirectory(prefix="odh-operator-") as tmp_dir:
        return _run(args, tmp_dir)


if __name__ == "__main__":
    sys.exit(main())
