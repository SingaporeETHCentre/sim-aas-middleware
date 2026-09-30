# Sim-aaS EC2 deployment findings

Investigated 2026-09-30. Instance `i-072148647d006be08` (`simaas-instance`), account `418272787672`, region `ap-southeast-1`.

## Summary

- The deployment is fully manual: no CloudFormation, no Auto Scaling group, no user data, no systemd unit, no cron job.
- **The node is not running.** Nothing starts it on boot, so it did not come back after the stop/start on 29–30 Sept 2026.
- Data, keystore and processor images are intact on `/mnt/storage`.

## Instance

| Property | Value |
|---|---|
| Name tag | `simaas-instance` (only tag) |
| Type | `t2.medium`, x86_64 |
| AMI | `ami-0672fd5b9210aa093`, stock Canonical Ubuntu 24.04 (2025-01-15 build) |
| Created | 2025-02-22 (volume attach time) |
| AZ / VPC / subnet | `ap-southeast-1b` / `vpc-09400c6c98c1c445b` / `subnet-0fbcf4e6e493b086f` |
| Private IP | `172.31.33.8` |
| Public IP | `18.143.187.26` at time of check; no Elastic IP, so it changes on every stop/start |
| Key pair | `ce2m-heiko` |
| Instance profile | `SSMInstanceRole` (`AmazonSSMManagedInstanceCore` + inline `urscape-prod-awslogs`) |
| Security group | `sg-0ca4e36c9fd2548a5` (`simaas-default`) |
| Volumes | `vol-0e31d17575a42e855` (root, 50 GB gp3), `vol-0fcd38779d89fa4ad` (`/mnt/storage`, 50 GB gp3) |
| Access | SSM only (agent online) |

### Security group rules

| Direction | Port | Source | Description |
|---|---|---|---|
| In | 4001 | `sg-04c88501d8a0de888` | Sim-aaS Node P2P interface |
| In | 5101 | `0.0.0.0/0` | Substrate API service |
| In | 4999 | `0.0.0.0/0` | (none) |
| In | 5999 | `0.0.0.0/0` | (none) |
| In | 2049 | `sg-0ca4e36c9fd2548a5` (itself) | (none) — NFS/EFS port |
| Out | 0–65535 tcp | `0.0.0.0/0` | (none) |

## How it was deployed

| Step | What was done |
|---|---|
| Storage | Second EBS volume mounted at `/mnt/storage` via `fstab` (`nofail`); Docker `data-root` set to `/mnt/storage/docker` in `/etc/docker/daemon.json` |
| Code | `git clone` of `sim-aas-middleware` and `saas-adapters` into `/mnt/storage`; updated with `git pull` and branch switches |
| Install | venv at `/mnt/storage/venv-simaas` (Python 3.12.3), then `pip install sim-aas-middleware/` |
| Launch | `/mnt/storage/run_simaas_instance.sh`, run inside a `screen` session as `ubuntu` |
| Processors | `simaas-cli image build-local .`, then `image import <file>.pdi`, then `rti proc deploy` |
| Gateway | Started separately by hand: `simaas-cli service gateway --datastore $STORAGE/datastore-gateway` |

### Layout of `/mnt/storage`

| Path | Size | Purpose |
|---|---|---|
| `datastore/` | 6.3 GB | Node datastore: `dor.db`, `rti.db`, `node.db`, `dor-master/`, `jobs/`, `procs/` |
| `datastore-gateway/` | 40 KB | Gateway database (`db.dat`) |
| `keystore/` | 20 KB | One keystore JSON file |
| `docker/` | 14 GB | Docker data root |
| `saas-adapters/` | 6.7 GB | Processor repo, including built `.pdi` files |
| `sim-aas-middleware/` | 98 MB | Middleware repo |
| `venv-simaas/` | 203 MB | Python venv |
| `job/`, `scratch/`, `temp/` | small | Job inputs, RTI scratch, temp dir |
| `run_simaas_instance.sh` | — | Launch script (dated 2025-09-19) |

### Launch script (secrets redacted)

```bash
#!/bin/bash
export STORAGE=/mnt/storage

export SIMAAS_AWS_REGION=ap-southeast-1
export SIMAAS_AWS_ACCESS_KEY_ID=***REDACTED***
export SIMAAS_AWS_SECRET_ACCESS_KEY=***REDACTED***
export SIMAAS_AWS_ROLE_ARN="arn:aws:iam::418272787672:role/simaas-service"
export SIMAAS_AWS_JOB_QUEUE=simaas-queue

export SIMAAS_REPO_PATH=$STORAGE/sim-aas-middleware
export SIMAAS_RTI_DOCKER_SCRATCH_PATH=$STORAGE/scratch

source /mnt/storage/venv-simaas/bin/activate

simaas-cli --keystore $STORAGE/keystore \
  --keystore-id byipx27qq8oybhzvs5sb92dd3pcdmcr4r6v4mhzwma53ftubvhx3yxxvksitux8g \
  --temp-dir $STORAGE/temp \
  service --datastore $STORAGE/datastore \
  --rest-address 172.31.33.8:5001 \
  --p2p-address tcp://172.31.33.8:4001 \
  --boot-node 172.31.33.8:5001
```

