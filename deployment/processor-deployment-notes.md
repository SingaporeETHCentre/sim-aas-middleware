# Processor deployment notes

Written 2026-09-30. Status: discussion notes, nothing decided beyond what is marked as such.

Follows on from `deployment-pipeline-plan.md` (middleware pipeline) and `simaas-ec2-deployment-findings.md` (state of the instance). This file covers the questions that came up about how processors fit into the picture.

## Reusing the existing AWS resources

The middleware pipeline plan already targets the existing instance, storage and Batch setup. No new compute or storage is needed.

| Resource | Reused as-is? | Notes |
|---|---|---|
| EC2 `simaas-instance` | Yes | Has `SSMInstanceRole` and SSM agent online, so SSM Run Command works today |
| `/mnt/storage` volume | Yes | Datastore, keystore, gateway DB stay in place; releases go in `releases/<tag>/` with a `current` link |
| Batch queue `simaas-queue`, env `simaas-fargate` | Yes | Plugin only needs `SIMAAS_AWS_JOB_QUEUE=simaas-queue` |
| ECR `simaas-processors` | Yes | Already holds the processor images |
| IAM role `simaas-service` | Yes | Already the `SIMAAS_AWS_ROLE_ARN` the plugin assumes |

New resources needed, all one-off:

1. OIDC provider and a deploy role for GitHub Actions. Needs only `ssm:SendCommand` on the instance and `s3:PutObject` on the artifact bucket.
2. An S3 artifact bucket, with `s3:GetObject` on it for `SSMInstanceRole`.
3. Secrets Manager entries for the keystore password and (for now) the static AWS key pair, with `secretsmanager:GetSecretValue` for `SSMInstanceRole`.

### Why the bucket

The repo is private, so the instance cannot `git clone` it without a GitHub credential on the box. Instead the workflow packages the tagged source as an archive and uploads it to S3; the deploy script downloads it with the instance role. No GitHub secrets on the box, no static keys. Old archives stay in the bucket, so redeploying an older tag is just fetching that key again.

### Static keys

`simaas/plugins/builtins/rti_aws/service.py` hard-requires `SIMAAS_AWS_ACCESS_KEY_ID` and `SIMAAS_AWS_SECRET_ACCESS_KEY`. Short term: keep them in Secrets Manager and export them from the start script. Better: make those two optional so boto3 falls back to the instance profile, and give `SSMInstanceRole` `sts:AssumeRole` on `simaas-service`.

## Where things run

| Where | What | Lifetime |
|---|---|---|
| EC2 instance | Node and gateway (the middleware) | Always on |
| Fargate | One container per job, from a processor image in ECR | Only while the job runs |
| ECR | Processor images | Stored, not running |

The middleware does not contain processors. It runs them. When a job arrives, the node's RTI (`--rti aws`) submits it to Batch, Fargate pulls the image from ECR, runs it, and the container is gone when the job finishes.

The instance also holds a `saas-adapters` checkout and 14 GB of processor images in its local Docker store. That is because it was used as the build machine for processor images, not because it runs them. With images built on GitHub Actions (see below) none of that needs to be on the box.

### Why processor images contain the middleware

A processor container is not just the model. Something inside it has to read the job descriptor, fetch inputs, call the model, write outputs and report progress to the node. That runtime is part of the middleware, so each Dockerfile does `COPY /sim-aas-middleware` and `pip install` it, and the container's entrypoint is `simaas-cli run ...`. `SIMAAS_REPO_PATH` exists so the node knows where to copy the middleware source from at build time.

Consequence: an image holds a snapshot of the middleware from when it was built. An image built against 4.1.0 keeps running the 4.1.0 runtime inside the container after the node moves to 4.3.0. That is fine as long as the node–container job protocol has not changed. A middleware release therefore *sometimes* needs a processor rebuild; rebuilding on every release is the safe default.

## Two repos, two pipelines

| Repo | Workflow | Triggers |
|---|---|---|
| `sim-aas-middleware` | Deploy node and gateway to the instance (P1–P9 in the plan) | Middleware tag |
| `sim-aas-adapters` | Build processor image, push to ECR, import and deploy on the node | Processor change |

