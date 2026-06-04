"""Tests for rules/production_scope.py"""

from pathlib import Path
from unittest.mock import patch, MagicMock
import json
import subprocess

from rules.common import ProductionScope, is_in_production_scope, is_yaml_in_production_scope
from rules.production_scope import (
    _parse_dockerfile,
    _find_all_dockerfiles,
    _find_go_entrypoints_heuristic,
    _go_list_deps,
    _iter_json_objects,
    _join_continuations,
    _collect_go_embedded_yamls,
    collect_manifest_scope_files,
    compute_production_scope,
)
from rules.operator_manifest import parse_component_manifest_mapping


# ---------------------------------------------------------------------------
# _join_continuations
# ---------------------------------------------------------------------------

class TestJoinContinuations:
    def test_no_continuations(self):
        assert _join_continuations(["a", "b"]) == ["a", "b"]

    def test_single_continuation(self):
        result = _join_continuations(["RUN go build \\", "  -o /bin ./cmd/m"])
        assert result == ["RUN go build    -o /bin ./cmd/m"]

    def test_multi_continuation(self):
        result = _join_continuations(["RUN a \\", "  b \\", "  c"])
        assert result == ["RUN a    b    c"]


# ---------------------------------------------------------------------------
# _parse_dockerfile
# ---------------------------------------------------------------------------

class TestParseDockerfile:
    def test_simple_go_build(self, tmp_path):
        df = tmp_path / "Dockerfile"
        df.write_text(
            "FROM golang:1.21 AS builder\n"
            "COPY . .\n"
            "RUN go build -o /manager ./cmd/manager\n"
            "\n"
            "FROM registry.access.redhat.com/ubi9/ubi-minimal:latest\n"
            "COPY --from=builder /manager /manager\n"
            "ENTRYPOINT [\"/manager\"]\n"
        )
        assert _parse_dockerfile(df) == ["./cmd/manager"]

    def test_multi_stage_collects_all(self, tmp_path):
        df = tmp_path / "Dockerfile"
        df.write_text(
            "FROM golang:1.21 AS tools\n"
            "RUN go build -o /lint ./cmd/lint\n"
            "\n"
            "FROM golang:1.21 AS builder\n"
            "RUN go build -o /app ./cmd/app\n"
            "\n"
            "FROM ubi9\n"
            "COPY --from=builder /app /app\n"
        )
        assert _parse_dockerfile(df) == ["./cmd/lint", "./cmd/app"]

    def test_numeric_copy_from(self, tmp_path):
        df = tmp_path / "Dockerfile"
        df.write_text(
            "FROM golang:1.21\n"
            "RUN go build -o /svc ./cmd/svc\n"
            "\n"
            "FROM ubi9\n"
            "COPY --from=0 /svc /svc\n"
        )
        assert _parse_dockerfile(df) == ["./cmd/svc"]

    def test_no_go_build(self, tmp_path):
        df = tmp_path / "Dockerfile"
        df.write_text(
            "FROM python:3.11\n"
            "COPY . /app\n"
            "RUN pip install -r requirements.txt\n"
        )
        assert _parse_dockerfile(df) == []

    def test_line_continuation(self, tmp_path):
        df = tmp_path / "Dockerfile"
        df.write_text(
            "FROM golang:1.21 AS builder\n"
            "RUN CGO_ENABLED=0 go build \\\n"
            "  -ldflags '-s -w' \\\n"
            "  -o /ctrl ./cmd/controller\n"
            "\n"
            "FROM ubi9\n"
            "COPY --from=builder /ctrl /ctrl\n"
        )
        assert _parse_dockerfile(df) == ["./cmd/controller"]

    def test_comments_skipped(self, tmp_path):
        df = tmp_path / "Dockerfile"
        df.write_text(
            "FROM golang:1.21 AS builder\n"
            "# RUN go build -o /fake ./cmd/fake\n"
            "RUN go build -o /real ./cmd/real\n"
            "\n"
            "FROM ubi9\n"
            "COPY --from=builder /real /real\n"
        )
        assert _parse_dockerfile(df) == ["./cmd/real"]

    def test_dot_package(self, tmp_path):
        df = tmp_path / "Dockerfile"
        df.write_text(
            "FROM golang:1.21\n"
            "RUN go build -o /app .\n"
        )
        assert _parse_dockerfile(df) == ["."]

    def test_missing_file(self, tmp_path):
        assert _parse_dockerfile(tmp_path / "no-such-file") == []

    def test_multiple_builds_same_stage(self, tmp_path):
        df = tmp_path / "Dockerfile"
        df.write_text(
            "FROM golang:1.21 AS builder\n"
            "RUN go build -o /manager ./cmd/manager\n"
            "RUN go build -o /webhook ./cmd/webhook\n"
            "\n"
            "FROM ubi9\n"
            "COPY --from=builder /manager /manager\n"
        )
        assert _parse_dockerfile(df) == ["./cmd/manager", "./cmd/webhook"]

    def test_deduplicates_packages(self, tmp_path):
        df = tmp_path / "Dockerfile"
        df.write_text(
            "FROM golang:1.21 AS builder\n"
            "RUN go build -o /mgr ./cmd/manager\n"
            "RUN go build -o /mgr2 ./cmd/manager\n"
        )
        assert _parse_dockerfile(df) == ["./cmd/manager"]


