from typing import Iterator, List, Optional, cast

import kubernetes.config
from dagster import (
    Field,
    IntSource,
    StringSource,
    _check as check,
    executor,
)
from dagster._core.definitions.executor_definition import multiple_process_executor_requirements
from dagster._core.errors import DagsterUnmetExecutorRequirementsError
from dagster._core.events import DagsterEvent, EngineEventData, MetadataEntry
from dagster._core.execution.retries import RetryMode, get_retries_config
from dagster._core.execution.tags import get_tag_concurrency_limits_config
from dagster._core.executor.base import Executor
from dagster._core.executor.init import InitExecutorContext
from dagster._core.executor.step_delegating import (
    CheckStepHealthResult,
    StepDelegatingExecutor,
    StepHandler,
    StepHandlerContext,
)
from dagster._utils.merger import merge_dicts

from dagster_k8s.launcher import K8sRunLauncher

from .client import DagsterKubernetesClient
from .container_context import K8sContainerContext
from .job import (
    DagsterK8sJobConfig,
    construct_dagster_k8s_job,
    get_k8s_job_name,
    get_user_defined_k8s_config,
)

_K8S_EXECUTOR_CONFIG_SCHEMA = merge_dicts(
    DagsterK8sJobConfig.config_type_job(),
    {
        "job_namespace": Field(StringSource, is_required=False),
        "retries": get_retries_config(),
        "max_concurrent": Field(
            IntSource,
            is_required=False,
            description=(
                "Limit on the number of pods that will run concurrently within the scope "
                "of a Dagster run. Note that this limit is per run, not global."
            ),
        ),
        "tag_concurrency_limits": get_tag_concurrency_limits_config(),
    },
)


@executor(
    name="k8s",
    config_schema=_K8S_EXECUTOR_CONFIG_SCHEMA,
    requirements=multiple_process_executor_requirements(),
)
def k8s_job_executor(init_context: InitExecutorContext) -> Executor:
    """Executor which launches steps as Kubernetes Jobs.

    To use the `k8s_job_executor`, set it as the `executor_def` when defining a job:

    .. literalinclude:: ../../../../../../python_modules/libraries/dagster-k8s/dagster_k8s_tests/unit_tests/test_example_executor_mode_def.py
       :start-after: start_marker
       :end-before: end_marker
       :language: python

    Then you can configure the executor with run config as follows:

    .. code-block:: YAML

        execution:
          config:
            job_namespace: 'some-namespace'
            image_pull_policy: ...
            image_pull_secrets: ...
            service_account_name: ...
            env_config_maps: ...
            env_secrets: ...
            env_vars: ...
            job_image: ... # leave out if using userDeployments
            max_concurrent: ...

    `max_concurrent` limits the number of pods that will execute concurrently for one run. By default
    there is no limit- it will maximally parallel as allowed by the DAG. Note that this is not a
    global limit.

    Configuration set on the Kubernetes Jobs and Pods created by the `K8sRunLauncher` will also be
    set on Kubernetes Jobs and Pods created by the `k8s_job_executor`.

    Configuration set using `tags` on a `@job` will only apply to the `run` level. For configuration
    to apply at each `step` it must be set using `tags` for each `@op`.
    """
    run_launcher = init_context.instance.run_launcher
    if not isinstance(run_launcher, K8sRunLauncher):
        raise DagsterUnmetExecutorRequirementsError(
            (
                "This engine is only compatible with a K8sRunLauncher; configure the "
                "K8sRunLauncher on your instance to use it."
            ),
        )

    exc_cfg = init_context.executor_config

    k8s_container_context = K8sContainerContext(
        image_pull_policy=exc_cfg.get("image_pull_policy"),  # type: ignore
        image_pull_secrets=exc_cfg.get("image_pull_secrets"),  # type: ignore
        service_account_name=exc_cfg.get("service_account_name"),  # type: ignore
        env_config_maps=exc_cfg.get("env_config_maps"),  # type: ignore
        env_secrets=exc_cfg.get("env_secrets"),  # type: ignore
        env_vars=exc_cfg.get("env_vars"),  # type: ignore
        volume_mounts=exc_cfg.get("volume_mounts"),  # type: ignore
        volumes=exc_cfg.get("volumes"),  # type: ignore
        labels=exc_cfg.get("labels"),  # type: ignore
        namespace=exc_cfg.get("job_namespace"),  # type: ignore
        resources=exc_cfg.get("resources"),  # type: ignore
        scheduler_name=exc_cfg.get("scheduler_name"),  # type: ignore
    )

    return StepDelegatingExecutor(
        K8sStepHandler(
            image=exc_cfg.get("job_image"),  # type: ignore
            container_context=k8s_container_context,
            load_incluster_config=run_launcher.load_incluster_config,
            kubeconfig_file=run_launcher.kubeconfig_file,
        ),
        retries=RetryMode.from_config(exc_cfg["retries"]),  # type: ignore
        max_concurrent=check.opt_int_elem(exc_cfg, "max_concurrent"),
        tag_concurrency_limits=check.opt_list_elem(exc_cfg, "tag_concurrency_limits"),
        should_verify_step=True,
    )


