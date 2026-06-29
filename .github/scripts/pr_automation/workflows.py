#!/usr/bin/env python3
"""
Workflow Operations for DRS PR Automation

All workflow-related functionality including detection, template rendering,
and workflow management.
"""

from ruamel.yaml import YAML
from typing import Tuple, List
from dataclasses import dataclass
import io
import os
import re

from github import UnknownObjectException, GithubException

from .utils import retry_github_operation, READY_FOR_WORKFLOW, ALREADY_HAS_WORKFLOW, ARCHIVED_REPOSITORY
from .config import AutomationConfig


@dataclass
class UpdateResult:
    """Result of workflow update analysis."""
    needs_update: bool = False
    structure_updated: bool = False
    new_parameters: List[str] = None
    removed_parameters: List[str] = None

    def __post_init__(self):
        if self.new_parameters is None:
            self.new_parameters = []
        if self.removed_parameters is None:
            self.removed_parameters = []


class WorkflowDetector:
    """Handles workflow detection and repository processing decisions."""

    def has_disconnected_workflow(self, repo) -> Tuple[bool, str]:
        """Check if repository already has disconnected readiness workflow or pending PR."""
        try:
            repo.get_contents('.github/workflows/disconnected-readiness.yml')
            return True, "Workflow file exists"
        except UnknownObjectException:
            pass  # File doesn't exist, continue to check for pending PRs

        # Check for specific fixed branch names instead of patterns
        fixed_branches = ['drs-workflow-add', 'drs-workflow-update', 'drs-template-update']

        for branch_name in fixed_branches:
            try:
                # Check if our branch exists
                repo.get_git_ref(f"heads/{branch_name}")
                # If branch exists, check if there's an open PR from it
                def _get_branch_prs():
                    return list(repo.get_pulls(state='open', head=f"{repo.owner.login}:{branch_name}"))

                open_prs = retry_github_operation(_get_branch_prs)
                if open_prs:
                    pr = open_prs[0]
                    return True, f"Open PR already exists: #{pr.number} - {pr.title}"
            except UnknownObjectException:
                continue  # Branch doesn't exist, try next one
            except GithubException as e:
                # API error (permission, rate limit, etc.) - log and continue
                print(f"    Warning: Error checking branch '{branch_name}': {e}")
                continue

        return False, "No workflow found"

    def should_process_repository(self, repo) -> Tuple[bool, str]:
        """Determine if repository should be processed."""
        # Check if already has workflow
        has_workflow, workflow_reason = self.has_disconnected_workflow(repo)
        if has_workflow:
            return False, f"{ALREADY_HAS_WORKFLOW}: {workflow_reason}"

        # Check if archived
        if repo.archived:
            return False, ARCHIVED_REPOSITORY

        return True, READY_FOR_WORKFLOW


class TemplateRenderer:
    """Handles workflow template rendering."""

    def __init__(self, config: AutomationConfig):
        self.config = config

    def render_workflow_template(self) -> str:
        """Render workflow template."""
        template_path = self.config.get_workflow_template_path()

        with open(template_path, 'r') as f:
            return f.read()



