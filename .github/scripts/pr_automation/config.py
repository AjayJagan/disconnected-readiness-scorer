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

    def load_inclusions(self) -> Set[str]:
        """Load repository inclusion configuration for phased rollout."""
        try:
            config_file = Path(f"{self.repo_root}/.github/config/repositories.yaml")

            if config_file.exists():
                yaml = YAML(typ='safe', pure=True)
                with open(config_file, 'r') as f:
                    config = yaml.load(f) or {}
                return set(config.get('included_repositories') or [])
            else:
                print("Warning: No repository configuration found, no repos will be processed")
                return set()

        except (OSError, IOError, ValueError) as e:
            print(f"Warning: Could not load repository configuration: {e}")
            return set()


    def get_workflow_template_path(self) -> Path:
        """Get path to workflow template."""
        return Path(f"{self.repo_root}/.github/templates/workflow.yml")

    def get_config_template_path(self) -> Path:
        """Get path to config template."""
        return Path(f"{self.repo_root}/.github/templates/config.yaml")


