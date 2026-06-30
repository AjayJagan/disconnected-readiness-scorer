# Versioning Strategy for Disconnected Readiness Scorer

## Overview

The disconnected-readiness-scorer repository uses a **floating major version tag** strategy that balances security, usability, and automatic updates for downstream consumers.

## Versioning Scheme

### Semantic Versioning Foundation
All releases follow semantic versioning (semver) format: `v{MAJOR}.{MINOR}.{PATCH}`

- **MAJOR** (v1 → v2): Breaking changes to workflow inputs, outputs, or behavior
- **MINOR** (v1.0 → v1.1): New features, backward-compatible improvements
- **PATCH** (v1.0.0 → v1.0.1): Bug fixes, security updates, no breaking changes

### Tag Types

#### 1. Immutable Semantic Version Tags
- **Format**: `v1.0.0`, `v1.1.0`, `v2.0.0`
- **Purpose**: Exact version pinning for maximum control
- **Behavior**: Never updated after creation
- **Usage**: Conservative consumers who want explicit control over updates

#### 2. Floating Major Version Tags
- **Format**: `v1`, `v2`, `v3`
- **Purpose**: Automatic updates within major version boundaries
- **Behavior**: Automatically updated to point to latest release within major version
- **Usage**: Default recommendation for most consumers

### Floating Tag Guarantees

| Tag | Points To | Updates When | Guarantees |
|-----|-----------|--------------|------------|
| `v1` | Latest v1.x.x | Any v1.x.x release | No breaking changes |
| `v2` | Latest v2.x.x | Any v2.x.x release | Breaking changes from v1 |
| `v3` | Latest v3.x.x | Any v3.x.x release | Breaking changes from v2 |

## Consumer Usage Patterns

### Recommended: Floating Major Version
```yaml
# Automatically receives patch and minor updates
uses: opendatahub-io/disconnected-readiness-scorer/.github/workflows/disconnected-readiness-check.yml@v1
```

**Benefits:**
- Automatic security patches (v1.0.0 → v1.0.1)
- Automatic feature updates (v1.0.0 → v1.1.0)
- No action needed for non-breaking changes
- Manual upgrade only for breaking changes (v1 → v2)

### Conservative: Explicit Version Pinning
```yaml
# Manual control over all updates
uses: opendatahub-io/disconnected-readiness-scorer/.github/workflows/disconnected-readiness-check.yml@v1.0.0
```

**Benefits:**
- Complete control over version changes
- Predictable, unchanging behavior
- Manual work for security updates
- Manual work for bug fixes

### Security-Focused: SHA Pinning
```yaml
# Maximum security, immutable reference
uses: opendatahub-io/disconnected-readiness-scorer/.github/workflows/disconnected-readiness-check.yml@d0bc6a3ce275e4d493891b25ffb822d0bddf7878
```

**Benefits:**
- Immutable, cannot be tampered with
- Maximum supply chain security
- No automatic security updates
- Requires manual SHA updates

## Security Considerations

### Tag Management Policy
- **Semantic version tags** (v1.0.0) follow immutability policy - once created, they are never moved or updated
- **Floating major tags** (v1) are intentionally mutable - updated through the release workflow to point to latest release

### Supply Chain Security
- Release workflow requires elevated permissions
- Floating tags only move forward within major version boundaries
- Release process is auditable through GitHub Actions logs

### Compromise Mitigation
- If floating tag (`v1`) is compromised, consumers can pin to known-good semantic version
- Semantic version tags provide immutable fallback references
- SHA pinning available for maximum security environments

## Consumer Pinning Strategy

### The Balance: Reproducibility vs. Ease of Adoption

Our strategy provides both options to balance competing needs - teams choose the approach that fits their security and operational requirements.

## Release Information

Releases are managed through a manual GitHub Actions workflow that:
- Creates immutable semantic version tags (v1.2.3)  
- Updates floating major version tags (v1 → latest v1.x.x)
- Generates release notes and GitHub Releases

**For complete release procedures, see [RELEASE_PROCESS.md](RELEASE_PROCESS.md)**
