"""Tests for rules/python_imports.py"""

from pathlib import Path

from rules.python_imports import (
    check_requirements_file, check_runtime_pip_installs,
    run, KNOWN_BUNDLED,
)




class TestCheckRequirementsFile:
    def test_git_dep_is_blocker(self, tmp_path):
        f = tmp_path / "requirements.txt"
        f.write_text("git+https://github.com/org/pkg@main\n")
        findings = check_requirements_file(f, tmp_path, KNOWN_BUNDLED)
        assert len(findings) == 1
        assert findings[0].severity == "blocker"
        assert "git+https" in findings[0].message

    def test_known_package_no_finding(self, tmp_path):
        f = tmp_path / "requirements.txt"
        f.write_text("numpy>=1.21\n")
        findings = check_requirements_file(f, tmp_path, KNOWN_BUNDLED)
        assert findings == []

    def test_unknown_package_in_prod_is_info(self, tmp_path):
        f = tmp_path / "requirements.txt"
        f.write_text("my-special-lib==1.0\n")
        findings = check_requirements_file(f, tmp_path, KNOWN_BUNDLED)
        assert len(findings) == 1
        assert findings[0].severity == "info"

    def test_unknown_package_in_test_req_is_info(self, tmp_path):
        f = tmp_path / "test-requirements.txt"
        f.write_text("my-test-lib==1.0\n")
        findings = check_requirements_file(f, tmp_path, KNOWN_BUNDLED)
        assert len(findings) == 1
        assert findings[0].severity == "info"

    def test_unknown_package_in_dev_req_is_info(self, tmp_path):
        f = tmp_path / "dev-requirements.txt"
        f.write_text("my-dev-lib==1.0\n")
        findings = check_requirements_file(f, tmp_path, KNOWN_BUNDLED)
        assert len(findings) == 1
        assert findings[0].severity == "info"

    def test_comment_lines_skipped(self, tmp_path):
        f = tmp_path / "requirements.txt"
        f.write_text("# this is a comment\n")
        findings = check_requirements_file(f, tmp_path, KNOWN_BUNDLED)
        assert findings == []

    def test_empty_lines_skipped(self, tmp_path):
        f = tmp_path / "requirements.txt"
        f.write_text("\n\n\n")
        findings = check_requirements_file(f, tmp_path, KNOWN_BUNDLED)
        assert findings == []

    def test_flag_lines_skipped(self, tmp_path):
        f = tmp_path / "requirements.txt"
        f.write_text("--index-url https://pypi.org/simple/\n")
        findings = check_requirements_file(f, tmp_path, KNOWN_BUNDLED)
        assert findings == []

    def test_editable_git_dep_detected(self, tmp_path):
        f = tmp_path / "requirements.txt"
        f.write_text("-e git+https://github.com/org/pkg.git\n")
        findings = check_requirements_file(f, tmp_path, KNOWN_BUNDLED)
        assert len(findings) == 1
        assert findings[0].severity == "blocker"

    def test_single_char_package_skipped(self, tmp_path):
        f = tmp_path / "requirements.txt"
        f.write_text("x\n")
        findings = check_requirements_file(f, tmp_path, KNOWN_BUNDLED)
        assert findings == []

    def test_package_normalization(self, tmp_path):
        f = tmp_path / "requirements.txt"
        f.write_text("scikit-learn==1.0\n")
        findings = check_requirements_file(f, tmp_path, KNOWN_BUNDLED)
        assert findings == []

    def test_unreadable_file(self, tmp_path):
        f = tmp_path / "requirements.txt"
        f.write_bytes(b'\x80\x81\x82' * 100)
        findings = check_requirements_file(f, tmp_path, KNOWN_BUNDLED)
        assert findings == []


class TestCheckRuntimePipInstalls:
    def test_subprocess_pip_install_is_blocker(self, tmp_path):
        f = tmp_path / "setup.py"
        f.write_text("subprocess.run('pip install pkg', shell=True)")
        findings = check_runtime_pip_installs(f, tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "blocker"

    def test_pip3_install_is_blocker(self, tmp_path):
        f = tmp_path / "install.py"
        f.write_text("os.system('pip3 install torch')")
        findings = check_runtime_pip_installs(f, tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "blocker"

    def test_clean_file(self, tmp_path):
        f = tmp_path / "main.py"
        f.write_text("import numpy\nprint('hello')")
        findings = check_runtime_pip_installs(f, tmp_path)
        assert findings == []

    def test_unreadable_file(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_bytes(b'\x80\x81\x82' * 100)
        findings = check_runtime_pip_installs(f, tmp_path)
        assert findings == []

    def test_pip_install_in_test_dir_is_blocker(self, tmp_path):
        test_dir = tmp_path / "tests"
        test_dir.mkdir()
        f = test_dir / "conftest.py"
        f.write_text("subprocess.run('pip install test-pkg', shell=True)")
        findings = check_runtime_pip_installs(f, tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "blocker"

    def test_pip_install_in_e2e_dir_is_blocker(self, tmp_path):
        e2e = tmp_path / "e2e"
        e2e.mkdir()
        f = e2e / "setup.py"
        f.write_text("os.system('pip3 install torch')")
        findings = check_runtime_pip_installs(f, tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "blocker"


class TestRun:
    def test_empty_repo(self, tmp_path):
        result = run(str(tmp_path))
        assert result.passed is True
        assert result.findings == []
        assert result.rule == "python-imports-bundled"

    def test_git_dep_in_requirements_fails(self, tmp_path):
        f = tmp_path / "requirements.txt"
        f.write_text("git+https://github.com/org/pkg@main\n")
        result = run(str(tmp_path))
        assert result.passed is False

    def test_runtime_pip_in_python_fails(self, tmp_path):
        f = tmp_path / "main.py"
        f.write_text("subprocess.run('pip install numpy', shell=True)")
        result = run(str(tmp_path))
        assert result.passed is False

    def test_git_dep_in_setup_py_fails(self, tmp_path):
        f = tmp_path / "setup.py"
        f.write_text('install_requires=["git+https://github.com/org/pkg"]')
        result = run(str(tmp_path))
        assert result.passed is False
        blockers = [f for f in result.findings if f.severity == "blocker"]
        assert len(blockers) >= 1

    def test_git_dep_in_pyproject_toml_fails(self, tmp_path):
        f = tmp_path / "pyproject.toml"
        f.write_text('dependencies = ["git+https://github.com/org/pkg"]')
        result = run(str(tmp_path))
        assert result.passed is False

    def test_skips_vendor_dir(self, tmp_path):
        vendor = tmp_path / "vendor"
        vendor.mkdir()
        f = vendor / "requirements.txt"
        f.write_text("git+https://github.com/org/pkg@main\n")
        result = run(str(tmp_path))
        assert result.passed is True

    def test_skips_venv_dir(self, tmp_path):
        venv = tmp_path / ".venv"
        venv.mkdir()
        f = venv / "lib.py"
        f.write_text('subprocess.run(["pip", "install", "pkg"])')
        result = run(str(tmp_path))
        assert result.passed is True


    def test_nested_requirements_found(self, tmp_path):
        sub = tmp_path / "subdir"
        sub.mkdir()
        f = sub / "requirements.txt"
        f.write_text("git+https://github.com/org/pkg@main\n")
        result = run(str(tmp_path))
        assert result.passed is False
