"""Tests for main.py orchestrator functions."""

import json
import sys
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from main import (
    _build_exception_snippets,
    _build_false_positive_section,
    _get_repo_name,
    _render_template_simple,
    adapt_manifest_result,
    apply_exceptions,
    compute_score,
    load_central_config,
    parse_args,
    print_summary,
    render_json,
    render_markdown,
    resolve_rules,
    validate_repo_exceptions,
    main,
)
from rules.common import Finding, RuleResult


def load_exceptions(path):
    """Test helper: load exceptions list from a config file."""
    return load_central_config(path)["exceptions"]


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
        assert args.output == "out.md"


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
    @patch("main.importlib.import_module")
    def test_all_pass_returns_0(self, mock_import, _mock_scope):
        fake_mod = MagicMock()
        fake_mod.run.return_value = RuleResult(rule="test-rule", passed=True)
        fake_mod.detect_image_pattern.return_value = "static_csv"
        mock_import.return_value = fake_mod

        exit_code = main([".", "--rules", "csv,tags,egress,python", "--report", "json"])
        assert exit_code == 0

    @patch("main.compute_production_scope", return_value=None)
    @patch("main.importlib.import_module")
    def test_blocker_returns_1(self, mock_import, _mock_scope):
        fake_mod = MagicMock()
        fake_mod.run.return_value = RuleResult(
            rule="test-rule", passed=False,
            findings=[Finding("blocker", "f.go", 1, "img", "fail")],
        )
        fake_mod.detect_image_pattern.return_value = "static_csv"
        mock_import.return_value = fake_mod

        exit_code = main([".", "--rules", "csv", "--report", "json"])
        assert exit_code == 1

    @patch("main.compute_production_scope", return_value=None)
    @patch("main.importlib.import_module")
    def test_output_flag_writes_file(self, mock_import, _mock_scope, tmp_path):
        fake_mod = MagicMock()
        fake_mod.run.return_value = RuleResult(rule="r", passed=True)
        fake_mod.detect_image_pattern.return_value = "static_csv"
        mock_import.return_value = fake_mod

        out_file = tmp_path / "report.json"
        exit_code = main([".", "--rules", "csv", "--report", "json", "-o", str(out_file)])
        assert exit_code == 0
        content = out_file.read_text()
        data = json.loads(content.strip())
        assert data["score"] == "READY"

    @patch("main.compute_production_scope", return_value=None)
    def test_manifest_rule_triggers_adapt(self, _mock_scope):
        fake_manifest = FakeManifest(images=[], components=[], known_issues=[])

        with patch("main.load_manifest", return_value=(fake_manifest, set())) as mock_load, \
             patch("main.adapt_manifest_result", return_value=RuleResult(rule="operator-manifest")) as mock_adapt, \
             patch("importlib.import_module") as mock_import:
            mock_import.return_value = MagicMock()
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

        with patch("main.load_manifest", return_value=(fake_manifest, set())) as mock_load, \
             patch("importlib.import_module", return_value=fake_mod):
            exit_code = main([".", "--rules", "csv", "--report", "json"])
            assert exit_code == 0
            mock_load.assert_called_once()

    @patch("main.importlib.import_module")
    def test_per_repo_exception_applied_through_main(self, mock_import, tmp_path):
        fake_mod = MagicMock()
        fake_mod.run.return_value = RuleResult(
            rule="no-runtime-egress", passed=False,
            findings=[Finding("blocker", "internal/client.go", 42, "", "http.Get call")],
        )
        fake_mod.detect_image_pattern.return_value = "none"
        mock_import.return_value = fake_mod

        _write_repo_exceptions(tmp_path,
            "exceptions:\n"
            "  - rule: no-runtime-egress\n"
            '    path: "internal/client.go"\n'
            '    reason: "Calls cluster-internal Kubernetes API"\n'
        )

        exit_code = main([
            str(tmp_path), "--rules", "egress", "--report", "json",
        ])
        assert exit_code == 0

    @patch("main.importlib.import_module")
    def test_invalid_repo_exception_produces_blocker_not_crash(self, mock_import, tmp_path):
        fake_mod = MagicMock()
        fake_mod.run.return_value = RuleResult(rule="no-image-tags", passed=True)
        fake_mod.detect_image_pattern.return_value = "none"
        mock_import.return_value = fake_mod

        _write_repo_exceptions(tmp_path,
            "exceptions:\n"
            "  - rule: no-runtime-egress\n"
            '    reason: "missing scope filter"\n'
        )

        exit_code = main([
            str(tmp_path), "--rules", "tags", "--report", "json",
        ])
        assert exit_code == 1