The adapters repo is `https://github.com/SingaporeETHCentre/sim-aas-adapters` (private). The old org `sec-digital-twin-lab` no longer resolves; the checkout on the box still points at it.

Dependencies are one way:

- Middleware release → processors may need rebuilding. Middleware stays untouched otherwise.
- Processor release → middleware untouched. The node keeps running; `image import` and `rti proc deploy` are REST calls to it, no restart.

Both workflows share the same OIDC deploy role and SSM path, set up once in P4.

**Decided:** processor images are built on GitHub Actions and pushed to ECR from there, not on the instance.

**Order:** middleware pipeline first (the processor pipeline needs a running node to deploy into, and shares its AWS plumbing), then the adapters workflow. Optionally later, the middleware workflow's last step triggers the adapters workflow to rebuild all processors.

## How processors are versioned today

Looked at `sim-aas-adapters` at `1b1b8ac` (2026-05-06) and the middleware's `simaas/cli/cmd_image.py`.

- **No version numbers.** `descriptor.json` has `name`, `required_secrets` and the input/output specs. No version field. No git tags in the repo at all.
- **Content addressed.** The image name is `<name>:<sha256 of the processor folder>` (`cmd_image.py:67-80`, file paths and contents hashed in sorted order). Any change to the folder gives a new hash and a new image. The ECR tag is the same string with `:` replaced by `_`, which is where `ucm-cnrs:ca29f65d…` on the box comes from.
- **Git commit recorded, not used as identity.** `gpp.json` stores the repo URL, commit and path for provenance.
- **Layout.** 38 processor folders in five groups: `cd-ucm`, `cs-duct`, `ghg-cea`, `infrarisk`, `legacy-duct-fom` (23 of the 38, probably dead). Only three are deployed on the box: `ucm-cnrs`, `infrarisk-eqintensity`, `infrarisk-recovery`. Each folder has its own Dockerfile and they are not uniform (`ucm-cnrs` is a clean two-stage build; `infrarisk-eqintensity` also carries a Python 3.9 venv for a legacy wrapper).

### The hash does not cover the middleware

The content hash covers only the processor folder. The middleware is copied in at build time but is not part of the hash. So building the same processor against 4.1.0 and 4.3.0 gives the *same image name*, and the RTI's "already in ECR, skip" check (`rti_aws/service.py:464`) means the stale image stays in use. A middleware upgrade does not just need a rebuild; the current design will refuse to do one unless the ECR tag is deleted first or the build is forced.

This needs a middleware fix before P10 is useful. Options:

- Fold the middleware version into the image name (for example `<name>:<hash>-<middleware version>`).
- Have the workflow pass `--force-build` and delete the stale ECR tag on a middleware release.

Should go on the risk list in `deployment-pipeline-plan.md`.

## Options for the adapters workflow

Per-processor semver tags (`infrarisk-recovery/v1.2.0`) were the first idea, but nothing in the system would use the number. Two options that fit what exists:

1. **Keep content addressing, trigger on path change.** Push to `main` touching `infrarisk/recovery/**` builds that processor, pushes it to ECR under its hash, then imports and deploys it. The hash is the version. No naming decisions, matches how the CLI already works. **Recommended for now.**
2. **Add a version to `descriptor.json`** and tag `<name>/vX.Y.Z`. Cleaner for humans, but it is a middleware change (descriptor schema and image naming) before it means anything.

Either way, one workflow file handles every processor. Per-processor workflows would be N copies of the same ECR push and SSM deploy. Build-all-on-every-change is out: images are 1–3 GB each, and redeploying a processor changes its identity on the node, so users would see every processor change when only one did.

Also needed: a `workflow_dispatch` input to rebuild all (or a listed set of) processors against a given middleware version. That is the path a middleware release uses.

## Open questions

- Which of the 38 processors should the pipeline cover? Only three are deployed. The `legacy-duct-fom` group is probably dead.
- Which fix for the hash-vs-middleware problem: change the image naming, or force-build and delete on middleware release?
- Whether the Dockerfiles should be made uniform (shared base image, or a template) before automating builds, or left as they are.
