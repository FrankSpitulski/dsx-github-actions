# Promote a release candidate

`promote-rc.yml` publishes a stable release from one verified RC. It copies images
by digest and derives stable Helm archives from the published RC archives.
It does not build images or resolve chart dependencies.

## Product configuration

Add this caller to the product's protected default branch. Pin both shared
workflows to the same reviewed full commit SHA.

```yaml
name: Promote RC
on:
  workflow_dispatch:
    inputs:
      rc_version:
        description: Exact QA-approved RC, without v
        required: true
        type: string
permissions:
  contents: read
jobs:
  promote:
    uses: dsx-ai-factory/dsx-github-actions/.github/workflows/promote-rc.yml@<reviewed-sha>
    permissions:
      contents: write
      actions: read
      deployments: read
    with:
      rc_version: ${{ inputs.rc_version }}
```

Do not pass secrets, add `secrets: inherit`, or add concurrency to this caller.
The shared workflow owns the publishing environment and stable-release lock.

Add `.github/rc-promotion.json`. Keep `enabled` false until the rollout review.
Declare the complete inventory, including every chart whose stable version is
required. Registry paths and artifact declarations are reviewed configuration,
not dispatch inputs.

```json
{
  "schema": 1,
  "enabled": false,
  "source_workflow": ".github/workflows/release-rc.yml",
  "source_ngc_path": "example/components-dev",
  "destination_ngc_path": "example/components-dev",
  "images": ["service"],
  "charts": [
    {"name": "service", "version_value_paths": [["image", "tag"]]}
  ],
  "competing_workflows": [".github/workflows/release.yml"]
}
```

Use the same chart version-value paths as the RC publisher's `versionValuePaths`.
An empty array is valid when the chart uses `Chart.AppVersion`.

When charts are included, source and destination NGC paths **must be identical**.
Cross-namespace chart promotion is rejected before registry access, including
charts without declared version-value paths. This implementation does not
rewrite image repositories or relocate vendored dependency images. The example
therefore publishes stable tags alongside RC tags in the same namespace. Only
image-only policies may use different source and destination paths.

Opt in to evidence capture in the existing RC caller:

```yaml
with:
  promotion-manifest: true
```

The RC publisher records all image digests, chart archive digests, source commit,
workflow path, run ID, and run attempt after every artifact job succeeds. It
uploads `rc-promotion-manifest-<version>-<attempt>` with 90-day retention.
Promotion checks the artifact's GitHub digest and the successful source run.
Older or expired RCs without this evidence are rejected. A GitHub prerelease
alone is insufficient. Do not fabricate a manifest for a historical RC.

## Authorization and cutover

Repository write access permits requesting a manual run. Publishing additionally
requires approval through the fixed `stable-release` environment.

Before activation, repository administrators must configure:

- A protected default branch. Runs from other refs, unprotected refs, and stale
  default-branch commits are rejected.
- `stable-release` with required **team** reviewers, self-review prevention, and
  administrator bypass disabled. Its sole deployment branch policy must be the
  exact default branch name. Native required reviewers authorize any one listed
  team reviewer, not one approval from each team. A both-teams policy is not
  supported by this single gate and must not be activated.
- Environment-only secrets `RC_PROMOTION_SOURCE_NGC_KEY` and
  `RC_PROMOTION_DESTINATION_NGC_KEY`. Use source read and destination publish
  permissions. Do not create repository or organization secrets with these names.
- Existing destination chart repositories, immutable stable-version policy, and
  registry access from the runner. Promotion does not create chart repositories
  or modify registry permissions.
- Read access for the normal workflow token to run evidence and environment
  protection metadata. A denied metadata read stops promotion. Do not introduce
  an administrator token to bypass this check.

One repository variable, `DSX_RELEASE_MODE`, coordinates the cutover. Unset or
any value other than `rc-promotion` preserves legacy publishing. Every job in an
existing stable publisher must use:

```yaml
if: vars.DSX_RELEASE_MODE != 'rc-promotion'
```

That workflow must also share the promotion lock:

```yaml
concurrency:
  group: dsx-stable-release-${{ github.repository }}
  cancel-in-progress: false
```

The helper checks these guards on every declared competing workflow and on
`.github/workflows/release.yml` when present. A manually disabled workflow also
satisfies the collision check. The helper never changes workflow state.

Merge and review the guarded callers before setting `DSX_RELEASE_MODE` to
`rc-promotion` and enabling the policy. Do not set that variable on the
environment, where it could disagree with the legacy publisher. The shared
workflow reads it before entering the environment and carries that decision into
the publishing job.

Keep activation off until the authorization policy is agreed and a supervised
canary is authorized. Local tests do not validate live GitHub approvals, NGC
permissions, publication, or downstream deployment.

## Artifact identity

Only canonical `X.Y.Z-rc.N` versions are accepted, with a positive RC number and
no leading zeros or build metadata. The stable version is `X.Y.Z`.

Images are copied from the verified digest with `skopeo --all --preserve-digests`.
The destination tag must resolve to that exact digest. Embedded RC version
strings and OCI image labels remain unchanged. A stable registry tag is a new
name for the QA-tested bytes, not a rebuilt binary with rewritten version text.

Stable chart archives have **different digests**. Only root `Chart.yaml`
version/appVersion, promotion provenance annotations, and declared RC-valued
paths in root `values.yaml` change. Templates, other values, lock files, and
vendored dependencies retain their original bytes. A vendored RC dependency
keeps its RC version and archive even when a separate stable chart is published
for it. No dependency update, template rendering, or image rebuild occurs.
Vendored image references also retain their RC tags unless an explicitly
declared root value overrides them. A stable root chart is **not** a promise that
every runtime image uses a stable tag. Retain the referenced RC images and their
access permissions; this workflow performs no RC cleanup. Products requiring
all-stable dependency references or namespace relocation are unsupported and
must remain disabled until those transformations and rendered-reference checks
are implemented and reviewed.

The archive records the RC version, source commit, original chart digest, and RC
manifest digest. This is traceability, not a new security approval or signing
attestation. External OCI referrer signatures are not copied by this workflow.
Products requiring new signatures or changed vendored dependency versions need
those capabilities before activation.

## Retries and partial publication

Every source artifact and existing destination is checked before any write.
Missing artifacts, changed digests, unexpected inventory, or a different stable
version stop the run.

A draft GitHub release stores the exact promotion intent before registry writes.
Attempt reports remain release assets. Matching outputs are verified and reused
on a rerun; different outputs are never deliberately overwritten. The original
source run and attempt remain bound to that intent.

The stable tag points to the RC commit. It is created only after all destination
artifacts verify, then the draft is published without changing the latest-release
marker. A failure can leave some registry versions or a draft release. Do not
delete them or choose a different RC for the same stable version. Inspect the
intent and attempt reports, correct access or transport failures, and rerun the
same RC. A failed or ambiguous write is not automatically retried.

Registry writes do not provide a cross-service transaction or compare-and-set
tag operation. The common GitHub concurrency group serializes these workflows;
registry policy must exclude independent writers or tag replacement. Publication
to another system is outside this workflow.
