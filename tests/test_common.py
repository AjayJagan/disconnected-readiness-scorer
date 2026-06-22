"""Tests for rules.common dataclasses."""

from pathlib import Path

import pytest

from rules.common import (
    ArchAnalyzerResult,
    ConfigError,
    Finding,
    ProductionScope,
    RuleResult,
    Severity,
    build_overlay_file_map,
    is_non_production_overlay_file,
    load_config_file,
)


class TestSeverity:
    def test_values(self):
        assert Severity.BLOCKER == "blocker"
        assert Severity.INFO == "info"

    def test_is_str_subclass(self):
        assert isinstance(Severity.BLOCKER, str)


class TestFinding:
    def test_fields(self):
        f = Finding(severity="blocker", file="foo.go", line=10, image="quay.io/x:latest", message="bad tag")
        assert f.severity == "blocker"
        assert f.file == "foo.go"
        assert f.line == 10
        assert f.image == "quay.io/x:latest"
        assert f.message == "bad tag"

    def test_severity_coerced_to_enum(self):
        f = Finding("blocker", "f.go", 1, "", "m")
        assert f.severity == Severity.BLOCKER

    def test_invalid_severity_raises(self):
        with pytest.raises(ValueError, match="Invalid severity"):
            Finding("warning", "f.go", 1, "", "m")

    def test_equality(self):
        a = Finding("info", "a.py", 1, "", "msg")
        b = Finding("info", "a.py", 1, "", "msg")
        assert a == b

    def test_inequality(self):
        a = Finding("blocker", "a.py", 1, "", "msg")
        b = Finding("info", "a.py", 1, "", "msg")
        assert a != b

    def test_severity_string_comparison(self):
        f = Finding("blocker", "f.go", 1, "", "m")
        assert f.severity == "blocker"
        assert f.severity != "info"


class TestRuleResult:
    def test_defaults(self):
        r = RuleResult(rule="test-rule")
        assert r.rule == "test-rule"
        assert r.passed is True
        assert r.findings == []
        assert r.files_checked == []

    def test_mutable_default_isolation(self):
        r1 = RuleResult(rule="a")
        r2 = RuleResult(rule="b")
        r1.findings.append(Finding("info", "", 0, "", "x"))
        assert r2.findings == []

    def test_files_checked_isolation(self):
        r1 = RuleResult(rule="a")
        r2 = RuleResult(rule="b")
        r1.files_checked.append("foo.go")
        assert r2.files_checked == []

    def test_passed_override(self):
        r = RuleResult(rule="x", passed=False)
        assert r.passed is False

    def test_findings_provided(self):
        findings = [Finding("blocker", "f.go", 5, "img", "m")]
        r = RuleResult(rule="x", passed=False, findings=findings)
        assert len(r.findings) == 1
        assert r.findings[0].severity == "blocker"


class TestArchAnalyzerResult:
    def test_from_dict_full(self):
        data = {
            "dockerfiles": [{
                "path": "Dockerfile",
                "copy_instructions": [
                    {"original_sources": ["src", "pkg"], "manifest_hint": False},
                    {"original_sources": ["config"], "manifest_hint": True},
                ],
                "build_commands": [{"entry_point": "cmd/main"}],
            }],
            "kustomize_overlay_refs": [
                {"overlay_path": "overlays/odh", "file_path": "config/base/deploy.yaml"},
            ],
            "kustomize_components": [
                {"support_file": "internal/components/kserve/support.go", "overlay_paths": ["overlays/odh"]},
            ],
        }
        result = ArchAnalyzerResult.from_dict(data)
        assert len(result.dockerfiles) == 1
        df = result.dockerfiles[0]
        assert df.path == "Dockerfile"
        assert len(df.copy_instructions) == 2
        assert df.copy_instructions[0].original_sources == ["src", "pkg"]
        assert df.copy_instructions[0].manifest_hint is False
        assert df.copy_instructions[1].manifest_hint is True
        assert df.build_commands[0].entry_point == "cmd/main"
        assert len(result.kustomize_overlay_refs) == 1
        assert result.kustomize_overlay_refs[0].overlay_path == "overlays/odh"
        assert len(result.kustomize_components) == 1
        assert result.kustomize_components[0].overlay_paths == ["overlays/odh"]

    def test_from_dict_empty(self):
        result = ArchAnalyzerResult.from_dict({})
        assert result.dockerfiles == []
        assert result.kustomize_overlay_refs == []
        assert result.kustomize_components == []

    def test_from_dict_missing_fields_use_defaults(self):
        data = {"dockerfiles": [{"path": "Dockerfile"}]}
        result = ArchAnalyzerResult.from_dict(data)
        df = result.dockerfiles[0]
        assert df.copy_instructions == []
        assert df.build_commands == []

    def test_from_fixture_file(self):
        from tests.conftest import load_arch_fixture
        result = load_arch_fixture("go_operator")
        assert len(result.dockerfiles) == 1
        assert result.dockerfiles[0].path == "Dockerfile"
        assert len(result.dockerfiles[0].copy_instructions) == 3
        assert len(result.kustomize_overlay_refs) == 4
        assert len(result.kustomize_components) == 1