# ---------------------------------------------------------------------------
# _find_all_dockerfiles
# ---------------------------------------------------------------------------

class TestFindAllDockerfiles:
    def test_root_dockerfile(self, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM ubi9\n")
        result = _find_all_dockerfiles(tmp_path)
        assert result == [tmp_path / "Dockerfile"]

    def test_containerfile(self, tmp_path):
        (tmp_path / "Containerfile").write_text("FROM ubi9\n")
        result = _find_all_dockerfiles(tmp_path)
        assert result == [tmp_path / "Containerfile"]

    def test_build_dir_found(self, tmp_path):
        bd = tmp_path / "build"
        bd.mkdir()
        (bd / "Dockerfile").write_text("FROM ubi9\n")
        result = _find_all_dockerfiles(tmp_path)
        assert result == [bd / "Dockerfile"]

    def test_root_preferred_over_build(self, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM ubi9\n")
        bd = tmp_path / "build"
        bd.mkdir()
        (bd / "Dockerfile").write_text("FROM ubi9\n")
        result = _find_all_dockerfiles(tmp_path)
        assert result[0] == tmp_path / "Dockerfile"

    def test_no_dockerfile(self, tmp_path):
        assert _find_all_dockerfiles(tmp_path) == []


# ---------------------------------------------------------------------------
# _find_go_entrypoints_heuristic
# ---------------------------------------------------------------------------

class TestFindGoEntrypointsHeuristic:
    def test_single_cmd(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/repo\n")
        cmd = tmp_path / "cmd" / "manager"
        cmd.mkdir(parents=True)
        (cmd / "main.go").write_text("package main\n")
        result = _find_go_entrypoints_heuristic(tmp_path)
        assert len(result) == 1
        assert result[0][0] == "./cmd/manager"

    def test_multiple_cmds_picks_first_alphabetically(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/repo\n")
        for name in ["zebra", "alpha"]:
            d = tmp_path / "cmd" / name
            d.mkdir(parents=True)
            (d / "main.go").write_text("package main\n")
        result = _find_go_entrypoints_heuristic(tmp_path)
        assert len(result) == 2
        assert result[0][0] == "./cmd/alpha"

    def test_no_cmd_dir(self, tmp_path):
        assert _find_go_entrypoints_heuristic(tmp_path) == []

    def test_cmd_without_main_go(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/repo\n")
        cmd = tmp_path / "cmd" / "tool"
        cmd.mkdir(parents=True)
        (cmd / "helper.go").write_text("package main\n")
        assert _find_go_entrypoints_heuristic(tmp_path) == []


# ---------------------------------------------------------------------------
# _iter_json_objects
# ---------------------------------------------------------------------------

class TestIterJsonObjects:
    def test_single_object(self):
        objs = list(_iter_json_objects('{"a": 1}'))
        assert objs == [{"a": 1}]

    def test_multiple_objects(self):
        text = '{"a": 1}\n{"b": 2}\n'
        objs = list(_iter_json_objects(text))
        assert objs == [{"a": 1}, {"b": 2}]

    def test_empty(self):
        assert list(_iter_json_objects("")) == []

    def test_whitespace_only(self):
        assert list(_iter_json_objects("   \n  ")) == []


# ---------------------------------------------------------------------------
# _go_list_deps
# ---------------------------------------------------------------------------

class TestGoListDeps:
    def test_success(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        pkg_dir = repo / "cmd" / "svc"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "main.go").write_text("package main\n")

        go_list_output = json.dumps({
            "Dir": str(pkg_dir),
            "ImportPath": "example.com/repo/cmd/svc",
            "GoFiles": ["main.go"],
        })

        with patch("rules.production_scope.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=go_list_output,
            )
            result = _go_list_deps(repo, "./cmd/svc")

        assert result is not None
        assert (pkg_dir / "main.go").resolve() in result

    def test_includes_cgo_files(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        pkg_dir = repo / "pkg" / "native"
        pkg_dir.mkdir(parents=True)

        go_list_output = json.dumps({
            "Dir": str(pkg_dir),
            "ImportPath": "example.com/repo/pkg/native",
            "GoFiles": ["pure.go"],
            "CgoFiles": ["bridge.go"],
        })

        with patch("rules.production_scope.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=go_list_output)
            result = _go_list_deps(repo, "./cmd/svc")

        assert result is not None
        assert (pkg_dir / "pure.go").resolve() in result
        assert (pkg_dir / "bridge.go").resolve() in result

    def test_excludes_external_packages(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()

        go_list_output = json.dumps({
            "Dir": "/usr/local/go/src/fmt",
            "ImportPath": "fmt",
            "GoFiles": ["format.go"],
        })

        with patch("rules.production_scope.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=go_list_output)
            result = _go_list_deps(repo, "./cmd/svc")

        assert result is None  # no files under repo → None

    def test_go_not_installed(self, tmp_path):
        with patch("rules.production_scope.subprocess.run", side_effect=FileNotFoundError):
            assert _go_list_deps(tmp_path, "./cmd/svc") is None

    def test_nonzero_exit(self, tmp_path):
        with patch("rules.production_scope.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            assert _go_list_deps(tmp_path, "./cmd/svc") is None

    def test_timeout(self, tmp_path):
        with patch("rules.production_scope.subprocess.run", side_effect=subprocess.TimeoutExpired("go", 60)):
            assert _go_list_deps(tmp_path, "./cmd/svc") is None


# ---------------------------------------------------------------------------
# compute_production_scope
# ---------------------------------------------------------------------------

class TestComputeProductionScope:
    def test_full_pipeline(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/repo\n")
        (tmp_path / "Dockerfile").write_text(
            "FROM golang:1.21 AS builder\n"
            "RUN go build -o /mgr ./cmd/manager\n"
            "FROM ubi9\n"
            "COPY --from=builder /mgr /mgr\n"
        )
        pkg_dir = tmp_path / "cmd" / "manager"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "main.go").write_text("package main\n")

        go_list_output = json.dumps({
            "Dir": str(pkg_dir),
            "ImportPath": "example.com/repo/cmd/manager",
            "GoFiles": ["main.go"],
        })

        with patch("rules.production_scope.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=go_list_output)
            scope = compute_production_scope(tmp_path)

        assert scope is not None
        assert scope.method == "go-import-graph"
        assert (pkg_dir / "main.go").resolve() in scope.production_files

    def test_no_dockerfile_uses_heuristic(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/repo\n")
        pkg_dir = tmp_path / "cmd" / "ctrl"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "main.go").write_text("package main\n")

        go_list_output = json.dumps({
            "Dir": str(pkg_dir),
            "ImportPath": "example.com/repo/cmd/ctrl",
            "GoFiles": ["main.go"],
        })

        with patch("rules.production_scope.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=go_list_output)
            scope = compute_production_scope(tmp_path)

        assert scope is not None
        assert (pkg_dir / "main.go").resolve() in scope.production_files

    def test_no_entrypoint_returns_none(self, tmp_path):
        assert compute_production_scope(tmp_path) is None

    def test_go_list_fails_returns_none(self, tmp_path):
        pkg_dir = tmp_path / "cmd" / "svc"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "main.go").write_text("package main\n")

        with patch("rules.production_scope.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="err")
            scope = compute_production_scope(tmp_path)

        assert scope is None


# ---------------------------------------------------------------------------
# is_in_production_scope (from common.py)
# ---------------------------------------------------------------------------

class TestIsInProductionScope:
    def test_none_scope(self):
        assert is_in_production_scope(Path("foo.go"), None) is None

    def test_non_go_file(self):
        scope = ProductionScope(production_files=set(), method="test")
        assert is_in_production_scope(Path("foo.py"), scope) is None

    def test_go_file_in_scope(self, tmp_path):
        f = tmp_path / "main.go"
        f.write_text("")
        scope = ProductionScope(production_files={f.resolve()}, method="test")
        assert is_in_production_scope(f, scope) is True

    def test_go_file_out_of_scope_empty_set(self, tmp_path):
        f = tmp_path / "tool.go"
        f.write_text("")
        scope = ProductionScope(production_files=set(), method="test")
        assert is_in_production_scope(f, scope) is None

    def test_go_file_out_of_scope(self, tmp_path):
        f = tmp_path / "tool.go"
        f.write_text("")
        other = tmp_path / "main.go"
        other.write_text("")
        scope = ProductionScope(production_files={other.resolve()}, method="test")
        assert is_in_production_scope(f, scope) is False


# ---------------------------------------------------------------------------
# is_yaml_in_production_scope
# ---------------------------------------------------------------------------

class TestIsYamlInProductionScope:
    def test_none_scope(self):
        assert is_yaml_in_production_scope(Path("deploy.yaml"), None) is None

    def test_no_manifest_files(self):
        scope = ProductionScope(production_files=set(), method="test")
        assert is_yaml_in_production_scope(Path("deploy.yaml"), scope) is None

    def test_non_yaml_file(self):
        scope = ProductionScope(
            production_files=set(), method="test",
            manifest_files=set(),
        )
        assert is_yaml_in_production_scope(Path("main.go"), scope) is None

    def test_yaml_in_scope(self, tmp_path):
        f = tmp_path / "deploy.yaml"
        f.write_text("")
        scope = ProductionScope(
            production_files=set(), method="test",
            manifest_files={f.resolve()},
        )
        assert is_yaml_in_production_scope(f, scope) is True

    def test_yaml_out_of_scope(self, tmp_path):
        f = tmp_path / "sample.yaml"
        f.write_text("")
        scope = ProductionScope(
            production_files=set(), method="test",
            manifest_files=set(),
        )
        assert is_yaml_in_production_scope(f, scope) is False

    def test_yml_extension(self, tmp_path):
        f = tmp_path / "deploy.yml"
        f.write_text("")
        scope = ProductionScope(
            production_files=set(), method="test",
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
        result = _collect_go_embedded_yamls(tmp_path, {go_file.resolve()})
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
        result = _collect_go_embedded_yamls(tmp_path, {go_file.resolve()})
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
        result = _collect_go_embedded_yamls(tmp_path, {go_file.resolve()})
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
        result = _collect_go_embedded_yamls(tmp_path, {go_file.resolve()})
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
# parse_component_manifest_mapping
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
        result = parse_component_manifest_mapping(str(tmp_path))
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
        result = parse_component_manifest_mapping(str(tmp_path))
        assert result["odh-gitops"] == ["charts/deps/cert"]

    def test_missing_script(self, tmp_path):
        assert parse_component_manifest_mapping(str(tmp_path)) == {}

    def test_merges_multiple_entries_for_same_repo(self, tmp_path):
        script = tmp_path / "get_all_manifests.sh"
        script.write_text(
            '#!/bin/bash\n'
            'declare -A ODH_COMPONENT_MANIFESTS=(\n'
            '    ["nb-ctrl"]="opendatahub-io:kubeflow:main@abc:components/nb/config"\n'
            '    ["odh-ctrl"]="opendatahub-io:kubeflow:main@abc:components/odh/config"\n'
            ')\n'
        )
        result = parse_component_manifest_mapping(str(tmp_path))
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

    def test_combined_go_and_manifest_scope(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/repo\n")
        pkg_dir = tmp_path / "cmd" / "mgr"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "main.go").write_text("package main\n")

        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "kustomization.yaml").write_text("resources:\n- deploy.yaml\n")
        (cfg / "deploy.yaml").write_text("kind: Deployment")

        go_list_output = json.dumps({
            "Dir": str(pkg_dir),
            "ImportPath": "example.com/repo/cmd/mgr",
            "GoFiles": ["main.go"],
        })

        with patch("rules.production_scope.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=go_list_output)
            scope = compute_production_scope(
                tmp_path, manifest_source_folders=["config"],
            )

        assert scope is not None
        assert scope.production_files
        assert scope.manifest_files
        assert scope.method == "go-import-graph"
