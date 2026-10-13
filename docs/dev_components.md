# Component Reference

This document provides technical details for each major component in the Sim-aaS Middleware.

## Core Infrastructure (`simaas/core/`)

Provides foundational services for identity management, cryptography, and computational model integration.

### Identity Management

Cryptographic identities built from several key pairs:

```python
class Identity(BaseModel):
    """Record of public information about an identity."""
    id: str                      # random 64-character id, assigned at keystore creation
    name: str
    email: str
    s_public_key: str            # EC, for signature verification
    e_public_key: str            # RSA, for encryption
    tls_cert: str                # PEM-encoded self-signed X.509 cert, used for P2P
    nonce: int                   # incremented on every update; higher nonce wins
    signature: Optional[str]     # over the identity token (id, profile, nonce, public keys)
    last_seen: Optional[int]

    def verify(self, message: bytes, signature: str) -> bool:
        """Verify a signature against this identity's public signing key"""

    def verify_integrity(self) -> bool:
        """Verify this identity record's own signature"""
```

### Keystore Security

Hierarchical encryption with master key protection:

```python
# Required keystore assets. All except 'master-key' are encrypted with the master key.
assets = {
    'master-key':         MasterKeyPairAsset,      # RSA, encrypted with the user password
    'signing-key':        KeyPairAsset,            # EC, signs keystore content + identity tokens
    'encryption-key':     KeyPairAsset,            # RSA, encrypts/decrypts data payloads
    'tls-cert':           TLSCertAsset,            # self-signed X.509 cert + key, used for P2P
    'content-keys':       ContentKeysAsset,        # per-data-object symmetric keys
    'ssh-credentials':    SSHCredentialsAsset,     # Git repository access
    'github-credentials': GithubCredentialsAsset,  # GitHub API access
}
```

The TLS material is reached via `tls_cert_pem()`, `tls_key_pem()` and `tls_spki_hex()`; the
certificate is also published as part of the node's identity.

**Security Model**:
1. Master key encrypted with user password using PBKDF2
2. All other keys encrypted with master key
3. Thread-safe with mutex protection
4. Content keys are generated per data object (Fernet) and stored in the `content-keys` asset

### Processor Framework

Template method pattern with standardized lifecycle:

```python
class ProcessorBase(ABC):
    """Abstract base for all computational processors"""

    @abstractmethod
    def run(self, working_directory: str, job: Job,
            listener: ProgressListener, namespace: Namespace,
            secrets: Dict[str, str]) -> None:
        """Execute processor logic"""

    @abstractmethod
    def interrupt(self) -> None:
        """Cancel execution gracefully"""
```

---

## Data Object Repository (`simaas/dor/`)

Distributed storage system for immutable data objects with metadata, provenance tracking, and fine-grained access control.

### Data Object Model

Content-addressable storage with rich metadata:

```python
class DataObject:
    obj_id: str                    # UUID for object identification
    c_hash: str                    # SHA-256 content hash
    data_type: str                 # Semantic type (e.g., "ProcessorDockerImage")
    data_format: str               # Format specification (e.g., "json", "tar")

    # Access control
    owner_iid: str                 # Owner identity ID
    access_restricted: bool        # Whether access control applies
    access: List[str]              # List of identity IDs with access

    # Metadata and provenance
    created: CreationDetails
    tags: Dict[str, Any]
    recipe: DataObjectRecipe       # Provenance and creation details

    # Storage
    custodian: str                 # Node responsible for storage
    content_encrypted: bool
```

### Storage Architecture

- **Local Layer**: File system storage with hash-based naming, SQLAlchemy metadata, full-text search
- **Distributed Layer**: Automatic replication, P2P discovery, intelligent caching, eventual consistency

### Access Control

Owner-based permissions with explicit grants:

```python
if data_object.access_restricted:
    if requester_iid not in data_object.access:
        raise AccessDeniedError()
```

---

## Runtime Infrastructure (`simaas/rti/`)

Job execution engine for computational workloads with containerized processors and multi-platform deployment.

### Processor Lifecycle

```
[INACTIVE] → deploy() → [BUSY_DEPLOY] → [READY]
    ↑                                      ↓
    └── undeploy() ← [BUSY_UNDEPLOY] ← ──┘
```

### Job Execution Model

