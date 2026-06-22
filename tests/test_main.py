"""Tests for main.py orchestrator functions."""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from main import (
    ArchAnalyzerError,
    _build_exception_snippets,
    _build_false_positive_section,
    _get_repo_name,
    _render_template_simple,
    _run_arch_analyzer,
    _validate_config_schema,
    adapt_manifest_result,
    apply_exceptions,
    compute_score,
    load_central_config,
    parse_args,
    print_summary,
    render_json,
    render_markdown,
    resolve_rules,
    main,
)
from rules.common import Finding, RuleResult


def load_exceptions(path):
    """Test helper: load exceptions list from a config file."""
    return load_central_config(path)["exceptions"]


def _make_import_side_effect(fake_mod):
    """Return an importlib.import_module side_effect that is module-aware.

    Returns *fake_mod* for rule modules but a proper mock with
    parse_manifest_entries / parse_overlay_paths_from_arch_data for
    ``rules.operator_manifest`` so production-scope code works.
    """
    op_manifest_mock = MagicMock()
    op_manifest_mock.parse_manifest_entries.return_value = ({}, {})
    op_manifest_mock.parse_overlay_paths_from_arch_data.return_value = []

    def _side_effect(name):
        if name == "rules.operator_manifest":
            return op_manifest_mock
        return fake_mod

    return _side_effect


# --- parse_args ---

class TestParseArgs:
    def test_defaults(self):
        args = parse_args([])
        assert args.repo_root == "."
        assert args.rules == "all"
        assert args.report == "markdown"
        assert args.operator_path is None
        assert args.output is None

    def test_positional_repo(self):
        args = parse_args(["/tmp/repo"])
        assert args.repo_root == "/tmp/repo"

    def test_rules_flag(self):
        args = parse_args([".", "--rules", "csv,tags"])
        assert args.rules == "csv,tags"

    def test_report_json(self):
        args = parse_args([".", "--report", "json"])
        assert args.report == "json"

    def test_operator_path(self):
        args = parse_args([".", "--operator-path", "/tmp/op"])
        assert args.operator_path == "/tmp/op"

    def test_output_short(self):
        args = parse_args([".", "-o", "out.md"])
        assert args.output == ["out.md"]

    def test_output_dual(self):
        args = parse_args([".", "--report", "json,markdown", "-o", "r.json", "r.md"])
        assert args.report == "json,markdown"
        assert args.output == ["r.json", "r.md"]


# --- resolve_rules ---

class TestResolveRules:
    def test_all_returns_defaults(self):
        from main import DEFAULT_RULES
        result = resolve_rules("all")
        assert result == list(DEFAULT_RULES)

    def test_empty_returns_defaults(self):
        from main import DEFAULT_RULES
        assert resolve_rules("") == list(DEFAULT_RULES)

    def test_none_returns_defaults(self):
        from main import DEFAULT_RULES
        assert resolve_rules(None) == list(DEFAULT_RULES)

    def test_specific_rules(self):
        assert resolve_rules("csv,tags") == ["csv", "tags"]

    def test_single_rule(self):
        assert resolve_rules("egress") == ["egress"]

    def test_unknown_rule_exits(self):
        with pytest.raises(SystemExit, match="Unknown rule 'nope'"):
            resolve_rules("nope")

    def test_whitespace_stripped(self):
        assert resolve_rules(" csv , tags ") == ["csv", "tags"]


# --- compute_score ---

class TestComputeScore:
    def test_ready(self):
        results = [RuleResult(rule="a"), RuleResult(rule="b")]
        assert compute_score(results) == "READY"

    def test_ready_with_info(self):
        r = RuleResult(rule="a", findings=[Finding("info", "", 0, "", "ok")])
        assert compute_score([r]) == "READY"

    def test_not_ready(self):
        r = RuleResult(rule="a", passed=False,
                       findings=[Finding("blocker", "f", 1, "img", "bad")])
        assert compute_score([r]) == "NOT READY"

    def test_not_ready_with_mixed_rules(self):
        r1 = RuleResult(rule="a", findings=[Finding("info", "", 0, "", "ok")])
        r2 = RuleResult(rule="b", passed=False)
        assert compute_score([r1, r2]) == "NOT READY"

    def test_empty_results(self):
        assert compute_score([]) == "READY"


# --- adapt_manifest_result ---

@dataclass
class FakeImageEntry:
    env_var: str
    image: str = ""

@dataclass
class FakeManifest:
    images: list = field(default_factory=list)
    components: list = field(default_factory=list)
    known_issues: list = field(default_factory=list)


