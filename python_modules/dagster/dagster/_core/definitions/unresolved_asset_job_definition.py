import operator
from collections import defaultdict
from datetime import datetime
from functools import reduce
from typing import TYPE_CHECKING, Any, Mapping, NamedTuple, Optional, Sequence, Union, cast

import dagster._check as check
from dagster._core.definitions import AssetKey
from dagster._core.definitions.run_request import RunRequest
from dagster._core.errors import DagsterInvalidDefinitionError
from dagster._core.instance import DagsterInstance
from dagster._core.selector.subset_selector import parse_clause
from dagster._utils.backcompat import deprecation_warning

from .asset_layer import build_asset_selection_job
from .config import ConfigMapping

if TYPE_CHECKING:
    from dagster._core.definitions import (
        AssetsDefinition,
        AssetSelection,
        ExecutorDefinition,
        JobDefinition,
        PartitionedConfig,
        PartitionsDefinition,
        SourceAsset,
    )
    from dagster._core.definitions.asset_graph import InternalAssetGraph


class UnresolvedAssetJobDefinition(
    NamedTuple(
        "_UnresolvedAssetJobDefinition",
        [
            ("name", str),
            ("selection", "AssetSelection"),
            (
                "config",
                Optional[Union[ConfigMapping, Mapping[str, Any], "PartitionedConfig"]],
            ),
            ("description", Optional[str]),
            ("tags", Optional[Mapping[str, Any]]),
            ("partitions_def", Optional["PartitionsDefinition"]),
            ("executor_def", Optional["ExecutorDefinition"]),
        ],
    )
):
    def __new__(
        cls,
        name: str,
        selection: "AssetSelection",
        config: Optional[Union[ConfigMapping, Mapping[str, Any], "PartitionedConfig"]] = None,
        description: Optional[str] = None,
        tags: Optional[Mapping[str, Any]] = None,
        partitions_def: Optional["PartitionsDefinition"] = None,
        executor_def: Optional["ExecutorDefinition"] = None,
    ):
        from dagster._core.definitions import (
            AssetSelection,
            ExecutorDefinition,
            PartitionsDefinition,
        )
        from dagster._core.definitions.run_config import convert_config_input

        return super(UnresolvedAssetJobDefinition, cls).__new__(
            cls,
            name=check.str_param(name, "name"),
            selection=check.inst_param(selection, "selection", AssetSelection),
            config=convert_config_input(config),
            description=check.opt_str_param(description, "description"),
            tags=check.opt_mapping_param(tags, "tags"),
            partitions_def=check.opt_inst_param(
                partitions_def, "partitions_def", PartitionsDefinition
            ),
            executor_def=check.opt_inst_param(executor_def, "partitions_def", ExecutorDefinition),
        )

    def run_request_for_partition(
        self,
        partition_key: str,
        run_key: Optional[str] = None,
        tags: Optional[Mapping[str, str]] = None,
        asset_selection: Optional[Sequence[AssetKey]] = None,
        run_config: Optional[Mapping[str, Any]] = None,
        instance: Optional[DagsterInstance] = None,
        current_time: Optional[datetime] = None,
    ) -> RunRequest:
        """Creates a RunRequest object for a run that processes the given partition.

        Args:
            partition_key: The key of the partition to request a run for.
            run_key (Optional[str]): A string key to identify this launched run. For sensors, ensures that
                only one run is created per run key across all sensor evaluations.  For schedules,
                ensures that one run is created per tick, across failure recoveries. Passing in a `None`
                value means that a run will always be launched per evaluation.
            tags (Optional[Dict[str, str]]): A dictionary of tags (string key-value pairs) to attach
                to the launched run.
            run_config (Optional[Mapping[str, Any]]: Configuration for the run. If the job has
                a :py:class:`PartitionedConfig`, this value will override replace the config
                provided by it.
            current_time (Optional[datetime): Used to determine which time-partitions exist.
                Defaults to now.

        Returns:
            RunRequest: an object that requests a run to process the given partition.
        """
        from dagster._core.definitions.partition import (
            DynamicPartitionsDefinition,
            PartitionedConfig,
        )

        if not self.partitions_def:
            check.failed("Called run_request_for_partition on a non-partitioned job")

        partitioned_config = PartitionedConfig.from_flexible_config(
            self.config, self.partitions_def
        )

        if isinstance(self.partitions_def, DynamicPartitionsDefinition) and not instance:
            check.failed(
                "Must provide a dagster instance when calling run_request_for_partition on a "
                "dynamic partition set"
            )

        partition = self.partitions_def.get_partition(
            partition_key, dynamic_partitions_store=instance, current_time=current_time
        )
        run_config = (
            run_config
            if run_config is not None
            else partitioned_config.get_run_config_for_partition_key(
                partition.name, instance=instance, current_time=current_time
            )
        )
        run_request_tags = {
            **(tags or {}),
            **partitioned_config.get_tags_for_partition_key(
                partition_key, instance, current_time=current_time, job_name=self.name
            ),
        }

        return RunRequest(
            job_name=self.name,
            run_key=run_key,
            run_config=run_config,
            tags=run_request_tags,
            asset_selection=asset_selection,
        )

    def resolve(
        self,
        assets: Optional[Sequence["AssetsDefinition"]] = None,
        source_assets: Optional[Sequence["SourceAsset"]] = None,
        default_executor_def: Optional["ExecutorDefinition"] = None,
        asset_graph: Optional["InternalAssetGraph"] = None,
    ) -> "JobDefinition":
        """Resolve this UnresolvedAssetJobDefinition into a JobDefinition.

        The assets and source_assets arguments are deprecated. Although they were never technically
        public, a lot of users use them, so going to wait until a minor release to get rid of them.
        """
        from dagster._core.definitions.asset_graph import AssetGraph

        if asset_graph is not None:
            if assets is not None or source_assets is not None:
                check.failed(
                    "If providing asset_graph, can't also provide assets and source_assets, and"
                    " vice-versa."
                )
            assets = asset_graph.assets
            source_assets = asset_graph.source_assets
        else:
            if assets is None or source_assets is None:
                check.failed(
                    "If asset_graph is not provided, must provide both assets and source_assets"
                )
            deprecation_warning(
                "`assets` and `source_assets` arguments to `resolve`",
                "1.3.0",
                "Please use the `asset_graph` argument instead.",
            )
            asset_graph = AssetGraph.from_assets([*assets, *source_assets])

        selected_asset_keys = self.selection.resolve(asset_graph)

        asset_keys_by_partitions_def = defaultdict(set)
        for asset_key in selected_asset_keys:
            partitions_def = asset_graph.get_partitions_def(asset_key)
            if partitions_def is not None:
                asset_keys_by_partitions_def[partitions_def].add(asset_key)

        if len(asset_keys_by_partitions_def) == 0 and self.partitions_def:
            raise DagsterInvalidDefinitionError(
                "Tried to build a partitioned job, but none of the selected assets are partitioned."
            )

        if len(asset_keys_by_partitions_def) > 1:
            keys_by_partitions_def_str = "\n".join(
                f"{partitions_def}: {asset_keys}"
                for partitions_def, asset_keys in asset_keys_by_partitions_def.items()
            )
            raise DagsterInvalidDefinitionError(
                f"Multiple partitioned assets exist in assets job '{self.name}'. Selected assets"
                " must have the same partitions definitions, but the selected assets have"
                f" different partitions definitions: \n{keys_by_partitions_def_str}"
            )

        inferred_partitions_def = (
            next(iter(asset_keys_by_partitions_def.keys()))
            if asset_keys_by_partitions_def
            else None
        )
        if (
            inferred_partitions_def
            and self.partitions_def != inferred_partitions_def
            and self.partitions_def is not None
        ):
            raise DagsterInvalidDefinitionError(
                f"Job '{self.name}' received a partitions_def of {self.partitions_def}, but the"
                f" selected assets {next(iter(asset_keys_by_partitions_def.values()))} have a"
                f" non-matching partitions_def of {inferred_partitions_def}"
            )

        return build_asset_selection_job(
            name=self.name,
            assets=assets,
            config=self.config,
            source_assets=source_assets,
            description=self.description,
            tags=self.tags,
            asset_selection=selected_asset_keys,
            partitions_def=self.partitions_def if self.partitions_def else inferred_partitions_def,
            executor_def=self.executor_def or default_executor_def,
        )