The script passes no password or plugin choice, so it most likely prompts for them. That would explain why it was always run inside `screen`. (Inferred, not confirmed.)

## Software state on the box

| Item | State |
|---|---|
| Installed `simaas` | 4.1.0 |
| Python | 3.12.3 |
| Docker | 28.4.0 |
| `sim-aas-middleware` checkout | `main` at `01aab52` (2026-05-06), remote `github.com/sec-digital-twin-lab/sim-aas-middleware` |
| `saas-adapters` checkout | branch `13-capability-demo` at `d0a7c22` (2026-05-05), three untracked `.pdi` files |
| Local repo (laptop) | 4.3.0, README asks for Python 3.13 |

### Processor images present

Each exists under a local tag and an ECR tag (`418272787672.dkr.ecr.ap-southeast-1.amazonaws.com/simaas-processors`).

| Image | Age at check | Size |
|---|---|---|
| `ucm-cnrs:ca29f65d…` | 4 months | 1.03 GB |
| `infrarisk-eqintensity:22235284…` | 5 months | 2.84 GB |
| `infrarisk-recovery:d4bcdae2…` | 5 months | 2.67 GB |
| `ubuntu/infrarisk-eqintensity:187b58cc…` | 12 months | 2.79 GB |
| `ubuntu/infrarisk-recovery:af3bb428…` | 12 months | 2.68 GB |

## Current runtime state

- No Sim-aaS, gateway or Substrate process running.
- No Docker containers (running or stopped).
- No `screen` or `tmux` sessions.
- Listening ports on the host: 22, 111 (rpcbind), 53 (local resolver) only.
- No systemd unit, crontab, `rc.local` or profile hook launches Sim-aaS. `.bashrc` only exports `STORAGE` and `SIMAAS_REPO_PATH`.

## Timeline

| Date | Event |
|---|---|
| 2025-02-20 | ECR repo `simaas-processors` created |
| 2025-02-22 | Instance and both volumes created |
| 2025-09-19 | venv, keystore and launch script created |
| 2025-09-25 | Last write to `~/.substrate/db.dat` |
| 2026-04-27 | Gateway datastore last written |
| 2026-05-06 | Last long interactive session; middleware repo at this commit |
| 2026-06-04 | Last write to the node datastore (`dor.db`, `rti.db`, `node.db`) |
| 2026-06-07 | Last interactive login as `ubuntu`; `authorized_keys` emptied |
| 2026-06-12 to 06-19 | Several short reboots |
| 2026-09-29 | Started by `awadmaharoof` at 13:50 SGT, stopped at 16:10 SGT; one SSM session 13:55–13:56 |
| 2026-09-30 | Started by `awadmaharoof` at 08:30 SGT |

## Related AWS resources

| Resource | Detail |
|---|---|
| Batch job queue | `simaas-queue` (enabled) |
| Batch compute environment | `simaas-fargate` (Fargate, enabled) |
| IAM role | `simaas-service` (referenced by the launch script) |
| ECR | `simaas-processors` |
| EFS | `fs-0bf7f8e5a6ae69397` (`wrf-data`, about 9.1 GB) |
| Other instances | `traefik-instance`, `rds-bastion-host`, `ur-scape-web-instance` |

## Issues found

1. **Plaintext AWS keys:** the launch script holds a static access key and secret and is world-readable (`-rwxrwxr-x`). The instance role has no Batch or ECR permissions, which is probably why static keys were used.
2. **No autostart:** any reboot or stop/start takes the node down until someone logs in and runs the script.
3. **SSH closed off:** `authorized_keys` is empty and the security group has no port 22 rule. SSM is the only way in.
4. **Ports don't line up:** 4999, 5101 and 5999 are open to the internet with nothing listening; the node's REST port 5001 is not open at all.
5. **No Elastic IP:** the public IP changes on every stop/start.
6. **Version drift:** box runs 4.1.0 on Python 3.12; current code is 4.3.0 and asks for Python 3.13.
7. **Leftovers:** Substrate API was removed (`venv-substrate`, `substrate-api` deleted) but its security group rule and `~/.substrate` remain.

## Not determined

- Whether jobs ran on local Docker or AWS Batch. The plugin is chosen at the prompt; the Batch queue, Fargate environment and ECR-tagged images suggest Batch was used at least some of the time.
- What sits behind ports 4999 and 5999, and whether `traefik-instance` fronts this node.
- What security group `sg-04c88501d8a0de888` belongs to.
- Whether the static AWS key in the script is still active.

## How this was gathered

- AWS CLI read-only calls: EC2, IAM, SSM, CloudTrail, CloudFormation, Auto Scaling, ECR, EFS, Batch.
- Three read-only `AWS-RunShellScript` commands over SSM, with secrets redacted on the instance before output was returned:
  - `70d55e70-ef82-403f-82dc-777516cdb27a`
  - `cb73fc93-11d2-4a2a-9143-41446bbb7340`
  - `86e2a75a-1594-4438-81c7-030da79916a2`
- Nothing on the instance was changed.
