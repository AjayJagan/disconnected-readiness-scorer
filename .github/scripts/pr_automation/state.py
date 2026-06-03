#!/usr/bin/env python3
"""
State Management for DRS PR Automation

Manages persistent state for repository automation, including organization
tracking and template change detection.
"""

import hashlib
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Tuple
from ruamel.yaml import YAML

from .config import AutomationConfig
from .utils import (
    OP_CHECK, OP_MARK_CHECKED,
    OP_CHECK_CHANGED, OP_UPDATE_STATE,
    MSG_TEMPLATE_UNCHANGED, MSG_TEMPLATE_NOT_FOUND, MSG_INITIAL_TEMPLATE_SETUP
)


class StateManager:
    """Manages persistent state for repository automation."""

    def __init__(self, config: AutomationConfig):
        self.config = config
        self.state_file = Path(f"{config.repo_root}/.github/config/automation-state.yml")

    def load_state(self) -> Dict[str, Any]:
        """Load automation state from YAML file."""
        try:
            yaml = YAML(typ='safe', pure=True)  # Equivalent to yaml.safe_load
            with open(self.state_file, 'r') as f:
                return yaml.load(f) or {'organizations': {}, 'template_state': {}}
        except Exception as e:
            print(f"Warning: Could not load state file {self.state_file}: {e}")
            return {'organizations': {}, 'template_state': {}}

    def save_state(self, state: Dict[str, Any]) -> None:
        """Save automation state to YAML file."""
        try:
            state['last_updated'] = datetime.now().isoformat()
            yaml = YAML()
            yaml.default_flow_style = False
            yaml.sort_keys = True
            with open(self.state_file, 'w') as f:
                yaml.dump(state, f)
        except Exception as e:
            print(f"Error: Could not save state file {self.state_file}: {e}")
            raise

    def manage_organization(self, org_name: str, current_repo_count: int, operation: str = OP_CHECK) -> Tuple[bool, str]:
        """Unified organization management - check if processing needed or mark as processed."""
        state = self.load_state()
        org_state = state.get('organizations', {}).get(org_name, {})

        if operation == OP_CHECK:
            # Determine if organization should be processed
            stored_count = org_state.get('repo_count', 0)

            # Only scan if repositories were ADDED (not deleted)
            if current_repo_count > stored_count:
                return True, f"New repositories detected: {stored_count} → {current_repo_count} (+{current_repo_count - stored_count})"
            elif current_repo_count < stored_count:
                # Repositories deleted - just update count, no scan needed
                self.manage_organization(org_name, current_repo_count, OP_MARK_CHECKED)
                return False, f"Repositories deleted: {stored_count} → {current_repo_count} (-{stored_count - current_repo_count}), no scan needed"

            return False, f"No changes detected ({current_repo_count} repos)"

        elif operation == OP_MARK_CHECKED:
            # Mark organization as checked and update state
            now = datetime.now().isoformat()
            if org_name not in state['organizations']:
                state['organizations'][org_name] = {}

            updates = {
                'repo_count': current_repo_count,
                'last_check': now
            }

            state['organizations'][org_name].update(updates)
            self.save_state(state)
            return True, f"Organization {org_name} marked as processed"

    def manage_template(self, operation: str, new_hash: str = None) -> Tuple[bool, str]:
        """Unified template management - check for changes or update state."""
        state = self.load_state()
        template_path = self.config.get_workflow_template_path()

        if operation == OP_CHECK_CHANGED:
            # Check if workflow template has changed since last run
            if not template_path.exists():
                return False, MSG_TEMPLATE_NOT_FOUND

            try:
                current_hash = hashlib.sha256(template_path.read_bytes()).hexdigest()
            except Exception as e:
                print(f"Warning: Could not hash template file {template_path}: {e}")
                return False, f"Error reading template: {e}"

            stored_hash = state.get('template_state', {}).get('workflow_yml_hash', '')

            if not stored_hash and current_hash:
                return True, MSG_INITIAL_TEMPLATE_SETUP

            if current_hash != stored_hash:
                return True, f"Template changed: {stored_hash[:8]} → {current_hash[:8]}"

            return False, MSG_TEMPLATE_UNCHANGED

        elif operation == OP_UPDATE_STATE:
            # Update stored template hash after processing
            if not new_hash:
                try:
                    new_hash = hashlib.sha256(template_path.read_bytes()).hexdigest()
                except Exception:
                    return False, "Failed to calculate template hash"

            if 'template_state' not in state:
                state['template_state'] = {}

            state['template_state'].update({
                'workflow_yml_hash': new_hash,
                'last_template_update': datetime.now().isoformat()
            })

            self.save_state(state)
            return True, f"Template state updated: {new_hash[:8]}"

    def get_summary_report(self) -> str:
        """Generate a summary report of all organization states."""
        state = self.load_state()
        orgs = state.get('organizations', {})

        if not orgs:
            return "No organization state data found."

        lines = ["AUTOMATION STATE SUMMARY", "=" * 40]

        # Template state summary
        template_state = state.get('template_state', {})
        template_path = self.config.get_workflow_template_path()

        current_hash = ""
        if template_path.exists():
            try:
                current_hash = hashlib.sha256(template_path.read_bytes()).hexdigest()
            except Exception:
                pass

        stored_hash = template_state.get('workflow_yml_hash', 'None')
        last_update = template_state.get('last_template_update', 'Never')

        lines.extend([
            "Template State:",
            f"   Current hash: {current_hash[:12] if current_hash else 'None'}...",
            f"   Stored hash: {stored_hash[:12] if stored_hash and stored_hash != 'None' else 'None'}...",
            f"   Changed: {'Yes' if current_hash and current_hash != stored_hash else 'No'}",
            f"   Last update: {self._format_timestamp(last_update)}",
            ""
        ])

        for org_name, org_data in sorted(orgs.items()):
            repo_count = org_data.get('repo_count', 0)
            last_check = org_data.get('last_check', 'Never')
            last_full_scan = org_data.get('last_full_scan', 'Never')

            lines.extend([
                f"{org_name}:",
                f"   Repositories: {repo_count}",
                f"   Last check: {self._format_timestamp(last_check)}",
                f"   Last full scan: {self._format_timestamp(last_full_scan)}",
                ""
            ])

        return "\n".join(lines)


    def _format_timestamp(self, timestamp: str) -> str:
        """Format timestamp for display."""
        if timestamp == 'Never':
            return timestamp
        try:
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            return dt.strftime('%Y-%m-%d %H:%M UTC')
        except (ValueError, TypeError):
            return timestamp