import os
import time
from typing import List, Tuple

from dagster._core.execution.backfill import BulkActionStatus, PartitionBackfill
from dagster._core.host_representation.origin import ExternalPartitionSetOrigin
from dagster._core.storage.pipeline_run import DagsterRun, DagsterRunStatus
from dagster._core.storage.tags import PARTITION_NAME_TAG
from dagster._core.test_utils import create_run_for_test
from dagster._core.utils import make_new_backfill_id
from dagster._seven import get_system_temp_directory
from dagster_graphql.client.query import LAUNCH_PARTITION_BACKFILL_MUTATION
from dagster_graphql.test.utils import (
    execute_dagster_graphql,
    execute_dagster_graphql_and_finish_runs,
    infer_repository_selector,
)

from .graphql_context_test_suite import (
    ExecutingGraphQLContextTestMatrix,
    ReadonlyGraphQLContextTestMatrix,
)

PARTITION_PROGRESS_QUERY = """
  query PartitionProgressQuery($backfillId: String!) {
    partitionBackfillOrError(backfillId: $backfillId) {
      ... on PartitionBackfill {
        __typename
        backfillId
        status
        numCancelable
        partitionNames
        numPartitions
        fromFailure
        reexecutionSteps
        partitionStatuses {
          results {
            partitionName
            runStatus
          }
        }
        hasCancelPermission
        hasResumePermission
      }
      ... on PythonError {
        message
        stack
      }
    }
  }
"""

CANCEL_BACKFILL_MUTATION = """
  mutation($backfillId: String!) {
    cancelPartitionBackfill(backfillId: $backfillId) {
      __typename
      ... on CancelBackfillSuccess {
        backfillId
      }
      ... on PythonError {
        message
        stack
      }
    }
  }
"""

RESUME_BACKFILL_MUTATION = """
  mutation($backfillId: String!) {
    resumePartitionBackfill(backfillId: $backfillId) {
      __typename
      ... on ResumeBackfillSuccess {
        backfillId
      }
      ... on PythonError {
        message
        stack
      }
    }
  }
"""

GET_PARTITION_BACKFILLS_QUERY = """
  query PartitionBackfillsQuery($repositorySelector: RepositorySelector!, $partitionSetName: String!) {
    partitionSetOrError(repositorySelector: $repositorySelector, partitionSetName: $partitionSetName) {
      __typename
      ...on PartitionSet {
        name
        pipelineName
        backfills {
          backfillId
        }
      }
    }
  }

"""


def _seed_runs(graphql_context, partition_runs: List[Tuple[str, DagsterRunStatus]], backfill_id):
    for status, partition in partition_runs:
        create_run_for_test(
            instance=graphql_context.instance,
            status=status,
            tags={
                **DagsterRun.tags_for_backfill_id(backfill_id),
                PARTITION_NAME_TAG: partition,
            },
        )


def _get_run_stats(partition_statuses):
    return {
        "total": len(partition_statuses),
        "queued": len([status for status in partition_statuses if status["runStatus"] == "QUEUED"]),
        "in_progress": len(
            [status for status in partition_statuses if status["runStatus"] == "STARTED"]
        ),
        "success": len(
            [status for status in partition_statuses if status["runStatus"] == "SUCCESS"]
        ),
        "failure": len(
            [status for status in partition_statuses if status["runStatus"] == "FAILURE"]
        ),
        "canceled": len(
            [status for status in partition_statuses if status["runStatus"] == "CANCELED"]
        ),
    }


