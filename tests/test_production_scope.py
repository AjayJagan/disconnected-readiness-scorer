"""Tests for rules/production_scope.py"""

import json
from pathlib import Path

from rules.common import ProductionScope, is_file_in_production_scope, is_yaml_in_production_scope
from rules.production_scope import (
    _collect_go_embedded_yamls,
    _extract_production_sources_from_arch_data,
    _find_go_module_dir,
    _glob_source,
    _is_glob_source,
    _is_js_monorepo,
    _nearest_package_json_dir,
    _normalize_glob,
    collect_manifest_scope_files,
    compute_production_scope,
)
from rules.operator_manifest import parse_manifest_entries


# ---------------------------------------------------------------------------
# _join_continuations
# ---------------------------------------------------------------------------


class TestIsInProductionScope:
    def test_none_scope(self):
        assert is_file_in_production_scope(Path("foo.go"), None) is None

    def test_non_go_file(self):
        scope = ProductionScope(method="test")
        assert is_file_in_production_scope(Path("foo.py"), scope) is None

    def test_go_file_in_scope(self, tmp_path):
        f = tmp_path / "main.go"
        f.write_text("")
        scope = ProductionScope(production_dirs={f.parent.resolve()}, method="test")
        assert is_file_in_production_scope(f, scope) is True

    def test_go_file_out_of_scope_empty_set(self, tmp_path):
        f = tmp_path / "tool.go"
        f.write_text("")
        scope = ProductionScope(method="test")
        assert is_file_in_production_scope(f, scope) is None

    def test_go_file_out_of_scope(self, tmp_path):
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        f = tools_dir / "tool.go"
        f.write_text("")
        cmd_dir = tmp_path / "cmd"
        cmd_dir.mkdir()
        other = cmd_dir / "main.go"
        other.write_text("")
        scope = ProductionScope(production_dirs={cmd_dir.resolve()}, method="test")
        assert is_file_in_production_scope(f, scope) is False

    def test_file_in_production_files_returns_true(self, tmp_path):
        f = tmp_path / "go.mod"
        f.write_text("module example.com\n")
        scope = ProductionScope(production_files={f.resolve()}, method="test")
        assert is_file_in_production_scope(f, scope) is True

    def test_file_not_in_production_files_returns_false(self, tmp_path):
        f = tmp_path / "go.mod"
        f.write_text("module example.com\n")
        other = tmp_path / "tools" / "go.mod"
        scope = ProductionScope(production_files={f.resolve()}, method="test")
        assert is_file_in_production_scope(other, scope) is False

    def test_production_files_and_dirs_combined(self, tmp_path):
        root_file = tmp_path / "go.mod"
        root_file.write_text("module example.com\n")
        src = tmp_path / "src"
        src.mkdir()
        src_file = src / "main.go"
        src_file.write_text("")
        scope = ProductionScope(
            production_dirs={src.resolve()},
            production_files={root_file.resolve()},
            method="test",
        )
        assert is_file_in_production_scope(root_file, scope) is True
        assert is_file_in_production_scope(src_file, scope) is True
        other = tmp_path / "tools" / "tool.go"
        assert is_file_in_production_scope(other, scope) is False


# ---------------------------------------------------------------------------
# is_yaml_in_production_scope
# ---------------------------------------------------------------------------

class TestIsYamlInProductionScope:
    def test_none_scope(self):
        assert is_yaml_in_production_scope(Path("deploy.yaml"), None) is None

    def test_no_manifest_files(self):
        scope = ProductionScope(method="test")
        assert is_yaml_in_production_scope(Path("deploy.yaml"), scope) is None

    def test_non_yaml_file(self):
        scope = ProductionScope(
            method="test",
            manifest_files=set(),
        )
        assert is_yaml_in_production_scope(Path("main.go"), scope) is None

    def test_yaml_in_scope(self, tmp_path):
        f = tmp_path / "deploy.yaml"
        f.write_text("")
        scope = ProductionScope(
            method="test",
            manifest_files={f.resolve()},
        )
        assert is_yaml_in_production_scope(f, scope) is True

    def test_yaml_out_of_scope(self, tmp_path):
        f = tmp_path / "sample.yaml"
        f.write_text("")
        scope = ProductionScope(
            method="test",
            manifest_files=set(),
        )
        assert is_yaml_in_production_scope(f, scope) is False

    def test_yml_extension(self, tmp_path):
        f = tmp_path / "deploy.yml"
        f.write_text("")
        scope = ProductionScope(
            method="test",
            manifest_files={f.resolve()},
        )
        assert is_yaml_in_production_scope(f, scope) is True


