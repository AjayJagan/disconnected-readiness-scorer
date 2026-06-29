# Disconnected Readiness — Remediation Guide

Guide for component teams on how to investigate and remediate each category of finding from the disconnected readiness scorer.

## Background: How Image Injection Works

In disconnected (air-gapped) environments, cluster nodes cannot pull images from public registries. The platform solves this through a multi-stage pipeline:

1. **Build-Config repos** ([RHOAI-Build-Config](https://github.com/red-hat-data-services/RHOAI-Build-Config), [ODH-Build-Config](https://github.com/opendatahub-io/ODH-Build-Config)) declare every container image as a `RELATED_IMAGE_*` entry in `bundle-patch.yaml`. OLM uses these to populate the CSV `relatedImages` list, which `oc-mirror` reads to catalog all images for mirroring.

2. **Operator `*_support.go` files** define an `imageParamMap` (or `imagesMap`) per component that maps `params.env` keys to `RELATED_IMAGE_*` env var names. For example:
   ```go
   imageParamMap = map[string]string{
       "odh-kuberay-operator-controller-image": "RELATED_IMAGE_ODH_KUBERAY_OPERATOR_CONTROLLER_IMAGE",
   }
   ```

3. **At runtime**, OLM injects `RELATED_IMAGE_*` env vars (containing mirrored image digests) into the operator pod. The operator's `ApplyParams()` function reads each `params.env` key, looks up the corresponding `RELATED_IMAGE_*` value via `os.Getenv()`, and overwrites the `params.env` default with the mirrored reference.

4. **Kustomize renders** the updated `params.env` values into manifests, which are applied to the cluster through the deploy action from the operator. All image references now point at the mirror registry.

Any break in this chain — a missing `params.env` key, a missing `imageParamMap` entry, or a missing Build-Config `RELATED_IMAGE_*` — means that image will not be overridden and will fail to pull in disconnected environments.

The opendatahub-operator also runs [validate-related-images.sh](https://github.com/opendatahub-io/opendatahub-operator/blob/main/.github/scripts/validate-related-images.sh) in CI to validate this chain end-to-end. Component teams can reference its output when debugging wiring issues.

---

## 1. Image Manifest Completeness (`image-manifest-complete`)

Every container image reference must be mapped to a `RELATED_IMAGE_*` env var (or listed in CSV `relatedImages`) so the operator can inject mirrored images in disconnected environments. Unmapped images will not be mirrored and will cause pull failures.

**Investigate:** Go to the reported file/line. Is this image actually pulled at runtime on a customer cluster? Check whether a `RELATED_IMAGE_*` variable covers it on the same line, in the same file, or in a sibling file.

**Remediate:** The fix depends on which pattern your repo uses:

- **params.env repos** (most components): Add the image to `params.env` with an appropriate key, wire it via kustomize `configMapKeyRef` or replacement, then ensure the operator's `*_support.go` maps the key to a `RELATED_IMAGE_*` var (see the [params.env wiring section](#5-paramsenv--kustomize-wiring-params-env-wiring) for the full chain). Finally, ensure the `RELATED_IMAGE_*` is declared in both [RHOAI-Build-Config](https://github.com/red-hat-data-services/RHOAI-Build-Config) and [ODH-Build-Config](https://github.com/opendatahub-io/ODH-Build-Config) `bundle-patch.yaml`.

- **RELATED_IMAGE env var repos** (repos that read `os.Getenv("RELATED_IMAGE_*")` directly): Replace the hardcoded image string with a `RELATED_IMAGE_*` env var lookup. Ensure the var is declared in the Build-Config repos and mapped in the operator's `*_support.go` `imageParamMap`/`imagesMap` for that component.

- **Stale vars**: If the scanner flags a `RELATED_IMAGE_*` var that exists in your repo but is no longer in the operator manifest, remove the stale reference from your code.

**False positives:** Build-time-only images (in scripts that generate Dockerfiles but never run on-cluster), images behind disabled feature gates, and files marked `[out of production scope]` (not compiled into the production binary). If a non-production file is not already auto-excepted, add a path exception in [config/config.yaml](https://github.com/opendatahub-io/disconnected-readiness-scorer/blob/main/config/config.yaml).

## 2. Mutable Image Tags (`no-image-tags`)

All image references must use `@sha256:` digests, not mutable tags (`:latest`, `:v1.2.3`). Tags can change after mirroring, causing silent drift or pull failures.

**Investigate:** Check whether the tagged image is deployed to a customer cluster at runtime. Images in `params.env` files are already auto-downgraded (the release process resolves tags to digests in the Build-Config repos before they reach the CSV).

**Remediate:** Replace tags with digest pins. Use `skopeo inspect --format '{{.Digest}}' docker://registry/image:tag` to look up the current digest.

**False positives:** Build-time images (Dockerfiles, CI scripts) that are never pulled on-cluster should be auto-excepted. Files marked `[out of production scope]` are already downgraded. Non-image strings that happen to match the `registry/org/name:tag` pattern (npm refs in `package.json` are already excluded, but other formats may occasionally trigger).

## 3. Runtime Network Egress (`no-runtime-egress`)

Detects outbound HTTP/network calls (`http.Get`, `requests.get`, `fetch`, `curl`, HuggingFace downloads) that would fail air-gapped. Distinguishes hardcoded external URLs (blocker) from configurable URLs and cluster-internal calls (info).

**Investigate:** Is the URL hardcoded to an external endpoint, or configurable via env var / config / CR field? Is the target cluster-internal (e.g. `*.svc.cluster.local`)? The scanner detects configurability by looking for `os.Getenv`, `config.`, `viper.`, `${...}` on the same line — if the config read is on a different line, the scanner may miss it.

**Remediate:** Make hardcoded external URLs configurable through environment variables or config files so customers can point them at an internal mirror or proxy. For HuggingFace models, pre-bundle them in the container image instead of downloading at runtime. Remove unneeded external calls entirely where possible. For reference, the odh-model-controller supports `spec.components.kserve.nim.airGapped` on the DSC to skip external NIM API calls and use locally cached model catalogs instead — this is the pattern for handling features that inherently need external access.

**False positives:** HTTP client setup code that constructs a client but only calls internal endpoints; Go files outside production scope; URLs that are configurable but the config read happens on a different line. Verify manually and add a central exception if confirmed safe.

## 4. Python Dependency Availability (`python-imports-bundled`)

Flags `git+https://` dependencies (require internet at install time), runtime `pip install` calls, and packages not in the known-bundled list.

**Investigate:** For `git+https://` deps, check if the package is available on PyPI or an internal mirror. For runtime `pip install`, determine if the code path runs in production or only in dev/build scripts. "Not in known-bundled list" findings are info-level — just verify availability in the internal PyPI mirror.

**Remediate:** Add the dependency to the bundled package list, vendor it, or confirm it is available in the internal PyPI mirror. Replace `git+https://` deps with PyPI-hosted versions. Pre-install dependencies in the container image at build time instead of using runtime `pip install`. Add verified packages to `known_mirrors.bundled_packages` in [config/config.yaml](https://github.com/opendatahub-io/disconnected-readiness-scorer/blob/main/config/config.yaml) to suppress info findings.

**False positives:** Build-time and CI scripts that call `pip install` but never run on a customer cluster (e.g. lockfile generators, CVE scanners). Check if the file is in `scripts/`, `.tekton/`, or `hack/`.

## 5. Params.env + Kustomize Wiring (`params-env-wiring`)

Validates the full wiring chain: `params.env` key → kustomize `configMapKeyRef`/replacement → rendered `RELATED_IMAGE_*` env var → Go `os.Getenv()`. There are three types of blockers:

- **Hardcoded images**: Container images defined directly in kustomize-managed manifests instead of being sourced from `params.env`. These will not be overridden with mirrored refs in disconnected environments.
- **Unwired params.env keys**: Keys defined in `params.env` that are not consumed by any kustomize overlay as a `configMapKeyRef` or replacement. The image value exists but is never injected into a manifest.
- **Orphan Go `os.Getenv` calls**: Go code references a `RELATED_IMAGE_*` environment variable that has no corresponding entry in the rendered kustomize manifests. The controller expects an image that will never be injected.

**Investigate:** For hardcoded images, check the kustomize overlay to see if the image should be parameterized. For unwired keys, check whether a `configMapKeyRef` or replacement is missing. For orphan `os.Getenv`, check for typos in the var name or missing kustomize mappings.

**Remediate per blocker type:**

- **Hardcoded images**: Move the image reference into `params.env` and wire it through kustomize (`configMapKeyRef` or replacement in `kustomization.yaml`).
- **Unwired keys**: Add the corresponding `configMapKeyRef` entry in the appropriate kustomize overlay so the value is injected into the deployment.
- **Orphan `os.Getenv`**: Add the missing key and image reference to `params.env`, then wire it through kustomize.

For any of these, the full chain also requires:

1. **Operator repo**: A mapping in the component's `internal/controller/components/<component>/<component>_support.go`:
   ```go
   imageParamMap = map[string]string{
       "my-sidecar-image": "RELATED_IMAGE_ODH_MY_SIDECAR_IMAGE",
   }
   ```

2. **Build-Config repos**: The `RELATED_IMAGE_ODH_MY_SIDECAR_IMAGE` in `bundle-patch.yaml` in both [RHOAI-Build-Config](https://github.com/red-hat-data-services/RHOAI-Build-Config) and [ODH-Build-Config](https://github.com/opendatahub-io/ODH-Build-Config).

Run the opendatahub-operator's [validate-related-images.sh](https://github.com/opendatahub-io/opendatahub-operator/blob/main/.github/scripts/validate-related-images.sh) CI check to verify the chain is complete.

**False positives:** Orphan `os.Getenv` findings are the most likely to be false positives — the scanner checks for `os.Getenv("RELATED_IMAGE_*")` calls repo-wide, which may match code in non-production binaries (e.g. `cmd/test-tool/`), utility functions that are never called at runtime, or vars that are injected through a different mechanism than kustomize. Verify the Go file is in the production binary's import graph before treating these as real blockers.

---

## Identifying False Positives

Ask yourself: **does this code actually run on a customer cluster in production?**

- **No, it's test/CI/docs/examples** → Should be auto-excepted; if not, add a path exception
- **No, it only runs at build time** → Not a runtime concern (e.g. Dockerfiles, build scripts, lockfile generators)
- **No, it's in a manifest that isn't deployed** → Not a customer-facing resource
- **Unsure** → Check whether the finding is annotated `[out of production scope]`, which means the scanner determined it's not in the production code path. If there's no annotation and you still believe it's a false positive, open a PR to request a central exception with a reason.
- **Yes, it runs in production** → The finding is real and needs remediation

## Configuring Exceptions

The centralized [config/config.yaml](https://github.com/opendatahub-io/disconnected-readiness-scorer/blob/main/config/config.yaml) already excludes common test directories, CI config, build files, docs, examples, and samples. For repo-specific overrides, create a new exception entry that references your repo, open a PR, and request a review.

Example:
```yaml
exceptions:
  - rule: "*"
    paths:
      - "internal/devtools/**"
    repo: opendatahub-io/kserve
    reason: "Dev tooling — not deployed in production"
```

**Time-bounded exceptions:** For temporary workarounds, add an `expires: "YYYY-MM-DD"` field. The scanner will stop honoring the exception after that date, and the PR check will start failing again — ensuring the team returns to fix the root cause. The scanner warns 14 days before expiration in its report output. To renew, update the `expires` date and submit a PR.

```yaml
exceptions:
  - rule: no-runtime-egress
    repo: my-component
    paths:
      - "internal/legacy_client.go"
    reason: "Legacy HTTP client — migrating to configurable URL"
    expires: "2026-12-31"
```