class TestAdaptManifestResult:
    def test_basic(self):
        manifest = FakeManifest(
            images=[FakeImageEntry("VAR_A"), FakeImageEntry("VAR_B")],
            components=["comp1"],
        )
        result = adapt_manifest_result(manifest)
        assert result.rule == "operator-manifest"
        assert result.passed is True
        assert len(result.findings) == 1
        assert "2 RELATED_IMAGE vars" in result.findings[0].message

    def test_duplicate_env_vars_counted_unique(self):
        manifest = FakeManifest(
            images=[FakeImageEntry("VAR_A"), FakeImageEntry("VAR_A")],
            components=[],
        )
        result = adapt_manifest_result(manifest)
        assert "1 RELATED_IMAGE vars" in result.findings[0].message

    def test_known_issues_become_info(self):
        manifest = FakeManifest(
            images=[],
            components=[],
            known_issues=["stale ref", "missing var"],
        )
        result = adapt_manifest_result(manifest)
        assert len(result.findings) == 3  # 3 info findings
        assert all(f.severity == "info" for f in result.findings)
        issue_findings = [f for f in result.findings if "Known issue" in f.message]
        assert len(issue_findings) == 2
        assert "stale ref" in issue_findings[0].message


# --- print_summary ---

class TestPrintSummary:
    def test_output_to_stderr(self, capsys):
        results = [
            RuleResult(rule="r1", passed=True, findings=[
                Finding("info", "", 0, "", "ok"),
            ]),
            RuleResult(rule="r2", passed=False, findings=[
                Finding("blocker", "f.go", 1, "img", "bad"),
            ]),
        ]
        print_summary("NOT READY", results)
        err = capsys.readouterr().err
        assert "NOT READY" in err
        assert "PASS" in err
        assert "FAIL" in err

    def test_pass_tag(self, capsys):
        results = [
            RuleResult(rule="r1", findings=[
                Finding("info", "x.py", 1, "", "informational"),
            ]),
        ]
        print_summary("READY", results)
        err = capsys.readouterr().err
        assert "READY" in err
        assert "All checks passed" in err


# --- render_json ---

class TestRenderJson:
    def test_structure(self):
        results = [
            RuleResult(rule="a", passed=True, findings=[
                Finding("blocker", "f.go", 10, "img", "msg"),
                Finding("info", "g.go", 20, "", "imsg"),
            ]),
        ]
        raw = render_json("NOT READY", results, "my-repo")
        data = json.loads(raw)
        assert data["repo"] == "my-repo"
        assert data["score"] == "NOT READY"
        assert len(data["rules"]) == 1
        rule = data["rules"][0]
        assert rule["name"] == "a"
        assert rule["passed"] is True
        assert rule["blockers"] == 1
        assert "warnings" not in rule
        assert len(rule["findings"]) == 2

    def test_empty_results(self):
        data = json.loads(render_json("READY", [], "repo"))
        assert data["rules"] == []
        assert data["score"] == "READY"

    def test_verbose_includes_files_checked(self):
        r = RuleResult(rule="b", passed=True)
        r.files_checked = ["src/a.go", "src/b.go", "src/a.go"]
        data = json.loads(render_json("READY", [r], "repo", verbose=True))
        rule = data["rules"][0]
        assert "files_checked" in rule
        assert rule["files_checked"] == ["src/a.go", "src/b.go"]  # sorted+deduped

    def test_non_verbose_omits_files_checked(self):
        r = RuleResult(rule="b", passed=True)
        r.files_checked = ["src/a.go"]
        data = json.loads(render_json("READY", [r], "repo", verbose=False))
        assert "files_checked" not in data["rules"][0]

    def test_verbose_omits_files_checked_when_empty(self):
        r = RuleResult(rule="c", passed=True)
        data = json.loads(render_json("READY", [r], "repo", verbose=True))
        assert "files_checked" not in data["rules"][0]


# --- _render_template_simple ---

class TestRenderTemplateSimple:
    def test_variable_substitution(self):
        result = _render_template_simple("Hello {{ name }}", {"name": "world"})
        assert result == "Hello world"

    def test_upper_filter(self):
        result = _render_template_simple("{{ x | upper }}", {"x": "abc"})
        assert result == "ABC"

    def test_for_loop(self):
        template = "{% for item in items %}[{{ item.v }}]{% endfor %}"
        ctx = {"items": [{"v": "a"}, {"v": "b"}]}
        assert _render_template_simple(template, ctx) == "[a]\n[b]"

    def test_dot_access_in_loop(self):
        template = "{% for r in rules %}{{ r.name }},{% endfor %}"
        ctx = {"rules": [{"name": "x"}, {"name": "y"}]}
        assert _render_template_simple(template, ctx) == "x,\ny,"

    def test_nested_for_raises(self):
        template = "{% for a in x %}{% for b in y %}{% endfor %}{% endfor %}"
        with pytest.raises(ValueError, match="Nested"):
            _render_template_simple(template, {"x": [1], "y": [2]})

    def test_missing_variable_returns_empty(self):
        assert _render_template_simple("{{ missing }}", {}) == ""