class K8sStepHandler(StepHandler):
    @property
    def name(self):
        return "K8sStepHandler"

    def __init__(
        self,
        image: Optional[str],
        container_context: K8sContainerContext,
        load_incluster_config: bool,
        kubeconfig_file: Optional[str],
        k8s_client_batch_api=None,
    ):
        super().__init__()

        self._executor_image = check.opt_str_param(image, "image")
        self._executor_container_context = check.inst_param(
            container_context, "container_context", K8sContainerContext
        )

        if load_incluster_config:
            check.invariant(
                kubeconfig_file is None,
                "`kubeconfig_file` is set but `load_incluster_config` is True.",
            )
            kubernetes.config.load_incluster_config()
        else:
            check.opt_str_param(kubeconfig_file, "kubeconfig_file")
            kubernetes.config.load_kube_config(kubeconfig_file)

        self._api_client = DagsterKubernetesClient.production_client(
            batch_api_override=k8s_client_batch_api
        )

    def _get_step_key(self, step_handler_context: StepHandlerContext) -> str:
        step_keys_to_execute = cast(
            List[str], step_handler_context.execute_step_args.step_keys_to_execute
        )
        assert len(step_keys_to_execute) == 1, "Launching multiple steps is not currently supported"
        return step_keys_to_execute[0]

    def _get_container_context(
        self, step_handler_context: StepHandlerContext
    ) -> K8sContainerContext:
        step_key = self._get_step_key(step_handler_context)

        context = K8sContainerContext.create_for_run(
            step_handler_context.dagster_run,
            cast(K8sRunLauncher, step_handler_context.instance.run_launcher),
            include_run_tags=False,  # For now don't include job-level dagster-k8s/config tags in step pods
        )
        context = context.merge(self._executor_container_context)

        user_defined_k8s_config = get_user_defined_k8s_config(
            step_handler_context.step_tags[step_key]
        )
        return context.merge(K8sContainerContext(run_k8s_config=user_defined_k8s_config.to_dict()))

    def _get_k8s_step_job_name(self, step_handler_context: StepHandlerContext):
        step_key = self._get_step_key(step_handler_context)

        name_key = get_k8s_job_name(
            step_handler_context.execute_step_args.pipeline_run_id,
            step_key,
        )

        if step_handler_context.execute_step_args.known_state:
            retry_state = step_handler_context.execute_step_args.known_state.get_retry_state()
            if retry_state.get_attempt_count(step_key):
                return "dagster-step-%s-%d" % (name_key, retry_state.get_attempt_count(step_key))

        return "dagster-step-%s" % (name_key)

    def launch_step(self, step_handler_context: StepHandlerContext) -> Iterator[DagsterEvent]:
        step_key = self._get_step_key(step_handler_context)

        job_name = self._get_k8s_step_job_name(step_handler_context)
        pod_name = job_name

        container_context = self._get_container_context(step_handler_context)

        job_config = container_context.get_k8s_job_config(
            self._executor_image, step_handler_context.instance.run_launcher
        )

        args = step_handler_context.execute_step_args.get_command_args(
            skip_serialized_namedtuple=True
        )

        if not job_config.job_image:
            job_config = job_config.with_image(
                step_handler_context.execute_step_args.pipeline_origin.repository_origin.container_image
            )

        if not job_config.job_image:
            raise Exception("No image included in either executor config or the job")

        job = construct_dagster_k8s_job(
            job_config=job_config,
            args=args,
            job_name=job_name,
            pod_name=pod_name,
            component="step_worker",
            user_defined_k8s_config=container_context.get_run_user_defined_k8s_config(),
            labels={
                "dagster/job": step_handler_context.dagster_run.pipeline_name,
                "dagster/op": step_key,
                "dagster/run-id": step_handler_context.execute_step_args.pipeline_run_id,
            },
            env_vars=[
                *step_handler_context.execute_step_args.get_command_env(),
                {
                    "name": "DAGSTER_RUN_JOB_NAME",
                    "value": step_handler_context.dagster_run.pipeline_name,
                },
                {"name": "DAGSTER_RUN_STEP_KEY", "value": step_key},
                *container_context.env,
            ],
        )

        yield DagsterEvent.step_worker_starting(
            step_handler_context.get_step_context(step_key),
            message=f'Executing step "{step_key}" in Kubernetes job {job_name}.',
            metadata_entries=[
                MetadataEntry("Kubernetes Job name", value=job_name),
            ],
        )

        self._api_client.batch_api.create_namespaced_job(
            body=job, namespace=container_context.namespace
        )

    def check_step_health(self, step_handler_context: StepHandlerContext) -> CheckStepHealthResult:
        step_key = self._get_step_key(step_handler_context)

        job_name = self._get_k8s_step_job_name(step_handler_context)

        container_context = self._get_container_context(step_handler_context)

        status = self._api_client.get_job_status(
            namespace=container_context.namespace,
            job_name=job_name,
        )
        if status.failed:
            return CheckStepHealthResult.unhealthy(
                reason=f"Discovered failed Kubernetes job {job_name} for step {step_key}.",
            )

        return CheckStepHealthResult.healthy()

    def terminate_step(self, step_handler_context: StepHandlerContext) -> Iterator[DagsterEvent]:
        step_key = self._get_step_key(step_handler_context)

        job_name = self._get_k8s_step_job_name(step_handler_context)
        container_context = self._get_container_context(step_handler_context)

        yield DagsterEvent.engine_event(
            step_handler_context.get_step_context(step_key),
            message=f"Deleting Kubernetes job {job_name} for step",
            event_specific_data=EngineEventData(),
        )

        self._api_client.delete_job(job_name=job_name, namespace=container_context.namespace)
