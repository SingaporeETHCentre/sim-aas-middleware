"""Integration tests for the P2P networking service."""

import asyncio
import json
import logging
import os
import tempfile
from typing import List, Dict
from unittest.mock import patch

import pytest

from simaas.core.helpers import get_timestamp_now
from simaas.core.identity import Identity
from simaas.core.keystore import Keystore
from simaas.core.logging import get_logger, initialise
from simaas.dor.api import DORProxy
from simaas.core.errors import NetworkError
from simaas.dor.protocol import P2PLookupDataObject, P2PFetchDataObject, P2PPushDataObject, P2PRelayPushDataObject
from simaas.dor.schemas import DataObject
from simaas.helpers import PortMaster
from simaas.node.base import Node
from simaas.node.default import DefaultNode
from simaas.nodedb.api import NodeDBProxy
from simaas.plugins.builtins.dor_fs import FilesystemDORService
from simaas.nodedb.protocol import P2PJoinNetwork, P2PLeaveNetwork, P2PUpdateIdentity
from simaas.nodedb.schemas import NodeInfo
from simaas.p2p.base import P2PAddress
from simaas.core.errors import NetworkError as PeerUnavailableError  # Alias for backwards compat in tests
from simaas.p2p.protocol import P2PLatency, P2PThroughput

initialise(level=logging.DEBUG)
log = get_logger(__name__, 'test')


# ==============================================================================
# Module-level fixtures
# ==============================================================================

@pytest.fixture(scope="session")
def p2p_server(test_context) -> Node:
    """Create a session-scoped P2P server node for networking tests."""
    keystore: Keystore = Keystore.new('p2p_server')
    _node: Node = test_context.get_node(keystore, enable_rest=True, dor_plugin_class=FilesystemDORService, rti_plugin_class=None)
    _node.p2p.add(P2PLatency())
    _node.p2p.add(P2PThroughput())

    yield _node

    _node.shutdown()


@pytest.fixture(scope="session")
def p2p_client(test_context) -> Node:
    """Create a session-scoped P2P client node for networking tests."""
    keystore: Keystore = Keystore.new('p2p_client')
    _node: Node = test_context.get_node(keystore, enable_rest=False, dor_plugin_class=None, rti_plugin_class=None)

    yield _node

    _node.shutdown()