# --- render_markdown ---

class TestRenderMarkdown:
    def test_fallback_on_missing_template(self, tmp_path, monkeypatch):
        monkeypatch.setattr("main.Path", lambda *a: tmp_path / "nope" if len(a) == 1 else type(tmp_path)(*a))
        result = render_markdown("READY", [], "repo")
        assert "READY" in result

    def test_uses_builtin_renderer_without_jinja(self):
        saved = sys.modules.get("jinja2")
        sys.modules["jinja2"] = None
        try:
            result = render_markdown("READY", [], "repo")
        except AttributeError:
            pytest.fail("Fallback did not catch jinja2 unavailability")
        finally:
            if saved is not None:
                sys.modules["jinja2"] = saved
            else:
                sys.modules.pop("jinja2", None)
        assert "READY" in result


# --- main (integration-level) ---

class TestMain:
    @patch("main.compute_production_scope", return_value=None)
    def test_all_pass_returns_0(self, _mock_scope):
        fake_mod = MagicMock()
        fake_mod.run.return_value = RuleResult(rule="test-rule", passed=True)
        fake_mod.detect_image_pattern.return_value = "static_csv"

        with patch("main._run_arch_analyzer", return_value=None), \
             patch("importlib.import_module", side_effect=_make_import_side_effect(fake_mod)):
            exit_code = main([".", "--rules", "csv,tags,egress,python", "--report", "json"])
        assert exit_code == 0

    @patch("main.compute_production_scope", return_value=None)
    def test_blocker_returns_1(self, _mock_scope):
        fake_mod = MagicMock()
        fake_mod.run.return_value = RuleResult(
            rule="test-rule", passed=False,
            findings=[Finding("blocker", "f.go", 1, "img", "fail")],
        )
        fake_mod.detect_image_pattern.return_value = "static_csv"

        with patch("main._run_arch_analyzer", return_value=None), \
             patch("importlib.import_module", side_effect=_make_import_side_effect(fake_mod)):
            exit_code = main([".", "--rules", "csv", "--report", "json"])
        assert exit_code == 1

    @patch("main.compute_production_scope", return_value=None)
    def test_output_flag_writes_file(self, _mock_scope, tmp_path):
        fake_mod = MagicMock()
        fake_mod.run.return_value = RuleResult(rule="r", passed=True)
        fake_mod.detect_image_pattern.return_value = "static_csv"

        out_file = tmp_path / "report.json"
        with patch("main._run_arch_analyzer", return_value=None), \
             patch("importlib.import_module", side_effect=_make_import_side_effect(fake_mod)):
            exit_code = main([".", "--rules", "csv", "--report", "json", "-o", str(out_file)])
        assert exit_code == 0
        content = out_file.read_text()
        data = json.loads(content.strip())
        assert data["score"] == "READY"

    @patch("main.compute_production_scope", return_value=None)
    def test_manifest_rule_triggers_adapt(self, _mock_scope):
        fake_manifest = FakeManifest(images=[], components=[], known_issues=[])
        fake_mod = MagicMock()

        with patch("main._run_arch_analyzer", return_value=None), \
             patch("main.load_manifest", return_value=(fake_manifest, set())) as mock_load, \
             patch("main.adapt_manifest_result", return_value=RuleResult(rule="operator-manifest")) as mock_adapt, \
             patch("importlib.import_module", side_effect=_make_import_side_effect(fake_mod)):
            exit_code = main([".", "--rules", "manifest", "--report", "json"])
            assert exit_code == 0
            mock_load.assert_called_once()
            mock_adapt.assert_called_once_with(fake_manifest)

    @patch("main.compute_production_scope", return_value=None)
    def test_env_var_pattern_triggers_manifest_load(self, _mock_scope):
        fake_mod = MagicMock()
        fake_mod.detect_image_pattern.return_value = "env_var"
        fake_mod.run.return_value = RuleResult(rule="csv", passed=True)

        fake_manifest = FakeManifest(images=[], components=[], known_issues=[])

        with patch("main._run_arch_analyzer", return_value=None), \
             patch("main.load_manifest", return_value=(fake_manifest, set())) as mock_load, \
             patch("importlib.import_module", side_effect=_make_import_side_effect(fake_mod)):
            exit_code = main([".", "--rules", "csv", "--report", "json"])
            assert exit_code == 0
            mock_load.assert_called_once()



# --- load_exceptions ---

