import io
import json
import os
import random
import shutil
import tarfile
import tempfile
import threading
import time

from typing import Optional, Tuple, Dict, List

import firecrest
from firecrest.v2 import Firecrest
from pydantic import BaseModel

from simaas.core.errors import ConfigurationError, ExceptionContent, ValidationError, OperationError, NotFoundError
from simaas.core.helpers import get_timestamp_now
from simaas.core.logging import get_logger
from simaas.dor.protocol import P2PLookupDataObject, P2PFetchDataObject
from simaas.dor.schemas import GitProcessorPointer, DataObject, ProcessorDescriptor
from simaas.helpers import docker_client, docker_find_image, docker_load_image, docker_get_exposed_ports
from simaas.p2p.base import P2PAddress
from simaas.rti.base import RTIServiceBase, DBJobInfo, DBDeployedProcessor
from simaas.rti.protocol import P2PInterruptJob
from simaas.rti.schemas import Processor, Job, JobStatus

log = get_logger('simaas.rti', 'rti')


class FirecRESTConfiguration(BaseModel):
    url: str
    token_uri: str
    client_id: str
    client_secret: str
    system: str
    base_path: str
    simaas_cli: str = 'simaas-cli'
    walltime: str = '01:00:00'
    account: Optional[str] = None
    partition: Optional[str] = None
    port_range: Tuple[int, int] = (7000, 7999)
    poll_interval: int = 5


REQUIRED_ENV = [
    'SIMAAS_F7T_URL', 'SIMAAS_F7T_TOKEN_URI', 'SIMAAS_F7T_CLIENT_ID', 'SIMAAS_F7T_CLIENT_SECRET',
    'SIMAAS_F7T_SYSTEM', 'SIMAAS_F7T_BASE_PATH'
]

# Slurm states after which the job will not run anymore
SLURM_TERMINAL_STATES = {
    'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY', 'NODE_FAIL', 'PREEMPTED', 'BOOT_FAIL',
    'DEADLINE'
}

# seconds to wait for a P2P interrupt to reach the runner before cancelling the Slurm job
INTERRUPT_TIMEOUT = 5

SIMAAS_ACTIVE_STATES ={JobStatus.State.UNINITIALISED, JobStatus.State.INITIALISED, JobStatus.State.RUNNING}


def get_default_f7t_config() -> Optional[FirecRESTConfiguration]:
    if not all(var in os.environ for var in REQUIRED_ENV):
        return None

    config = FirecRESTConfiguration(
        url=os.environ['SIMAAS_F7T_URL'],
        token_uri=os.environ['SIMAAS_F7T_TOKEN_URI'],
        client_id=os.environ['SIMAAS_F7T_CLIENT_ID'],
        client_secret=os.environ['SIMAAS_F7T_CLIENT_SECRET'],
        system=os.environ['SIMAAS_F7T_SYSTEM'],
        base_path=os.environ['SIMAAS_F7T_BASE_PATH'].rstrip('/'),
    )
    if 'SIMAAS_F7T_SIMAAS_CLI' in os.environ:
        config.simaas_cli = os.environ['SIMAAS_F7T_SIMAAS_CLI']
    if 'SIMAAS_F7T_WALLTIME' in os.environ:
        config.walltime = os.environ['SIMAAS_F7T_WALLTIME']
    if 'SIMAAS_F7T_ACCOUNT' in os.environ:
        config.account = os.environ['SIMAAS_F7T_ACCOUNT']
    if 'SIMAAS_F7T_PARTITION' in os.environ:
        config.partition = os.environ['SIMAAS_F7T_PARTITION']
    if 'SIMAAS_F7T_PORT_RANGE' in os.environ:
        lo, hi = os.environ['SIMAAS_F7T_PORT_RANGE'].split('-')
        config.port_range = (int(lo), int(hi))
    if 'SIMAAS_F7T_POLL_INTERVAL' in os.environ:
        config.poll_interval = int(os.environ['SIMAAS_F7T_POLL_INTERVAL'])
    return config