class TestPartitionBackillReadonlyFailure(ReadonlyGraphQLContextTestMatrix):
    def _create_backfill(self, graphql_context):
        code_location = graphql_context.get_code_location("test")
        repository = code_location.get_repository("test_repo")

        backfill = PartitionBackfill(
            backfill_id=make_new_backfill_id(),
            partition_set_origin=ExternalPartitionSetOrigin(
                external_repository_origin=repository.get_external_origin(),
                partition_set_name="integer_partition",
            ),
            status=BulkActionStatus.REQUESTED,
            partition_names=["one", "two", "three"],
            from_failure=False,
            reexecution_steps=None,
            tags=None,
            backfill_timestamp=time.time(),
        )
        graphql_context.instance.add_backfill(backfill)
        return backfill.backfill_id

    def test_launch_backill_failure(self, graphql_context):
        repository_selector = infer_repository_selector(graphql_context)
        result = execute_dagster_graphql(
            graphql_context,
            LAUNCH_PARTITION_BACKFILL_MUTATION,
            variables={
                "backfillParams": {
                    "selector": {
                        "repositorySelector": repository_selector,
                        "partitionSetName": "integer_partition",
                    },
                    "partitionNames": ["2", "3"],
                }
            },
        )
        assert not result.errors
        assert result.data
        assert result.data["launchPartitionBackfill"]["__typename"] == "UnauthorizedError"

    def test_cancel_backfill_failure(self, graphql_context):
        backfill_id = self._create_backfill(graphql_context)
        result = execute_dagster_graphql(
            graphql_context,
            CANCEL_BACKFILL_MUTATION,
            variables={"backfillId": backfill_id},
        )

        assert not result.errors
        assert result.data
        assert result.data["cancelPartitionBackfill"]["__typename"] == "UnauthorizedError"

    def test_no_permission(self, graphql_context):
        backfill_id = self._create_backfill(graphql_context)

        result = execute_dagster_graphql(
            graphql_context,
            PARTITION_PROGRESS_QUERY,
            variables={"backfillId": backfill_id},
        )
        assert not result.errors
        assert result.data
        assert result.data["partitionBackfillOrError"]["hasCancelPermission"] is False
        assert result.data["partitionBackfillOrError"]["hasResumePermission"] is False

    def test_resume_backfill_failure(self, graphql_context):
        backfill_id = self._create_backfill(graphql_context)

        result = execute_dagster_graphql(
            graphql_context,
            RESUME_BACKFILL_MUTATION,
            variables={"backfillId": backfill_id},
        )
        assert result.data
        assert result.data["resumePartitionBackfill"]["__typename"] == "UnauthorizedError", str(
            result.data
        )