class TestLoadExceptions:
    def test_load_from_file(self, tmp_path):
        exc_file = tmp_path / "exceptions.yaml"
        exc_file.write_text(
            "exceptions:\n"
            "  - rule: no-runtime-egress\n"
            "    paths:\n"
            '      - "src/main.go"\n'
            '    reason: "internal proxy"\n'
        )
        result = load_exceptions(str(exc_file))
        assert len(result) == 1
        assert result[0]["rule"] == "no-runtime-egress"

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_exceptions(str(tmp_path / "nope.yaml")) == []

    def test_empty_exceptions_returns_empty(self, tmp_path):
        exc_file = tmp_path / "exceptions.yaml"
        exc_file.write_text("exceptions: []\n")
        assert load_exceptions(str(exc_file)) == []

    def test_non_mapping_root_raises(self, tmp_path):
        exc_file = tmp_path / "exceptions.yaml"
        exc_file.write_text("- rule: no-image-tags\n  reason: test\n")
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            load_exceptions(str(exc_file))

    def test_missing_reason_raises(self, tmp_path):
        exc_file = tmp_path / "exceptions.yaml"
        exc_file.write_text(
            "exceptions:\n"
            "  - rule: no-image-tags\n"
            "    paths:\n"
            '      - "deploy.yaml"\n'
        )
        with pytest.raises(ValueError, match="reason"):
            load_exceptions(str(exc_file))

    def test_missing_rule_raises(self, tmp_path):
        exc_file = tmp_path / "exceptions.yaml"
        exc_file.write_text(
            "exceptions:\n"
            "  - paths:\n"
            '      - "deploy.yaml"\n'
            '    reason: "test"\n'
        )
        with pytest.raises(ValueError, match="rule"):
            load_exceptions(str(exc_file))

    def test_non_dict_entry_raises(self, tmp_path):
        exc_file = tmp_path / "exceptions.yaml"
        exc_file.write_text("exceptions:\n  - no-image-tags\n")
        with pytest.raises(ValueError):
            load_exceptions(str(exc_file))

    def test_malformed_yaml_raises(self, tmp_path):
        exc_file = tmp_path / "exceptions.yaml"
        exc_file.write_text('exceptions:\n  - rule: "missing close quote\n')
        with pytest.raises(ValueError, match="Failed to parse"):
            load_exceptions(str(exc_file))

    def test_unreadable_file_raises(self, tmp_path):
        exc_dir = tmp_path / "exceptions.yaml"
        exc_dir.mkdir()
        with pytest.raises(ValueError, match="Cannot read"):
            load_exceptions(str(exc_dir))

    def test_fallback_parser_handles_simple_format(self, tmp_path):
        exc_file = tmp_path / "exceptions.yaml"
        exc_file.write_text(
            "exceptions:\n"
            "  - rule: no-image-tags, no-runtime-egress\n"
            "    paths:\n"
            '      - "install/*"\n'
            '    reason: "historical snapshots"\n'
        )
        result = load_exceptions(str(exc_file))
        assert len(result) == 1
        assert result[0]["rule"] == "no-image-tags, no-runtime-egress"
        assert result[0]["paths"] == ["install/*"]



# --- _get_repo_name ---

class TestGetRepoName:
    def test_https_remote(self, tmp_path):
        subprocess_run = patch(
            "main.subprocess.check_output",
            return_value="https://github.com/org-a/my-repo.git\n",
        )
        with subprocess_run:
            assert _get_repo_name(str(tmp_path)) == "org-a/my-repo"

    def test_ssh_remote(self, tmp_path):
        subprocess_run = patch(
            "main.subprocess.check_output",
            return_value="git@github.com:org-a/my-repo.git\n",
        )
        with subprocess_run:
            assert _get_repo_name(str(tmp_path)) == "org-a/my-repo"

    def test_no_remote_falls_back_to_basename(self, tmp_path):
        from subprocess import CalledProcessError
        subprocess_run = patch(
            "main.subprocess.check_output",
            side_effect=CalledProcessError(1, "git"),
        )
        with subprocess_run:
            assert _get_repo_name(str(tmp_path)) == tmp_path.name


# --- apply_exceptions ---