def extract_processor_from_image(image_name: str, destination: str) -> None:
    """
    Copies the processor implementation (/processor) out of a PDI image into a local directory. The same PDI that
    rti_docker runs as a container is run natively on the cluster.
    """
    with docker_client() as client:
        container = client.containers.create(image_name)
        try:
            stream, _ = container.get_archive('/processor')
            buffer = io.BytesIO(b''.join(stream))
        finally:
            container.remove(force=True)

    # the archive contains a top-level 'processor/' folder -> strip it
    os.makedirs(destination, exist_ok=True)
    with tarfile.open(fileobj=buffer) as tar:
        members = []
        for member in tar.getmembers():
            parts = member.name.split('/', 1)
            if len(parts) < 2 or not parts[1]:
                continue
            member.name = parts[1]
            members.append(member)
        tar.extractall(destination, members=members, filter='data')


def make_tarball(source: str, tarball_path: str) -> None:
    with tarfile.open(tarball_path, 'w:gz') as tar:
        for entry in sorted(os.listdir(source)):
            tar.add(os.path.join(source, entry), arcname=entry)


class FirecRESTRTIService(RTIServiceBase):
    def __init__(self, node, db_path: str, retain_job_history: bool = False, strict_deployment: bool = True,
                 f7t_config: Optional[FirecRESTConfiguration] = None):
        super().__init__(
            node=node, db_path=db_path, retain_job_history=retain_job_history, strict_deployment=strict_deployment
        )

        self._config = f7t_config if f7t_config else get_default_f7t_config()
        if self._config is None:
            raise ConfigurationError(setting=str(REQUIRED_ENV), hint='environment variables required')

        # the auth object caches the OIDC token, so it is shared by all clients
        self._auth = firecrest.ClientCredentialsAuth(
            self._config.client_id, self._config.client_secret, self._config.token_uri
        )
        log.info('init', 'Using FirecREST', url=self._config.url, system=self._config.system,
                 base_path=self._config.base_path)

        # background thread that reconciles Sim-aaS job state with the Slurm job state
        self._monitor_stop = threading.Event()
        self._monitor = threading.Thread(target=self._monitor_jobs, daemon=True)
        self._monitor.start()

    @classmethod
    def plugin_name(cls) -> str:
        return 'firecrest'

    def type(self) -> str:
        return 'firecrest'

    def _client(self) -> Firecrest:
        return Firecrest(firecrest_url=self._config.url, authorization=self._auth)

    def _remote_proc_path(self, proc_id: str) -> str:
        return f"{self._config.base_path}/procs/{proc_id}"

    def _remote_job_path(self, job_id: str) -> str:
        return f"{self._config.base_path}/jobs/{job_id}"

    def _remote_rm(self, path: str) -> None:
        try:
            self._client().rm(self._config.system, path)
        except Exception as e:
            log.warning('remote', 'Failed to remove remote path', path=path, error=str(e))

    def _slurm_state(self, f7t_job_id: str) -> Optional[str]:
        info = self._client().job_info(self._config.system, f7t_job_id)
        # Slurm may qualify the state, e.g. 'CANCELLED by 1002' -> keep only the state itself
        return info[0]['status']['state'].split()[0] if info else None

    def _slurm_job_running(self, f7t_job_id: str) -> bool:
        state = self._slurm_state(f7t_job_id)
        return state is not None and state not in SLURM_TERMINAL_STATES

    def perform_deploy(self, proc: Processor) -> None:
        local_path = None
        try:
            # search the network for the processor docker image data object and fetch it
            protocol = P2PLookupDataObject(self._node)
            custodian = None
            proc_obj: Optional[DataObject] = None
            for node in [node for node in self._node.db.get_network() if node.has_dor()]:
                result: Dict[str, DataObject] = protocol.perform(node, [proc.id])
                proc_obj = result.get(proc.id)
                if proc_obj:
                    custodian = node
                    break

            # not found?
            if proc_obj is None:
                raise NotFoundError(resource_type='processor', resource_id=proc.id, hint='not found in DOR(s)')

            # determine the image name
            image_name = proc_obj.tags.get('image_name')
            if image_name is None:
                raise ValidationError(field='tags.image_name', hint='missing from processor data object')

            # make sure the PDI is loaded locally (we only need it to extract the processor code)
            if not docker_find_image(image_name):
                if proc_obj.data_format != 'tar':
                    raise ValidationError(field='data_format', expected='tar', actual=proc_obj.data_format,
                                          hint='rti_firecrest requires a PDI')

                meta_path = os.path.join(self._procs_path, f"{proc.id}.meta")
                content_path = os.path.join(self._procs_path, f"{proc.id}.content")
                P2PFetchDataObject(self._node).perform(custodian, proc.id, meta_path, content_path)
                if docker_load_image(content_path, image_name) is None:
                    raise OperationError(operation='docker_load', stage='verify',
                                         cause=f'image {image_name} not found after load')

            # extract the processor and add the GPP descriptor required by the job runner
            gpp = GitProcessorPointer(
                repository=proc_obj.tags['repository'],
                commit_id=proc_obj.tags['commit_id'],
                proc_path=proc_obj.tags['proc_path'],
                proc_descriptor=ProcessorDescriptor.model_validate(proc_obj.tags['proc_descriptor'])
            )
            local_path = tempfile.mkdtemp(prefix=f"f7t-{proc.id[:8]}-", dir=self._procs_path)
            proc_path = os.path.join(local_path, 'processor')
            extract_processor_from_image(image_name, proc_path)
            with open(os.path.join(proc_path, 'gpp.json'), 'w') as f:
                json.dump(gpp.model_dump(), f, indent=2)

            # upload to the cluster and unpack it there
            tarball_path = os.path.join(local_path, 'processor.tar.gz')
            make_tarball(proc_path, tarball_path)

            client = self._client()
            remote_path = self._remote_proc_path(proc.id)
            log.info('deploy', 'Uploading processor to cluster', proc=proc.id, system=self._config.system,
                     path=remote_path, size=os.path.getsize(tarball_path))
            client.mkdir(self._config.system, remote_path, create_parents=True)
            client.upload(self._config.system, tarball_path, remote_path, 'processor.tar.gz',
                          account=self._config.account)
            client.extract(self._config.system, f"{remote_path}/processor.tar.gz", remote_path,
                           account=self._config.account)
            self._remote_rm(f"{remote_path}/processor.tar.gz")

            # update processor object
            proc.state = Processor.State.READY
            proc.image_name = image_name
            proc.ports = docker_get_exposed_ports(image_name)
            proc.gpp = gpp
            self.update_proc_db(proc)
            log.info('deploy', 'Processor deployed on cluster', proc=proc.id, path=remote_path)

        except Exception as e:
            log.error('deploy', 'Deployment failed', exc=e, proc=proc.id)

            proc.state = Processor.State.FAILED
            proc.error = str(e)
            self.update_proc_db(proc)

        finally:
            if local_path:
                shutil.rmtree(local_path, ignore_errors=True)

    def perform_undeploy(self, proc: Processor, keep_image: bool = True) -> None:
        self._remote_rm(self._remote_proc_path(proc.id))

        # remove the record from the db - no lock needed, session is per-call
        with self._session_maker() as session:
            record = session.get(DBDeployedProcessor, proc.id)
            if record:
                session.delete(record)
                session.commit()
            else:
                log.warning('undeploy', 'DB record not found for removal', proc=proc.id)

    def _custodian_address(self) -> str:
        # the address the job runner on the cluster uses to call home: the node's advertised P2P address, unless
        # the cluster reaches the node under a different host name (e.g. through a tunnel or NAT)
        if "SIMAAS_CUSTODIAN_HOST" in os.environ:
            return f"tcp://{os.environ['SIMAAS_CUSTODIAN_HOST']}:{self._node.p2p.port()}"
        else:
            return self._node.p2p.address()

    def _job_script(self, job: Job, proc: Processor, port: int) -> str:
        job_path = self._remote_job_path(job.id)
        directives = [
            f"#SBATCH --job-name=simaas-{job.id}",
            f"#SBATCH --output={job_path}/runner.log",
            f"#SBATCH --error={job_path}/runner.log",
            "#SBATCH --nodes=1",
            f"#SBATCH --time={self._config.walltime}",
        ]
        if job.task.budget is not None:
            directives.append(f"#SBATCH --cpus-per-task={job.task.budget.vcpus}")
            directives.append(f"#SBATCH --mem={job.task.budget.memory}M")
        if self._config.account:
            directives.append(f"#SBATCH --account={self._config.account}")
        if self._config.partition:
            directives.append(f"#SBATCH --partition={self._config.partition}")

        return "\n".join([
            "#!/bin/bash",
            *directives,
            "",
            f"export JOB_ID={job.id}",
            f"export SIMAAS_CUSTODIAN_ADDRESS={self._custodian_address()}",
            "export SIMAAS_CUSTODIAN_TLS_CERT=\"$(cat <<'SIMAAS_EOF'",
            self._node.identity.tls_cert.strip(),
            "SIMAAS_EOF",
            ")\"",
            # the runner advertises tcp://$HOSTNAME:<port> as its P2P address
            "export HOSTNAME=$(hostname)",
            "export EXTERNAL_P2P_ADDRESS=HOSTNAME",
            "",
            f"exec {self._config.simaas_cli} --log-console run \\",
            f"    --job-path {job_path} \\",
            f"    --proc-path {self._remote_proc_path(proc.id)} \\",
            f"    --service-address tcp://0.0.0.0:{port}",
            "",
        ])

    def _perform_submit(self, job: Job, proc: Processor, submitted: Optional[List[Tuple[Job, str]]] = None) -> str:
        client = self._client()

        # create the job working directory on the cluster
        job_path = self._remote_job_path(job.id)
        client.mkdir(self._config.system, job_path, create_parents=True)

        # submit the job to Slurm
        port = random.randint(*self._config.port_range)
        result = client.submit(
            self._config.system, working_dir=job_path, script_str=self._job_script(job, proc, port),
            account=self._config.account, partition=self._config.partition
        )
        f7t_job_id = str(result['jobId'])

        if submitted is not None:
            # keep information to terminate if necessary
            submitted.append((job, f7t_job_id))

        # update the runner information
        with self._session_maker() as session:
            record = session.get(DBJobInfo, job.id)
            record.runner['f7t_job_id'] = f7t_job_id
            record.runner['f7t_job_path'] = job_path
            record.runner['f7t_port'] = port
            session.commit()

        return f7t_job_id

    def perform_submit_single(self, job: Job, proc: Processor) -> None:
        f7t_job_id = None
        try:
            f7t_job_id = self._perform_submit(job, proc)
            log.info('submit', 'Single job submitted', proc=proc.id, job=job.id, f7t_job=f7t_job_id)

        except Exception:
            if f7t_job_id:
                log.error('submit', 'Submission failed, cancelling Slurm job', proc=proc.id, job=job.id,
                          f7t_job=f7t_job_id)
                self._client().cancel_job(self._config.system, f7t_job_id)
            else:
                log.error('submit', 'Submission failed', proc=proc.id, job=job.id)

            raise

    def perform_submit_batch(self, batch: List[Tuple[Job, JobStatus, Processor]], batch_id: str) -> None:
        submitted: List[Tuple[Job, str]] = []
        for job, status, proc in batch:
            try:
                f7t_job_id = self._perform_submit(job, proc, submitted=submitted)
                log.info('submit', 'Batch job submitted', batch=batch_id, proc=proc.id, job=job.id,
                         f7t_job=f7t_job_id)

            except Exception:
                log.error('submit', 'Batch job submission failed', batch=batch_id, proc=proc.id, job=job.id)

                # Something went wrong, cancel already submitted Slurm jobs so there are no zombies from this batch.
                for submitted_job, f7t_job_id in submitted:
                    log.info('submit', 'Cancelling zombie Slurm job', batch=batch_id, job=submitted_job.id,
                             f7t_job=f7t_job_id)
                    try:
                        self._client().cancel_job(self._config.system, f7t_job_id)
                    except Exception as e:
                        log.warning('submit', 'Cancelling Slurm job failed', f7t_job=f7t_job_id, error=str(e))

                raise

    def perform_cancel(self, job_id: str, peer_address: Optional[P2PAddress], grace_period: int = 30) -> None:
        runner_iid: str = None
        try:
            # mark cancelled in DB first (state is correct even if we crash later)
            self.mark_job_cancelled(job_id)

            # get Slurm job id and runner identity (quick read, don't hold session)
            with self._session_maker() as session:
                record = session.get(DBJobInfo, job_id)
                f7t_job_id = record.runner.get('f7t_job_id') if record else None
                if record and record.runner.get('identity'):
                    runner_iid = record.runner['identity'].get('id')

            if not f7t_job_id:
                return

            # send P2P interrupt (best effort - the node often cannot reach compute nodes, in which case the
            # attempt may hang on DNS/connect timeouts, so it is bounded and done in a separate thread)
            interrupted = threading.Event()
            if peer_address:
                def _interrupt():
                    try:
                        P2PInterruptJob.perform(peer_address, max_attempts=1)
                        interrupted.set()
                    except Exception:
                        pass

                threading.Thread(target=_interrupt, daemon=True).start()
                if not interrupted.wait(INTERRUPT_TIMEOUT):
                    log.warning('cancel', 'P2P interrupt not delivered, cancelling Slurm job directly', job=job_id)

            # if the runner got the interrupt, give it a grace period to stop by itself
            if interrupted.is_set():
                deadline = get_timestamp_now() + grace_period * 1000
                while get_timestamp_now() < deadline:
                    if not self._slurm_job_running(f7t_job_id):
                        break
                    time.sleep(1)

            # force cancel if still running
            if self._slurm_job_running(f7t_job_id):
                log.info('cancel', 'Cancelling Slurm job', job=job_id, f7t_job=f7t_job_id)
                self._client().cancel_job(self._config.system, f7t_job_id)

            # delete the runner identity (always, regardless of retain_job_history)
            if runner_iid:
                log.info('cancel', 'Deleting runner identity', job=job_id, runner_iid=runner_iid)
                self._node.db.delete_identity(runner_iid)

        except Exception as e:
            log.error('cancel', 'Job cancellation failed', exc=e, job=job_id)

        finally:
            self.on_cancellation_worker_done(job_id)

    def perform_purge(self, record: DBJobInfo) -> None:
        f7t_job_id = record.runner.get('f7t_job_id')
        if f7t_job_id:
            try:
                self._client().cancel_job(self._config.system, f7t_job_id)
            except Exception:
                log.warning('purge', 'Cancelling Slurm job failed', job=record.id, f7t_job=f7t_job_id)

        if not self.retain_job_history:
            self._remote_rm(self._remote_job_path(record.id))

    def perform_job_cleanup(self, job_id: str) -> None:
        try:
            with self._session_maker() as session:
                record: DBJobInfo = session.get(DBJobInfo, job_id)
                runner_iid = record.runner.get('identity', {}).get('id') if record else None
                f7t_job_id = record.runner.get('f7t_job_id') if record else None

            # only clean up the job folder if we are not supposed to keep the job history
            if not self.retain_job_history and f7t_job_id:
                # wait for the Slurm job to finish (the runner may still be pushing outputs)
                while self._slurm_job_running(f7t_job_id):
                    time.sleep(self._config.poll_interval)

                log.info('cleanup', 'Deleting remote job folder', job=job_id)
                self._remote_rm(self._remote_job_path(job_id))

            # delete the runner identity (always, regardless of retain_job_history)
            if runner_iid:
                log.info('cleanup', 'Deleting runner identity', job=job_id, runner_iid=runner_iid)
                self._node.db.delete_identity(runner_iid)

        except Exception as e:
            log.error('cleanup', 'Job cleanup failed', exc=e, job=job_id)

        finally:
            self.on_cleanup_worker_done(job_id)

    def resolve_port_mapping(self, job_id: str, runner_details: dict) -> dict:
        # the runner reports its P2P address (tcp://<compute node>:<port>). other exposed ports are assumed to be
        # reachable on the same host under the same port number.
        ports: Dict[str, Optional[str]] = runner_details['ports']
        ext_host = runner_details['address'].split("://", 1)[-1].split(":", 1)[0]
        for local, external in ports.items():
            if local == '6000/tcp':
                ports[local] = runner_details['address']
            elif external is None:
                port, protocol = local.split("/")
                ports[local] = f"{protocol}://{ext_host}:{port}"
        return ports

    def _monitor_jobs(self) -> None:
        """
        The job runner reports status updates via P2P. If it dies before it can report (e.g., killed by Slurm due to
        walltime or memory limits, or it never starts), the Sim-aaS job would stay active forever. This loop compares
        active Sim-aaS jobs with their Slurm state and fails those whose Slurm job has ended.
        """
        # Slurm job ids seen in a terminal state while the Sim-aaS job was still active. Jobs are only failed
        # when this is seen twice in a row, to give the runner time to push its final status.
        suspects: Dict[str, str] = {}

        while not self._monitor_stop.wait(self._config.poll_interval):
            try:
                with self._session_maker() as session:
                    active = []
                    for record in session.query(DBJobInfo).all():
                        f7t_job_id = record.runner.get('f7t_job_id')
                        if f7t_job_id and JobStatus.State(record.status['state']) in SIMAAS_ACTIVE_STATES:
                            active.append((record.id, f7t_job_id, record.runner.get('f7t_job_path')))

                for job_id, f7t_job_id, job_path in active:
                    state = self._slurm_state(f7t_job_id)
                    if state is None or state not in SLURM_TERMINAL_STATES:
                        suspects.pop(job_id, None)
                        continue

                    if job_id not in suspects:
                        suspects[job_id] = state
                        continue

                    suspects.pop(job_id, None)
                    self._fail_lost_job(job_id, f7t_job_id, state, job_path)

            except Exception as e:
                log.warning('monitor', 'Job monitoring iteration failed', error=str(e))

    def _fail_lost_job(self, job_id: str, f7t_job_id: str, slurm_state: str, job_path: Optional[str]) -> None:
        # grab the end of the runner log to make the failure diagnosable
        log_tail = None
        if job_path:
            try:
                log_tail = self._client().tail(self._config.system, f"{job_path}/runner.log", num_lines=30)['content']
            except Exception:
                pass

        log.warning('monitor', 'Slurm job ended without final status from runner', job=job_id,
                    f7t_job=f7t_job_id, slurm_state=slurm_state)

        status = self.get_job_status(job_id)
        status.state = JobStatus.State.CANCELLED if slurm_state == 'CANCELLED' else JobStatus.State.FAILED
        status.errors.append(JobStatus.Error(
            message=f"Slurm job {f7t_job_id} ended in state {slurm_state} without the runner reporting completion",
            exception=ExceptionContent(
                id='none', reason='slurm_job_lost',
                details={'f7t_job_id': f7t_job_id, 'slurm_state': slurm_state, 'runner_log_tail': log_tail}
            )
        ))
        self.update_job_status(job_id, status)
