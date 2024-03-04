#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""

This contains the TorchX Kubernetes Kueue Job scheduler which can be used to run TorchX
components on a Kubernetes cluster via Kueue.

Prerequisites
==============

The TorchX Kubernetes scheduler depends on Kueue.

"""
import json
import logging
import warnings
from dataclasses import dataclass
from datetime import datetime
from typing import (
    Any,
    cast,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Tuple,
    TYPE_CHECKING,
)

import torchx
import yaml
from torchx.schedulers.api import (
    AppDryRunInfo,
    DescribeAppResponse,
    filter_regex,
    ListAppResponse,
    Scheduler,
    split_lines,
    Stream,
)
from torchx.schedulers.ids import make_unique
from torchx.specs.api import (
    AppDef,
    AppState,
    BindMount,
    CfgVal,
    DeviceMount,
    macros,
    ReplicaState,
    ReplicaStatus,
    Role,
    RoleStatus,
    runopts,
    VolumeMount,
)
from torchx.util.strings import normalize_str
from torchx.workspace.docker_workspace import DockerWorkspaceMixin
from typing_extensions import TypedDict

if TYPE_CHECKING:
    from docker import DockerClient
    from kubernetes.client import ApiClient, BatchV1Api, CoreV1Api, CustomObjectsApi
    from kubernetes.client.models import (  # noqa: F401 imported but unused
        V1Container,
        V1Pod,
    )
    from kubernetes.client.rest import ApiException

logger: logging.Logger = logging.getLogger(__name__)

# Kubernetes reserves a small amount of resources per host for the system. For
# TorchX we always assume the entire host is being requested so we adjust the
# requested numbers account for the node reserved resources.
#
# https://kubernetes.io/docs/tasks/administer-cluster/reserve-compute-resources/
RESERVED_MILLICPU = 100
RESERVED_MEMMB = 1024

JOB_STATE: Dict[str, AppState] = {
    # Pending is the phase that job is pending in the queue, waiting for
    # scheduling decision
    "Pending": AppState.PENDING,
    # Aborting is the phase that job is aborted, waiting for releasing pods
    "Aborting": AppState.RUNNING,
    # Aborted is the phase that job is aborted by user or error handling
    "Aborted": AppState.CANCELLED,
    # Running is the phase that minimal available tasks of Job are running
    "Running": AppState.RUNNING,
    # Restarting is the phase that the Job is restarted, waiting for pod
    # releasing and recreating
    "Restarting": AppState.RUNNING,
    # Completed is the phase that all tasks of Job are completed successfully
    "Completed": AppState.SUCCEEDED,
    # Terminating is the phase that the Job is terminated, waiting for releasing
    # pods
    "Terminating": AppState.RUNNING,
    # Teriminated is the phase that the job is finished unexpected, e.g. events
    "Terminated": AppState.FAILED,
    "Failed": ReplicaState.FAILED,
    "Suspended": AppState.SUSPENDED,
}

KUEUE_STATE: Dict[str, ReplicaState] = {
    # Kueue related States
    # JobSuspended is the state where Kueue has suspended the job
    "JobSuspended": ReplicaState.SUSPENDED,
    # JobResumed is the state where Kueue releases the Job
    "JobResumed": ReplicaState.RESUMED,
    # Unknown is the state where the Job state is unknown
    "Unknown": ReplicaState.UNKNOWN,
}

LABEL_VERSION = "torchx.pytorch.org/version"
LABEL_APP_NAME = "torchx.pytorch.org/app-name"
LABEL_ROLE_INDEX = "torchx.pytorch.org/role-index"
LABEL_ROLE_NAME = "torchx.pytorch.org/role-name"
LABEL_REPLICA_ID = "torchx.pytorch.org/replica-id"
LABEL_KUBE_APP_NAME = "app.kubernetes.io/name"
LABEL_ORGANIZATION = "app.kubernetes.io/managed-by"
LABEL_UNIQUE_NAME = "app.kubernetes.io/instance"

# Local Kueue and Priority class labels
LOCAL_QUEUE_LABEL = "kueue.x-k8s.io/queue-name"
PRIORITY_CLASS_LABEL = "kueue.x-k8s.io/priority-class"

ANNOTATION_ISTIO_SIDECAR = "sidecar.istio.io/inject"

LABEL_INSTANCE_TYPE = "node.kubernetes.io/instance-type"


def sanitize_for_serialization(obj: object) -> object:
    from kubernetes import client

    api = client.ApiClient()
    return api.sanitize_for_serialization(obj)


def role_to_pod(name: str, role: Role, service_account: Optional[str]) -> "V1Pod":
    from kubernetes.client.models import (  # noqa: F811 redefinition of unused
        V1Container,
        V1ContainerPort,
        V1EmptyDirVolumeSource,
        V1EnvVar,
        V1HostPathVolumeSource,
        V1ObjectMeta,
        V1PersistentVolumeClaimVolumeSource,
        V1Pod,
        V1PodSpec,
        V1ResourceRequirements,
        V1SecurityContext,
        V1Volume,
        V1VolumeMount,
    )

    # limits puts an upper cap on the resources a pod may consume.
    # requests is how much the scheduler allocates. We assume that the jobs will
    # be allocation the whole machine so requests is slightly lower than the
    # requested resources to account for the Kubernetes node reserved resources.
    limits = {}
    requests = {}

    resource = role.resource
    if resource.cpu > 0:
        mcpu = int(resource.cpu * 1000)
        limits["cpu"] = f"{mcpu}m"
        request_mcpu = max(mcpu - RESERVED_MILLICPU, 0)
        requests["cpu"] = f"{request_mcpu}m"
    if resource.memMB > 0:
        limits["memory"] = f"{int(resource.memMB)}M"
        request_memMB = max(int(resource.memMB) - RESERVED_MEMMB, 0)
        requests["memory"] = f"{request_memMB}M"
    if resource.gpu > 0:
        requests["nvidia.com/gpu"] = limits["nvidia.com/gpu"] = str(resource.gpu)

    for device_name, device_limit in resource.devices.items():
        requests[device_name] = str(device_limit)
        limits[device_name] = str(device_limit)

    resources = V1ResourceRequirements(
        limits=limits,
        requests=requests,
    )

    node_selector: Dict[str, str] = {}
    if LABEL_INSTANCE_TYPE in resource.capabilities:
        node_selector[LABEL_INSTANCE_TYPE] = resource.capabilities[LABEL_INSTANCE_TYPE]

    # To support PyTorch dataloaders we need to set /dev/shm to larger than the
    # 64M default so we mount an unlimited sized tmpfs directory on it.
    SHM_VOL = "dshm"
    volumes = [
        V1Volume(
            name=SHM_VOL,
            empty_dir=V1EmptyDirVolumeSource(
                medium="Memory",
            ),
        ),
    ]
    volume_mounts = [
        V1VolumeMount(name=SHM_VOL, mount_path="/dev/shm"),
    ]
    security_context = V1SecurityContext()

    for i, mount in enumerate(role.mounts):
        mount_name = f"mount-{i}"
        if isinstance(mount, BindMount):
            volumes.append(
                V1Volume(
                    name=mount_name,
                    host_path=V1HostPathVolumeSource(
                        path=mount.src_path,
                    ),
                )
            )
            volume_mounts.append(
                V1VolumeMount(
                    name=mount_name,
                    mount_path=mount.dst_path,
                    read_only=mount.read_only,
                )
            )
        elif isinstance(mount, VolumeMount):
            volumes.append(
                V1Volume(
                    name=mount_name,
                    persistent_volume_claim=V1PersistentVolumeClaimVolumeSource(
                        claim_name=mount.src,
                    ),
                )
            )
            volume_mounts.append(
                V1VolumeMount(
                    name=mount_name,
                    mount_path=mount.dst_path,
                    read_only=mount.read_only,
                )
            )
        elif isinstance(mount, DeviceMount):
            volumes.append(
                V1Volume(
                    name=mount_name,
                    host_path=V1HostPathVolumeSource(
                        path=mount.src_path,
                    ),
                )
            )
            volume_mounts.append(
                V1VolumeMount(
                    name=mount_name,
                    mount_path=mount.dst_path,
                    read_only=(
                        "w" not in mount.permissions and "m" not in mount.permissions
                    ),
                )
            )
            security_context.privileged = True
        else:
            raise TypeError(f"unknown mount type {mount}")

    container = V1Container(
        command=[role.entrypoint] + role.args,
        image=role.image,
        name=name,
        env=[
            V1EnvVar(
                name=name,
                value=value,
            )
            for name, value in role.env.items()
        ],
        resources=resources,
        ports=[
            V1ContainerPort(
                name=name,
                container_port=port,
            )
            for name, port in role.port_map.items()
        ],
        volume_mounts=volume_mounts,
        security_context=security_context,
    )

    return V1Pod(
        spec=V1PodSpec(
            containers=[container],
            restart_policy="Never",
            service_account_name=service_account,
            volumes=volumes,
            node_selector=node_selector,
        ),
        metadata=V1ObjectMeta(
            annotations={
                # Disable the istio sidecar as it prevents the containers from
                # exiting once finished.
                ANNOTATION_ISTIO_SIDECAR: "false",
            },
            labels={},
        ),
    )


def app_to_resource(
    app: AppDef,
    service_account: Optional[str],
    local_queue: Optional[str] = None,
    kueue_priority_class: Optional[str] = None,
    annotations: Optional[dict] = None,
) -> Dict[str, object]:
    """
    app_to_resource creates a kubernetes batch job resource definition from
    the provided AppDef. The resource definition can be used to launch the
    app on Kubernetes.

    The local queue is a required variable to add to the job labels.
    kueue_priority_class is used to provide the workload priority class name see: https://kueue.sigs.k8s.io/docs/concepts/workload_priority_class/#how-to-use-workloadpriorityclass-on-jobs
    """
    unique_app_id = normalize_str(make_unique(app.name))
    for role_idx, role in enumerate(app.roles):
        for replica_id in range(role.num_replicas):
            values = macros.Values(
                img_root="",
                app_id=unique_app_id,
                replica_id=str(replica_id),
                rank0_env=f"KUEUE_JOB_{normalize_str(app.roles[0].name)}_0_HOSTS".upper(),
            )
            if role_idx == 0 and replica_id == 0:
                values.rank0_env = "TORCHX_RANK0_HOST"
            name = normalize_str(f"{role.name}-{replica_id}")
            replica_role = values.apply(role)
            if role_idx == 0 and replica_id == 0:
                replica_role.env["TORCHX_RANK0_HOST"] = "localhost"

            pod = role_to_pod(name, replica_role, service_account)
            pod.metadata.labels.update(
                pod_labels(
                    app=app,
                    role_idx=role_idx,
                    role=role,
                    replica_id=replica_id,
                    app_id=unique_app_id,
                    local_queue=local_queue,
                    kueue_priority_class=kueue_priority_class,
                )
            )
            task: Dict[str, Any] = {
                "replicas": 1,
                "name": name,
                "template": pod,
            }
            if role.max_retries > 0:
                task["backoffLimit"] = role.max_retries

            if role.min_replicas is not None:
                # first min_replicas tasks are required, afterward optional
                task["minAvailable"] = 1 if replica_id < role.min_replicas else 0

    resource: Dict[str, object] = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": f"{unique_app_id}"},
        "spec": task,
    }
    if annotations is not None:
        resource["metadata"]["annotations"] = annotations
    return resource


@dataclass
class KueueJob:
    images_to_push: Dict[str, Tuple[str, str]]
    resource: Dict[str, object]

    def __str__(self) -> str:
        return yaml.dump(sanitize_for_serialization(self.resource))

    def __repr__(self) -> str:
        return str(self)


class KueueJobOpts(TypedDict, total=False):
    namespace: Optional[str]
    image_repo: Optional[str]
    service_account: Optional[str]
    local_queue: Optional[str]
    kueue_priority_class: Optional[str]
    annotations: Optional[dict]


class KueueJobScheduler(DockerWorkspaceMixin, Scheduler[KueueJobOpts]):
    """
    KueueJobScheduler is a TorchX scheduling interface to Kubernetes that relies on Kueue.

    You can install Kueue here https://kueue.sigs.k8s.io/docs/installation/#install-a-released-version

    .. code-block:: bash

        $ pip install torchx[kueue_job]
        $ torchx run --scheduler kueue_job --scheduler_args namespace=default,local_queue="default-kueue",image_repo="user/alpine" utils.echo --image alpine:latest --msg hello
        kueue_job://torchx_user/1234
        $ torchx status kueue_job://torchx_user/1234
        ...

    **Config Options**

    .. runopts::
        class: torchx.schedulers.kueue_job_scheduler.create_scheduler

    **Mounts**

    Mounting external filesystems/volumes is via the HostPath and
    PersistentVolumeClaim support.

    * hostPath volumes: ``type=bind,src=<host path>,dst=<container path>[,readonly]``
    * PersistentVolumeClaim: ``type=volume,src=<claim>,dst=<container path>[,readonly]``
    * host devices: ``type=device,src=/dev/foo[,dst=<container path>][,perm=rwm]``
      If you specify a host device the job will run in privileged mode since
      Kubernetes doesn't expose a way to pass `--device` to the underlying
      container runtime. Users should prefer to use device plugins.

    See :py:func:`torchx.specs.parse_mounts` for more info.

    External docs: https://kubernetes.io/docs/concepts/storage/persistent-volumes/

    **Resources / Allocation**

    To select a specific machine type you can add a capability to your resources
    with ``node.kubernetes.io/instance-type`` which will constrain the launched
    jobs to nodes of that instance type.

    >>> from torchx import specs
    >>> specs.Resource(
    ...     cpu=4,
    ...     memMB=16000,
    ...     gpu=2,
    ...     capabilities={
    ...         "node.kubernetes.io/instance-type": "<cloud instance type>",
    ...     },
    ... )
    Resource(...)

    Kubernetes may reserve some memory for the host. TorchX assumes you're
    scheduling on whole hosts and thus will automatically reduce the resource
    request by a small amount to account for the node reserved CPU and memory.
    If you run into scheduling issues you may need to reduce the requested CPU
    and memory from the host values.

    **Compatibility**

    .. compatibility::
        type: scheduler
        features:
            cancel: true
            logs: true
            distributed: true
            describe: |
                Partial support. KueJobScheduler will return job and job suspension status but does not provide the complete original AppSpec.
            workspaces: true
            mounts: true
            elasticity: Requires Kueue >= v0.5.0
    """

    def __init__(
        self,
        session_name: str,
        client: Optional["ApiClient"] = None,
        docker_client: Optional["DockerClient"] = None,
    ) -> None:
        # NOTE: make sure any new init options are supported in create_scheduler(...)
        super().__init__("kueue_job", session_name, docker_client=docker_client)

        self._client = client

    def _api_client(self) -> "ApiClient":
        from kubernetes import client, config

        c = self._client
        if c is None:
            configuration = client.Configuration()
            try:
                config.load_kube_config(client_configuration=configuration)
            except config.ConfigException as e:
                warnings.warn(f"failed to load kube config: {e}")

            c = self._client = client.ApiClient(configuration)

        return c

    def _custom_objects_api(self) -> "CustomObjectsApi":
        from kubernetes import client

        return client.CustomObjectsApi(self._api_client())

    def _batchv1_api(self) -> "BatchV1Api":
        from kubernetes import client

        return client.BatchV1Api(self._api_client())

    def _corev1_api(self) -> "CoreV1Api":
        from kubernetes import client

        return client.CoreV1Api(self._api_client())

    def _get_job_name_from_exception(self, e: "ApiException") -> Optional[str]:
        try:
            return json.loads(e.body)["details"]["name"]
        except Exception as e:
            logger.exception("Unable to retrieve job name, got exception", e)
            return None

    def _get_active_context(self) -> Dict[str, Any]:
        from kubernetes import config

        contexts, active_context = config.list_kube_config_contexts()
        return active_context

    def schedule(self, dryrun_info: AppDryRunInfo[KueueJob]) -> str:
        from kubernetes.client.rest import ApiException

        cfg = dryrun_info._cfg
        assert cfg is not None, f"{dryrun_info} missing cfg"
        namespace = cfg.get("namespace") or "default"

        images_to_push = dryrun_info.request.images_to_push
        self.push_images(images_to_push)

        resource = dryrun_info.request.resource
        try:
            resp = self._batchv1_api().create_namespaced_job(
                namespace=namespace,
                body=resource,
            )

        except ApiException as e:
            if e.status == 409 and e.reason == "Conflict":
                job_name = self._get_job_name_from_exception(e)
                raise ValueError(
                    f"Job `{job_name}` already exists. This seems like a transient exception, try resubmitting job"
                ) from e
            else:
                raise

        return f"{namespace}:{resp.metadata.name}"

    def _submit_dryrun(self, app: AppDef, cfg: KueueJobOpts) -> AppDryRunInfo[KueueJob]:
        # map any local images to the remote image
        images_to_push = self.dryrun_push_images(app, cast(Mapping[str, CfgVal], cfg))

        service_account = cfg.get("service_account")
        assert service_account is None or isinstance(
            service_account, str
        ), "service_account must be a str"

        local_queue = cfg.get("local_queue")
        assert isinstance(
            local_queue, str
        ), "local_queue is a required string please specify local_queue in scheduler_args"

        kueue_priority_class = cfg.get("kueue_priority_class")
        assert kueue_priority_class is None or isinstance(
            kueue_priority_class, str
        ), "kueue_priority_class must be a str"

        annotations = cfg.get("annotations")
        assert annotations is None or isinstance(
            annotations, dict
        ), "annotations must be a dict"

        resource = app_to_resource(
            app, service_account, local_queue, kueue_priority_class, annotations
        )
        req = KueueJob(
            resource=resource,
            images_to_push=images_to_push,
        )
        return AppDryRunInfo(req, repr)

    def _validate(self, app: AppDef, scheduler: str) -> None:
        # Skip validation step
        pass

    def _cancel_existing(self, app_id: str) -> None:
        from kubernetes import client

        namespace, name = app_id.split(":")

        self._batchv1_api().delete_namespaced_job(
            namespace=namespace,
            name=name,
            body=client.V1DeleteOptions(propagation_policy="Foreground"),
        )

    def _run_opts(self) -> runopts:
        opts = runopts()
        opts.add(
            "namespace",
            type_=str,
            help="Kubernetes namespace to schedule Job in",
            default="default",
        )
        opts.add(
            "service_account",
            type_=str,
            help="The service account name to set on the pod specs",
        )
        opts.add(
            "local_queue",
            type_=str,
            help="The Local Kueue name to set on the local Kueue label",
        )
        opts.add(
            "kueue_priority_class",
            type_=str,
            help="The kueue priority class name to use for the priority class label",
        )
        opts.add(
            "annotations",
            type_=dict,
            help="The annotations to add to the job",
        )
        return opts

    def describe(self, app_id: str) -> Optional[DescribeAppResponse]:
        from kubernetes import client

        namespace, name = app_id.split(":")
        roles = {}
        roles_statuses = {}

        try:
            api_instance = self._batchv1_api()
            job = api_instance.read_namespaced_job_status(name, namespace)
        except client.ApiException as e:
            return f"Exception: {e}"
        try:
            status = job.status
        except Exception as e:
            print(f"Cannot gather job status: {e}")
            status = None

        if status:
            for condition in status.conditions or []:
                role, _, idx = job.metadata.name.rpartition("-")
                if condition.type == "Suspended":
                    if condition.reason == "JobSuspended":
                        app_state = JOB_STATE["Suspended"]
                        state = KUEUE_STATE["JobSuspended"]
                    elif condition.reason == "JobResumed":
                        state = KUEUE_STATE["JobResumed"]
                        if status.active is not None:
                            app_state = JOB_STATE["Running"]
                elif status.active is not None:
                    state = JOB_STATE["Running"]
                    app_state = JOB_STATE["Running"]
                elif condition.type == "Complete":
                    state = JOB_STATE["Completed"]
                    app_state = JOB_STATE["Completed"]
                elif condition.type == "Failed":
                    state = JOB_STATE["Failed"]
                    app_state = JOB_STATE["Failed"]
                else:
                    state = JOB_STATE["Pending"]
                    app_state = JOB_STATE["Pending"]

                if role not in roles:
                    roles[role] = Role(name=role, num_replicas=0, image="")
                    roles_statuses[role] = RoleStatus(role, [])

                roles[role].num_replicas += 1
                roles_statuses[role].replicas.append(
                    ReplicaStatus(id=0, role=role, state=state, hostname="")
                )
        else:
            app_state = AppState.UNKNOWN

        return DescribeAppResponse(
            app_id=app_id,
            roles=list(roles.values()),
            roles_statuses=list(roles_statuses.values()),
            state=app_state,
        )

    def log_iter(
        self,
        app_id: str,
        role_name: str,
        k: int = 0,
        regex: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        should_tail: bool = False,
        streams: Optional[Stream] = None,
    ) -> Iterable[str]:
        assert until is None, "kubernetes API doesn't support until"

        if streams not in (None, Stream.COMBINED):
            raise ValueError("KueueJobScheduler only supports COMBINED log stream")

        from kubernetes import client, watch

        namespace, name = app_id.split(":")

        pod_name = get_pod_name_from_job(self, job_name=name, namespace=namespace)
        if pod_name is None:
            raise ValueError("Pods not found. Is the Job Suspended?")

        args: Dict[str, object] = {
            "name": pod_name,
            "namespace": namespace,
            "timestamps": True,
        }
        if since is not None:
            args["since_seconds"] = (datetime.now() - since).total_seconds()

        core_api = client.CoreV1Api(self._api_client())
        if should_tail:
            w = watch.Watch()
            iterator = w.stream(core_api.read_namespaced_pod_log, **args)
        else:
            resp = core_api.read_namespaced_pod_log(**args)
            iterator = split_lines(resp)

        if regex:
            return filter_regex(regex, iterator)
        else:
            return iterator

    def list(self) -> List[ListAppResponse]:
        active_context = self._get_active_context()
        namespace = active_context["context"]["namespace"]
        resp = self._custom_objects_api().list_namespaced_custom_object(
            group="batch",
            version="v1",
            namespace=namespace,
            plural="job",
            timeout_seconds=30,
        )
        return [
            ListAppResponse(
                app_id=f"{namespace}:{app['metadata']['name']}",
                state=JOB_STATE[app["status"]["state"]["phase"]],
            )
            for app in resp["items"]
        ]


def get_pod_name_from_job(self, job_name, namespace):
    from kubernetes import client

    api_instance = self._batchv1_api()

    try:
        job = api_instance.read_namespaced_job(job_name, namespace)
    except client.ApiException as e:
        return f"Api Exception: {e}"

    selector = job.spec.selector.match_labels
    label = ",".join([f"{k}={v}" for k, v in selector.items()])

    api_instance = self._corev1_api()
    try:
        pods = api_instance.list_namespaced_pod(namespace, label_selector=label)
    except client.ApiException as e:
        return f"Api Exception {e}"

    if not pods.items:
        return None
    else:
        # Sort the list of pods by creation timestamp and get most recent one
        sorted_pods = sorted(
            pods.items, key=lambda p: str(p.metadata.creation_timestamp), reverse=True
        )
        most_recent_pod = sorted_pods[0].metadata.name

        return most_recent_pod


def create_scheduler(
    session_name: str,
    client: Optional["ApiClient"] = None,
    docker_client: Optional["DockerClient"] = None,
    **kwargs: Any,
) -> KueueJobScheduler:
    return KueueJobScheduler(
        session_name=session_name,
        client=client,
        docker_client=docker_client,
    )


def pod_labels(
    app: AppDef,
    role_idx: int,
    role: Role,
    replica_id: int,
    app_id: str,
    local_queue: str,
    kueue_priority_class: str,
) -> Dict[str, str]:

    labels = {
        LABEL_VERSION: torchx.__version__,
        LABEL_APP_NAME: app.name,
        LABEL_ROLE_INDEX: str(role_idx),
        LABEL_ROLE_NAME: role.name,
        LABEL_REPLICA_ID: str(replica_id),
        LABEL_KUBE_APP_NAME: app.name,
        LABEL_ORGANIZATION: "torchx.pytorch.org",
        LABEL_UNIQUE_NAME: app_id,
        LOCAL_QUEUE_LABEL: local_queue,
    }

    if kueue_priority_class is not None:
        labels[PRIORITY_CLASS_LABEL] = kueue_priority_class
    return labels