```python
class Task:
    proc_id: str                      # Processor to execute
    user_iid: str                     # Identity submitting task
    input: List[InputValue]           # Input data objects
    output: List[OutputSpecification] # Output requirements
    budget: ResourceDescriptor        # CPU/memory allocation
    secrets: List[str]                # Required environment secrets

class Job:
    id: str                          # Unique job identifier
    batch_id: str                    # Batch grouping
    task: Task
    custodian: NodeInfo              # Node managing execution
```

### Resource Management

```python
class ResourceDescriptor:
    vcpus: int = 1         # CPU allocation
    memory: int = 2048     # Memory in MB
    timeout: int = 3600    # Maximum execution time
```

### Execution Backends

**Docker (Local)**:
```python
container = docker_client.containers.run(
    image=processor_image,
    cpu_count=task.budget.vcpus,
    mem_limit=f"{task.budget.memory}m",
    detach=True
)
```

**AWS Batch (Cloud)**:
```python
response = batch_client.submit_job(
    jobName=f"simaas-{task.id}",
    jobQueue=self.job_queue,
    jobDefinition=self.job_definition
)
```

---

## P2P Networking (`simaas/p2p/`)

Peer-to-peer communication layer. Each message is a length-prefixed JSON header, optionally
followed by a streamed binary attachment, sent over a TLS 1.3 connection.

### Transport Security

Certificates are pinned rather than issued by a CA (`simaas/p2p/base.py`):

```python
build_client_ssl_context(peer_cert_pem)       # trusts the peer's published cert, and no other
build_server_ssl_context(cert_pem, key_pem)   # presents this node's own cert from its keystore
```

A node takes `peer_cert_pem` from the peer's `Identity.tls_cert`, so reaching a peer requires
already knowing its identity.

### Protocol Framework

```python
class P2PProtocol(ABC):
    """Base class for P2P protocols"""

    @abstractmethod
    def handle(self, request: BaseModel,
               attachment_path: Optional[str] = None,
               download_path: Optional[str] = None
               ) -> Tuple[Optional[BaseModel], Optional[str]]:
        """Process an incoming request; return (reply, optional reply attachment path)"""

    @staticmethod
    def request_type() -> BaseModel: ...

    @staticmethod
    def response_type() -> Optional[BaseModel]: ...
```

### Built-in Protocols

A node registers the protocols of whichever services it runs, via each service's
`get_p2p_protocols()`.

| Protocol | Wire name | Purpose |
|----------|-----------|---------|
| `P2PUpdateIdentity` | `nodedb-update-id` | Push an identity update to a peer |
| `P2PGetIdentity` | `nodedb-get-id` | Retrieve a single identity by id |
| `P2PGetNetwork` | `nodedb-get-network` | Retrieve a peer's view of the network |
| `P2PJoinNetwork` | `nodedb-join` | Exchange network snapshots when joining |
| `P2PLeaveNetwork` | `nodedb-leave` | Announce departure from the network |
| `P2PUpdateNamespaceBudget` | `nodedb-namespace-update` | Propagate a namespace budget change |
| `P2PReserveNamespaceResources` | `nodedb-namespace-reserve` | Reserve namespace resources for a job |
| `P2PCancelNamespaceReservation` | `nodedb-namespace-cancel` | Release a namespace reservation |
| `P2PLookupDataObject` | `dor-lookup` | Ask a peer which of the given objects it holds |
| `P2PFetchDataObject` | `dor-fetch` | Fetch object meta information and content |
| `P2PPushDataObject` | `dor-push` | Push an object into a peer's DOR |
| `P2PRelayPushDataObject` | `dor-relay-push` | Ask a custodian to relay a push to a third node |
| `P2PRunnerPerformHandshake` | `rti-runner-handshake` | Runner/custodian handshake at job start |
| `P2PPushJobStatus` | `rti-push-job-status` | Report job status back to the custodian |
| `P2PInterruptJob` | `rti-interrupt-job` | Cancel a running job (served by the runner) |
| `BatchBarrier` | `rti-batch-barrier` | Release a co-simulation barrier (served by the runner) |
| `P2PNamespaceServiceCall` | `namespace-service-call` | Signed, authorised call into a node's DOR/RTI |
| `P2PLatency` | `p2p-latency` | Round-trip latency probe |
| `P2PThroughput` | `p2p-throughput` | Throughput probe using an attachment |

`P2PNamespaceServiceCall` carries its own authorisation: the caller signs
`SHA256(service ‖ method ‖ canonical_json(args))`, and the recipient verifies the signature
against the caller's known identity before applying any per-method restrictions.

