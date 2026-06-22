"""Tests for rules/operator_manifest.py"""

from unittest.mock import patch

import pytest

from rules.common import ArchAnalyzerResult, RuleResult
from rules.operator_manifest import (
    clone_operator, parse_component_images, parse_known_issues,
    build_manifest, run, COMPONENTS_PATH,
    parse_overlay_paths_from_arch_data, parse_manifest_entries,
)


class TestCloneOperator:
    def test_reuses_existing_repo(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        with patch("rules.operator_manifest.subprocess.run") as mock_run:
            result = clone_operator(tmp_path)
            mock_run.assert_not_called()
        assert result == tmp_path

    @patch("rules.operator_manifest.subprocess.run")
    def test_clones_when_not_present(self, mock_run, tmp_path):
        target = tmp_path / "operator"
        clone_operator(target)
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "git"
        assert args[1] == "clone"
        assert "--depth" in args
        assert str(target) in args

    @patch("rules.operator_manifest.subprocess.run")
    def test_clone_failure_raises(self, mock_run, tmp_path):
        import subprocess
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        target = tmp_path / "operator"
        with pytest.raises(subprocess.CalledProcessError):
            clone_operator(target)


class TestParseComponentImages:
    def test_image_map_pattern(self, tmp_path):
        f = tmp_path / "images.go"
        f.write_text('"my-image": "RELATED_IMAGE_MY_IMAGE"')
        entries = parse_component_images(tmp_path, "dashboard")
        assert len(entries) == 1
        assert entries[0].env_var == "RELATED_IMAGE_MY_IMAGE"
        assert entries[0].manifest_key == "my-image"
        assert entries[0].component == "dashboard"

    def test_bare_related_image(self, tmp_path):
        f = tmp_path / "controller.go"
        f.write_text('os.Getenv("RELATED_IMAGE_FOO")')
        entries = parse_component_images(tmp_path, "ray")
        assert len(entries) == 1
        assert entries[0].env_var == "RELATED_IMAGE_FOO"
        assert entries[0].manifest_key == ""

    def test_wildcard_skipped(self, tmp_path):
        f = tmp_path / "util.go"
        f.write_text('"RELATED_IMAGE_*"')
        entries = parse_component_images(tmp_path, "comp")
        assert entries == []

    def test_test_file_skipped(self, tmp_path):
        f = tmp_path / "handler_test.go"
        f.write_text('"RELATED_IMAGE_FOO"')
        entries = parse_component_images(tmp_path, "comp")
        assert entries == []

    def test_int_test_file_skipped(self, tmp_path):
        f = tmp_path / "handler_int_test.go"
        f.write_text('"RELATED_IMAGE_FOO"')
        entries = parse_component_images(tmp_path, "comp")
        assert entries == []

    def test_duplicate_env_var_deduped(self, tmp_path):
        f = tmp_path / "controller.go"
        f.write_text('"RELATED_IMAGE_FOO"\n"RELATED_IMAGE_FOO"')
        entries = parse_component_images(tmp_path, "comp")
        assert len(entries) == 1

    def test_unreadable_file_skipped(self, tmp_path):
        f = tmp_path / "bad.go"
        f.write_bytes(b'\x80\x81\x82' * 100)
        entries = parse_component_images(tmp_path, "comp")
        assert entries == []

    def test_map_takes_precedence_over_bare(self, tmp_path):
        f = tmp_path / "images.go"
        f.write_text('"my-key": "RELATED_IMAGE_FOO"')
        entries = parse_component_images(tmp_path, "comp")
        assert len(entries) == 1
        assert entries[0].manifest_key == "my-key"

    def test_nested_go_files_found(self, tmp_path):
        sub = tmp_path / "subpkg"
        sub.mkdir()
        f = sub / "inner.go"
        f.write_text('"RELATED_IMAGE_INNER"')
        entries = parse_component_images(tmp_path, "comp")
        assert len(entries) == 1
        assert entries[0].env_var == "RELATED_IMAGE_INNER"


class TestParseKnownIssues:
    def test_no_params_file(self, tmp_path):
        result = parse_known_issues(tmp_path)
        assert result == []

    def test_parses_known_issues(self, tmp_path):
        f = tmp_path / "component-params-env.yaml"
        f.write_text(
            "# known_issues:\n"
            "- image: RELATED_IMAGE_BROKEN\n"
            "- image: RELATED_IMAGE_STALE\n"
        )
        known = parse_known_issues(tmp_path)
        assert "RELATED_IMAGE_BROKEN" in known
        assert "RELATED_IMAGE_STALE" in known

    def test_only_known_issues_section_captured(self, tmp_path):
        f = tmp_path / "component-params-env.yaml"
        f.write_text(
            "# known_issues:\n"
            "- image: RELATED_IMAGE_A\n"
            "# other_section:\n"
            "- image: RELATED_IMAGE_B\n"
        )
        known = parse_known_issues(tmp_path)
        assert "RELATED_IMAGE_A" in known
        assert "RELATED_IMAGE_B" not in known

    def test_unreadable_file(self, tmp_path):
        f = tmp_path / "component-params-env.yaml"
        f.write_bytes(b'\x80\x81\x82' * 100)
        result = parse_known_issues(tmp_path)
        assert result == []


class TestBuildManifest:
    def _make_component(self, tmp_path, name, go_content):
        comp_dir = tmp_path / COMPONENTS_PATH / name
        comp_dir.mkdir(parents=True)
        (comp_dir / "images.go").write_text(go_content)

    def test_no_components_dir(self, tmp_path):
        manifest = build_manifest(tmp_path)
        assert manifest.images == []
        assert manifest.components == {}

    def test_discovers_component(self, tmp_path):
        self._make_component(tmp_path, "dashboard", '"RELATED_IMAGE_DASH"')
        manifest = build_manifest(tmp_path)
        assert len(manifest.images) == 1
        assert manifest.images[0].env_var == "RELATED_IMAGE_DASH"
        assert manifest.images[0].component == "dashboard"
        assert "dashboard" in manifest.components

    def test_skips_registry_dir(self, tmp_path):
        self._make_component(tmp_path, "registry", '"RELATED_IMAGE_REG"')
        manifest = build_manifest(tmp_path)
        assert manifest.images == []

    def test_skips_hidden_dir(self, tmp_path):
        self._make_component(tmp_path, ".hidden", '"RELATED_IMAGE_HIDDEN"')
        manifest = build_manifest(tmp_path)
        assert manifest.images == []

    def test_skips_non_dir_in_components(self, tmp_path):
        comp_dir = tmp_path / COMPONENTS_PATH
        comp_dir.mkdir(parents=True)
        (comp_dir / "README.md").write_text('"RELATED_IMAGE_README"')
        manifest = build_manifest(tmp_path)
        assert manifest.images == []

    def test_top_level_go_scanned(self, tmp_path):
        (tmp_path / COMPONENTS_PATH).mkdir(parents=True)
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "core.go").write_text('"RELATED_IMAGE_CORE"')
        manifest = build_manifest(tmp_path)
        assert len(manifest.images) == 1
        assert manifest.images[0].component == "operator-core"

    def test_top_level_dedupes_with_components(self, tmp_path):
        self._make_component(tmp_path, "dashboard", '"RELATED_IMAGE_DASH"')
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "core.go").write_text('"RELATED_IMAGE_DASH"')
        manifest = build_manifest(tmp_path)
        assert len(manifest.images) == 1
        assert manifest.images[0].component == "dashboard"

    def test_multiple_components(self, tmp_path):
        self._make_component(tmp_path, "dashboard", '"RELATED_IMAGE_DASH"')
        self._make_component(tmp_path, "ray", '"RELATED_IMAGE_RAY"')
        manifest = build_manifest(tmp_path)
        assert len(manifest.images) == 2
        assert len(manifest.components) == 2

    def test_known_issues_integrated(self, tmp_path):
        (tmp_path / COMPONENTS_PATH).mkdir(parents=True)
        f = tmp_path / "component-params-env.yaml"
        f.write_text("# known_issues:\n- image: RELATED_IMAGE_BROKEN\n")
        manifest = build_manifest(tmp_path)
        assert "RELATED_IMAGE_BROKEN" in manifest.known_issues

    def test_top_level_skips_vendor(self, tmp_path):
        (tmp_path / COMPONENTS_PATH).mkdir(parents=True)
        vendor = tmp_path / "vendor"
        vendor.mkdir()
        (vendor / "lib.go").write_text('"RELATED_IMAGE_VENDOR"')
        manifest = build_manifest(tmp_path)
        assert manifest.images == []

    def test_top_level_skips_test_files(self, tmp_path):
        (tmp_path / COMPONENTS_PATH).mkdir(parents=True)
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "core_test.go").write_text('"RELATED_IMAGE_CORE"')
        manifest = build_manifest(tmp_path)
        assert manifest.images == []


