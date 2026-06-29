# Release Process Guide

This document outlines the step-by-step process for cutting releases, managing floating tags, and ensuring reproducible builds while maintaining ease of adoption for consumers.

## Release Process Overview

The release process is designed to balance two key requirements:
1. **Reproducibility**: Deterministic builds that consumers can rely on
2. **Ease of adoption**: Consumers get security updates without manual intervention

## Cutting a New Release

### Prerequisites
- Write access to the repository
- Understanding of semantic versioning impact
- Completed testing of changes to be released

### Step-by-Step Release Process

#### 1. Determine Version Bump Type

Review recent changes and determine the appropriate version increment:

**Version bump decision matrix:**

| Change Type | Examples | Version Bump | Consumer Impact |
|-------------|----------|--------------|-----------------|
| **Breaking** | Required input changes, behavior changes, removed features, workflow input/output changes | MAJOR (v1.0.0 → v2.0.0) | Manual update required |
| **Feature** | New optional inputs, new outputs, backward-compatible features, new rules | MINOR (v1.0.0 → v1.1.0) | Automatic via floating tags |
| **Fix** | Bug fixes, security patches, documentation, rule accuracy improvements | PATCH (v1.0.0 → v1.0.1) | Automatic via floating tags |

**Review Process:**
1. **Check recent PRs/commits** - Look at what changed since the last release
2. **Assess consumer impact** - Would existing workflows break or need changes?
3. **Choose appropriate bump** - When in doubt, prefer minor over major, patch over minor

#### 2. Trigger Release Creation

1. **Navigate to GitHub Actions**:
   - Go to repository → Actions → "Create Release" workflow

2. **Run workflow dispatch**:
   ```
   Workflow: Create Release
   Branch: main
   Inputs:
     version: v1.2.3  # New version to create
     prerelease: false  # true for pre-release versions
   ```

3. **Automated process executes**:
   - Validates version format (`v{major}.{minor}.{patch}`)
   - Checks tag doesn't already exist
   - Generates release notes from commits
   - Creates immutable semantic version tag
   - Updates floating major version tag
   - Creates GitHub Release

#### 3. Verify Release Success

Check the following after release completion:

**Expected outcomes:**
- New semantic version tag exists (e.g., `v1.2.3`) - visible in GitHub repo tags
- Floating major version tag updated (e.g., `v1` → `v1.2.3`) - check GitHub repo tags  
- GitHub Release created with auto-generated release notes - visible in GitHub Releases page
- Release notes include "What's Changed" section with PR links and contributors

If any step failed, check the GitHub Actions logs for the "Create Release" workflow run.

#### 4. Post-Release Actions

1. **Update documentation** if breaking changes occurred (particularly for major versions)
2. **Monitor for issues** in the first 24-48 hours after release
3. **Note**: Consumers using floating tags (`@v1`, `@v2`, etc.) will automatically receive all patch and minor updates within their major version (including bug fixes, security patches, and new features)

## Emergency Procedures and Rollback Scenarios

### When to Consider Rollback

**Critical Issues Requiring Emergency Response:**
- Security vulnerabilities introduced in latest release
- Widespread breaking changes not caught in testing
- Data corruption or loss potential
- Consumer workflow failures blocking production deployments

**Floating Tag Advantage:** Teams using floating major version tags (`@v1`, `@v2`, `@v3`, etc.) automatically receive hotfixes without manual intervention, making **forward fixes the preferred strategy** over rollbacks.

### Emergency Rollback Process

**Step 1: Assess the Issue**
```bash
# Check what changed in problematic release
git diff v1.2.2..v1.2.3

# Review issue reports and affected consumers
```

**Step 2: Choose Rollback Strategy**

**Preferred: Forward Fix (Hotfix Release)**
```bash
# Create v1.2.4 with fix, rather than rolling back v1.2.3
# This maintains forward progress and clear audit trail
```

**✅ Automatic Fix for Floating Tag Users:** Teams using floating major version tags (e.g., `@v1`, `@v2`) will automatically get the hotfix when their respective floating tag moves. No manual action required by consumer teams.

**⚠️ Manual Action for Pinned Users:** Teams using pinned versions (e.g., `@v1.2.3`, `@v2.1.0`) must be notified to upgrade manually or receive a PR for the fix.

**Last Resort: Emergency Floating Tag Rollback**

