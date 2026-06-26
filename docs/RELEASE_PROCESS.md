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
| **Feature** | New optional inputs, new outputs, backward-compatible features, new rules | MINOR (v1.0.0 → v1.1.0) | Automatic via @v1 |
| **Fix** | Bug fixes, security patches, documentation, rule accuracy improvements | PATCH (v1.0.0 → v1.0.1) | Automatic via @v1 |

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
- GitHub Release created with release notes - visible in GitHub Releases page
- Release notes include usage examples and migration guidance

If any step failed, check the GitHub Actions logs for the "Create Release" workflow run.

#### 4. Post-Release Actions

1. **Update documentation** if breaking changes occurred (particularly for major versions)
2. **Monitor for issues** in the first 24-48 hours after release
3. **Note**: Consumers using `@v1` will automatically receive patch/minor updates

## Emergency Procedures

### Manual Floating Tag Rollback

**CAUTION: Emergency use only**

```bash
# Point floating tag back to previous version
git tag -fa v1 v1.2.2  # Point v1 back to v1.2.2
git push origin v1 --force

# Document the emergency action
echo "Emergency rollback: v1 → v1.2.2 due to critical issue in v1.2.3" >> ROLLBACK.log
```

**Requirements:**
- Critical security incident or widespread breaking bug
- Approval from team lead
- Immediate communication to affected teams
- Post-incident review scheduled

**For floating tag concepts, see [VERSIONING.md](VERSIONING.md)**

## Consumer Migration Support

### Quick Migration Examples

**From hardcoded SHA to floating tag:**
```diff
- uses: opendatahub-io/disconnected-readiness-scorer/.github/workflows/disconnected-readiness-check.yml@29ae4bc3591a988c6e3f6ec72d0184c0866650fe
+ uses: opendatahub-io/disconnected-readiness-scorer/.github/workflows/disconnected-readiness-check.yml@v1
```

**Testing new major versions:**
```yaml
# Test new major version safely
- name: Test with v2
  uses: opendatahub-io/disconnected-readiness-scorer/.github/workflows/disconnected-readiness-check.yml@v2
  continue-on-error: true
```

**For comprehensive migration guidance, see [VERSIONING.md](VERSIONING.md)**

**For versioning strategy and consumer guidelines, see [VERSIONING.md](VERSIONING.md)**

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

**Issue**: Consumers report old version after release
**Cause**: Git client caching old floating tag reference

**Solution**: Consumers should refresh their git references
```bash
# For consumers experiencing issues
git fetch --tags --force
```

#### Rollback Scenarios

**Emergency rollback process:**

1. **Identify the issue**:
   ```bash
   # Check what changed in problematic release
   git diff v1.2.2..v1.2.3
   ```

2. **Create hotfix release**:
   ```bash
   # Create v1.2.4 with fix, rather than rolling back v1.2.3
   # This maintains forward progress and clear audit trail
   ```

3. **Emergency floating tag rollback** (last resort only):
   ```bash
   git tag -fa v1 v1.2.2
   git push origin v1 --force
   
   # Immediately create communication plan
   echo "Alert: v1 rolled back to v1.2.2 due to critical issue in v1.2.3" > ALERT.md
   ```

## Release Automation Improvements

### Future Enhancements

1. **Automated Release Notes**:
   - Parse conventional commits for better release notes
   - Include breaking change warnings
   - Auto-generate migration guides

2. **Release Validation**:
   - Automated testing of workflow before tag creation
   - Consumer compatibility testing
   - Performance regression detection

3. **Release Notifications**:
   - Slack/email notifications for new releases
   - GitHub Discussions post for major releases
   - Documentation site updates

### Monitoring and Metrics

Track release health:
- Time between releases
- Number of consumers using floating vs. explicit tags
- Issue reports correlated with recent releases
- Security update adoption rates

This comprehensive release process ensures both reproducible builds and ease of adoption while maintaining security and operational excellence.