class TestDaemonPartitionBackfill(ExecutingGraphQLContextTestMatrix):
    def test_launch_full_pipeline_backfill(self, graphql_context):
        repository_selector = infer_repository_selector(graphql_context)
        result = execute_dagster_graphql(
            graphql_context,
            LAUNCH_PARTITION_BACKFILL_MUTATION,
            variables={
                "backfillParams": {
                    "selector": {
                        "repositorySelector": repository_selector,
                        "partitionSetName": "integers_partition_set",
                    },
                    "partitionNames": ["2", "3"],
                }
            },
        )

        assert not result.errors
        assert result.data
        assert result.data["launchPartitionBackfill"]["__typename"] == "LaunchBackfillSuccess"
        backfill_id = result.data["launchPartitionBackfill"]["backfillId"]

        result = execute_dagster_graphql(
            graphql_context,
            PARTITION_PROGRESS_QUERY,
            variables={"backfillId": backfill_id},
        )

        assert not result.errors
        assert result.data
        assert result.data["partitionBackfillOrError"]["__typename"] == "PartitionBackfill"
        assert result.data["partitionBackfillOrError"]["status"] == "REQUESTED"
        assert result.data["partitionBackfillOrError"]["numCancelable"] == 2
        assert result.data["partitionBackfillOrError"]["hasCancelPermission"] is True
        assert result.data["partitionBackfillOrError"]["hasResumePermission"] is True

        assert len(result.data["partitionBackfillOrError"]["partitionNames"]) == 2

    def test_get_partition_backfills(self, graphql_context):
        repository_selector = infer_repository_selector(graphql_context)
        # launch a backfill for this partition set
        launch_result = execute_dagster_graphql(
            graphql_context,
            LAUNCH_PARTITION_BACKFILL_MUTATION,
            variables={
                "backfillParams": {
                    "selector": {
                        "repositorySelector": repository_selector,
                        "partitionSetName": "integers_partition_set",
                    },
                    "partitionNames": ["2", "3"],
                }
            },
        )
        backfill_id = launch_result.data["launchPartitionBackfill"]["backfillId"]
        result = execute_dagster_graphql(
            graphql_context,
            GET_PARTITION_BACKFILLS_QUERY,
            variables={
                "repositorySelector": repository_selector,
                "partitionSetName": "integers_partition_set",
            },
        )
        assert not result.errors
        assert result.data
        assert result.data["partitionSetOrError"]["__typename"] == "PartitionSet"
        assert len(result.data["partitionSetOrError"]["backfills"]) == 1
        assert result.data["partitionSetOrError"]["backfills"][0]["backfillId"] == backfill_id

    def test_launch_partial_backfill(self, graphql_context):
        # execute a full pipeline, without the failure environment variable
        repository_selector = infer_repository_selector(graphql_context)
        partition_set_selector = {
            "repositorySelector": repository_selector,
            "partitionSetName": "chained_failure_job_partition_set",
        }

        # reexecute a partial pipeline
        partial_steps = ["after_failure"]
        result = execute_dagster_graphql_and_finish_runs(
            graphql_context,
            LAUNCH_PARTITION_BACKFILL_MUTATION,
            variables={
                "backfillParams": {
                    "selector": partition_set_selector,
                    "partitionNames": ["2", "3"],
                    "reexecutionSteps": partial_steps,
                }
            },
        )
        assert not result.errors
        assert result.data
        assert result.data["launchPartitionBackfill"]["__typename"] == "LaunchBackfillSuccess"
        backfill_id = result.data["launchPartitionBackfill"]["backfillId"]

        result = execute_dagster_graphql(
            graphql_context,
            PARTITION_PROGRESS_QUERY,
            variables={"backfillId": backfill_id},
        )

        assert not result.errors
        assert result.data
        assert result.data["partitionBackfillOrError"]["__typename"] == "PartitionBackfill"
        assert result.data["partitionBackfillOrError"]["status"] == "REQUESTED"
        assert result.data["partitionBackfillOrError"]["numCancelable"] == 2
        assert len(result.data["partitionBackfillOrError"]["partitionNames"]) == 2
        assert result.data["partitionBackfillOrError"]["reexecutionSteps"] == ["after_failure"]

    def test_cancel_backfill(self, graphql_context):
        repository_selector = infer_repository_selector(graphql_context)
        result = execute_dagster_graphql(
            graphql_context,
            LAUNCH_PARTITION_BACKFILL_MUTATION,
            variables={
                "backfillParams": {
                    "selector": {
                        "repositorySelector": repository_selector,
                        "partitionSetName": "integers_partition_set",
                    },
                    "partitionNames": ["2", "3"],
                }
            },
        )

        assert not result.errors
        assert result.data
        assert result.data["launchPartitionBackfill"]["__typename"] == "LaunchBackfillSuccess"
        backfill_id = result.data["launchPartitionBackfill"]["backfillId"]

        result = execute_dagster_graphql(
            graphql_context,
            PARTITION_PROGRESS_QUERY,
            variables={"backfillId": backfill_id},
        )

        assert not result.errors
        assert result.data
        assert result.data["partitionBackfillOrError"]["__typename"] == "PartitionBackfill"
        assert result.data["partitionBackfillOrError"]["status"] == "REQUESTED"
        assert result.data["partitionBackfillOrError"]["numCancelable"] == 2
        assert len(result.data["partitionBackfillOrError"]["partitionNames"]) == 2

        result = execute_dagster_graphql(
            graphql_context,
            CANCEL_BACKFILL_MUTATION,
            variables={"backfillId": backfill_id},
        )
        assert result.data
        assert result.data["cancelPartitionBackfill"]["__typename"] == "CancelBackfillSuccess"

        result = execute_dagster_graphql(
            graphql_context,
            PARTITION_PROGRESS_QUERY,
            variables={"backfillId": backfill_id},
        )
        assert not result.errors
        assert result.data
        assert result.data["partitionBackfillOrError"]["__typename"] == "PartitionBackfill"
        assert result.data["partitionBackfillOrError"]["status"] == "CANCELED"

    def test_resume_backfill(self, graphql_context):
        repository_selector = infer_repository_selector(graphql_context)
        result = execute_dagster_graphql(
            graphql_context,
            LAUNCH_PARTITION_BACKFILL_MUTATION,
            variables={
                "backfillParams": {
                    "selector": {
                        "repositorySelector": repository_selector,
                        "partitionSetName": "integers_partition_set",
                    },
                    "partitionNames": ["2", "3"],
                }
            },
        )

        assert not result.errors
        assert result.data
        assert result.data["launchPartitionBackfill"]["__typename"] == "LaunchBackfillSuccess"
        backfill_id = result.data["launchPartitionBackfill"]["backfillId"]

        result = execute_dagster_graphql(
            graphql_context,
            PARTITION_PROGRESS_QUERY,
            variables={"backfillId": backfill_id},
        )

        assert not result.errors
        assert result.data
        assert result.data["partitionBackfillOrError"]["__typename"] == "PartitionBackfill"
        assert result.data["partitionBackfillOrError"]["status"] == "REQUESTED"
        assert result.data["partitionBackfillOrError"]["numCancelable"] == 2
        assert len(result.data["partitionBackfillOrError"]["partitionNames"]) == 2

        # manually mark as failed
        backfill = graphql_context.instance.get_backfill(backfill_id)
        graphql_context.instance.update_backfill(backfill.with_status(BulkActionStatus.FAILED))

        result = execute_dagster_graphql(
            graphql_context,
            RESUME_BACKFILL_MUTATION,
            variables={"backfillId": backfill_id},
        )
        assert result.data
        assert result.data["resumePartitionBackfill"]["__typename"] == "ResumeBackfillSuccess"

        result = execute_dagster_graphql(
            graphql_context,
            PARTITION_PROGRESS_QUERY,
            variables={"backfillId": backfill_id},
        )
        assert not result.errors
        assert result.data
        assert result.data["partitionBackfillOrError"]["__typename"] == "PartitionBackfill"
        assert result.data["partitionBackfillOrError"]["status"] == "REQUESTED"

    def test_backfill_run_stats(self, graphql_context):
        repository_selector = infer_repository_selector(graphql_context)
        result = execute_dagster_graphql(
            graphql_context,
            LAUNCH_PARTITION_BACKFILL_MUTATION,
            variables={
                "backfillParams": {
                    "selector": {
                        "repositorySelector": repository_selector,
                        "partitionSetName": "integers_partition_set",
                    },
                    "partitionNames": ["2", "3", "4", "5"],
                }
            },
        )

        assert not result.errors
        assert result.data
        assert result.data["launchPartitionBackfill"]["__typename"] == "LaunchBackfillSuccess"
        backfill_id = result.data["launchPartitionBackfill"]["backfillId"]

        _seed_runs(
            graphql_context,
            [
                (DagsterRunStatus.SUCCESS, "5"),
                (DagsterRunStatus.STARTED, "2"),
                (DagsterRunStatus.STARTED, "3"),
                (DagsterRunStatus.STARTED, "4"),
                (DagsterRunStatus.STARTED, "5"),
                (DagsterRunStatus.CANCELED, "2"),
                (DagsterRunStatus.FAILURE, "3"),
                (DagsterRunStatus.SUCCESS, "4"),
            ],
            backfill_id,
        )

        result = execute_dagster_graphql(
            graphql_context,
            PARTITION_PROGRESS_QUERY,
            variables={"backfillId": backfill_id},
        )

        assert not result.errors
        assert result.data
        assert result.data["partitionBackfillOrError"]["__typename"] == "PartitionBackfill"
        assert result.data["partitionBackfillOrError"]["status"] == "REQUESTED"
        assert result.data["partitionBackfillOrError"]["numPartitions"] == 4
        run_stats = _get_run_stats(
            result.data["partitionBackfillOrError"]["partitionStatuses"]["results"]
        )
        assert run_stats.get("total") == 4
        assert run_stats.get("queued") == 0
        assert run_stats.get("in_progress") == 1
        assert run_stats.get("success") == 1
        assert run_stats.get("failure") == 1
        assert run_stats.get("canceled") == 1

        backfill = graphql_context.instance.get_backfill(backfill_id)

        # Artificially mark the backfill as complete - verify run status is INCOMPLETE until the runs all succeed
        graphql_context.instance.update_backfill(backfill.with_status(BulkActionStatus.COMPLETED))

        result = execute_dagster_graphql(
            graphql_context,
            PARTITION_PROGRESS_QUERY,
            variables={"backfillId": backfill_id},
        )
        assert result.data["partitionBackfillOrError"]["status"] == "COMPLETED"

    def test_backfill_run_completed(self, graphql_context):
        repository_selector = infer_repository_selector(graphql_context)
        result = execute_dagster_graphql(
            graphql_context,
            LAUNCH_PARTITION_BACKFILL_MUTATION,
            variables={
                "backfillParams": {
                    "selector": {
                        "repositorySelector": repository_selector,
                        "partitionSetName": "integers_partition_set",
                    },
                    "partitionNames": ["2", "3", "4", "5"],
                }
            },
        )
        assert not result.errors
        assert result.data
        assert result.data["launchPartitionBackfill"]["__typename"] == "LaunchBackfillSuccess"
        backfill_id = result.data["launchPartitionBackfill"]["backfillId"]

        backfill = graphql_context.instance.get_backfill(backfill_id)

        graphql_context.instance.update_backfill(backfill.with_status(BulkActionStatus.COMPLETED))

        _seed_runs(
            graphql_context,
            [
                (DagsterRunStatus.SUCCESS, "2"),
                (DagsterRunStatus.SUCCESS, "3"),
                (DagsterRunStatus.SUCCESS, "4"),
                (DagsterRunStatus.SUCCESS, "5"),
            ],
            backfill_id,
        )

        result = execute_dagster_graphql(
            graphql_context,
            PARTITION_PROGRESS_QUERY,
            variables={"backfillId": backfill_id},
        )

        assert not result.errors
        assert result.data
        assert result.data["partitionBackfillOrError"]["__typename"] == "PartitionBackfill"
        assert result.data["partitionBackfillOrError"]["status"] == "COMPLETED"
        assert result.data["partitionBackfillOrError"]["numPartitions"] == 4

        run_stats = _get_run_stats(
            result.data["partitionBackfillOrError"]["partitionStatuses"]["results"]
        )
        assert run_stats.get("total") == 4
        assert run_stats.get("queued") == 0
        assert run_stats.get("in_progress") == 0
        assert run_stats.get("success") == 4
        assert run_stats.get("failure") == 0

    def test_backfill_run_incomplete(self, graphql_context):
        repository_selector = infer_repository_selector(graphql_context)
        result = execute_dagster_graphql(
            graphql_context,
            LAUNCH_PARTITION_BACKFILL_MUTATION,
            variables={
                "backfillParams": {
                    "selector": {
                        "repositorySelector": repository_selector,
                        "partitionSetName": "integers_partition_set",
                    },
                    "partitionNames": ["2", "3", "4", "5"],
                }
            },
        )
        assert not result.errors
        assert result.data
        assert result.data["launchPartitionBackfill"]["__typename"] == "LaunchBackfillSuccess"
        backfill_id = result.data["launchPartitionBackfill"]["backfillId"]

        backfill = graphql_context.instance.get_backfill(backfill_id)

        graphql_context.instance.update_backfill(backfill.with_status(BulkActionStatus.COMPLETED))

        _seed_runs(
            graphql_context,
            [
                (DagsterRunStatus.SUCCESS, "2"),
                (DagsterRunStatus.SUCCESS, "3"),
                (DagsterRunStatus.SUCCESS, "4"),
                (DagsterRunStatus.CANCELED, "5"),
            ],
            backfill_id,
        )

        result = execute_dagster_graphql(
            graphql_context,
            PARTITION_PROGRESS_QUERY,
            variables={"backfillId": backfill_id},
        )

        assert not result.errors
        assert result.data
        assert result.data["partitionBackfillOrError"]["__typename"] == "PartitionBackfill"
        assert result.data["partitionBackfillOrError"]["status"] == "COMPLETED"
        assert result.data["partitionBackfillOrError"]["numPartitions"] == 4
        run_stats = _get_run_stats(
            result.data["partitionBackfillOrError"]["partitionStatuses"]["results"]
        )
        assert run_stats.get("total") == 4
        assert run_stats.get("queued") == 0
        assert run_stats.get("in_progress") == 0
        assert run_stats.get("success") == 3
        assert run_stats.get("failure") == 0
        assert run_stats.get("canceled") == 1


