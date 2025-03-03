import operator
from functools import reduce
from typing import AbstractSet, Iterable, Union

import pytest
from dagster import (
    AssetIn,
    DailyPartitionsDefinition,
    SourceAsset,
    TimeWindowPartitionMapping,
)
from dagster._core.definitions import AssetSelection, asset
from dagster._core.definitions.assets import AssetsDefinition
from dagster._core.definitions.events import AssetKey
from typing_extensions import TypeAlias

earth = SourceAsset("earth", group_name="planets")


@asset(group_name="ladies")
def alice(earth):
    return "alice"


@asset(group_name="gentlemen")
def bob(alice):
    return "bob"


@asset(group_name="ladies")
def candace(alice):
    return "candace"


@asset(group_name="gentlemen")
def danny(candace):
    return "danny"


@asset(group_name="gentlemen")
def edgar(danny):
    return "edgar"


@asset(group_name="ladies")
def fiona(danny):
    return "fiona"


@asset(group_name="gentlemen")
def george(bob, fiona):
    return "george"


_AssetList: TypeAlias = Iterable[Union[AssetsDefinition, SourceAsset]]


@pytest.fixture
def all_assets() -> _AssetList:
    return [
        earth,
        alice,
        bob,
        candace,
        danny,
        edgar,
        fiona,
        george,
    ]


def _asset_keys_of(assets_defs: _AssetList) -> AbstractSet[AssetKey]:
    return reduce(
        operator.or_,
        [item.keys if isinstance(item, AssetsDefinition) else {item.key} for item in assets_defs],
    )


def test_asset_selection_all(all_assets: _AssetList):
    sel = AssetSelection.all()
    assert sel.resolve(all_assets) == _asset_keys_of(all_assets) - {earth.key}


def test_asset_selection_and(all_assets: _AssetList):
    sel = AssetSelection.keys("alice", "bob") & AssetSelection.keys("bob", "candace")
    assert sel.resolve(all_assets) == _asset_keys_of({bob})


def test_asset_selection_downstream(all_assets: _AssetList):
    sel_depth_inf = AssetSelection.keys("candace").downstream()
    assert sel_depth_inf.resolve(all_assets) == _asset_keys_of(
        {candace, danny, edgar, fiona, george}
    )

    sel_depth_1 = AssetSelection.keys("candace").downstream(depth=1)
    assert sel_depth_1.resolve(all_assets) == _asset_keys_of({candace, danny})


def test_asset_selection_groups(all_assets: _AssetList):
    sel = AssetSelection.groups("ladies", "planets")
    # should not include source assets
    assert sel.resolve(all_assets) == _asset_keys_of({alice, candace, fiona})


def test_asset_selection_keys(all_assets: _AssetList):
    sel = AssetSelection.keys(AssetKey("alice"), AssetKey("bob"))
    assert sel.resolve(all_assets) == _asset_keys_of({alice, bob})

    sel = AssetSelection.keys("alice", "bob")
    assert sel.resolve(all_assets) == _asset_keys_of({alice, bob})


def test_select_source_asset_keys():
    a = SourceAsset("a")
    selection = AssetSelection.keys(a.key)
    assert selection.resolve([a]) == {a.key}


def test_asset_selection_assets(all_assets: _AssetList):
    sel = AssetSelection.assets(alice, bob)
    assert sel.resolve(all_assets) == _asset_keys_of({alice, bob})


def test_asset_selection_or(all_assets: _AssetList):
    sel = AssetSelection.keys("alice", "bob") | AssetSelection.keys("bob", "candace")
    assert sel.resolve(all_assets) == _asset_keys_of({alice, bob, candace})


def test_asset_selection_subtraction(all_assets: _AssetList):
    sel = AssetSelection.keys("alice", "bob") - AssetSelection.keys("bob", "candace")
    assert sel.resolve(all_assets) == _asset_keys_of({alice})

    sel = AssetSelection.groups("ladies") - AssetSelection.groups("gentlemen")
    assert sel.resolve(all_assets) == _asset_keys_of({alice, candace, fiona})

    sel = (AssetSelection.groups("ladies") | AssetSelection.keys("bob")) - AssetSelection.groups(
        "ladies"
    )
    assert sel.resolve(all_assets) == _asset_keys_of({bob})