class TestApplyExceptions:
    def test_matching_rule_downgrades_blocker(self):
        results = [RuleResult(
            rule="no-image-tags", passed=False,
            findings=[Finding("blocker", "deploy.yaml", 10, "img:latest", "bad tag")],
        )]
        exceptions = [{"rule": "no-image-tags", "reason": "known false positive"}]
        apply_exceptions(results, exceptions, "my-repo")
        assert results[0].findings[0].severity == "info"
        assert "[Exception:" in results[0].findings[0].message
        assert results[0].passed is True

    def test_info_findings_not_targeted(self):
        results = [RuleResult(
            rule="no-runtime-egress",
            findings=[Finding("info", "main.go", 5, "", "configurable URL")],
        )]
        exceptions = [{"rule": "no-runtime-egress", "reason": "internal only"}]
        apply_exceptions(results, exceptions, "repo")
        assert results[0].findings[0].severity == "info"

    def test_non_matching_rule_keeps_severity(self):
        results = [RuleResult(
            rule="no-image-tags", passed=False,
            findings=[Finding("blocker", "f.yaml", 1, "img", "bad")],
        )]
        exceptions = [{"rule": "no-runtime-egress", "reason": "wrong rule"}]
        apply_exceptions(results, exceptions, "repo")
        assert results[0].findings[0].severity == "blocker"
        assert results[0].passed is False

    def test_path_glob_matching(self):
        results = [RuleResult(
            rule="no-image-tags", passed=False,
            findings=[
                Finding("blocker", "src/main.go", 1, "img", "bad"),
                Finding("blocker", "deploy/app.yaml", 2, "img2", "also bad"),
            ],
        )]
        exceptions = [{"rule": "no-image-tags", "paths": ["src/*.go"], "reason": "source ok"}]
        apply_exceptions(results, exceptions, "repo")
        assert results[0].findings[0].severity == "info"
        assert results[0].findings[1].severity == "blocker"
        assert results[0].passed is False

    def test_doublestar_suffix_matches_bare_dir(self):
        results = [RuleResult(
            rule="no-image-tags", passed=False,
            findings=[
                Finding("blocker", "config/scorecard", 1, "img", "bad"),
                Finding("blocker", "config/scorecard/foo.yaml", 2, "img2", "bad"),
                Finding("blocker", "config/manager/deploy.yaml", 3, "img3", "bad"),
            ],
        )]
        exceptions = [{"rule": "*", "paths": ["**/config/scorecard/**"], "reason": "test"}]
        apply_exceptions(results, exceptions, "repo")
        assert results[0].findings[0].severity == "info"
        assert results[0].findings[1].severity == "info"
        assert results[0].findings[2].severity == "blocker"

    def test_repo_filter_matches(self):
        results = [RuleResult(
            rule="no-runtime-egress", passed=False,
            findings=[Finding("blocker", "f.go", 1, "", "egress")],
        )]
        exceptions = [{"rule": "no-runtime-egress", "repo": "org/my-repo", "reason": "ok"}]
        apply_exceptions(results, exceptions, "org/my-repo")
        assert results[0].findings[0].severity == "info"

    def test_repo_filter_no_match(self):
        results = [RuleResult(
            rule="no-runtime-egress", passed=False,
            findings=[Finding("blocker", "f.go", 1, "", "egress")],
        )]
        exceptions = [{"rule": "no-runtime-egress", "repo": "other-repo", "reason": "ok"}]
        apply_exceptions(results, exceptions, "my-repo")
        assert results[0].findings[0].severity == "blocker"

    def test_repo_filter_short_exception_matches_full_repo_name(self):
        results = [RuleResult(
            rule="no-runtime-egress", passed=False,
            findings=[Finding("blocker", "f.go", 1, "", "egress")],
        )]
        exceptions = [{"rule": "no-runtime-egress", "repo": "my-repo", "reason": "ok"}]
        apply_exceptions(results, exceptions, "org/my-repo")
        assert results[0].findings[0].severity == "info"

    def test_repo_filter_different_org_same_name_no_match(self):
        results = [RuleResult(
            rule="no-runtime-egress", passed=False,
            findings=[Finding("blocker", "f.go", 1, "", "egress")],
        )]
        exceptions = [{"rule": "no-runtime-egress", "repo": "org-a/foo", "reason": "ok"}]
        apply_exceptions(results, exceptions, "org-b/foo")
        assert results[0].findings[0].severity == "blocker"

    def test_passed_recomputed_after_downgrade(self):
        results = [RuleResult(
            rule="r", passed=False,
            findings=[
                Finding("blocker", "a.go", 1, "", "b1"),
                Finding("blocker", "b.go", 2, "", "b2"),
            ],
        )]
        exceptions = [
            {"rule": "r", "paths": ["a.go"], "reason": "ok"},
            {"rule": "r", "paths": ["b.go"], "reason": "ok"},
        ]
        apply_exceptions(results, exceptions, "repo")
        assert results[0].passed is True

    def test_no_exceptions_noop(self):
        results = [RuleResult(
            rule="r", passed=False,
            findings=[Finding("blocker", "f", 1, "", "msg")],
        )]
        apply_exceptions(results, [], "repo")
        assert results[0].findings[0].severity == "blocker"
        assert results[0].passed is False

    def test_comma_separated_rules(self):
        results = [
            RuleResult(
                rule="no-image-tags", passed=False,
                findings=[Finding("blocker", "install/v1/k.yaml", 1, "img", "bad")],
            ),
            RuleResult(
                rule="no-runtime-egress", passed=False,
                findings=[Finding("blocker", "install/v1/s.sh", 5, "", "curl")],
            ),
        ]
        exceptions = [{
            "rule": "no-image-tags, no-runtime-egress",
            "paths": ["install/*"],
            "reason": "historical snapshots",
        }]
        apply_exceptions(results, exceptions, "repo")
        assert results[0].findings[0].severity == "info"
        assert results[0].passed is True
        assert results[1].findings[0].severity == "info"
        assert results[1].passed is True

    def test_message_glob_matching(self):
        results = [RuleResult(
            rule="r", passed=False,
            findings=[Finding("blocker", "f.go", 1, "", "http.Get with hardcoded external URL")],
        )]
        exceptions = [{"rule": "r", "message": "*hardcoded*", "reason": "ok"}]
        apply_exceptions(results, exceptions, "repo")
        assert results[0].findings[0].severity == "info"

    def test_message_exact_no_glob_does_not_substring_match(self):
        results = [RuleResult(
            rule="r", passed=False,
            findings=[Finding("blocker", "f.go", 1, "", "http.Get with hardcoded external URL")],
        )]
        exceptions = [{"rule": "r", "message": "http", "reason": "ok"}]
        apply_exceptions(results, exceptions, "repo")
        assert results[0].findings[0].severity == "blocker"

    def test_images_list_matches(self):
        results = [RuleResult(
            rule="r", passed=False,
            findings=[Finding("blocker", "f.yaml", 1, "repo/REPLACE_IMAGE:tag", "tagged image")],
        )]
        exceptions = [{"rule": "*", "images": ["*/REPLACE_IMAGE:*"], "reason": "placeholder"}]
        apply_exceptions(results, exceptions, "repo")
        assert results[0].findings[0].severity == "info"
        assert results[0].passed is True

    def test_images_list_no_match_stays_blocker(self):
        results = [RuleResult(
            rule="r", passed=False,
            findings=[Finding("blocker", "f.yaml", 1, "quay.io/org/real:tag", "tagged image")],
        )]
        exceptions = [{"rule": "*", "images": ["*/REPLACE_IMAGE:*"], "reason": "placeholder"}]
        apply_exceptions(results, exceptions, "repo")
        assert results[0].findings[0].severity == "blocker"

    def test_images_list_any_pattern_matches(self):
        results = [RuleResult(
            rule="r", passed=False,
            findings=[Finding("blocker", "f.yaml", 1, "quay.io/org/app:replace", "tagged image")],
        )]
        exceptions = [{"rule": "*", "images": ["*/REPLACE_IMAGE:*", "*:replace"], "reason": "placeholder"}]
        apply_exceptions(results, exceptions, "repo")
        assert results[0].findings[0].severity == "info"

    def test_images_and_paths_both_must_match(self):
        results = [RuleResult(
            rule="r", passed=False,
            findings=[Finding("blocker", "prod/f.yaml", 1, "repo/REPLACE_IMAGE:tag", "tagged")],
        )]
        exceptions = [{"rule": "*", "images": ["*/REPLACE_IMAGE:*"], "paths": ["test/**"], "reason": "placeholder"}]
        apply_exceptions(results, exceptions, "repo")
        assert results[0].findings[0].severity == "blocker"


