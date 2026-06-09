#!/usr/bin/env python3
"""
PR Creation for DRS PR Automation

Handles PR creation for disconnected readiness workflows including
new workflow creation and enhancement of existing workflows.
"""

from dataclasses import dataclass

from github import UnknownObjectException

from .utils import retry_github_operation, FILE_OP_CREATE, FILE_OP_UPDATE, file_exists_in_repo
from .config import AutomationConfig
from .workflows import TemplateRenderer, SimpleWorkflowManager, UpdateResult
from .github_client import GitHubClient


@dataclass
class FileOperation:
    """Represents a single file operation in a multi-file PR"""
    file_path: str
    content: str
    operation_type: str  # FILE_OP_CREATE, FILE_OP_UPDATE
    commit_message: str
    existing_sha: str = None  # Required for updates


@dataclass
class PRCreationResult:
    """Result of PR creation operation"""
    success: bool
    action: str  # 'created', 'skipped', 'simulated', 'error'
    reason: str
    pr_url: str = ''
    pr_number: int = 0


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
            # Get base SHA for the new branch
            source = repo.get_branch(base_branch)
            base_sha = source.commit.sha

            # Explicitly close any open PRs from this branch before deletion
            # to avoid a race where GitHub hasn't processed the auto-closure
            # by the time we create a new PR from the same branch name.
            try:
                open_prs = list(repo.get_pulls(state='open', head=f"{repo.owner.login}:{branch_name}"))
                for pr in open_prs:
                    pr.edit(state='closed')
                    print(f"    Closed existing PR #{pr.number} from branch '{branch_name}'")
            except Exception as e:
                print(f"    Warning: could not close existing PRs: {e}")

            # Check if branch exists and delete it
            try:
                ref = repo.get_git_ref(f"heads/{branch_name}")
                ref.delete()
                print(f"    Deleted existing branch '{branch_name}'")
            except Exception as e:
                print(f"    Warning: could not delete branch (may not exist): {e}")

            # Create fresh branch
            repo.create_git_ref(f"refs/heads/{branch_name}", base_sha)
            print(f"    Created fresh branch '{branch_name}' from {base_branch}")
            return True

        except Exception as e:
            print(f"    Failed to ensure clean branch '{branch_name}': {e}")
            return False



    def _execute_file_operations(self, repo, file_operations: list, branch_name: str):
        """Execute multiple file operations with retry logic. Each operation uses its own commit message."""
        for operation in file_operations:
            if operation.operation_type == FILE_OP_CREATE:
                def _create_file():
                    repo.create_file(
                        operation.file_path,
                        operation.commit_message,
                        operation.content,
                        branch=branch_name
                    )
                retry_github_operation(_create_file)

            elif operation.operation_type == FILE_OP_UPDATE:
                def _update_file():
                    repo.update_file(
                        operation.file_path,
                        operation.commit_message,
                        operation.content,
                        operation.existing_sha,
                        branch=branch_name
                    )
                retry_github_operation(_update_file)

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
            except UnknownObjectException:
                pass  # File doesn't exist, we'll create a new one

            # Check if config file needs to be created
            config_needed = not file_exists_in_repo(repo, '.disconnected-readiness/config.yaml')

            if existing_file and existing_content:
                # Workflow exists - check if it needs updates
                return self._handle_existing_workflow(
                    repo, existing_file, existing_content,
                    branch_name_suffix, dry_run, trigger_reason, config_needed
                )
            else:
                # No existing workflow - create new one
                return self._create_new_workflow(
                    repo, branch_name_suffix, dry_run, config_needed
                )

        except Exception as e:
            return PRCreationResult(
                success=False,
                action='error',
                reason=str(e)
            )

    def _handle_existing_workflow(self, repo, existing_file, existing_content: str,
                                  branch_name_suffix: str, dry_run: bool, trigger_reason: str = "manual", config_needed: bool = False) -> PRCreationResult:
        """Handle updates to existing workflow while preserving team customizations."""

        # Generate latest template for comparison
        template_content = self.template_renderer.render_workflow_template()

        # Use simple approach: only touch 'with' section, preserve everything else
        try:
            updated_content, update_workflow_result = self.workflow_manager.update_workflow_safe(existing_content, template_content)
        except Exception as e:
            return PRCreationResult(
                success=False,
                action='error',
                reason=f'Failed to analyze workflow updates: {e}'
            )

        if not update_workflow_result.needs_update and not config_needed and trigger_reason != 'template_change':
            return PRCreationResult(
                success=True,
                action='skipped',
                reason='Workflow already up to date',
            )

        # For template changes, create enhancement PR even if no technical updates needed
        if not update_workflow_result.needs_update and not config_needed and trigger_reason == 'template_change':
            return PRCreationResult(
                success=True,
                action='skipped',
                reason='Template change: workflow already uses latest template structure',
            )

        if dry_run:
            update_details = []
            if update_workflow_result.structure_updated:
                update_details.append("structure update")
            if update_workflow_result.new_parameters:
                update_details.append(f"{len(update_workflow_result.new_parameters)} new parameters: {', '.join(update_workflow_result.new_parameters)}")
            if update_workflow_result.removed_parameters:
                update_details.append(f"{len(update_workflow_result.removed_parameters)} deprecated parameters removed: {', '.join(update_workflow_result.removed_parameters)}")
            if config_needed:
                update_details.append("add empty config file")

            return PRCreationResult(
                success=True,
                action='simulated',
                reason=f'Would enhance workflow: {", ".join(update_details)}',
            )

        # Create enhancement PR
        return self._create_enhancement_pr(
            repo, existing_file, updated_content, update_workflow_result,
            branch_name_suffix, config_needed
        )

    def _create_new_workflow(self, repo, branch_name_suffix: str, dry_run: bool, config_needed: bool = False) -> PRCreationResult:
        """Create a new workflow from template."""

        # Generate workflow content from template
        workflow_content = self.template_renderer.render_workflow_template()

        if dry_run:
            reason = 'Would create new workflow with all default rules'
            if config_needed:
                reason += ' and empty config file'
            return PRCreationResult(
                success=True,
                action='simulated',
                reason=reason,
            )

        # Create branch and PR
        return self._create_workflow_pr(
            repo, workflow_content,
            "Add disconnected readiness workflow",
            self._generate_new_workflow_pr_body(config_needed),
            branch_name_suffix, config_needed
        )

    def _create_enhancement_pr(self, repo, existing_file, enhanced_content: str, update_workflow_result: UpdateResult,
                               branch_name_suffix: str, config_needed: bool = False) -> PRCreationResult:
        """Create PR for workflow enhancements."""

        pr_title = "Update DRS workflow with new changes"
        pr_body = self._generate_enhanced_pr_body(update_workflow_result, config_needed)

        return self._update_workflow_pr(
            repo, existing_file, enhanced_content, pr_title, pr_body,
            branch_name_suffix, config_needed
        )

    def _create_workflow_pr(self, repo, workflow_content: str,
                            pr_title: str, pr_body: str, branch_name_suffix: str, config_needed: bool = False) -> PRCreationResult:
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

            # Prepare file operations with specific commit messages
            file_operations = [
                FileOperation(
                    file_path=".github/workflows/disconnected-readiness.yml",
                    content=workflow_content,
                    operation_type=FILE_OP_CREATE,
                    commit_message="Add disconnected readiness workflow"
                )
            ]

            if config_needed:
                file_operations.append(FileOperation(
                    file_path=".disconnected-readiness/config.yaml",
                    content=self._generate_default_config_content(),
                    operation_type=FILE_OP_CREATE,
                    commit_message="Add empty config for disconnected readiness customization"
                ))

            self._execute_file_operations(repo, file_operations, branch_name)

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
            )

        except Exception as e:
            return PRCreationResult(
                success=False,
                action='error',
                reason=f'Failed to create PR: {e}'
            )

    def _update_workflow_pr(self, repo, existing_file, updated_content: str, pr_title: str, pr_body: str,
                            branch_name_suffix: str, config_needed: bool = False) -> PRCreationResult:
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

            # Prepare file operations with specific commit messages
            file_operations = [
                FileOperation(
                    file_path=".github/workflows/disconnected-readiness.yml",
                    content=updated_content,
                    operation_type=FILE_OP_UPDATE,
                    commit_message="Update disconnected readiness workflow (preserves customizations)",
                    existing_sha=existing_file.sha
                )
            ]

            if config_needed:
                file_operations.append(FileOperation(
                    file_path=".disconnected-readiness/config.yaml",
                    content=self._generate_default_config_content(),
                    operation_type=FILE_OP_CREATE,
                    commit_message="Add empty config for disconnected readiness customization"
                ))

            self._execute_file_operations(repo, file_operations, branch_name)

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
            )

        except Exception as e:
            return PRCreationResult(
                success=False,
                action='error',
                reason=f'Failed to create enhancement PR: {e}'
            )

    def _generate_default_config_content(self) -> str:
        """Generate empty config file from template."""
        return self.template_renderer.render_config_template()

    def _generate_new_workflow_pr_body(self, config_added: bool = False) -> str:
        """Generate PR body for new workflow creation."""
        config_section = ""
        if config_added:
            config_section = """

**Config file added:** An empty `.disconnected-readiness/config.yaml` file has been added for customization. See the [schema reference](https://github.com/opendatahub-io/disconnected-readiness-scorer/blob/main/schemas/config.schema.json) for available options."""

        return f"""This PR adds a disconnected readiness check workflow to ensure this repository is compatible with air-gapped OpenShift deployments.

**Rules applied:** all default rules (empty = all)

**What this does:**
- Runs on every pull request
- Checks for disconnected readiness issues
- Reports findings as PR comments if issues are found

**You can customize the rules** by editing the `rules` parameter in the workflow file after this PR is merged.{config_section}

**Generated automatically by:** [disconnected-readiness-scorer](https://github.com/opendatahub-io/disconnected-readiness-scorer)
"""

    def _generate_enhanced_pr_body(self, update_workflow_result: UpdateResult, config_needed: bool) -> str:
        """Generate complete enhancement PR body including workflow updates and optional config file note."""
        pr_body = self.workflow_manager.generate_enhancement_pr_body(update_workflow_result)

        # Add config file note if needed
        if config_needed:
            pr_body += "\n\n**Config file added:** An empty `.disconnected-readiness/config.yaml` file has been added for customization. See the [schema reference](https://github.com/opendatahub-io/disconnected-readiness-scorer/blob/main/schemas/config.schema.json) for available options."

        return pr_body