class TestBuildOverlayFileMap:
    def test_valid_data(self, tmp_path):
        arch_data = ArchAnalyzerResult.from_dict({
            "kustomize_overlay_refs": [
                {"overlay_path": "overlays/odh", "file_path": "config/base/deploy.yaml"},
                {"overlay_path": "overlays/odh", "file_path": "config/base/svc.yaml"},
                {"overlay_path": "overlays/dev", "file_path": "config/dev/patch.yaml"},
            ]
        })
        result = build_overlay_file_map(arch_data, tmp_path)
        assert len(result) == 2
        assert len(result["overlays/odh"]) == 2
        assert (tmp_path / "config/base/deploy.yaml").resolve() in result["overlays/odh"]
        assert len(result["overlays/dev"]) == 1

    def test_none_returns_empty(self):
        assert build_overlay_file_map(None, Path(".")) == {}

    def test_empty_result_returns_empty(self):
        assert build_overlay_file_map(ArchAnalyzerResult(), Path(".")) == {}

    def test_missing_keys_skipped(self):
        arch_data = ArchAnalyzerResult.from_dict({
            "kustomize_overlay_refs": [
                {"overlay_path": "overlays/odh"},
                {"file_path": "config/base/deploy.yaml"},
                {},
            ]
        })
        assert build_overlay_file_map(arch_data, Path(".")) == {}

    def test_empty_refs_list(self):
        assert build_overlay_file_map(ArchAnalyzerResult(), Path(".")) == {}


class TestIsNonProductionOverlayFile:
    def test_file_in_non_production_overlay(self, tmp_path):
        f = tmp_path / "config" / "dev" / "patch.yaml"
        f.parent.mkdir(parents=True)
        f.touch()
        overlay_map = {"overlays/dev": {f.resolve()}}
        scope = ProductionScope(method="test", overlay_paths=["overlays/prod"])
        assert is_non_production_overlay_file(f, scope, overlay_map) is True

    def test_file_in_production_overlay(self, tmp_path):
        f = tmp_path / "config" / "prod" / "deploy.yaml"
        f.parent.mkdir(parents=True)
        f.touch()
        overlay_map = {"overlays/prod": {f.resolve()}}
        scope = ProductionScope(method="test", overlay_paths=["overlays/prod"])
        assert is_non_production_overlay_file(f, scope, overlay_map) is False

    def test_file_not_in_any_overlay(self, tmp_path):
        f = tmp_path / "src" / "main.go"
        f.parent.mkdir(parents=True)
        f.touch()
        overlay_map = {"overlays/dev": {(tmp_path / "other.yaml").resolve()}}
        scope = ProductionScope(method="test", overlay_paths=["overlays/prod"])
        assert is_non_production_overlay_file(f, scope, overlay_map) is False

    def test_empty_overlay_map(self, tmp_path):
        f = tmp_path / "f.yaml"
        f.touch()
        scope = ProductionScope(method="test", overlay_paths=["overlays/prod"])
        assert is_non_production_overlay_file(f, scope, {}) is False

    def test_none_production_scope(self, tmp_path):
        f = tmp_path / "f.yaml"
        f.touch()
        assert is_non_production_overlay_file(f, None, {"o": {f.resolve()}}) is False

    def test_none_overlay_paths(self, tmp_path):
        f = tmp_path / "f.yaml"
        f.touch()
        scope = ProductionScope(method="test", overlay_paths=None)
        assert is_non_production_overlay_file(f, scope, {"o": {f.resolve()}}) is False


class TestConfigError:
    def test_is_exception(self):
        assert isinstance(ConfigError("msg"), Exception)

    def test_message(self):
        assert str(ConfigError("test error")) == "test error"


class TestLoadConfigFile:
    def test_missing_file(self, tmp_path):
        assert load_config_file(tmp_path / "nonexistent.yaml") == {}

    def test_valid_yaml(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("key: value\nitems:\n  - one\n  - two\n")
        result = load_config_file(f)
        assert result == {"key": "value", "items": ["one", "two"]}

    def test_empty_yaml(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("")
        assert load_config_file(f) == {}

    def test_unreadable_raises_config_error(self, tmp_path):
        d = tmp_path / "config.yaml"
        d.mkdir()
        with pytest.raises(ConfigError, match="Cannot read"):
            load_config_file(d)

    def test_malformed_yaml_raises_config_error(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text(":\n  - :\n    : [")
        with pytest.raises(ConfigError):
            load_config_file(f)

    def test_non_dict_raises_config_error(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("- item1\n- item2\n")
        with pytest.raises(ConfigError, match="must be a YAML mapping"):
            load_config_file(f)
