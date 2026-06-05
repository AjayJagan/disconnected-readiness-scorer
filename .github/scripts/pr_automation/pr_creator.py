#!/usr/bin/env python3
"""
PR Creation for DRS PR Automation

Handles PR creation for disconnected readiness workflows including
new workflow creation and enhancement of existing workflows.
"""

from dataclasses import dataclass

from .utils import retry_github_operation
from .config import AutomationConfig
from .rule_assignment import get_rules_for_repo
from .workflows import TemplateRenderer, SimpleWorkflowManager, UpdateResult
from .github_client import GitHubClient


@dataclass
class PRCreationResult:
    """Result of PR creation operation"""
    success: bool
    action: str  # 'created', 'skipped', 'simulated', 'error'
    reason: str
    pr_url: str = ''
    pr_number: int = 0
    rules: str = ''


class PRCreator:
    """Handles PR creation for disconnected readiness workflows."""

    def __init__(self, config: AutomationConfig, template_renderer: TemplateRenderer, github_client: GitHubClient):
        self.config = config
        self.template_renderer = template_renderer
        self.github_client = github_client

        # Initialize simple workflow management
        self.workflow_manager = SimpleWorkflowManager()

    def _ensure_clean_branch(self, repo, branch_name: str, base_branch: str) -> bool:
        """
        Ensure clean branch: delete if exists, then create fresh.
        Simple idempotent operation.

        Returns True on success, False on failure.
        """
        try:
            print(f"    [ensure_clean_branch] Starting for '{branch_name}' (base: {base_branch})")

            # Get base SHA for the new branch
            source = repo.get_branch(base_branch)
            base_sha = source.commit.sha
            print(f"    [ensure_clean_branch] Base SHA: {base_sha[:12]}")

            # Explicitly close any open PRs from this branch before deletion
            # to avoid a race where GitHub hasn't processed the auto-closure
            # by the time we create a new PR from the same branch name.
            try:
                open_prs = list(repo.get_pulls(state='open', head=f"{repo.owner.login}:{branch_name}"))
                if open_prs:
                    for pr in open_prs:
                        pr.edit(state='closed')
                        print(f"    [ensure_clean_branch] Closed existing PR #{pr.number}")
                else:
                    print(f"    [ensure_clean_branch] No open PRs found for branch '{branch_name}'")
            except Exception as e:
                print(f"    [ensure_clean_branch] PR check skipped: {e}")

            # Check if branch exists and delete it
            try:
                ref = repo.get_git_ref(f"heads/{branch_name}")
                ref.delete()
                print(f"    [ensure_clean_branch] Deleted existing branch '{branch_name}'")
            except Exception as e:
                print(f"    [ensure_clean_branch] Branch '{branch_name}' not found or already deleted: {e}")

            # Create fresh branch
            repo.create_git_ref(f"refs/heads/{branch_name}", base_sha)
            print(f"    [ensure_clean_branch] Created fresh branch '{branch_name}' from {base_branch}")
            return True

        except Exception as e:
            print(f"    [ensure_clean_branch] FAILED for '{branch_name}': {e}")
            return False

    def create_disconnected_readiness_pr(self, repo, branch_name_suffix: str = "", dry_run: bool = False, trigger_reason: str = "manual") -> PRCreationResult:
        """Create or update a PR in a repository for disconnected readiness workflow."""

        try:
            # Check if workflow already exists (with retry)
            existing_file = None
            existing_content = None

            try:
                def _check_workflow_exists():
                    return repo.get_contents('.github/workflows/disconnected-readiness.yml')

                existing_file = retry_github_operation(_check_workflow_exists)
                existing_content = existing_file.decoded_content.decode('utf-8')
            except:
                pass  # File doesn't exist, we'll create a new one

            # Get repository metadata for rule determination
            metadata = self.github_client.get_repo_metadata(repo)

            # Determine rules using simplified logic
            rules = get_rules_for_repo(
                metadata['name'],
                metadata['language']
            )

            # No more exception path configuration - teams manage their own exceptions

            if existing_file and existing_content:
                # Workflow exists - check if it needs updates
                return self._handle_existing_workflow(
                    repo, existing_file, existing_content, rules,
                    branch_name_suffix, dry_run, trigger_reason
                )
            else:
                # No existing workflow - create new one
                return self._create_new_workflow(
                    repo, rules, branch_name_suffix, dry_run
                )

        except Exception as e:
            return PRCreationResult(
                success=False,
                action='error',
                reason=str(e)
            )

    def _handle_existing_workflow(self, repo, existing_file, existing_content: str, rules: str,
                                  branch_name_suffix: str, dry_run: bool, trigger_reason: str = "manual") -> PRCreationResult:
        """Handle updates to existing workflow while preserving team customizations."""

        # Generate latest template for comparison
        template_content = self.template_renderer.render_workflow_template(rules)

        # Use simple approach: only touch 'with' section, preserve everything else
        try:
            updated_content, result = self.workflow_manager.update_workflow_safe(existing_content, template_content)
        except Exception as e:
            return PRCreationResult(
                success=False,
                action='error',
                reason=f'Failed to analyze workflow updates: {e}'
            )

        if not result.needs_update and trigger_reason != 'template_change':
            return PRCreationResult(
                success=True,
                action='skipped',
                reason='Workflow already up to date',
                rules=rules
            )

        # For template changes, create enhancement PR even if no technical updates needed
        if not result.needs_update and trigger_reason == 'template_change':
            return PRCreationResult(
                success=True,
                action='skipped',
                reason='Template change: workflow already uses latest template structure',
                rules=rules
            )

        if dry_run:
            update_details = []
            if result.structure_updated:
                update_details.append("structure update")
            if result.new_parameters:
                update_details.append(f"{len(result.new_parameters)} new parameters: {', '.join(result.new_parameters)}")
            if result.removed_parameters:
                update_details.append(f"{len(result.removed_parameters)} deprecated parameters removed: {', '.join(result.removed_parameters)}")

            return PRCreationResult(
                success=True,
                action='simulated',
                reason=f'Would enhance workflow: {", ".join(update_details)}',
                rules=rules
            )

        # Create enhancement PR
        return self._create_enhancement_pr(
            repo, existing_file, updated_content, result,
            rules, branch_name_suffix
        )

    def _create_new_workflow(self, repo, rules: str, branch_name_suffix: str, dry_run: bool) -> PRCreationResult:
        """Create a new workflow from template."""

        # Generate workflow content from template
        workflow_content = self.template_renderer.render_workflow_template(rules)

        if dry_run:
            return PRCreationResult(
                success=True,
                action='simulated',
                reason=f'Would create new workflow with rules: {rules}',
                rules=rules
            )

        # Create branch and PR
        return self._create_workflow_pr(
            repo, workflow_content, rules,
            "Add disconnected readiness workflow",
            self._generate_new_workflow_pr_body(rules),
            branch_name_suffix
        )

    def _create_enhancement_pr(self, repo, existing_file, enhanced_content: str, result: UpdateResult,
                               rules: str, branch_name_suffix: str) -> PRCreationResult:
        """Create PR for workflow enhancements."""

        pr_title = "Update DRS workflow with new changes"
        pr_body = self.workflow_manager.generate_enhancement_pr_body(result)

        return self._update_workflow_pr(
            repo, existing_file, enhanced_content, pr_title, pr_body,
            rules, branch_name_suffix
        )

    def _create_workflow_pr(self, repo, workflow_content: str, rules: str,
                            pr_title: str, pr_body: str, branch_name_suffix: str) -> PRCreationResult:
        """Create a new workflow file and PR."""

        try:
            default_branch = repo.default_branch

            # Use fixed branch name
            branch_name = 'drs-workflow-add'
            if branch_name_suffix:
                branch_name += f'-{branch_name_suffix}'

            # Ensure clean branch (delete/recreate)
            if not self._ensure_clean_branch(repo, branch_name, default_branch):
                return PRCreationResult(
                    success=False,
                    action='error',
                    reason=f'Failed to create clean branch: {branch_name}'
                )

            # Create file (with retry)
            commit_message = "Add disconnected readiness workflow"

            def _create_workflow_file():
                repo.create_file(
                    ".github/workflows/disconnected-readiness.yml",
                    commit_message,
                    workflow_content,
                    branch=branch_name
                )

            retry_github_operation(_create_workflow_file)

            # Create PR (with retry)
            def _create_pull_request():
                return repo.create_pull(
                    title=pr_title,
                    body=pr_body,
                    head=branch_name,
                    base=default_branch
                )

            pr = retry_github_operation(_create_pull_request)

            return PRCreationResult(
                success=True,
                action='created',
                reason='PR created successfully',
                pr_url=pr.html_url,
                pr_number=pr.number,
                rules=rules
            )

        except Exception as e:
            return PRCreationResult(
                success=False,
                action='error',
                reason=f'Failed to create PR: {e}'
            )

    def _update_workflow_pr(self, repo, existing_file, updated_content: str, pr_title: str, pr_body: str,
                            rules: str, branch_name_suffix: str) -> PRCreationResult:
        """Update existing workflow file and create PR."""

        try:
            default_branch = repo.default_branch

            # Use fixed branch name for updates
            branch_name = 'drs-workflow-update'
            if branch_name_suffix:
                branch_name += f'-{branch_name_suffix}'

            # Ensure clean branch (delete/recreate)
            if not self._ensure_clean_branch(repo, branch_name, default_branch):
                return PRCreationResult(
                    success=False,
                    action='error',
                    reason=f'Failed to create clean branch: {branch_name}'
                )

            # Update file (with retry)
            commit_message = "Update disconnected readiness workflow (preserves customizations)"

            def _update_workflow_file():
                repo.update_file(
                    ".github/workflows/disconnected-readiness.yml",
                    commit_message,
                    updated_content,
                    existing_file.sha,
                    branch=branch_name
                )

            retry_github_operation(_update_workflow_file)

            # Create PR (with retry)
            def _create_pull_request():
                return repo.create_pull(
                    title=pr_title,
                    body=pr_body,
                    head=branch_name,
                    base=default_branch
                )

            pr = retry_github_operation(_create_pull_request)

            return PRCreationResult(
                success=True,
                action='updated',
                reason='Enhancement PR created successfully',
                pr_url=pr.html_url,
                pr_number=pr.number,
                rules=rules
            )

        except Exception as e:
            return PRCreationResult(
                success=False,
                action='error',
                reason=f'Failed to create enhancement PR: {e}'
            )

    def _generate_new_workflow_pr_body(self, rules: str) -> str:
        """Generate PR body for new workflow creation."""
        pr_body = f"""This PR adds a disconnected readiness check workflow to ensure this repository is compatible with air-gapped OpenShift deployments.

**Rules applied:** `{rules}`

**What this does:**
- Runs on every pull request
- Checks for disconnected readiness issues
- Reports findings as PR comments if issues are found

**You can customize the rules** by editing the `rules` parameter in the workflow file after this PR is merged.

**Generated automatically by:** [disconnected-readiness-scorer](https://github.com/opendatahub-io/disconnected-readiness-scorer)
"""

        return pr_body