# ---------------------------------------------------------------------------
# collect_manifest_scope_files
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _collect_go_embedded_yamls
# ---------------------------------------------------------------------------

class TestCollectGoEmbeddedYamls:
    def test_no_production_files(self):
        assert _collect_go_embedded_yamls(Path("."), None) == set()

    def test_embed_single_yaml(self, tmp_path):
        go_file = tmp_path / "main.go"
        yaml_file = tmp_path / "defaults.yaml"
        yaml_file.write_text("key: val")
        go_file.write_text(
            'package main\n'
            'import "embed"\n'
            '//go:embed defaults.yaml\n'
            'var config []byte\n'
        )
        result = _collect_go_embedded_yamls(tmp_path, {go_file.parent.resolve()})
        assert yaml_file.resolve() in result

    def test_embed_subdirectory(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        go_file = pkg / "handler.go"
        cfg_dir = pkg / "config"
        cfg_dir.mkdir()
        yaml_file = cfg_dir / "rules.yaml"
        yaml_file.write_text("rules: []")
        go_file.write_text(
            'package pkg\n'
            '//go:embed config/rules.yaml\n'
            'var rules string\n'
        )
        result = _collect_go_embedded_yamls(tmp_path, {go_file.parent.resolve()})
        assert yaml_file.resolve() in result

    def test_embed_glob_pattern(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        go_file = pkg / "handler.go"
        cfg = pkg / "templates"
        cfg.mkdir()
        (cfg / "a.yaml").write_text("a: 1")
        (cfg / "b.yml").write_text("b: 2")
        (cfg / "c.txt").write_text("not yaml")
        go_file.write_text(
            'package pkg\n'
            '//go:embed templates/*\n'
            'var tpls embed.FS\n'
        )
        result = _collect_go_embedded_yamls(tmp_path, {go_file.parent.resolve()})
        assert (cfg / "a.yaml").resolve() in result
        assert (cfg / "b.yml").resolve() in result
        assert (cfg / "c.txt").resolve() not in result

    def test_skips_non_production_go_files(self, tmp_path):
        go_file = tmp_path / "tool.go"
        yaml_file = tmp_path / "data.yaml"
        yaml_file.write_text("x: 1")
        go_file.write_text('//go:embed data.yaml\nvar d []byte\n')
        result = _collect_go_embedded_yamls(tmp_path, set())
        assert result == set()

    def test_nonexistent_embed_target(self, tmp_path):
        go_file = tmp_path / "main.go"
        go_file.write_text('//go:embed missing.yaml\nvar d []byte\n')
        result = _collect_go_embedded_yamls(tmp_path, {go_file.parent.resolve()})
        assert result == set()


class TestCollectManifestScopeFiles:
    def test_nonexistent_dir(self, tmp_path):
        assert collect_manifest_scope_files(tmp_path / "nope") is None

    def test_dir_without_kustomize_or_chart(self, tmp_path):
        (tmp_path / "random.yaml").write_text("foo: bar")
        assert collect_manifest_scope_files(tmp_path) is None

    def test_helm_chart_includes_all_yaml(self, tmp_path):
        (tmp_path / "Chart.yaml").write_text("name: test")
        (tmp_path / "values.yaml").write_text("key: val")
        tpl = tmp_path / "templates"
        tpl.mkdir()
        (tpl / "deploy.yaml").write_text("kind: Deployment")
        (tpl / "svc.yaml").write_text("kind: Service")

        result = collect_manifest_scope_files(tmp_path)
        assert result is not None
        assert len(result) == 4

    def test_helm_chart_excludes_test_templates(self, tmp_path):
        (tmp_path / "Chart.yaml").write_text("name: test")
        (tmp_path / "values.yaml").write_text("key: val")
        tpl = tmp_path / "templates"
        tpl.mkdir()
        (tpl / "deploy.yaml").write_text("kind: Deployment")
        tests_dir = tpl / "tests"
        tests_dir.mkdir()
        (tests_dir / "test-connection.yaml").write_text("kind: Pod")
        examples_dir = tmp_path / "examples"
        examples_dir.mkdir()
        (examples_dir / "sample.yaml").write_text("kind: ConfigMap")

        result = collect_manifest_scope_files(tmp_path)
        assert result is not None
        names = {f.name for f in result}
        assert "deploy.yaml" in names
        assert "values.yaml" in names
        assert "test-connection.yaml" not in names
        assert "sample.yaml" not in names

    def test_kustomize_collects_referenced_dirs(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        (base / "kustomization.yaml").write_text("resources:\n- ../default\n")
        (base / "params.env").write_text("key=val")

        default = tmp_path / "default"
        default.mkdir()
        (default / "kustomization.yaml").write_text("resources:\n- manager\n")
        (default / "deploy.yaml").write_text("kind: Deployment")

        mgr = default / "manager"
        mgr.mkdir()
        (mgr / "kustomization.yaml").write_text("resources:\n- deployment.yaml\n")
        (mgr / "deployment.yaml").write_text("kind: Deployment")

        result = collect_manifest_scope_files(tmp_path)
        assert result is not None
        assert (default / "deploy.yaml").resolve() in result
        assert (mgr / "deployment.yaml").resolve() in result
        assert (base / "kustomization.yaml").resolve() in result

    def test_kustomize_does_not_include_unreferenced_dirs(self, tmp_path):
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "kustomization.yaml").write_text("resources:\n- base\n")

        base = cfg / "base"
        base.mkdir()
        (base / "kustomization.yaml").write_text("resources: []\n")
        (base / "deploy.yaml").write_text("kind: Deployment")

        samples = cfg / "samples"
        samples.mkdir()
        (samples / "example.yaml").write_text("kind: InferenceService")

        result = collect_manifest_scope_files(cfg)
        assert result is not None
        assert (samples / "example.yaml").resolve() not in result


# ---------------------------------------------------------------------------
# parse_manifest_entries
# ---------------------------------------------------------------------------

class TestParseComponentManifestMapping:
    def test_parses_odh_manifests(self, tmp_path):
        script = tmp_path / "get_all_manifests.sh"
        script.write_text(
            '#!/bin/bash\n'
            'declare -A ODH_COMPONENT_MANIFESTS=(\n'
            '    ["kserve"]="opendatahub-io:kserve:main@abc123:config"\n'
            '    ["dashboard"]="opendatahub-io:odh-dashboard:main@def456:manifests"\n'
            ')\n'
        )
        result, _ = parse_manifest_entries(str(tmp_path))
        assert result["kserve"] == ["config"]
        assert result["odh-dashboard"] == ["manifests"]

    def test_parses_charts(self, tmp_path):
        script = tmp_path / "get_all_manifests.sh"
        script.write_text(
            '#!/bin/bash\n'
            'declare -A ODH_COMPONENT_CHARTS=(\n'
            '    ["cert-mgr"]="opendatahub-io:odh-gitops:main@abc:charts/deps/cert"\n'
            ')\n'
        )
        result, _ = parse_manifest_entries(str(tmp_path))
        assert result["odh-gitops"] == ["charts/deps/cert"]

    def test_missing_script(self, tmp_path):
        result, _ = parse_manifest_entries(str(tmp_path))
        assert result == {}

    def test_merges_multiple_entries_for_same_repo(self, tmp_path):
        script = tmp_path / "get_all_manifests.sh"
        script.write_text(
            '#!/bin/bash\n'
            'declare -A ODH_COMPONENT_MANIFESTS=(\n'
            '    ["nb-ctrl"]="opendatahub-io:kubeflow:main@abc:components/nb/config"\n'
            '    ["odh-ctrl"]="opendatahub-io:kubeflow:main@abc:components/odh/config"\n'
            ')\n'
        )
        result, _ = parse_manifest_entries(str(tmp_path))
        assert sorted(result["kubeflow"]) == sorted([
            "components/nb/config",
            "components/odh/config",
        ])


# ---------------------------------------------------------------------------
# compute_production_scope with manifest_source_folders
# ---------------------------------------------------------------------------

class TestComputeProductionScopeWithManifests:
    def test_manifest_only_scope(self, tmp_path):
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "kustomization.yaml").write_text("resources:\n- deploy.yaml\n")
        (cfg / "deploy.yaml").write_text("kind: Deployment")

        scope = compute_production_scope(tmp_path, manifest_source_folders=["config"])
        assert scope is not None
        assert scope.manifest_files is not None
        assert (cfg / "deploy.yaml").resolve() in scope.manifest_files
        assert scope.manifest_source == "config"

    def test_no_manifest_folders_no_scope(self, tmp_path):
        scope = compute_production_scope(tmp_path)
        assert scope is None


# ---------------------------------------------------------------------------
# _is_glob_source
# ---------------------------------------------------------------------------

class TestIsGlobSource:
    def test_double_star(self):
        assert _is_glob_source("cmd/**/main.go") is True

    def test_docker_arg(self):
        assert _is_glob_source("${APP_DIR}/main.go") is True

    def test_literal_path(self):
        assert _is_glob_source("cmd/main.go") is False

    def test_single_star_not_glob(self):
        assert _is_glob_source("cmd/*.go") is False


# ---------------------------------------------------------------------------
# _normalize_glob
# ---------------------------------------------------------------------------

class TestNormalizeGlob:
    def test_full_component_becomes_doublestar(self):
        assert _normalize_glob("${VAR}/main.go") == "**/main.go"

    def test_mid_component_becomes_star(self):
        assert _normalize_glob("file.${EXT}.txt") == "file.*.txt"

    def test_no_vars_unchanged(self):
        assert _normalize_glob("src/main.go") == "src/main.go"

    def test_multiple_vars(self):
        result = _normalize_glob("${A}/${B}/file.go")
        assert result == "**/**/file.go"

    def test_trailing_var(self):
        assert _normalize_glob("src/${PKG}") == "src/**"


# ---------------------------------------------------------------------------
# _glob_source
# ---------------------------------------------------------------------------

class TestGlobSource:
    def test_pure_doublestar_returns_empty(self, tmp_path):
        assert _glob_source("**", tmp_path, tmp_path.resolve()) == []

    def test_matches_dirs(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.go").write_text("")
        results = _glob_source("src/**", tmp_path, tmp_path.resolve())
        resolved = [r.resolve() for r in results]
        assert src.resolve() in resolved or any(
            r.resolve() == (src / "main.go").resolve() for r in results
        )

    def test_no_match_returns_empty(self, tmp_path):
        assert _glob_source("nonexistent/**", tmp_path, tmp_path.resolve()) == []

    def test_filters_root(self, tmp_path):
        (tmp_path / "file.txt").write_text("")
        results = _glob_source("*", tmp_path, tmp_path.resolve())
        assert tmp_path.resolve() not in [r.resolve() for r in results]


# ---------------------------------------------------------------------------
# _find_go_module_dir
# ---------------------------------------------------------------------------

class TestFindGoModuleDir:
    def test_finds_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com\n")
        result = _find_go_module_dir(["go.mod"], tmp_path)
        assert result == tmp_path.resolve()

    def test_finds_nested_go_mod(self, tmp_path):
        cmd = tmp_path / "cmd"
        cmd.mkdir()
        (cmd / "go.mod").write_text("module example.com/cmd\n")
        result = _find_go_module_dir(["cmd/go.mod"], tmp_path)
        assert result == cmd.resolve()

    def test_no_go_mod_returns_none(self, tmp_path):
        result = _find_go_module_dir(["src/main.go"], tmp_path)
        assert result is None

    def test_skips_non_gomod_pattern(self, tmp_path):
        (tmp_path / "go.sum").write_text("")
        result = _find_go_module_dir(["go.sum"], tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# _nearest_package_json_dir
# ---------------------------------------------------------------------------

class TestNearestPackageJsonDir:
    def test_in_current_dir(self, tmp_path):
        app = tmp_path / "app"
        app.mkdir()
        (app / "package.json").write_text("{}")
        result = _nearest_package_json_dir(app, tmp_path)
        assert result == app.resolve()

    def test_walks_up(self, tmp_path):
        app = tmp_path / "app"
        docker = app / "docker"
        docker.mkdir(parents=True)
        (app / "package.json").write_text("{}")
        result = _nearest_package_json_dir(docker, tmp_path)
        assert result == app.resolve()

    def test_stops_at_repo_root(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        result = _nearest_package_json_dir(deep, tmp_path)
        assert result == deep.resolve()


# ---------------------------------------------------------------------------
# _is_js_monorepo
# ---------------------------------------------------------------------------

class TestIsJsMonorepo:
    def test_with_workspaces(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"workspaces": ["packages/*"]}))
        assert _is_js_monorepo(tmp_path) is True

    def test_without_workspaces(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"name": "app"}))
        assert _is_js_monorepo(tmp_path) is False

    def test_no_package_json(self, tmp_path):
        assert _is_js_monorepo(tmp_path) is False

    def test_invalid_json(self, tmp_path):
        (tmp_path / "package.json").write_text("not json{{{")
        assert _is_js_monorepo(tmp_path) is False