---

## Node Database (`simaas/nodedb/`)

Network registry and discovery service for maintaining information about network participants.

### Node Information Model

```python
class NodeInfo(BaseModel):
    identity: Identity                       # Node's cryptographic identity
    last_seen: int                           # Timestamp of last communication

    # Service capabilities: the backend in use, or 'none'
    dor_service: str                         # e.g. 'basic' / 'none'
    rti_service: str                         # e.g. 'docker' / 'none'

    # Network information
    p2p_address: str                         # e.g. 'tcp://127.0.0.1:4001'
    rest_address: Optional[Tuple[str, int]]  # None if REST is disabled

    # RTI-only flags
    retain_job_history: Optional[bool]
    strict_deployment: Optional[bool]

    def has_dor(self) -> bool: ...           # dor_service set and not 'none'
    def has_rti(self) -> bool: ...           # rti_service set and not 'none'
```

### Service Discovery

The NodeDB holds the node's view of the network; filter it with the `NodeInfo` helpers:

```python
network: List[NodeInfo] = node.db.get_network()

storage_nodes   = [n for n in network if n.has_dor()]
execution_nodes = [n for n in network if n.has_rti()]
```

---

## REST API (`simaas/rest/`)

HTTP-based API layer for external system integration using FastAPI with cryptographic authentication.

### Authentication

Requests are authenticated using cryptographic signatures derived from the caller's keystore.
There are no sessions, cookies, or JWTs — every request is independently verifiable.

**Required Headers**:

| Header | Value |
|--------|-------|
| `saasauth-iid` | The caller's identity ID |
| `saasauth-signature` | Signature over the request (see below) |

**Signature Generation**:

The signature covers both the request target and the body, preventing replay across different
endpoints or with modified payloads.

```python
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend
import canonicaljson

# 1. Build the digest: SHA-256 of "METHOD:URL" + canonical JSON body
digest = hashes.Hash(hashes.SHA256(), backend=default_backend())
digest.update(f"{method}:{full_url}".encode('utf-8'))
if body:
    digest.update(canonicaljson.encode_canonical_json(body))
token = digest.finalize()

# 2. Sign the digest with the keystore's EC signing key
signature = keystore.sign(token)

# 3. Set headers
headers = {
    'saasauth-iid': keystore.identity.id,
    'saasauth-signature': signature,
}
```

**Server-Side Verification** (`simaas/rest/auth.py`):

1. Look up the identity by `saasauth-iid` in the node database
2. Recompute the digest from `METHOD:URL` + body
3. Verify the signature against the identity's public signing key
4. Reject with 403 if the identity is unknown or the signature is invalid

**Using Proxy Classes**: The `DORProxy`, `RTIProxy`, and `NodeDBProxy` classes handle signature
generation automatically — pass a `Keystore` instance and authentication is transparent.
See `simaas/rest/proxy.py` for the implementation.

### Key Endpoints

All endpoints are prefixed with `/api/v1`.

**DOR Endpoints** (`/api/v1/dor`):

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/dor` | GET | Search for data objects |
| `/api/v1/dor/statistics` | GET | Get DOR statistics |
| `/api/v1/dor/add` | POST | Upload new data object |
| `/api/v1/dor/{obj_id}` | DELETE | Remove data object |
| `/api/v1/dor/{obj_id}/meta` | GET | Get object metadata |
| `/api/v1/dor/{obj_id}/content` | GET | Download object content |
| `/api/v1/dor/{c_hash}/provenance` | GET | Get provenance by content hash |
| `/api/v1/dor/{obj_id}/access/{user_iid}` | POST | Grant access to user |
| `/api/v1/dor/{obj_id}/access/{user_iid}` | DELETE | Revoke access from user |
| `/api/v1/dor/{obj_id}/owner/{new_owner_iid}` | PUT | Transfer ownership |
| `/api/v1/dor/{obj_id}/tags` | PUT | Update tags |
| `/api/v1/dor/{obj_id}/tags` | DELETE | Remove tags |

**RTI Endpoints** (`/api/v1/rti`):

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/rti/proc` | GET | List deployed processors |
| `/api/v1/rti/proc/{proc_id}` | GET | Get processor details |
| `/api/v1/rti/proc/{proc_id}/deploy` | POST | Deploy processor |
| `/api/v1/rti/proc/{proc_id}/undeploy` | POST | Undeploy processor |
| `/api/v1/rti/job` | GET | List jobs |
| `/api/v1/rti/job/submit` | POST | Submit job |
| `/api/v1/rti/job/{job_id}/status` | GET | Get job status |
| `/api/v1/rti/job/{job_id}/cancel` | POST | Cancel job |

