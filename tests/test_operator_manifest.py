"""Tests for rules/operator_manifest.py"""

from pathlib import Path
from unittest.mock import patch

import pytest

from rules.operator_manifest import (
    clone_operator, parse_component_images, parse_known_issues,
    build_manifest, run, ImageEntry, COMPONENTS_PATH,
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
        assert result == ([], [])

    def test_parses_known_issues(self, tmp_path):
        f = tmp_path / "component-params-env.yaml"
        f.write_text(
            "# known_issues:\n"
            "- image: RELATED_IMAGE_BROKEN\n"
            "- image: RELATED_IMAGE_STALE\n"
        )
        known, _ = parse_known_issues(tmp_path)
        assert "RELATED_IMAGE_BROKEN" in known
        assert "RELATED_IMAGE_STALE" in known

    def test_all_image_entries_captured(self, tmp_path):
        f = tmp_path / "component-params-env.yaml"
        f.write_text(
            "# known_issues:\n"
            "- image: RELATED_IMAGE_A\n"
            "# other_section:\n"
            "- image: RELATED_IMAGE_B\n"
        )
        known, _ = parse_known_issues(tmp_path)
        assert "RELATED_IMAGE_A" in known
        assert "RELATED_IMAGE_B" in known

    def test_unreadable_file(self, tmp_path):
        f = tmp_path / "component-params-env.yaml"
        f.write_bytes(b'\x80\x81\x82' * 100)
        result = parse_known_issues(tmp_path)
        assert result == ([], [])


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
    def test_returns_dict_structure(self, tmp_path):
        result = run(str(tmp_path))
        assert "total_images" in result
        assert "total_components" in result
        assert "components" in result
        assert "known_issues" in result
        assert "all_env_vars" in result
        assert "image_details" in result

    def test_empty_manifest(self, tmp_path):
        result = run(str(tmp_path))
        assert result["total_images"] == 0
        assert result["total_components"] == 0
        assert result["image_details"] == []

    def test_deduplicates_env_vars_in_count(self, tmp_path):
        comp_dir = tmp_path / COMPONENTS_PATH / "comp"
        comp_dir.mkdir(parents=True)
        (comp_dir / "a.go").write_text('"RELATED_IMAGE_FOO"')
        (comp_dir / "b.go").write_text('"RELATED_IMAGE_FOO"')
        result = run(str(tmp_path))
        assert result["total_images"] == 1

    def test_image_details_populated(self, tmp_path):
        comp_dir = tmp_path / COMPONENTS_PATH / "dashboard"
        comp_dir.mkdir(parents=True)
        (comp_dir / "images.go").write_text('"my-key": "RELATED_IMAGE_DASH"')
        result = run(str(tmp_path))
        assert len(result["image_details"]) == 1
        detail = result["image_details"][0]
        assert detail["env_var"] == "RELATED_IMAGE_DASH"
        assert detail["component"] == "dashboard"
        assert detail["manifest_key"] == "my-key"