# ==============================================================================
# P2P Tests
# ==============================================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_p2p_latency(p2p_server, p2p_client):
    """Test P2P latency measurement between peers."""
    try:
        peer_address = P2PAddress(
            address=p2p_server.p2p.address(),
            curve_secret_key=p2p_client.keystore.curve_secret_key(),
            curve_public_key=p2p_client.keystore.curve_public_key(),
            curve_server_key=p2p_server.identity.c_public_key
        )

        latency, attempt = await P2PLatency.perform(peer_address)
        print(f"latency: {latency} msec")
        print(f"attempt: {attempt}")

    except Exception:
        assert False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p2p_throughput(p2p_server, p2p_client):
    """Test P2P throughput measurement between peers."""
    try:
        peer_address = P2PAddress(
            address=p2p_server.p2p.address(),
            curve_secret_key=p2p_client.keystore.curve_secret_key(),
            curve_public_key=p2p_client.keystore.curve_public_key(),
            curve_server_key=p2p_server.identity.c_public_key
        )

        upload, download, attempt = await P2PThroughput.perform(peer_address, 100*1024*1024)
        print(f"upload: {upload:.2f} kB/s")
        print(f"download: {download:.2f} kB/s")
        print(f"attempt: {attempt}")

    except Exception:
        assert False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p2p_unreachable(p2p_server, p2p_client):
    """Test handling of unreachable P2P peers."""
    protocol = P2PUpdateIdentity(p2p_client)

    info: NodeInfo = p2p_server.info
    info.p2p_address = PortMaster.generate_p2p_address()

    try:
        await protocol.perform(info)
        assert False
    except PeerUnavailableError:
        assert True
    except Exception:
        assert False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p2p_update_identity(p2p_server, p2p_client):
    """Test P2P identity update protocol."""
    protocol = P2PUpdateIdentity(p2p_client)

    try:
        result: Identity = await protocol.perform(p2p_server.info)
        assert result.id == p2p_server.identity.id
    except Exception:
        assert False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p2p_join_leave_network(p2p_server, p2p_client):
    """Test P2P network join and leave operations."""
    networkS: List[NodeInfo] = await p2p_server.db.get_network()
    networkC: List[NodeInfo] = await p2p_client.db.get_network()
    assert len(networkS) == 1
    assert len(networkC) == 1

    # since we don't know anything about the peer yet, get some info first
    boot_node: NodeInfo = p2p_server.info

    protocol = P2PJoinNetwork(p2p_client)
    result: NodeInfo = await protocol.perform(boot_node)
    assert result.identity.id == p2p_server.identity.id

    networkS: List[NodeInfo] = await p2p_server.db.get_network()
    networkC: List[NodeInfo] = await p2p_client.db.get_network()
    assert len(networkS) == 2
    assert len(networkC) == 2

    protocol = P2PLeaveNetwork(p2p_client)
    await protocol.perform(blocking=True)

    networkS: List[NodeInfo] = await p2p_server.db.get_network()
    networkC: List[NodeInfo] = await p2p_client.db.get_network()
    assert len(networkS) == 1
    assert len(networkC) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p2p_lookup_fetch_data_object(p2p_server, p2p_client):
    """Test P2P data object lookup and fetch operations."""
    # client is supposed to be the owner of the data object -> make the server aware of the identity
    owner = p2p_client.identity
    nodedb = NodeDBProxy(p2p_server.rest.address())
    nodedb.update_identity(owner)

    # upload the data object
    with tempfile.TemporaryDirectory() as temp_dir:
        content_path = os.path.join(temp_dir, 'content.json')
        with open(content_path, 'w') as f:
            # noinspection PyTypeChecker
            json.dump({'v': 1}, f, indent=2)

        dor = DORProxy(p2p_server.rest.address())
        meta = dor.add_data_object(content_path, owner, False, False, 'JSONObject', 'json')
        obj_id = meta.obj_id

    # perform the lookup
    protocol = P2PLookupDataObject(p2p_client)
    result: Dict[str, DataObject] = await protocol.perform(p2p_server.info, [obj_id])
    assert len(result) == 1
    assert obj_id in result

    protocol = P2PFetchDataObject(p2p_client)

    with tempfile.TemporaryDirectory() as temp_dir:
        meta_path = os.path.join(temp_dir, 'meta.json')
        content_path = os.path.join(temp_dir, 'content.json')

        # perform a valid fetch
        try:
            meta: DataObject = await protocol.perform(p2p_server.info, obj_id, meta_path, content_path)
            assert meta.obj_id == obj_id
            assert os.path.isfile(meta_path)
            assert os.path.isfile(content_path)
        except Exception:
            assert False

        # perform an invalid fetch
        try:
            await protocol.perform(p2p_server.info, '01234', meta_path, content_path)
            assert False
        except NetworkError as e:
            assert 'data object not found' in e.details['reason']
        except Exception:
            assert False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p2p_fetch_restricted(p2p_server):
    """Test P2P fetch of restricted data objects with access control."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # create a fresh client node
        keystore = Keystore.new(f"keystore-{get_timestamp_now()}")
        client = DefaultNode(
            keystore, os.path.join(temp_dir, 'client_node'), enable_db=True,
            dor_plugin_class=FilesystemDORService, rti_plugin_class=None
        )
        p2p_address = PortMaster.generate_p2p_address()
        rest_address = PortMaster.generate_rest_address()
        await client.startup(p2p_address, rest_address=rest_address)
        await asyncio.sleep(1)

        # create an owner for the data object -> make the server aware of the identity
        owner = Keystore.new(f"owner-{get_timestamp_now()}")
        nodedb = NodeDBProxy(p2p_server.rest.address())
        nodedb.update_identity(owner.identity)

        # upload the data object
        content_path = os.path.join(temp_dir, 'content.json')
        with open(content_path, 'w') as f:
            # noinspection PyTypeChecker
            json.dump({'v': 1}, f, indent=2)

        dor = DORProxy(p2p_server.rest.address())
        meta = dor.add_data_object(content_path, owner.identity, True, False, 'JSONObject', 'json')
        obj_id = meta.obj_id

        protocol = P2PFetchDataObject(client)
        meta_path = os.path.join(temp_dir, 'meta.json')
        content_path = os.path.join(temp_dir, 'content.json')

        # try to fetch a data object that doesn't exist
        try:
            fake_obj_id = 'abcdef'
            await protocol.perform(p2p_server.info, fake_obj_id, meta_path, content_path)
            assert False
        except NetworkError as e:
            assert 'data object not found' in e.details['reason']
        except Exception:
            assert False

        # the client identity is not known to the server at this point to receive the data object
        try:
            await protocol.perform(p2p_server.info, obj_id, meta_path, content_path, user_iid=client.identity.id)
            assert False
        except NetworkError as e:
            assert 'user id not found' in e.details['reason']
        except Exception:
            assert False

        # update the server with the client identity
        await p2p_server.db.update_identity(client.identity)

        # the client does not have permission at this point to receive the data object
        try:
            await protocol.perform(p2p_server.info, obj_id, meta_path, content_path, user_iid=client.identity.id)
            assert False
        except NetworkError as e:
            assert 'user does not have access' in e.details['reason']
        except Exception:
            assert False

        # grant permission
        dor = DORProxy(p2p_server.rest.address())
        meta = dor.grant_access(obj_id, owner, client.identity)
        assert client.identity.id in meta.access

        # the client does not have a valid permission at this point to receive the data object
        try:
            token = f"{client.identity.id}:12343245"
            invalid_signature = client.keystore.sign(token.encode('utf-8'))

            await protocol.perform(p2p_server.info, obj_id, meta_path, content_path, user_iid=client.identity.id,
                                   user_signature=invalid_signature)
            assert False
        except NetworkError as e:
            assert 'authorisation failed' in e.details['reason']
        except Exception:
            assert False

        # create valid user signature
        token = f"{client.identity.id}:{obj_id}"
        signature = client.keystore.sign(token.encode('utf-8'))

        # the client does not have permission at this point to receive the data object
        try:
            await protocol.perform(p2p_server.info, obj_id, meta_path, content_path,
                                   user_iid=client.identity.id, user_signature=signature)
            assert meta.obj_id == obj_id
            assert os.path.isfile(meta_path)
            assert os.path.isfile(content_path)
        except NetworkError:
            assert False
        except Exception:
            assert False


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.asyncio
async def test_p2p_push_large_attachment_timeout(p2p_server, p2p_client):
    """Test that size-aware timeout prevents failure for large P2P pushes.

    Pushes a 500 MB payload with the old hardcoded 5 s default (size-aware
    scaling disabled via patch) and verifies the transfer fails.  Then
    re-enables size-aware scaling and verifies the same transfer succeeds.
    """
    import time

    owner = p2p_client.identity
    nodedb = NodeDBProxy(p2p_server.rest.address())
    nodedb.update_identity(owner)

    with tempfile.TemporaryDirectory() as temp_dir:
        content_path = os.path.join(temp_dir, 'payload.bin')
        with open(content_path, 'wb') as f:
            f.truncate(500 * 1024 * 1024)

        push_kwargs = dict(
            p2p_address=p2p_server.p2p.address(),
            keystore=p2p_client.keystore,
            peer=p2p_server.identity,
            content_path=content_path,
            data_type='BinaryObject',
            data_format='binary',
            owner_iid=owner.id,
            creators_iid=[owner.id],
            access_restricted=False,
            content_encrypted=False,
            license=DataObject.License(by=True, sa=True, nc=True, nd=True),
        )

        # disable size-aware scaling so the 5 s timeout applies directly
        with patch('simaas.p2p.base._THROUGHPUT_FLOOR', float('inf')):
            t0 = time.monotonic()
            with pytest.raises(NetworkError):
                await P2PPushDataObject.perform(**push_kwargs, timeout=5000)
            elapsed = time.monotonic() - t0
            print(f"push with 5 s timeout failed after {elapsed:.2f} s")

        # let the server finish processing the timed-out request
        await asyncio.sleep(2)

        # with size-aware timeout (default) the same transfer succeeds
        t0 = time.monotonic()
        meta = await P2PPushDataObject.perform(**push_kwargs)
        elapsed = time.monotonic() - t0
        print(f"push with size-aware timeout succeeded in {elapsed:.2f} s")
        assert meta is not None
        assert meta.obj_id is not None


# ==============================================================================
# P2PRelayPushDataObject — runner pushes via custodian when it cannot reach the
# actual target directly (e.g. cloud function, behind NAT). The custodian, with
# full peer connectivity, performs the downstream push on the runner's behalf.
# ==============================================================================


def _relay_push_kwargs(custodian, runner_keystore, target_iid, content_path):
    return dict(
        custodian_p2p_address=custodian.p2p.address(),
        keystore=runner_keystore,
        custodian_identity=custodian.identity,
        target_iid=target_iid,
        content_path=content_path,
        data_type='JSONObject',
        data_format='json',
        owner_iid=runner_keystore.identity.id,
        creators_iid=[runner_keystore.identity.id],
        access_restricted=False,
        content_encrypted=False,
        license=DataObject.License(by=True, sa=True, nc=True, nd=True),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p2p_relay_push_happy_path(p2p_server, p2p_client, test_context):
    """Relay-push from a runner through the custodian to a separate DOR-enabled target node."""
    # spin up a fresh DOR-enabled target and join it to the custodian's network so
    # the custodian's local NodeDB knows where to forward.
    target_keystore = Keystore.new(f"relay-target-happy-{get_timestamp_now()}")
    target_node: Node = test_context.get_node(
        target_keystore, enable_rest=True, dor_plugin_class=FilesystemDORService, rti_plugin_class=None
    )
    await P2PJoinNetwork(target_node).perform(p2p_server.info)
    await asyncio.sleep(1)

    # custodian needs to know about the runner's identity (it stamps owner/creators)
    await p2p_server.db.update_identity(p2p_client.identity)

    with tempfile.TemporaryDirectory() as temp_dir:
        content_path = os.path.join(temp_dir, 'payload.json')
        with open(content_path, 'w') as f:
            # noinspection PyTypeChecker
            json.dump({'v': 42, 'tag': 'happy'}, f)

        meta = await P2PRelayPushDataObject.perform(
            **_relay_push_kwargs(p2p_server, p2p_client.keystore, target_node.identity.id, content_path)
        )
        assert meta is not None
        assert meta.obj_id is not None

        # the object should live on the TARGET — the custodian only relayed
        target_meta = await target_node.dor.get_meta(meta.obj_id)
        assert target_meta is not None
        assert target_meta.obj_id == meta.obj_id

        custodian_meta = await p2p_server.dor.get_meta(meta.obj_id)
        assert custodian_meta is None, "custodian should not retain a copy when relaying"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p2p_relay_push_target_unknown(p2p_server, p2p_client):
    """Relay-push with a target_iid the custodian doesn't know about returns a clean error."""
    with tempfile.TemporaryDirectory() as temp_dir:
        content_path = os.path.join(temp_dir, 'payload.json')
        with open(content_path, 'w') as f:
            # noinspection PyTypeChecker
            json.dump({'v': 1}, f)

        try:
            await P2PRelayPushDataObject.perform(
                **_relay_push_kwargs(
                    p2p_server, p2p_client.keystore,
                    target_iid='not-a-real-iid-' + 'x' * 50,
                    content_path=content_path,
                )
            )
            assert False, "expected NetworkError"
        except NetworkError as e:
            assert 'target node not found in network' in e.details['reason']


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p2p_relay_push_target_no_dor(p2p_server, p2p_client, test_context):
    """Relay-push to a known target that lacks DOR returns a clean error (no 5s timeout)."""
    no_dor_keystore = Keystore.new(f"relay-target-no-dor-{get_timestamp_now()}")
    no_dor_target: Node = test_context.get_node(
        no_dor_keystore, enable_rest=False, dor_plugin_class=None, rti_plugin_class=None
    )
    await P2PJoinNetwork(no_dor_target).perform(p2p_server.info)
    await asyncio.sleep(1)

    with tempfile.TemporaryDirectory() as temp_dir:
        content_path = os.path.join(temp_dir, 'payload.json')
        with open(content_path, 'w') as f:
            # noinspection PyTypeChecker
            json.dump({'v': 2}, f)

        try:
            await P2PRelayPushDataObject.perform(
                **_relay_push_kwargs(
                    p2p_server, p2p_client.keystore, no_dor_target.identity.id, content_path
                )
            )
            assert False, "expected NetworkError"
        except NetworkError as e:
            assert 'does not support DOR' in e.details['reason']


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p2p_relay_push_target_is_custodian(p2p_server, p2p_client):
    """Relay-push where target == custodian: handler stores via the local DOR.add path."""
    # custodian needs to know about the runner identity
    await p2p_server.db.update_identity(p2p_client.identity)

    with tempfile.TemporaryDirectory() as temp_dir:
        content_path = os.path.join(temp_dir, 'payload.json')
        with open(content_path, 'w') as f:
            # noinspection PyTypeChecker
            json.dump({'v': 99, 'tag': 'self-relay'}, f)

        meta = await P2PRelayPushDataObject.perform(
            **_relay_push_kwargs(
                p2p_server, p2p_client.keystore, p2p_server.identity.id, content_path
            )
        )
        assert meta is not None
        assert meta.obj_id is not None

        custodian_meta = await p2p_server.dor.get_meta(meta.obj_id)
        assert custodian_meta is not None
        assert custodian_meta.obj_id == meta.obj_id


