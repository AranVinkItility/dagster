import sys
from typing import Iterator

import pytest
from dagster import file_relative_path, op, repository
from dagster._core.definitions.decorators.job_decorator import job
from dagster._core.definitions.job_definition import JobDefinition
from dagster._core.definitions.repository_definition import RepositoryData
from dagster._core.instance import DagsterInstance
from dagster._core.test_utils import instance_for_test
from dagster._core.types.loadable_target_origin import LoadableTargetOrigin
from dagster._core.workspace.context import WorkspaceProcessContext
from dagster._core.workspace.load_target import GrpcServerTarget
from dagster._grpc.server import GrpcServerProcess


def define_do_something(num_calls):
    @op(name="do_something_" + str(num_calls))
    def do_something():
        return num_calls

    return do_something


@op
def do_input(x):
    return x


def define_foo_job(num_calls: int) -> JobDefinition:
    do_something = define_do_something(num_calls)

    @job(name="foo_" + str(num_calls))
    def foo_job():
        do_input(do_something())

    return foo_job


class TestDynamicRepositoryData(RepositoryData):
    def __init__(self):
        self._num_calls = 0

    # List of pipelines changes everytime get_all_pipelines is called
    def get_all_pipelines(self):
        self._num_calls = self._num_calls + 1
        return [define_foo_job(self._num_calls)]

    def get_top_level_resources(self):
        return {}

    def get_env_vars_by_top_level_resource(self):
        return {}


@repository
def bar_repo():
    return TestDynamicRepositoryData()


@pytest.fixture(name="instance")
def instance_fixture() -> Iterator[DagsterInstance]:
    with instance_for_test() as instance:
        yield instance


@pytest.fixture(name="workspace_process_context")
def workspace_process_context_fixture(
    instance: DagsterInstance,
) -> Iterator[WorkspaceProcessContext]:
    loadable_target_origin = LoadableTargetOrigin(
        executable_path=sys.executable,
        python_file=file_relative_path(__file__, "test_custom_repository_data.py"),
    )
    with GrpcServerProcess(
        instance_ref=instance.get_ref(),
        loadable_target_origin=loadable_target_origin,
        wait_on_exit=True,
    ) as server_process:
        with WorkspaceProcessContext(
            instance,
            GrpcServerTarget(
                host="localhost",
                socket=server_process.socket,
                port=server_process.port,
                location_name="test",
            ),
        ) as workspace_process_context:
            yield workspace_process_context


def test_repository_data_can_reload_without_restarting(
    workspace_process_context: WorkspaceProcessContext,
):
    request_context = workspace_process_context.create_request_context()
    code_location = request_context.get_code_location("test")
    repo = code_location.get_repository("bar_repo")
    # get_all_pipelines called on server init twice, then on repository load, so starts at 3
    # this is a janky test
    assert repo.has_external_job("foo_3")
    assert not repo.has_external_job("foo_1")
    assert not repo.has_external_job("foo_2")

    external_pipeline = repo.get_full_external_job("foo_3")
    assert external_pipeline.has_solid_invocation("do_something_3")

    # Reloading the location changes the pipeline without needing
    # to restart the server process
    workspace_process_context.reload_code_location("test")
    request_context = workspace_process_context.create_request_context()
    code_location = request_context.get_code_location("test")
    repo = code_location.get_repository("bar_repo")
    assert repo.has_external_job("foo_5")
    assert not repo.has_external_job("foo_3")

    external_pipeline = repo.get_full_external_job("foo_5")
    assert external_pipeline.has_solid_invocation("do_something_5")