# --- load_exceptions ---

class TestLoadExceptions:
    def test_load_from_file(self, tmp_path):
        exc_file = tmp_path / "exceptions.yaml"
        exc_file.write_text(
            "exceptions:\n"
            "  - rule: no-runtime-egress\n"
            '    path: "src/main.go"\n'
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
            '    path: "deploy.yaml"\n'
        )
        with pytest.raises(ValueError, match="missing required 'reason' field"):
            load_exceptions(str(exc_file))

    def test_missing_rule_raises(self, tmp_path):
        exc_file = tmp_path / "exceptions.yaml"
        exc_file.write_text(
            "exceptions:\n"
            '  - path: "deploy.yaml"\n'
            '    reason: "test"\n'
        )
        with pytest.raises(ValueError, match="missing required 'rule' field"):
            load_exceptions(str(exc_file))

    def test_non_dict_entry_raises(self, tmp_path):
        exc_file = tmp_path / "exceptions.yaml"
        exc_file.write_text("exceptions:\n  - no-image-tags\n")
        with pytest.raises(ValueError, match="must be a mapping"):
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
            '    path: "install/*"\n'
            '    reason: "historical snapshots"\n'
        )
        result = load_exceptions(str(exc_file))
        assert len(result) == 1
        assert result[0]["rule"] == "no-image-tags, no-runtime-egress"
        assert result[0]["path"] == "install/*"



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
        exceptions = [{"rule": "no-image-tags", "path": "src/*.go", "reason": "source ok"}]
        apply_exceptions(results, exceptions, "repo")
        assert results[0].findings[0].severity == "info"
        assert results[0].findings[1].severity == "blocker"
        assert results[0].passed is False

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
            {"rule": "r", "path": "a.go", "reason": "ok"},
            {"rule": "r", "path": "b.go", "reason": "ok"},
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
            "path": "install/*",
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


# --- validate_repo_exceptions ---



def _write_repo_exceptions(tmp_path, yaml_content):
    """Create .disconnected-readiness/config.yaml with exceptions in tmp_path."""
    exc_dir = tmp_path / ".disconnected-readiness"
    exc_dir.mkdir(exist_ok=True)
    exc_file = exc_dir / "config.yaml"
    exc_file.write_text(yaml_content)
    return exc_file


_VALID_REPO_EXCEPTION = {
    "rule": "no-runtime-egress",
    "path": "internal/client.go",
    "reason": "Calls cluster-internal API",
}


def _repo_exception(**overrides):
    """Build a per-repo exception dict from the valid base, applying overrides."""
    exc = dict(_VALID_REPO_EXCEPTION)
    for k, v in overrides.items():
        if v is None:
            exc.pop(k, None)
        else:
            exc[k] = v
    return exc


class TestValidateRepoExceptions:
    @pytest.mark.parametrize(
        "desc, overrides, error_match",
        [
            # accepted cases
            ("valid with path scope", {}, None),
            ("valid with image scope", {"path": None, "image": "quay.io/org/img:*", "rule": "no-image-tags"}, None),
            ("valid with message scope", {"path": None, "message": "http.DefaultClient"}, None),
            ("no reference accepted", {}, None),
            ("with reference accepted", {"reference": "https://example.com/issue/1"}, None),
            # rejected cases (rule and reason validated by load_exceptions, not here)
            ("missing scope filter", {"path": None}, "at least one scope filter"),
            ("empty string scope filter", {"path": ""}, "at least one scope filter"),
            ("repo field forbidden", {"repo": "opendatahub-io/odh-dashboard"}, "'repo' field is not allowed"),
            ("unknown field rejected", {"typo_field": "value"}, "unknown field"),
            ("missing rule rejected", {"rule": None}, "missing required 'rule' field"),
            ("missing reason rejected", {"reason": None}, "missing required 'reason' field"),
            ("non-string path rejected", {"path": 123}, "'path' must be a string"),
            ("non-string image rejected", {"path": None, "image": 42, "rule": "no-image-tags"}, "'image' must be a string"),
            ("non-string message rejected", {"path": None, "message": True}, "'message' must be a string"),
        ],
        ids=lambda x: x if isinstance(x, str) else "",
    )
    def test_validate_repo_exception(self, desc, overrides, error_match):
        exceptions = [_repo_exception(**overrides)]
        if error_match is None:
            validate_repo_exceptions(exceptions, "test.yaml")
        else:
            with pytest.raises(ValueError, match=error_match):
                validate_repo_exceptions(exceptions, "test.yaml")

    def test_non_dict_entry_rejected(self):
        with pytest.raises(ValueError, match="must be a mapping"):
            validate_repo_exceptions(["not-a-dict"], "test.yaml")