**Other Endpoints**:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/db/node` | GET | List known network nodes |
| `/api/v1/docs` | GET | OpenAPI documentation |

---

## Command Line Interface (`simaas/cli/`)

Comprehensive CLI providing user-friendly access to all platform capabilities.

### Command Organization

```
simaas-cli
├── identity          # Identity and credential management
│   ├── create, remove, show, update, list
│   ├── discover, publish
│   └── credentials
│       ├── add (ssh, github)
│       ├── test (ssh, github)
│       ├── remove, list
├── service           # Start services
│   ├── node          # Start a Sim-aaS node instance
│   └── gateway       # Start a Gateway API service
├── run               # Job runner (used inside containers)
├── image             # Processor Docker Image (PDI) management
│   ├── build-local, build-github
│   └── import, export
├── dor               # Data Object Repository
│   ├── search, add, meta, download, remove
│   ├── tag, untag
│   └── access (grant, revoke, show)
├── rti               # Runtime Infrastructure
│   ├── volume (list, create, delete)
│   ├── proc (deploy, undeploy, list, show)
│   └── job (list, submit, status, inspect, logs, cancel)
├── namespace         # Namespace management
│   └── list, update, show
├── network           # Network operations
│   └── list, ping, status
├── node              # Node diagnostics
│   └── status, info
└── gateway           # Gateway administration
    ├── user (list, create, delete, enable, disable, publish)
    └── key (list, create, delete)
```

### Interactive Features

- Progressive disclosure with intelligent prompts
- Auto-discovery of keystores
- Interactive password prompts
- Rich progress indicators for long-running operations
- Support for both interactive and batch processing modes

---

## Node Lifecycle (`simaas/node/`)

The `Node` class is the central orchestrator that manages all services. Its API is
**fully synchronous**: the P2P and REST services run in their own daemon threads, and
every method below blocks until its work is done.

> Earlier releases exposed `startup()`, `join_network()`, `leave_network()`,
> `shutdown_rti()` and `update_identity()` as coroutines requiring `asyncio.run()` or
> `await`. As of 5.0.0 async has been removed from the codebase (uvicorn excepted) and
> those calls are plain method calls.

### Methods

```python
def startup(self, p2p_address: str, rest_address: Tuple[str, int] = None,
            bind_all_address: bool = False, wait_until_ready: bool = True) -> None:
    """Start P2P and REST daemon threads, wait for services to be ready."""

def join_network(self, boot_node_address: Tuple[str, int]) -> None:
    """Join the P2P network via a boot node."""

def leave_network(self, blocking: bool = False) -> None:
    """Inform peers and leave the network."""

def shutdown_rti(self, timeout: int = 60) -> None:
    """Undeploy all processors and wait for workers to finish."""

def update_identity(self, name: str = None, email: str = None,
                    propagate: bool = True) -> Identity:
    """Update identity and optionally broadcast to peers."""

def shutdown(self) -> None:
    """Stop P2P and REST daemon threads."""
```

### Usage

```python
from simaas.node.default import DefaultNode
from simaas.core.keystore import Keystore

# Create and configure node
keystore = Keystore.load("path/to/keystore", password="secret")
node = DefaultNode(keystore, "path/to/datastore", enable_db=True,
                   dor_plugin_class=FilesystemDORService,
                   rti_plugin_class=DockerRTIService)

# Start daemon services
node.startup("tcp://0.0.0.0:4000", rest_address=("0.0.0.0", 5000))

# Join network (if connecting to an existing network)
node.join_network(("192.168.1.100", 5000))

try:
    ...  # application runs
finally:
    # Clean shutdown
    node.leave_network()
    node.shutdown_rti()
    node.shutdown()
```

### Design Rationale

The P2P and REST services run in their own daemon threads because they are
long-running servers: they continuously accept connections independent of
application logic, and isolating them keeps that work off the calling thread.

Keeping the Node API synchronous means callers need no event loop of their own.
The only async left in the codebase is inside uvicorn/FastAPI, which manages its
own loop within the REST daemon thread and does not surface to callers.