class TestRun:
    def test_returns_rule_result(self, tmp_path):
        result = run(str(tmp_path))
        assert isinstance(result, RuleResult)
        assert result.rule == "operator-manifest"
        assert isinstance(result.passed, bool)
        assert isinstance(result.findings, list)

    def test_empty_manifest(self, tmp_path):
        result = run(str(tmp_path))
        assert result.passed is True
        info_msgs = [f.message for f in result.findings if f.severity == "info"]
        assert any("0 unique RELATED_IMAGE" in m for m in info_msgs)

    def test_deduplicates_env_vars_in_count(self, tmp_path):
        comp_dir = tmp_path / COMPONENTS_PATH / "comp"
        comp_dir.mkdir(parents=True)
        (comp_dir / "a.go").write_text('"RELATED_IMAGE_FOO"')
        (comp_dir / "b.go").write_text('"RELATED_IMAGE_FOO"')
        result = run(str(tmp_path))
        info_msgs = [f.message for f in result.findings if f.severity == "info"]
        assert any("1 unique RELATED_IMAGE" in m for m in info_msgs)

class TestParseOverlayPathsFromArchData:
    def test_basic_overlays(self):
        arch_data = ArchAnalyzerResult.from_dict({
            "kustomize_components": [{
                "support_file": "internal/controller/components/kserve/kserve_support.go",
                "overlay_paths": ["overlays/odh"],
            }]
        })
        result = parse_overlay_paths_from_arch_data(arch_data, "kserve")
        assert result == ["overlays/odh"]

    def test_multiple_overlays(self):
        arch_data = ArchAnalyzerResult.from_dict({
            "kustomize_components": [{
                "support_file": "internal/controller/components/dashboard/dashboard_support.go",
                "overlay_paths": ["overlays/rhoai", "overlays/odh"],
            }]
        })
        result = parse_overlay_paths_from_arch_data(arch_data, "dashboard")
        assert result == ["overlays/rhoai", "overlays/odh"]

    def test_component_dir_map(self):
        arch_data = ArchAnalyzerResult.from_dict({
            "kustomize_components": [{
                "support_file": "internal/controller/components/modelsasservice/maas_support.go",
                "overlay_paths": ["overlays/odh"],
            }]
        })
        result = parse_overlay_paths_from_arch_data(arch_data, "maas")
        assert result == ["overlays/odh"]

    def test_component_key_with_slash(self):
        arch_data = ArchAnalyzerResult.from_dict({
            "kustomize_components": [{
                "support_file": "internal/controller/components/workbenches/wb_support.go",
                "overlay_paths": ["overlays/odh"],
            }]
        })
        result = parse_overlay_paths_from_arch_data(arch_data, "workbenches/kf-notebook-controller")
        assert result == ["overlays/odh"]

    def test_skip_operator_component(self):
        result = parse_overlay_paths_from_arch_data(ArchAnalyzerResult(), "operator")
        assert result == []

    def test_empty_arch_data(self):
        result = parse_overlay_paths_from_arch_data(ArchAnalyzerResult(), "kserve")
        assert result == []

    def test_leading_slash_stripped(self):
        arch_data = ArchAnalyzerResult.from_dict({
            "kustomize_components": [{
                "support_file": "internal/controller/components/kserve/kserve_support.go",
                "overlay_paths": ["/overlays/odh/"],
            }]
        })
        result = parse_overlay_paths_from_arch_data(arch_data, "kserve")
        assert result == ["overlays/odh"]