# ---------------------------------------------------------------------------
# _extract_production_sources_from_arch_data
# ---------------------------------------------------------------------------

class TestExtractProductionSources:
    def test_empty_dockerfiles(self, tmp_path):
        dirs, files, m_dirs, m_folders = _extract_production_sources_from_arch_data(
            {"dockerfiles": []}, tmp_path
        )
        assert dirs == set()
        assert files == set()
        assert m_dirs == set()
        assert m_folders == []

    def test_literal_source_dir(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        arch_data = {"dockerfiles": [{
            "path": "Dockerfile",
            "copy_instructions": [{"original_sources": ["src"]}],
        }]}
        dirs, _, _, _ = _extract_production_sources_from_arch_data(arch_data, tmp_path)
        assert src.resolve() in dirs

    def test_literal_source_file(self, tmp_path):
        f = tmp_path / "go.mod"
        f.write_text("module example.com\n")
        arch_data = {"dockerfiles": [{
            "path": "Dockerfile",
            "copy_instructions": [{"original_sources": ["go.mod"]}],
        }]}
        _, files, _, _ = _extract_production_sources_from_arch_data(arch_data, tmp_path)
        assert f.resolve() in files

    def test_manifest_hint_dir(self, tmp_path):
        cfg = tmp_path / "config"
        cfg.mkdir()
        arch_data = {"dockerfiles": [{
            "path": "Dockerfile",
            "copy_instructions": [{
                "original_sources": ["config"],
                "manifest_hint": True,
            }],
        }]}
        _, _, m_dirs, m_folders = _extract_production_sources_from_arch_data(arch_data, tmp_path)
        assert cfg.resolve() in m_dirs
        assert "config" in m_folders

    def test_entry_points_and_copy_sources_both_included(self, tmp_path):
        """Entry points and COPY sources are both included in production scope."""
        src = tmp_path / "src"
        src.mkdir()
        cmd = tmp_path / "cmd"
        cmd.mkdir()
        arch_data = {"dockerfiles": [{
            "path": "Dockerfile",
            "build_commands": [{"entry_point": "cmd"}],
            "copy_instructions": [{"original_sources": ["src"]}],
        }]}
        dirs, _, _, _ = _extract_production_sources_from_arch_data(arch_data, tmp_path)
        assert cmd.resolve() in dirs
        assert src.resolve() in dirs

    def test_root_source_with_docker_context(self, tmp_path):
        app = tmp_path / "app"
        app.mkdir()
        arch_data = {"dockerfiles": [{
            "path": "Dockerfile",
            "copy_instructions": [{"original_sources": ["."]}],
        }]}
        dirs, _, _, _ = _extract_production_sources_from_arch_data(
            arch_data, tmp_path, docker_contexts={"Dockerfile": "app"}
        )
        assert app.resolve() in dirs

    def test_glob_source_with_var(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "main.go").write_text("")
        arch_data = {"dockerfiles": [{
            "path": "Dockerfile",
            "copy_instructions": [{"original_sources": ["${APP}/main.go"]}],
        }]}
        dirs, files, _, _ = _extract_production_sources_from_arch_data(arch_data, tmp_path)
        resolved_files = {f.resolve() for f in files}
        resolved_dirs = {d.resolve() for d in dirs}
        assert (pkg / "main.go").resolve() in resolved_files or pkg.resolve() in resolved_dirs

    def test_no_copy_instructions(self, tmp_path):
        arch_data = {"dockerfiles": [{
            "path": "Dockerfile",
        }]}
        dirs, files, _, _ = _extract_production_sources_from_arch_data(
            arch_data, tmp_path
        )
        assert dirs == set()
        assert files == set()

