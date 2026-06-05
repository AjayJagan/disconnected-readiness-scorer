#!/usr/bin/env python3
"""
Configuration Management for DRS PR Automation

Handles all configuration loading and path resolution.
"""

import os
from pathlib import Path
from typing import Set
from ruamel.yaml import YAML


class AutomationConfig:
    """Handles all configuration loading and path resolution."""

    def __init__(self):
        self.repo_root = self._find_repo_root()

    def _find_repo_root(self) -> Path:
        """Find repository root using GitHub Actions workspace or current directory."""
        return Path(os.environ.get("GITHUB_WORKSPACE", Path.cwd()))

    def load_exclusions(self) -> Set[str]:
        """Load repository exclusion configuration."""
        try:
            config_file = Path(f"{self.repo_root}/.github/config/repository-exclusions.yaml")

            if config_file.exists():
                yaml = YAML(typ='safe', pure=True)  # Equivalent to yaml.safe_load
                with open(config_file, 'r') as f:
                    config = yaml.load(f) or {}
                return set(config.get('excluded_repositories') or [])
            else:
                print("Warning: No exclusion configuration found, proceeding without exclusions")
                return set()

        except Exception as e:
            print(f"Warning: Could not load exclusion configuration: {e}")
            return set()


    def get_workflow_template_path(self) -> Path:
        """Get path to workflow template."""
        return Path(f"{self.repo_root}/.github/templates/workflow.yml")