def test_asset_selection_nothing(all_assets: _AssetList):
    sel = AssetSelection.keys()
    assert sel.resolve(all_assets) == set()

    sel = AssetSelection.groups("ladies") - AssetSelection.groups("ladies")
    assert sel.resolve(all_assets) == set()

    sel = AssetSelection.keys("alice", "bob") - AssetSelection.keys("alice", "bob")
    assert sel.resolve(all_assets) == set()


def test_asset_selection_sinks(all_assets: _AssetList):
    sel = AssetSelection.keys("alice", "bob").sinks()
    assert sel.resolve(all_assets) == _asset_keys_of({bob})

    sel = AssetSelection.all().sinks()
    assert sel.resolve(all_assets) == _asset_keys_of({edgar, george})

    sel = AssetSelection.groups("ladies").sinks()
    # fiona is a sink because it has no downstream dependencies within the "ladies" group
    # candace is not a sink because it is an upstream dependency of fiona
    assert sel.resolve(all_assets) == _asset_keys_of({fiona})


def test_asset_selection_upstream(all_assets: _AssetList):
    sel_depth_inf = AssetSelection.keys("george").upstream()
    assert sel_depth_inf.resolve(all_assets) == _asset_keys_of(
        {alice, bob, candace, danny, fiona, george}
    )

    sel_depth_1 = AssetSelection.keys("george").upstream(depth=1)
    assert sel_depth_1.resolve(all_assets) == _asset_keys_of({bob, fiona, george})


def test_downstream_include_self(all_assets: _AssetList):
    selection = AssetSelection.keys("candace").downstream(include_self=False)
    assert selection.resolve(all_assets) == _asset_keys_of({danny, edgar, fiona, george})

    selection = AssetSelection.groups("gentlemen").downstream(include_self=False)
    assert selection.resolve(all_assets) == _asset_keys_of({fiona})

    selection = AssetSelection.groups("ladies").downstream(include_self=False)
    assert selection.resolve(all_assets) == _asset_keys_of({george, danny, bob, edgar})


def test_upstream_include_self(all_assets: _AssetList):
    selection = AssetSelection.keys("george").upstream(include_self=False)
    assert selection.resolve(all_assets) == _asset_keys_of({danny, fiona, alice, bob, candace})

    selection = AssetSelection.groups("gentlemen").upstream(include_self=False)
    assert selection.resolve(all_assets) == _asset_keys_of({fiona, alice, candace})

    selection = AssetSelection.groups("ladies").upstream(include_self=False)
    assert selection.resolve(all_assets) == _asset_keys_of({danny})


def test_roots():
    @asset
    def a():
        pass

    @asset
    def b(a):
        pass

    @asset
    def c(b):
        pass

    assert AssetSelection.keys("a", "b", "c").roots().resolve([a, b, c]) == {a.key}
    assert AssetSelection.keys("a", "c").roots().resolve([a, b, c]) == {a.key}
    assert AssetSelection.keys("b", "c").roots().resolve([a, b, c]) == {b.key}
    assert AssetSelection.keys("c").roots().resolve([a, b, c]) == {c.key}


def test_sources():
    @asset
    def a():
        pass

    @asset
    def b(a):
        pass

    @asset
    def c(b):
        pass

    with pytest.warns(DeprecationWarning):
        assert AssetSelection.keys("a", "b", "c").sources().resolve([a, b, c]) == {a.key}


def test_self_dep():
    @asset(
        partitions_def=DailyPartitionsDefinition(start_date="2020-01-01"),
        ins={
            "a": AssetIn(
                partition_mapping=TimeWindowPartitionMapping(start_offset=-1, end_offset=-1)
            )
        },
    )
    def a(a):
        ...

    assert AssetSelection.keys("a").resolve([a]) == {a.key}
    assert AssetSelection.keys("a").upstream().resolve([a]) == {a.key}
    assert AssetSelection.keys("a").upstream(include_self=False).resolve([a]) == set()
    assert AssetSelection.keys("a").sources().resolve([a]) == {a.key}
    assert AssetSelection.keys("a").sinks().resolve([a]) == {a.key}