**CAUTION: Emergency use only - requires team lead approval**

```bash
# Point floating tag back to previous known-good version
git tag -fa v1 v1.2.2 -m "Emergency rollback: v1 -> v1.2.2 due to critical issue in v1.2.3"
git push origin v1 --force

# Document the emergency action immediately
echo "$(date): Emergency rollback: v1 → v1.2.2 due to critical issue in v1.2.3" >> ROLLBACK.log
echo "Reason: [Brief description of critical issue]" >> ROLLBACK.log
echo "Approver: [Team lead name]" >> ROLLBACK.log

# Immediately create communication plan
echo "Alert: v1 rolled back to v1.2.2 due to critical issue in v1.2.3" > ALERT.md
```

**Step 3: Post-Rollback Actions**

**Communication Strategy (varies by user type):**
- **Floating tag users (e.g., `@v1`, `@v2`)**: General incident notification only — they get fixes automatically
- **Pinned users (e.g., `@v1.2.3`, `@v2.1.0`)**: Direct notification with upgrade instructions or manual PR creation
- **Emergency rollback**: Immediate alert to all teams regardless of usage pattern

**Follow-up Actions:**
- Create hotfix for the underlying issue  
- Schedule post-incident review
- Update documentation if process gaps were identified
- Monitor consumer adoption of the fix (higher urgency for pinned users)

**Requirements for Emergency Rollback:**
- Critical security incident or widespread breaking bug
- Approval from team lead
- Immediate communication to affected teams
- Post-incident review scheduled within 48 hours

**For floating tag concepts, see [VERSIONING.md](VERSIONING.md)**

## Automated Consumer Onboarding

### How It Works

Consumer repositories are **automatically onboarded** via the `create-drs-prs.yml` workflow:

✅ **Automated PR creation** - Workflow creates PRs in target repositories  
✅ **Template propagation** - Changes to `.github/templates/workflow.yml` trigger updates  
✅ **Weekly scans** - Discovers new repositories that need onboarding  
✅ **Safe process** - All changes go through normal PR review


### Automated PR Strategy

**The automation creates PRs using floating major version tags** (e.g., `@v1`, `@v2`) by default. This choice balances:

✅ **Automatic security patches** - Teams get fixes without manual intervention  
✅ **Reduced maintenance burden** - No weekly update PRs across 15+ repositories  
✅ **Breaking change protection** - Manual upgrade only needed for major versions (v1→v2)

### Alternative Security Approaches

**If your team requires stricter security controls:**

**Manual pinning** - Edit the automated PR to use specific versions:
```diff
- uses: opendatahub-io/disconnected-readiness-scorer/.github/workflows/disconnected-readiness-check.yml@v1
+ uses: opendatahub-io/disconnected-readiness-scorer/.github/workflows/disconnected-readiness-check.yml@v1.2.3
```


**For complete security tier documentation, see [VERSIONING.md](VERSIONING.md)**

## Troubleshooting Release Process

### Common Issues and Solutions

#### Release Workflow Fails

**Issue**: Version format validation fails
```
Invalid version format: 1.2.3
Version must be in format: v1.0.0, v1.1.0, v2.0.0
```

**Solution**: Ensure version starts with 'v' and follows semver
```bash
# Correct format
v1.2.3

# Incorrect formats
1.2.3     # Missing 'v' prefix
v1.2      # Missing patch version
v1.2.3.4  # Too many version components
```

**Issue**: Tag already exists
```
Tag v1.2.3 already exists!
Release tags are immutable. Use a different version number.
```

**Solution**: Use next available version number
```bash
# Check existing tags
git tag --list | grep "v1\."

# Use next available version
# If v1.2.3 exists, use v1.2.4 or v1.3.0 depending on change type
```

#### Floating Tag Update Issues

**Update Timing**: Floating tags are updated **immediately** when the release workflow completes (typically 2-3 minutes after triggering the release).

**Issue**: Consumers report old version after release  
**Cause**: Git client caching old floating tag reference

**Solution**: Consumers should refresh their git references
```bash
# For consumers experiencing issues
git fetch --tags --force
```

**GitHub Actions automatically fetches latest tags**, so most consumer workflows will get updates on their next run without manual intervention.


This comprehensive release process ensures both reproducible builds and ease of adoption while maintaining security and operational excellence.
