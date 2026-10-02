# Deployment pipeline plan

Written 2026-09-30. Status: plan only, nothing built yet.

This covers how a tagged release of this repo gets onto the Sim-aaS EC2 instance.

## Goal

Pushing a git tag deploys that version of the middleware to the instance, restarts the node and gateway, checks they are healthy, and rolls back if they are not. No manual steps on the box.

## Decisions made

| Topic | Decision |
|---|---|
| Trigger | Git tag |
| Runner | GitHub Actions |
| AWS auth from GitHub | OIDC role, no stored AWS keys |
| Deploy channel | SSM Run Command, no SSH |
| Process manager | systemd, one unit for the node and one for the gateway |
| Secrets | Read from AWS Secrets Manager at service start |
| Execution backend | AWS Batch on Fargate (`--rti aws`) |
| Processors | Separate pipeline, later |

## What exists today

- `.github/workflows/workflow.yml` runs on pull requests only: Ruff, then `test_unit_*.py` on Python 3.11 to 3.13.
- No deploy workflow, no systemd units, no deploy scripts.
- The old instance was installed and started by hand.

## Responsibility split

| Owner | Provides |
|---|---|
| This repo | systemd units, start scripts, deploy script, the workflow |
| Pipeline run | Release artifact, install, restart, health check, rollback |

## Work packages

Each package can be done on its own once its dependencies are met. P1 to P3 need no AWS access at all.

### P1. Non-interactive start commands

Depends on: nothing.

- [ ] Work out the full node command with no prompts, using `--keystore`, `--keystore-id`, `--password`, `--dor fs`, `--rti aws`, `--datastore`, `--rest-address`, `--p2p-address`, `--boot-node`, `--simaas-repo-path`
- [ ] Decide: write every flag out, or use `--profile prod` (a shortcut that sets several flags, including Docker as the job runtime) and override it with `--rti aws`
- [ ] Work out the gateway command: `service gateway --address --datastore --service-address`
- [ ] Confirm both start locally with no prompt

Done when: both commands start from a script with no keyboard input.

### P2. Start scripts and systemd units

Depends on: P1.

- [ ] Add a `deploy/` folder to this repo
- [ ] Node start script: read the config file, fetch secrets from Secrets Manager, export the `SIMAAS_AWS_*` variables, start the node
- [x] Decided: keystore password and AWS key pair live in Secrets Manager, nothing in files on the box
- [ ] Gateway start script
- [ ] Node unit: restart on failure, start after Docker and network
- [ ] Gateway unit: start after the node
- [ ] Run as a dedicated user, not root

Done when: `systemctl start` brings both up and a reboot brings both back.

### P3. Release gate

Depends on: nothing.

- [x] Tag format: `vX.Y.Z`, as already used (`v4.0.0` to `v4.3.0`)
- [ ] Check the tag matches `__version__` in `simaas/meta.py`
- [ ] Run lint and test waves 1 to 3 (no Docker needed, about 4 minutes)
- [x] Decided: only waves 1 to 3 gate a release. Waves 4 to 6 run on `main` separately, not on tags

Done when: a bad tag or failing test stops the workflow before any deploy step.

### P4. GitHub to AWS authentication

Depends on: the deploy role, artifact bucket and instance being in place.

- [ ] Store role ARN, region, instance ID and bucket name as repository variables
- [ ] Add OIDC permissions to the workflow
- [ ] Protect the deploy job with a GitHub environment, with approval if wanted

Done when: the workflow can assume the role and run a harmless SSM command.

### P5. Build and ship the release

Depends on: P3, P4.

- [ ] Package the tagged source as an archive
- [ ] Upload it to the artifact bucket under the tag name
- [ ] Verify the box can build processor images from an unpacked archive with no `.git` folder (the node needs the repo source via `SIMAAS_REPO_PATH`)

Done when: the archive for a tag is in the bucket.

### P6. Deploy script

Depends on: P2, P5.

- [ ] Download and unpack the archive into a per-release folder
- [ ] Create a fresh venv for the release and install into it
- [ ] Install or update the unit files and start scripts
- [ ] Record the previous release, then switch the `current` link
- [ ] Restart the node, then the gateway
- [ ] Keep the last few releases, delete older ones
- [ ] Refuse to run if jobs are in progress, unless forced

Done when: running the script on the box by hand upgrades it cleanly.

### P7. Health check

Depends on: P6.

- [ ] Both units report active
- [ ] Node answers `GET /api/v1/db/node` on its private address (no auth needed)
- [ ] Gateway answers on port 5101
- [ ] Reported version matches the tag
- [ ] Retry for a short period before failing

Done when: a broken release fails the check.

### P8. Rollback

Depends on: P6, P7.

- [ ] On a failed health check, switch `current` back to the previous release and restart
- [ ] Run the health check again
- [ ] Mark the workflow as failed either way
- [ ] Allow a manual rollback to any kept release

Done when: deploying a deliberately broken tag leaves the previous version running.

### P9. Assemble the workflow

Depends on: P3 to P8.

- [ ] One workflow: gate → build → deploy → health check → rollback on failure
- [ ] Allow one deploy at a time
- [ ] Add a manual trigger for redeploying an existing tag
- [ ] Write a short runbook in `docs/`

Done when: pushing a tag deploys with no manual step.

### P10. Processor pipeline (later)

Depends on: P9. Lives in the `saas-adapters` repo.

- [ ] Build the processor image
- [ ] Import it into the node
- [ ] Deploy it on the node

## Suggested order

P1 → P2 → P3 → P4 → P5 → P6 → P7 → P8 → P9 → P10

## Open decisions

| Decision | Recommendation |
|---|---|
| How code reaches the box | Archive in S3, fetched with the instance role. Avoids putting GitHub credentials on the box, since this repo is private |
| Test depth before deploy | Waves 1 to 3 on every tag; full suite on pull requests to `main` |
| Approval step | Require approval on the deploy environment at first, remove once trusted |
| Python version | README asks for 3.13; CI tests 3.11 to 3.13; Ubuntu 24.04 ships 3.12. Choose one and match it on the box |
| Deploy while jobs are running | Refuse by default |

## Risks

- **Processor images bake in middleware code:** after a middleware upgrade, deployed processors may run old code until rebuilt. See `docs/dev_testing.md`.
- **Datastore across versions:** shell history on the old box shows `node.db` and `rti.db` being deleted after version changes. Test an upgrade against a copy of real data before trusting it.
- **Password on the command line:** `--password` is a CLI flag, so it is visible in the process list on the box. Acceptable on a single-purpose instance; a middleware change could remove it.
- **Processor image name ignores the middleware version:** the name is `<processor>:<hash of the processor folder>`. Rebuilding after a middleware upgrade gives the same name, so the build is skipped and the old image stays in use. Fix needed before processor rebuilds are automated; see `processor-deployment-notes.md`.
- **Static AWS keys:** the AWS plugin requires an access key and secret as environment variables. Using the instance role instead needs a change in `simaas/plugins/builtins/rti_aws/service.py`.
- **Restart interrupts work:** restarting the node stops anything it is coordinating.
