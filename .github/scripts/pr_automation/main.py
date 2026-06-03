#!/usr/bin/env python3
"""
DRS PR Automation Main Orchestrator

Main entry point and orchestration logic for DRS PR automation.
"""

import os
import sys
from datetime import datetime

from .utils import RATE_LIMIT_CHECK_INTERVAL, OP_CHECK, OP_MARK_CHECKED, OP_CHECK_CHANGED, OP_UPDATE_STATE
from .config import AutomationConfig
from .github_client import GitHubClient
from .state import StateManager
from .workflows import WorkflowDetector, TemplateRenderer
from .repositories import RepositoryClassifier
from .utils import READY_FOR_WORKFLOW, ALREADY_HAS_WORKFLOW, EXCLUDED_REPOSITORY
from .pr_creator import PRCreator


class DRSAutomation:
    """Main orchestrator for DRS PR automation."""

    def __init__(self):
        self.config = AutomationConfig()
        self.github_client = GitHubClient()
        self.state_manager = StateManager(self.config)
        self.workflow_detector = WorkflowDetector()
        self.template_renderer = TemplateRenderer(self.config)
        self.pr_creator = PRCreator(
            self.config, self.template_renderer, self.github_client
        )

    def _parse_organizations_input(self):
        """Parse organization list from INPUT_ORGANIZATIONS environment variable."""
        organizations_input = os.getenv('INPUT_ORGANIZATIONS', '')

        organizations = [org.strip() for org in organizations_input.split(',') if org.strip()]

        if not organizations:
            print("ERROR: No organizations specified")
            sys.exit(1)

        return organizations

    def run(self):
        """Main entry point for DRS PR automation."""
        # Parse inputs from environment (set by GitHub Actions)
        organizations = self._parse_organizations_input()
        dry_run = os.getenv('INPUT_DRY_RUN', 'true').lower() == 'true'
        force_full_scan = os.getenv('INPUT_FORCE_FULL_SCAN', 'false').lower() == 'true'
        trigger_reason = os.getenv('TRIGGER_REASON', 'manual')

        print("DRS PR AUTOMATION")
        print(f"Organizations: {organizations}")
        print(f"Trigger reason: {trigger_reason}")
        print(f"Dry run: {dry_run}")
        if force_full_scan:
            print("FORCE FULL SCAN: Ignoring state optimization")
        print("=" * 60)

        # Load exclusions configuration
        exclusions = self.config.load_exclusions()

        # Handle template change detection
        if trigger_reason == 'template_change':
            should_process_on_template, template_reason = self.state_manager.manage_template(OP_CHECK_CHANGED)

            if not should_process_on_template:
                print(f"Template change processing not needed: {template_reason}")
                sys.exit(0)

            print(f"Template change detected: {template_reason}")
            print("   → Processing ALL configured organizations")
            force_full_scan = True

        # Show current state summary
        print("Current State:")
        print(self.state_manager.get_summary_report())
        print()

        # Check rate limits before starting
        rate_limit = self.github_client.check_rate_limit()
        print(f"API Status: {rate_limit.status_message}")

        if not rate_limit.is_safe:
            print("WARNING: Low API rate limit. Consider running later or with fewer organizations.")
            if not dry_run:
                print("Continuing anyway, but processing may be throttled...")

        # Initialize classifier and counters
        classifier = RepositoryClassifier(exclusions, self.workflow_detector)
        total_orgs_processed = 0
        total_repos = 0
        success_count = 0
        already_has_workflow = 0
        skip_count = 0
        excluded_count = 0
        orgs_skipped_no_changes = 0
        failed_orgs = []

        for org_name in organizations:
            org_name = org_name.strip()
            if not org_name:
                continue

            try:
                # Get account object (organization or user)
                account, account_type, current_repo_count = self.github_client.get_account(org_name)

                print(f"\n Processing {account_type}: {org_name}")

                # Check if we need to process this organization (state-based optimization)
                if force_full_scan:
                    should_process = True
                    reason = "Force full scan requested" if trigger_reason != 'template_change' else "Template change propagation"
                else:
                    should_process, reason = self.state_manager.manage_organization(org_name, current_repo_count, OP_CHECK)

                if not should_process:
                    print(f"  Skipping {org_name}: {reason}")
                    self.state_manager.manage_organization(org_name, current_repo_count, OP_MARK_CHECKED)
                    orgs_skipped_no_changes += 1
                    continue

                print(f" {reason} - performing full repository scan...")
                total_orgs_processed += 1

                # Get all repositories and classify them (with retry)
                from .utils import retry_github_operation

                def _get_repositories():
                    return list(account.get_repos())

                repositories = retry_github_operation(_get_repositories)
                total_repos += len(repositories)

                # Classify all repositories at once (with exclusions)
                grouped_classifications = classifier.classify_repositories(repositories, trigger_reason)

                # Print analysis summary
                print(classifier.generate_summary_report(grouped_classifications))

                # Process repositories that are ready for workflows
                ready_repos = grouped_classifications.get(READY_FOR_WORKFLOW, [])
                existing_repos = grouped_classifications.get(ALREADY_HAS_WORKFLOW, [])
                excluded_repos = grouped_classifications.get(EXCLUDED_REPOSITORY, [])

                already_has_workflow += len(existing_repos)
                excluded_count += len(excluded_repos)

                if excluded_repos:
                    print(f" {len(excluded_repos)} repositories excluded by configuration")

                if ready_repos:
                    print(f"\n Processing {len(ready_repos)} repositories that need workflows...")

                    for classification in ready_repos:
                        try:
                            def _get_repo():
                                return self.github_client.client.get_repo(classification.repo_name)

                            repo = retry_github_operation(_get_repo)
                            print(f"  Processing {repo.full_name}...")

                            # Use enhanced PR creation function
                            result = self.pr_creator.create_disconnected_readiness_pr(
                                repo,
                                branch_name_suffix=f"-{trigger_reason}" if trigger_reason != 'manual' else "",
                                dry_run=dry_run,
                                trigger_reason=trigger_reason
                            )

                            if result.action == 'skipped':
                                print(f"      Skipped: {result.reason}")
                                skip_count += 1
                            elif result.action in ['created', 'simulated']:
                                if result.action == 'created':
                                    print(f"     Created PR: {result.pr_url}")
                                    print(f"     Rules: {result.rules}")
                                    if result.central_exceptions_found:
                                        print(f"     Central exceptions found: {result.central_exceptions_found}")
                                else:
                                    print(f"     {result.reason}")
                                success_count += 1
                            elif result.action == 'error':
                                print(f"     Failed: {result.reason}")

                        except Exception as e:
                            print(f"     ERROR processing {classification.repo_name}: {str(e)[:100]}")

                        # Check rate limits periodically
                        if success_count % RATE_LIMIT_CHECK_INTERVAL == 0:
                            rate_limit = self.github_client.check_rate_limit()
                            if not rate_limit.is_safe:
                                print(f"      Rate limit warning: {rate_limit.status_message}")

                else:
                    print(" No repositories need processing in this organization")

                # Mark organization as processed
                self.state_manager.manage_organization(org_name, current_repo_count, OP_MARK_CHECKED)

            except Exception as e:
                error_msg = f"Failed to process organization {org_name}: {str(e)}"
                print(f" ERROR: {error_msg}")
                failed_orgs.append({
                    'org_name': org_name,
                    'error': str(e),
                    'timestamp': datetime.now().isoformat()
                })
                continue

        # Update template state if this was a template change run
        if trigger_reason == 'template_change' and not dry_run:
            success, message = self.state_manager.manage_template(OP_UPDATE_STATE)
            if success:
                print(f"\n {message}")
            else:
                print(f"\n Warning: Failed to update template state: {message}")

        # Final summary
        print("\n" + "=" * 70)
        print("DRS PR AUTOMATION SUMMARY")
        print("=" * 70)
        print(f" Trigger: {trigger_reason}")
        print(f" Organizations processed: {total_orgs_processed}/{len(organizations)}")
        print(f"  Organizations skipped (no changes): {orgs_skipped_no_changes}")
        print(f" Total repositories analyzed: {total_repos}")
        print(f" PRs created/simulated: {success_count}")
        print(f" Already had workflows: {already_has_workflow}")
        print(f" Excluded repositories: {excluded_count}")
        print(f"  Other skipped: {skip_count}")
        print(f" Success rate: {((success_count + already_has_workflow) / total_repos * 100):.1f}%" if total_repos > 0 else "Success rate: 0%")
        print(f" Efficiency: {orgs_skipped_no_changes} orgs skipped = ~{orgs_skipped_no_changes * 50} API calls saved")
        print(f" Mode: {'DRY RUN - No changes made' if dry_run else 'LIVE DEPLOYMENT'}")

        # Report failed organizations if any
        if failed_orgs:
            print(f" FAILED ORGANIZATIONS: {len(failed_orgs)}")
            for failure in failed_orgs:
                print(f"   {failure['org_name']}: {failure['error'][:100]}")

        # Final rate limit check
        final_rate_limit = self.github_client.check_rate_limit()
        print(f" Final API status: {final_rate_limit.status_message}")

        # Show updated state
        print(f"\n Updated State:")
        print(self.state_manager.get_summary_report())


def main():
    """Entry point for the automation script."""
    automation = DRSAutomation()
    automation.run()


if __name__ == '__main__':
    main()
