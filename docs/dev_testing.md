# Testing Guide

## Environment Setup

### Virtual Environment

Tests must be run using the project's virtual environment:

```bash
# Create virtual environment (if not exists)
python3.13 -m venv .venv

# Install dependencies
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
```

### Environment Variables

Create a `.env` file in the project root. The test framework automatically loads it via `python-dotenv`.

#### Required for Docker RTI Tests

| Variable | Description |
|----------|-------------|
| `SIMAAS_REPO_PATH` | Absolute path to the sim-aas-middleware repository |

#### Required for AWS RTI Tests

See [Running a Sim-aaS Node - AWS RTI Service](usage_run_simaas_node.md#aws-rti-service) for detailed AWS setup.

| Variable | Description |
|----------|-------------|
| `SIMAAS_AWS_REGION` | AWS region (e.g., `ap-southeast-1`) |
| `SIMAAS_AWS_ACCESS_KEY_ID` | AWS access key ID |
| `SIMAAS_AWS_SECRET_ACCESS_KEY` | AWS secret access key |
| `SIMAAS_AWS_ROLE_ARN` | IAM role ARN for Batch execution |
| `SIMAAS_AWS_JOB_QUEUE` | AWS Batch job queue name |

#### AWS SSH Tunneling (for local development)

| Variable | Description |
|----------|-------------|
| `SSH_TUNNEL_HOST` | EC2 instance public DNS |
| `SSH_TUNNEL_USER` | SSH username (typically `ubuntu`) |
| `SSH_TUNNEL_KEY_PATH` | Path to SSH private key |
| `SIMAAS_CUSTODIAN_HOST` | EC2 instance private DNS |

#### GitHub Credentials (for image builds)

| Variable | Description |
|----------|-------------|
| `GITHUB_USERNAME` | GitHub username |
| `GITHUB_TOKEN` | GitHub Personal Access Token |

### Example `.env` File

```bash
# Required for Docker RTI tests
SIMAAS_REPO_PATH=/path/to/sim-aas-middleware

# GitHub credentials (optional)
GITHUB_USERNAME=your-username
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx

# AWS RTI (optional)
SIMAAS_AWS_REGION=ap-southeast-1
SIMAAS_AWS_ACCESS_KEY_ID=AKIA...
SIMAAS_AWS_SECRET_ACCESS_KEY=...
SIMAAS_AWS_ROLE_ARN=arn:aws:iam::123456789:role/simaas-role
SIMAAS_AWS_JOB_QUEUE=simaas-queue
```

## Running Tests

### Prerequisites

- Docker must be running (required for RTI tests)
- Virtual environment activated or use `.venv/bin/python` prefix

### Commands

```bash
# Run all tests
.venv/bin/python -m pytest simaas/tests/ -v

# Run specific category
.venv/bin/python -m pytest simaas/tests/test_unit_*.py -v    # Unit tests
.venv/bin/python -m pytest simaas/tests/test_cli_*.py -v     # CLI tests
.venv/bin/python -m pytest simaas/tests/test_processor_*.py -v  # Processor tests

# Run with coverage
.venv/bin/python -m coverage run -m pytest simaas/tests/ -v
.venv/bin/python -m coverage report --ignore-errors --include="simaas/*" --omit="simaas/tests/*"

# Run specific test file
.venv/bin/python -m pytest simaas/tests/test_dor.py -v

# Run specific test function
.venv/bin/python -m pytest simaas/tests/test_dor.py::test_dor_add_search -v

# Run by marker
.venv/bin/python -m pytest simaas/tests/ -v -m "docker_only"
.venv/bin/python -m pytest simaas/tests/ -v -m "aws_only"
```

## Async Tests

Some tests use `pytest-asyncio`:

```bash
# Run async tests
.venv/bin/python -m pytest simaas/tests/test_p2p.py -v
```

**Patterns**:
- Use `@pytest.mark.asyncio` decorator for async test functions
- Use `run_coro_safely()` from `simaas.core.async_helpers` when calling coroutines from sync context

## Test Waves

The suite is organised into *waves*: ordered groups that run cheapest-first and
respect the dependencies between tiers. Waves are a convention, not a pytest
marker - run one by passing its files (commands below).

Two things drive the ordering. First, the **Docker boundary**: waves 1-3 need no
containers at all, so they run anywhere, while waves 4-6 are gated on
`docker_available`. Second, **Wave 0 builds the processor images that waves 5 and
6 deploy** - without them `fixture_rti.py` refuses to deploy and those waves fail.

| Wave | Contents | Tests | Approx. time | Requires |
|------|----------|-------|--------------|----------|
| **0 - Images** (opt-in) | `test_cli_image.py` | 5 | ~20 min | Docker |
| **1 - Unit** | `test_unit_core.py`, `test_unit_helpers.py`, `test_errors.py`, `test_logging.py`, `test_cli_helpers.py`, `test_proc_worker.py` | 176 | ~15 s | nothing |
| **2 - Services** | `test_dor.py`, `test_nodedb.py`, `test_p2p.py`, `test_rest.py`, `test_rest_errors.py` | 67 | ~2.5 min | in-process nodes |
| **3 - CLI** | `test_cli_dor.py`, `test_cli_identity.py`, `test_cli_runner.py` | 20 | ~1 min | in-process nodes |
| **4 - Node/Docker** | `test_namespace.py`, `test_cli_gateway.py`, `test_cli_misc.py`, `test_fork_safety.py` | 27 | ~1.5 min | Docker |
| **5 - RTI** | `test_rti.py`, `test_rti_2node.py`, `test_cli_rti.py` | 40 | ~9 min | Wave 0 images |
| **6 - Processors** | `test_processor_*.py` (6 files) | 50 | ~4 min | Wave 0 images |

Not in any wave: `test_ssh_tunnel.py` (4 tests) - AWS-only, skips without
`SSH_TUNNEL_*` credentials.

**Wave 0 is opt-in.** It rebuilds all 8 processor images from scratch
(`force_build=True`) and takes far longer than every other wave combined. Skip it
and reuse cached images unless you changed a processor, a `Dockerfile`, or
middleware code that gets baked into the images (see *Processor Docker Images*
below).

### Running a wave

```bash
# Wave 0 - rebuild all processor images (slow, opt-in)
.venv/bin/python -m pytest simaas/tests/test_cli_image.py -v

# Wave 1 - unit; no Docker, no nodes. Fastest useful gate.
.venv/bin/python -m pytest simaas/tests/test_unit_core.py simaas/tests/test_unit_helpers.py \
    simaas/tests/test_errors.py simaas/tests/test_logging.py \
    simaas/tests/test_cli_helpers.py simaas/tests/test_proc_worker.py -v

# Wave 2 - services
.venv/bin/python -m pytest simaas/tests/test_dor.py simaas/tests/test_nodedb.py \
    simaas/tests/test_p2p.py simaas/tests/test_rest.py simaas/tests/test_rest_errors.py -v

# Wave 3 - CLI
.venv/bin/python -m pytest simaas/tests/test_cli_dor.py simaas/tests/test_cli_identity.py \
    simaas/tests/test_cli_runner.py -v

# Wave 4 - node behaviour requiring Docker
.venv/bin/python -m pytest simaas/tests/test_namespace.py simaas/tests/test_cli_gateway.py \
    simaas/tests/test_cli_misc.py simaas/tests/test_fork_safety.py -v

# Wave 5 - RTI (requires Wave 0 images)
.venv/bin/python -m pytest simaas/tests/test_rti.py simaas/tests/test_rti_2node.py \
    simaas/tests/test_cli_rti.py -v

# Wave 6 - processors (requires Wave 0 images)
.venv/bin/python -m pytest simaas/tests/test_processor_*.py -v
```

### Notes on individual waves

- **Wave 1** is the cheapest meaningful signal: 176 tests in about 30 seconds with
  no Docker and no node startup (measured: 14 s). `test_fork_safety.py` deliberately sits in Wave 4
  instead - it is Docker-gated and builds a `linux/amd64` image, which costs
  minutes on Apple Silicon under Rosetta.
- **Wave 5** parametrises `rti_context` over `[docker, aws]`. The `[aws]`
  variants skip unless AWS is configured, so a Docker-only run reports 11 skips.
- **Wave 6** contains `test_processor_kgraph_job`, which depends on the
  `blazegraph` fixture (an external container). It is known to fail
  intermittently under full-suite load while passing in isolation.

## Test Statistics

For current test timings, coverage details, and recommended timeouts, see `TEST_STATS.md` in the repository root.

**Summary**:
- **Total tests**: 389 collected (385 in waves 0-6, plus 4 AWS-only)
- **Total runtime**: ~18 minutes for waves 1-6; ~20 minutes more if Wave 0 runs
- **Coverage**: 82% (target: 80%)

> Note: `TEST_STATS.md` still reports 160 tests / ~19 minutes and is out of date.

## Processor Docker Images (PDIs)

Tests that execute processors inside Docker containers use cached Processor Docker Images (PDIs).
These images bake in parts of the middleware source code (e.g., `job_runner`, `sync.py`, P2P
service code).

If you modify any of these files, **the cached images contain the old code** and tests will fail
with confusing errors even though your fix is correct.

To fix this, remove stale images and rebuild:
```bash
# Remove cached processor images
docker images --format "{{.Repository}}:{{.Tag}}" | grep "^proc-" | xargs -r docker rmi -f

# Rebuild all PDIs
.venv/bin/python -m pytest simaas/tests/test_cli_image.py -v
```

## Notes

- **Docker Requirement**: RTI-related tests require Docker to be running
- **AWS Tests**: Run with mock/fallback behavior if AWS credentials are not configured
- **GitHub Credentials**: Required for tests that build processor images from GitHub
- **IDE Support**: Tests work in PyCharm and other IDEs with pytest support