# --- per-repo exception loading ---

class TestRepoExceptionLoading:
    def test_repo_exceptions_loaded_from_target_repo(self, tmp_path):
        exc_file = _write_repo_exceptions(tmp_path,
            "exceptions:\n"
            "  - rule: no-runtime-egress\n"
            '    path: "internal/client.go"\n'
            '    reason: "Calls cluster-internal API"\n'
        )
        exceptions = load_exceptions(str(exc_file))
        assert len(exceptions) == 1
        assert exceptions[0]["rule"] == "no-runtime-egress"
        validate_repo_exceptions(exceptions, str(exc_file))

    def test_repo_exception_missing_rule_rejected_by_load(self, tmp_path):
        exc_file = _write_repo_exceptions(tmp_path,
            "exceptions:\n"
            '  - path: "f.go"\n'
            '    reason: "test"\n'
        )
        with pytest.raises(ValueError, match="missing required 'rule' field"):
            load_exceptions(str(exc_file))

    def test_repo_exception_missing_reason_rejected_by_load(self, tmp_path):
        exc_file = _write_repo_exceptions(tmp_path,
            "exceptions:\n"
            "  - rule: no-runtime-egress\n"
            '    path: "f.go"\n'
        )
        with pytest.raises(ValueError, match="missing required 'reason' field"):
            load_exceptions(str(exc_file))

    def test_repo_exceptions_missing_file_skipped(self, tmp_path):
        exc_path = tmp_path / ".disconnected-readiness" / "exceptions.yaml"
        exceptions = load_exceptions(str(exc_path))
        assert exceptions == []

    def test_repo_exceptions_merged_with_central(self):
        central = [{"rule": "no-image-tags", "reason": "central rule"}]
        repo = [{
            "rule": "no-runtime-egress",
            "path": "f.go",
            "reason": "repo rule",
        }]
        merged = central + repo
        results = [
            RuleResult(rule="no-image-tags", passed=False,
                       findings=[Finding("blocker", "a.yaml", 1, "img", "tag")]),
            RuleResult(rule="no-runtime-egress", passed=False,
                       findings=[Finding("blocker", "f.go", 5, "", "egress")]),
        ]
        apply_exceptions(results, merged, "repo")
        assert results[0].findings[0].severity == "info"
        assert results[1].findings[0].severity == "info"

    def test_repo_exception_downgrades_finding(self, tmp_path):
        exc_file = _write_repo_exceptions(tmp_path,
            "exceptions:\n"
            "  - rule: no-image-tags\n"
            '    path: "deploy/*.yaml"\n'
            '    reason: "Tags replaced by RELATED_IMAGE at deploy time"\n'
        )
        exceptions = load_exceptions(str(exc_file))
        validate_repo_exceptions(exceptions, str(exc_file))

        results = [RuleResult(
            rule="no-image-tags", passed=False,
            findings=[Finding("blocker", "deploy/app.yaml", 10, "img:latest", "mutable tag")],
        )]
        apply_exceptions(results, exceptions, "test-repo")
        assert results[0].findings[0].severity == "info"
        assert results[0].passed is True
        assert "[Exception:" in results[0].findings[0].message


# --- exception snippets and false positive section ---

class TestExceptionSnippets:
    def test_builds_snippets_from_blockers_only(self):
        results = [RuleResult(rule="no-image-tags", findings=[
            Finding("blocker", "deploy.yaml", 10, "img:latest", "mutable tag"),
            Finding("warning", "src/main.go", 5, "img:v1", "source tag"),
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
        assert ".disconnected-readiness/config.yaml" in section
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
        assert ".disconnected-readiness/config.yaml" in output

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