# ==============================================================================
# P2P concurrency tests
# ==============================================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_p2p_concurrent_requests_no_starvation(p2p_server, p2p_client):
    """Fire N concurrent requests against one server and assert all complete.

    Regression test for the bug where the P2P server processed requests
    serially: a burst of concurrent handshakes from spawned containers
    would time out because each was waiting behind the ones ahead of it.
    Under concurrent dispatch, the server should service all N requests in
    parallel (up to its semaphore cap) and none should raise NetworkError.
    """
    peer = P2PAddress(
        address=p2p_server.p2p.address(),
        curve_secret_key=p2p_client.keystore.curve_secret_key(),
        curve_public_key=p2p_client.keystore.curve_public_key(),
        curve_server_key=p2p_server.identity.c_public_key,
    )

    # 32 in parallel - well above the 14-way container spawn that triggered
    # the original deadlock, still well below the 64-permit handler cap.
    n = 32
    results = await asyncio.gather(
        *[P2PLatency.perform(peer, max_attempts=3) for _ in range(n)],
        return_exceptions=True,
    )

    failures = [(i, r) for i, r in enumerate(results) if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/{n} concurrent requests failed: {failures[:3]}"

    latencies_ms = [pair[0] for pair in results]
    log.info(f"32 concurrent P2P requests: latencies={[f'{x:.0f}ms' for x in sorted(latencies_ms)]}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p2p_slow_handler_does_not_block_fast(p2p_server, p2p_client):
    """A slow (large-payload) request must not delay a subsequent small one.

    With serial handling, the throughput-test request (upload+download of
    several MB) would hold the socket for its whole duration and every
    other client would wait for it. With concurrent handling, a small
    latency probe fired 100ms after the throughput probe should return
    well before the throughput probe does.
    """
    peer = P2PAddress(
        address=p2p_server.p2p.address(),
        curve_secret_key=p2p_client.keystore.curve_secret_key(),
        curve_public_key=p2p_client.keystore.curve_public_key(),
        curve_server_key=p2p_server.identity.c_public_key,
    )

    # 20 MB throughput probe (upload + download) will run for at least
    # several hundred ms even on localhost.
    slow_task = asyncio.create_task(P2PThroughput.perform(peer, 20 * 1024 * 1024))
    await asyncio.sleep(0.1)   # let it start

    t0 = asyncio.get_event_loop().time()
    fast_latency, _ = await P2PLatency.perform(peer, max_attempts=3)
    fast_elapsed_ms = (asyncio.get_event_loop().time() - t0) * 1000

    # Wait for the slow one so we can compare
    upload, download, _ = await slow_task
    slow_total_ms = 20 * 1024 / (upload + 1e-9) * 1000 + 20 * 1024 / (download + 1e-9) * 1000

    log.info(f"fast probe: {fast_elapsed_ms:.0f}ms; slow probe: {slow_total_ms:.0f}ms")
    # Fast probe should complete in a small fraction of the slow one's total.
    # Even a modest concurrency benefit makes this ratio << 1.
    assert fast_elapsed_ms < slow_total_ms / 2, (
        f"fast probe took {fast_elapsed_ms:.0f}ms, slow probe {slow_total_ms:.0f}ms - "
        "server may be serialising handlers"
    )