# --- report sorting ---

class TestReportSorting:
    def test_markdown_blockers_section(self):
        results = [
            RuleResult(rule="r1", passed=False, findings=[
                Finding("info", "i.go", 1, "", "info msg"),
                Finding("blocker", "b.go", 2, "img", "block msg"),
            ]),
        ]
        output = render_markdown("NOT READY", results, "repo")
        assert "## Blockers" in output
        assert "block msg" in output
        assert "## Warnings" not in output

    def test_json_findings_sorted_by_severity(self):
        results = [
            RuleResult(rule="r", findings=[
                Finding("info", "i.go", 1, "", "info"),
                Finding("blocker", "b.go", 2, "", "blocker"),
            ]),
        ]
        data = json.loads(render_json("NOT READY", results, "repo"))
        severities = [f["severity"] for f in data["rules"][0]["findings"]]
        assert severities == ["blocker", "info"]

    def test_fallback_renderer_two_for_loops(self):
        template = (
            "{% for b in blockers %}B:{{ b.msg }}{% endfor %}"
            "{% for i in infos %}I:{{ i.msg }}{% endfor %}"
        )
        ctx = {
            "blockers": [{"msg": "b1"}],
            "infos": [{"msg": "i1"}, {"msg": "i2"}],
        }
        result = _render_template_simple(template, ctx)
        assert result == "B:b1I:i1\nI:i2"


# --- parse_args exceptions flag ---

class TestParseArgsExceptions:
    def test_exceptions_flag(self, tmp_path):
        exc_path = tmp_path / "exc.yaml"
        args = parse_args([".", "--config", str(exc_path)])
        assert args.config == str(exc_path)

    def test_exceptions_default_none(self):
        args = parse_args(["."])
        assert args.config is None