class SimpleWorkflowManager:
    """Simple workflow management with addition-only principle."""

    def _should_propagate_update(self, current_uses: str, template_uses: str) -> tuple[bool, str]:
        """
        Central Authority Logic: Decide if update should propagate based on current usage pattern.

        Strategy:
        1. Floating tag users (@v1) only get updates for major version changes (@v1 → @v2)
        2. Pinned users (@v1.2.3) only get updates for major version changes
        3. Minor/patch releases are handled automatically via floating tags
        """
        trigger_reason = os.getenv('TRIGGER_REASON', 'unknown')


        # Extract reference patterns
        current_ref_match = re.search(r'@(v[0-9]+(?:\.[0-9]+\.[0-9]+)?)', current_uses)
        template_ref_match = re.search(r'@(v[0-9]+(?:\.[0-9]+\.[0-9]+)?)', template_uses)

        if not current_ref_match or not template_ref_match:
            return False, "Could not parse version references"

        current_ref = current_ref_match.group(1)  # e.g., "v1" or "v1.2.3"
        template_ref = template_ref_match.group(1)  # e.g., "v2" or "v2.0.0"

        # Determine reference types
        is_current_floating = re.match(r'^v[0-9]+$', current_ref)  # v1, v2, etc.
        is_current_pinned = re.match(r'^v[0-9]+\.[0-9]+\.[0-9]+$', current_ref)  # v1.2.3

        # Parse major versions
        current_major = int(current_ref.split('.')[0][1:])  # Extract major number from v1.2.3 or v1
        template_major = int(template_ref.split('.')[0][1:])  # Extract major number from v2.0.0 or v2

        is_major_version_change = current_major != template_major

        # Decision logic based on trigger and reference patterns
        if trigger_reason == 'major_version_release':
            if is_major_version_change:
                if is_current_floating:
                    return True, f"Major version update for floating tag user: {current_ref} → {template_ref}"
                elif is_current_pinned:
                    return True, f"Major version upgrade offered for pinned user: {current_ref} → {template_ref}"
                else:
                    return False, f"Unknown reference pattern: {current_ref}"
            else:
                return False, f"Major release but same major version ({current_major})"

        elif trigger_reason == 'minor_patch_release':
            return False, f"Minor/patch release - teams using {current_ref} get updates automatically"

        elif trigger_reason == 'template_change':
            # Template file changed - always propagate
            return True, f"Template file updated - propagating to all repositories"

        elif trigger_reason == 'manual':
            # Manual execution - always propagate for review
            return True, f"Manual execution - updating {current_ref} → {template_ref}"

        elif trigger_reason == 'scheduled':
            # Scheduled execution - only major changes
            if is_major_version_change:
                return True, f"Scheduled: Major version update {current_ref} → {template_ref}"
            else:
                return False, f"Scheduled: No major version change needed"

        # Default: allow update but note the reason
        return True, f"Unknown trigger ({trigger_reason}) - allowing update {current_ref} → {template_ref}"


    def update_workflow_safe(self, existing_content: str, template_content: str) -> tuple[str, UpdateResult]:
        """
        Update workflow safely with simple rules:
        1. Always update 'uses' path to mimic what is present in the template
        2. Only ADD missing 'with' parameters, never replace existing
        3. Preserve everything else (name, triggers, job structure)
        """
        try:
            # Create YAML instance that preserves formatting and comments
            yaml = YAML()
            yaml.preserve_quotes = True
            yaml.width = 4096  # Prevent line wrapping

            existing = yaml.load(existing_content)
            template = yaml.load(template_content)

            result = UpdateResult()

            current_uses = existing['jobs']['check'].get('uses', '')
            template_uses = template['jobs']['check']['uses']

            # Central Authority Logic: Determine if propagation should happen
            should_propagate, propagation_reason = self._should_propagate_update(current_uses, template_uses)

            if not should_propagate:
                print(f"    Skipping propagation: {propagation_reason}")
                return existing_content, result

            if current_uses != template_uses:
                existing['jobs']['check']['uses'] = template_uses
                result.structure_updated = True
                result.needs_update = True
                print(f"    Propagating update: {current_uses} → {template_uses}")
                print(f"    Reason: {propagation_reason}")

            # 'with' section: ADD missing parameters, REMOVE deprecated ones, preserve team customizations
            existing_with = existing['jobs']['check'].get('with', {})
            template_with = template['jobs']['check']['with']

            # Dynamic comparison approach: template defines what should exist
            template_params = set(template_with.keys())
            existing_params = set(existing_with.keys())

            # Add missing parameters (in template but not in existing)
            missing_params = template_params - existing_params
            for param_name in missing_params:
                existing_with[param_name] = template_with[param_name]
                result.new_parameters.append(param_name)
                result.needs_update = True

            # Remove deprecated parameters (in existing but not in template)
            deprecated_params = existing_params - template_params
            for param_name in deprecated_params:
                del existing_with[param_name]
                result.removed_parameters.append(param_name)
                result.needs_update = True

            # Update the workflow structure with the modified 'with' section
            existing['jobs']['check']['with'] = existing_with

            # Generate updated content using string buffer to maintain formatting
            output_buffer = io.StringIO()
            yaml.dump(existing, output_buffer)
            updated_content = output_buffer.getvalue()
            return updated_content, result

        except Exception as e:
            raise Exception(f"Failed to update workflow: {e}")

    def generate_enhancement_pr_body(self, result: UpdateResult) -> str:
        """Generate simple PR body explaining what changed."""
        trigger_reason = os.getenv('TRIGGER_REASON', 'unknown')

        # Standard enhancement PR body
        body = "This PR enhances your DRS workflow while preserving all your customizations.\n\n"

        # Add major version upgrade notice for template changes (which happen during major releases)
        if trigger_reason == 'template_change':
            body += "**Major version upgrade** - This update contains breaking changes. Please review before merging.\n\n"

        if result.structure_updated:
            body += "## Structure Updates\n"
            body += "- Updated reusable workflow path to latest version\n\n"

        if result.new_parameters:
            body += "## New Optional Parameters Added\n"
            for param in result.new_parameters:
                body += f"- `{param}`: New optional parameter with default value\n"
            body += "\n**You can customize these values** or remove parameters you don't need.\n\n"

        if result.removed_parameters:
            body += "## Deprecated Parameters Removed\n"
            for param in result.removed_parameters:
                body += f"- `{param}`: No longer supported in current template\n"
            body += "\n"

        body += "**Your existing customizations (rules, etc.) are unchanged.**\n\n"
        body += "**Generated by:** [DRS Automation](https://github.com/opendatahub-io/disconnected-readiness-scorer)"

        return body