class TestLaunchDaemonBackfillFromFailure(ExecutingGraphQLContextTestMatrix):
    def test_launch_from_failure(self, graphql_context):
        repository_selector = infer_repository_selector(graphql_context)
        partition_set_selector = {
            "repositorySelector": repository_selector,
            "partitionSetName": "chained_failure_job_partition_set",
        }

        # trigger failure in the conditionally_fail solid

        output_file = os.path.join(
            get_system_temp_directory(), "chained_failure_pipeline_conditionally_fail"
        )
        try:
            with open(output_file, "w", encoding="utf8"):
                result = execute_dagster_graphql_and_finish_runs(
                    graphql_context,
                    LAUNCH_PARTITION_BACKFILL_MUTATION,
                    variables={
                        "backfillParams": {
                            "selector": partition_set_selector,
                            "partitionNames": ["2", "3"],
                        }
                    },
                )
        finally:
            os.remove(output_file)

        assert not result.errors
        assert result.data
        assert result.data["launchPartitionBackfill"]["__typename"] == "LaunchBackfillSuccess"

        # re-execute from failure (without the failure file)
        result = execute_dagster_graphql_and_finish_runs(
            graphql_context,
            LAUNCH_PARTITION_BACKFILL_MUTATION,
            variables={
                "backfillParams": {
                    "selector": partition_set_selector,
                    "partitionNames": ["2", "3"],
                    "fromFailure": True,
                }
            },
        )

        assert not result.errors
        assert result.data
        assert result.data["launchPartitionBackfill"]["__typename"] == "LaunchBackfillSuccess"
        backfill_id = result.data["launchPartitionBackfill"]["backfillId"]

        result = execute_dagster_graphql(
            graphql_context,
            PARTITION_PROGRESS_QUERY,
            variables={"backfillId": backfill_id},
        )
        assert not result.errors
        assert result.data
        assert result.data["partitionBackfillOrError"]["__typename"] == "PartitionBackfill"
        assert result.data["partitionBackfillOrError"]["status"] == "REQUESTED"
        assert result.data["partitionBackfillOrError"]["numCancelable"] == 2
        assert len(result.data["partitionBackfillOrError"]["partitionNames"]) == 2
        assert result.data["partitionBackfillOrError"]["fromFailure"]

    def test_launch_backfill_with_all_partitions_flag(self, graphql_context):
        repository_selector = infer_repository_selector(graphql_context)
        partition_set_selector = {
            "repositorySelector": repository_selector,
            "partitionSetName": "chained_failure_job_partition_set",
        }

        result = execute_dagster_graphql_and_finish_runs(
            graphql_context,
            LAUNCH_PARTITION_BACKFILL_MUTATION,
            variables={
                "backfillParams": {
                    "selector": partition_set_selector,
                    "allPartitions": True,
                }
            },
        )
        assert not result.errors
        assert result.data
        assert result.data["launchPartitionBackfill"]["__typename"] == "LaunchBackfillSuccess"
        backfill_id = result.data["launchPartitionBackfill"]["backfillId"]

        result = execute_dagster_graphql(
            graphql_context,
            PARTITION_PROGRESS_QUERY,
            variables={"backfillId": backfill_id},
        )

        assert not result.errors
        assert result.data
        assert result.data["partitionBackfillOrError"]["__typename"] == "PartitionBackfill"
        assert result.data["partitionBackfillOrError"]["status"] == "REQUESTED"
        assert result.data["partitionBackfillOrError"]["numCancelable"] == 10
        assert len(result.data["partitionBackfillOrError"]["partitionNames"]) == 10