# --- central exception loading ---


class TestLoadCentralConfig:
    def test_missing_file_returns_defaults(self, tmp_path):
        from main import load_central_config
        cfg = load_central_config(str(tmp_path / "nope.yaml"))
        assert cfg["exceptions"] == []

    def test_present_file_has_all_keys(self, tmp_path):
        from main import load_central_config
        f = tmp_path / "config.yaml"
        f.write_text("exceptions: []\n")
        cfg = load_central_config(str(f))
        assert cfg["docker_contexts"] == {}
        assert cfg["known_non_image_prefixes"] == []
        assert cfg["params_env_filenames"] == {}

    def test_loads_docker_contexts(self, tmp_path):
        from main import load_central_config
        f = tmp_path / "config.yaml"
        f.write_text(
            "docker_contexts:\n"
            "  myrepo:\n"
            "    Dockerfile: src/\n"
        )
        cfg = load_central_config(str(f))
        assert cfg["docker_contexts"] == {"myrepo": {"Dockerfile": "src/"}}

    def test_loads_known_non_image_prefixes(self, tmp_path):
        from main import load_central_config
        f = tmp_path / "config.yaml"
        f.write_text("known_non_image_prefixes:\n  - ghcr.io/foo/bar\n")
        cfg = load_central_config(str(f))
        assert "ghcr.io/foo/bar" in cfg["known_non_image_prefixes"]

    def test_loads_params_env_filenames(self, tmp_path):
        from main import load_central_config
        f = tmp_path / "config.yaml"
        f.write_text(
            "params_env_filenames:\n"
            "  myrepo:\n"
            "    - params-latest.env\n"
        )
        cfg = load_central_config(str(f))
        assert cfg["params_env_filenames"] == {"myrepo": ["params-latest.env"]}


class TestArchAnalyzerError:
    def test_is_exception(self):
        from main import ArchAnalyzerError
        err = ArchAnalyzerError("binary missing")
        assert isinstance(err, Exception)
        assert "binary missing" in str(err)


class TestValidateConfigSchema:
    def test_valid_config_passes(self):
        _validate_config_schema({"exceptions": []}, "test.yaml")

    def test_invalid_config_raises(self):
        with pytest.raises(ValueError, match="schema validation error"):
            _validate_config_schema({"exceptions": "not_a_list"}, "test.yaml")

    def test_missing_schema_file_no_error(self, monkeypatch):
        import main
        monkeypatch.setattr(main, "_SCHEMA_PATH", Path("/nonexistent/schema.json"))
        _validate_config_schema({"anything": True}, "test.yaml")


class TestRunArchAnalyzer:
    def test_deletes_existing_json_before_run(self, tmp_path, monkeypatch):
        """Pre-existing JSON is deleted (supply chain safety), binary must exist."""
        (tmp_path / "component-architecture.json").write_text('{"old": true}')
        with pytest.raises(ArchAnalyzerError, match="not found"):
            _run_arch_analyzer("nonexistent-bin", str(tmp_path))
        assert not (tmp_path / "component-architecture.json").exists()

    def test_binary_not_found_raises(self, tmp_path):
        with pytest.raises(ArchAnalyzerError, match="not found"):
            _run_arch_analyzer(str(tmp_path / "no-such-bin"), str(tmp_path))

    def test_subprocess_failure_raises(self, tmp_path, monkeypatch):
        import subprocess
        bin_path = tmp_path / "fake-bin"
        bin_path.touch()
        monkeypatch.setattr(
            "main.subprocess.run",
            MagicMock(side_effect=subprocess.CalledProcessError(1, "cmd", stderr=b"fail")),
        )
        with pytest.raises(ArchAnalyzerError, match="failed"):
            _run_arch_analyzer(str(bin_path), str(tmp_path))

    def test_timeout_raises(self, tmp_path, monkeypatch):
        import subprocess
        bin_path = tmp_path / "fake-bin"
        bin_path.touch()
        monkeypatch.setattr(
            "main.subprocess.run",
            MagicMock(side_effect=subprocess.TimeoutExpired("cmd", 300)),
        )
        with pytest.raises(ArchAnalyzerError, match="failed"):
            _run_arch_analyzer(str(bin_path), str(tmp_path))

    def test_missing_output_after_run_raises(self, tmp_path, monkeypatch):
        bin_path = tmp_path / "fake-bin"
        bin_path.touch()
        monkeypatch.setattr("main.subprocess.run", MagicMock())
        with pytest.raises(ArchAnalyzerError, match="did not generate"):
            _run_arch_analyzer(str(bin_path), str(tmp_path))

    def test_invalid_json_raises(self, tmp_path, monkeypatch):
        bin_path = tmp_path / "fake-bin"
        bin_path.touch()

        def fake_run(*args, **kwargs):
            (tmp_path / "component-architecture.json").write_text("not json{{{")

        monkeypatch.setattr("main.subprocess.run", fake_run)
        with pytest.raises(ArchAnalyzerError, match="Failed to parse"):
            _run_arch_analyzer(str(bin_path), str(tmp_path))