def _selection_from_string(string: str) -> "AssetSelection":
    from dagster._core.definitions import AssetSelection

    if string == "*":
        return AssetSelection.all()

    parts = parse_clause(string)
    if not parts:
        check.failed(f"Invalid selection string: {string}")
    u, item, d = parts

    selection: AssetSelection = AssetSelection.keys(item)
    if u:
        selection = selection.upstream(u)
    if d:
        selection = selection.downstream(d)
    return selection


def define_asset_job(
    name: str,
    selection: Optional[
        Union[
            str,
            Sequence[str],
            Sequence[AssetKey],
            Sequence[Union["AssetsDefinition", "SourceAsset"]],
            "AssetSelection",
        ]
    ] = None,
    config: Optional[Union[ConfigMapping, Mapping[str, Any], "PartitionedConfig[object]"]] = None,
    description: Optional[str] = None,
    tags: Optional[Mapping[str, Any]] = None,
    partitions_def: Optional["PartitionsDefinition[Any]"] = None,
    executor_def: Optional["ExecutorDefinition"] = None,
) -> UnresolvedAssetJobDefinition:
    """Creates a definition of a job which will either materialize a selection of assets or observe
    a selection of source assets. This will only be resolved to a JobDefinition once placed in a
    code location.

    Args:
        name (str):
            The name for the job.
        selection (Union[str, Sequence[str], Sequence[AssetKey], Sequence[Union[AssetsDefinition, SourceAsset]], AssetSelection]):
            The assets that will be materialized or observed when the job is run.

            The selected assets must all be included in the assets that are passed to the assets
            argument of the Definitions object that this job is included on.

            The string "my_asset*" selects my_asset and all downstream assets within the code
            location. A list of strings represents the union of all assets selected by strings
            within the list.

            The selection will be resolved to a set of assets when the location is loaded. If the
            selection resolves to all source assets, the created job will perform source asset
            observations. If the selection resolves to all regular assets, the created job will
            materialize assets. If the selection resolves to a mixed set of source assets and
            regular assets, an error will be thrown.

        config:
            Describes how the Job is parameterized at runtime.

            If no value is provided, then the schema for the job's run config is a standard
            format based on its solids and resources.

            If a dictionary is provided, then it must conform to the standard config schema, and
            it will be used as the job's run config for the job whenever the job is executed.
            The values provided will be viewable and editable in the Dagit playground, so be
            careful with secrets.

            If a :py:class:`ConfigMapping` object is provided, then the schema for the job's run config is
            determined by the config mapping, and the ConfigMapping, which should return
            configuration in the standard format to configure the job.
        tags (Optional[Mapping[str, Any]]):
            Arbitrary information that will be attached to the execution of the Job.
            Values that are not strings will be json encoded and must meet the criteria that
            `json.loads(json.dumps(value)) == value`.  These tag values may be overwritten by tag
            values provided at invocation time.
        description (Optional[str]):
            A description for the Job.
        partitions_def (Optional[PartitionsDefinition]):
            Defines the set of partitions for this job. All AssetDefinitions selected for this job
            must have a matching PartitionsDefinition. If no PartitionsDefinition is provided, the
            PartitionsDefinition will be inferred from the selected AssetDefinitions.
        executor_def (Optional[ExecutorDefinition]):
            How this Job will be executed. Defaults to :py:class:`multi_or_in_process_executor`,
            which can be switched between multi-process and in-process modes of execution. The
            default mode of execution is multi-process.


    Returns:
        UnresolvedAssetJobDefinition: The job, which can be placed inside a code location.

    Examples:
        .. code-block:: python

            # A job that targets all assets in the code location:
            @asset
            def asset1():
                ...

            defs = Definitions(
                assets=[asset1],
                jobs=[define_asset_job("all_assets")],
            )

            # A job that targets a single asset
            @asset
            def asset1():
                ...

            defs = Definitions(
                assets=[asset1],
                jobs=[define_asset_job("all_assets", selection=[asset1])],
            )

            # A job that targets all the assets in a group:
            defs = Definitions(
                assets=assets,
                jobs=[define_asset_job("marketing_job", selection=AssetSelection.groups("marketing"))],
            )

            @observable_source_asset
            def source_asset():
                ...

            # A job that observes a source asset:
            defs = Definitions(
                assets=assets,
                jobs=[define_asset_job("observation_job", selection=[source_asset])],
            )

            # Resources are supplied to the assets, not the job:
            @asset(required_resource_keys={"slack_client"})
            def asset1():
                ...

            defs = Definitions(
                assets=[asset1],
                jobs=[define_asset_job("all_assets")],
                resources={"slack_client": prod_slack_client},
            )

    """
    from dagster._core.definitions import AssetsDefinition, AssetSelection, SourceAsset

    # convert string-based selections to AssetSelection objects
    resolved_selection: AssetSelection
    if selection is None:
        resolved_selection = AssetSelection.all()
    elif isinstance(selection, str):
        resolved_selection = _selection_from_string(selection)
    elif isinstance(selection, AssetSelection):
        resolved_selection = selection
    elif isinstance(selection, list) and all(isinstance(el, str) for el in selection):
        resolved_selection = reduce(
            operator.or_, [_selection_from_string(cast(str, s)) for s in selection]
        )
    elif isinstance(selection, list) and all(
        isinstance(el, (AssetsDefinition, SourceAsset)) for el in selection
    ):
        resolved_selection = AssetSelection.keys(
            *(el.key for el in cast(Sequence[Union[AssetsDefinition, SourceAsset]], selection))
        )
    elif isinstance(selection, list) and all(isinstance(el, AssetKey) for el in selection):
        resolved_selection = AssetSelection.keys(*cast(Sequence[AssetKey], selection))
    else:
        check.failed(
            "selection argument must be one of str, Sequence[str], Sequence[AssetKey],"
            " Sequence[AssetsDefinition], Sequence[SourceAsset], AssetSelection. Was"
            f" {type(selection)}."
        )

    return UnresolvedAssetJobDefinition(
        name=name,
        selection=resolved_selection,
        config=config,
        description=description,
        tags=tags,
        partitions_def=partitions_def,
        executor_def=executor_def,
    )