class TestParseRepoComponentKey:
    def test_parses_manifest_entries(self, tmp_path):
        script = tmp_path / "get_all_manifests.sh"
        script.write_text('''
declare -A ODH_COMPONENT_MANIFESTS=(
    ["dashboard"]="opendatahub-io:odh-dashboard:main:manifests"
    ["workbenches/kf-notebook-controller"]="opendatahub-io:kubeflow:main:config"
    ["kserve"]="opendatahub-io:kserve:release-v0.17:config"
)
''')
        _, result = parse_manifest_entries(str(tmp_path))
        assert result["odh-dashboard"] == "dashboard"
        assert result["kubeflow"] == "workbenches/kf-notebook-controller"
        assert result["kserve"] == "kserve"

    def test_missing_script(self, tmp_path):
        _, keys = parse_manifest_entries(str(tmp_path))
        assert keys == {}


class TestParseManifestEntries:
    def test_parses_all_array_types(self, tmp_path):
        script = tmp_path / "get_all_manifests.sh"
        script.write_text('''
declare -A ODH_COMPONENT_MANIFESTS=(
    ["kserve"]="opendatahub-io:kserve:release-v0.17:config"
)
declare -A ODH_COMPONENT_CHARTS=(
    ["trustyai"]="trustyai-explainability:trustyai-service-operator:main:chart"
)
declare -A ODH_CCM_CHARTS=(
    ["ccm/model-registry"]="opendatahub-io:model-registry-operator:main:config"
)
''')
        source_folders, component_keys = parse_manifest_entries(str(tmp_path))
        assert source_folders["kserve"] == ["config"]
        assert source_folders["trustyai-service-operator"] == ["chart"]
        assert source_folders["model-registry-operator"] == ["config"]
        assert component_keys["kserve"] == "kserve"
        assert component_keys["trustyai-service-operator"] == "trustyai"
        assert component_keys["model-registry-operator"] == "ccm/model-registry"

    def test_missing_script(self, tmp_path):
        folders, keys = parse_manifest_entries(str(tmp_path))
        assert folders == {}
        assert keys == {}


    def test_known_issues_become_info(self, tmp_path):
        (tmp_path / COMPONENTS_PATH).mkdir(parents=True)
        (tmp_path / "component-params-env.yaml").write_text(
            "# known_issues:\n- image: RELATED_IMAGE_BROKEN\n"
        )
        result = run(str(tmp_path))
        issue_findings = [f for f in result.findings
                          if f.image == "RELATED_IMAGE_BROKEN"]
        assert len(issue_findings) == 1
        assert issue_findings[0].severity == "info"