class TestApplyExceptionsHits:
    def test_returns_hit_counts(self):
        results = [RuleResult(
            rule="r", passed=False,
            findings=[Finding("blocker", "f.go", 1, "", "msg")],
        )]
        exceptions = [
            {"rule": "r", "reason": "ok"},
            {"rule": "other", "reason": "unused"},
        ]
        hits = apply_exceptions(results, exceptions, "repo")
        assert hits == [1, 0]

    def test_empty_exceptions_returns_empty(self):
        results = [RuleResult(rule="r", findings=[Finding("blocker", "f", 1, "", "m")])]
        hits = apply_exceptions(results, [], "repo")
        assert hits == []

    def test_wildcard_rule_counts(self):
        results = [RuleResult(
            rule="r", passed=False,
            findings=[
                Finding("blocker", "a.go", 1, "", "m1"),
                Finding("blocker", "b.go", 2, "", "m2"),
                Finding("blocker", "c.go", 3, "", "m3"),
            ],
        )]
        exceptions = [{"rule": "*", "reason": "all ok"}]
        hits = apply_exceptions(results, exceptions, "repo")
        assert hits == [3]


# --- exception snippets and false positive section ---

class TestExceptionSnippets:
    def test_builds_snippets_from_blockers_only(self):
        results = [RuleResult(rule="no-image-tags", findings=[
            Finding("blocker", "deploy.yaml", 10, "img:latest", "mutable tag"),
            Finding("info", "src/main.go", 5, "img:v1", "source tag"),
            Finding("info", "test/t.go", 1, "img:dev", "test file"),
        ])]
        snippets = _build_exception_snippets(results)
        assert len(snippets) == 1
        assert snippets[0]["rule"] == "no-image-tags"
        assert snippets[0]["file"] == "deploy.yaml"

    def test_snippets_exclude_empty_fields(self):
        results = [RuleResult(rule="no-runtime-egress", findings=[
            Finding("blocker", "client.go", 5, "", "egress call"),
        ])]
        snippets = _build_exception_snippets(results)
        assert "image" not in snippets[0]
        assert snippets[0]["message"] == "egress call"

    def test_empty_when_no_blockers(self):
        results = [RuleResult(rule="r", findings=[
            Finding("info", "f", 1, "", "ok"),
        ])]
        assert _build_exception_snippets(results) == []

    def test_false_positive_section_empty_when_no_snippets(self):
        assert _build_false_positive_section([]) == ""

    def test_false_positive_section_shows_count_and_link(self):
        snippets = [
            {"rule": "r1", "file": "a.go", "line": 1, "image": "", "message": "m1"},
            {"rule": "r2", "file": "b.go", "line": 2, "image": "", "message": "m2"},
        ]
        section = _build_false_positive_section(snippets)
        assert "2 blocker findings" in section
        assert "central config file" in section
        assert "#reporting-false-positives" in section

    def test_false_positive_section_singular_for_one_blocker(self):
        snippets = [{"rule": "r", "file": "f", "line": 1, "image": "", "message": "m"}]
        section = _build_false_positive_section(snippets)
        assert "1 blocker finding" in section

    def test_markdown_report_includes_false_positive_section(self):
        results = [RuleResult(rule="no-image-tags", passed=False, findings=[
            Finding("blocker", "deploy.yaml", 10, "img:latest", "mutable tag"),
        ])]
        output = render_markdown("NOT READY", results, "test-repo")
        assert "Reporting False Positives" in output
        assert "central config file" in output

    def test_markdown_report_omits_section_when_no_blockers(self):
        results = [RuleResult(rule="r", findings=[
            Finding("info", "f", 1, "", "ok"),
        ])]
        output = render_markdown("READY", results, "repo")
        assert "Reporting False Positives" not in output

    def test_json_report_includes_false_positive_help(self):
        results = [RuleResult(rule="no-image-tags", passed=False, findings=[
            Finding("blocker", "deploy.yaml", 10, "img:latest", "mutable tag"),
        ])]
        data = json.loads(render_json("NOT READY", results, "test-repo"))
        assert "false_positive_help" in data
        assert "exception_snippets" in data["false_positive_help"]
        assert len(data["false_positive_help"]["exception_snippets"]) == 1

    def test_json_report_omits_help_when_no_blockers(self):
        results = [RuleResult(rule="r", findings=[
            Finding("info", "f", 1, "", "ok"),
        ])]
        data = json.loads(render_json("READY", results, "repo"))
        assert "false_positive_help" not in data
