"""When a range still needs the Windows golden image, and when it does not.

A scenario that names no catalog workload is cloned from ``incus.image_alias`` —
the golden Windows image — so a range holding one cannot run without it. A range
whose scenarios all name a workload is built entirely from those workloads'
images, and is a perfectly healthy range with no Windows image anywhere.

That distinction is what keeps ``ontrak doctor`` honest. Without it, a Linux-only
range reports the golden image as **missing** (a failure, non-zero exit) and
points the operator at a build their host may not even be able to run — see
docs/operations.md, "Building the golden image on a nested host".

The catalogue the range actually ships is the load-bearing case: it *does* need
the image. The Linux-only case is built by copying that catalogue and dropping
the scenarios that fall back to it, so the test exercises the real loader rather
than a hand-made list of ids.
"""
from __future__ import annotations

import shutil

from ontrak.catalog import Catalog
from ontrak.scenarios import ScenarioRepository
from ontrak.sessions import SessionManager


def _catalog(settings) -> Catalog:
    """The real workload catalog the range ships with."""
    catalog = Catalog(settings.catalog_dir)
    catalog.load()
    return catalog


def _manager(settings, store, repo, incus, driver, catalog) -> SessionManager:
    return SessionManager(settings, store, repo=repo, incus=incus, driver=driver, catalog=catalog)


def _catalogue_without_scenarios_that_use_the_golden_image(settings, tmp_path) -> ScenarioRepository:
    """A copy of the shipped `scenarios/` holding only scenarios that name a workload."""
    keep = {scenario.id for scenario in ScenarioRepository(settings.scenarios_dir).list() if scenario.platform_workloads}
    target = tmp_path / "linux-only-scenarios"
    shutil.copytree(settings.scenarios_dir, target)
    for directory in target.iterdir():
        if directory.is_dir() and directory.name != "_lib" and directory.name not in keep:
            shutil.rmtree(directory)
    return ScenarioRepository(target)


def test_the_shipped_range_needs_the_golden_image(settings, store, repo, incus, driver):
    """Windows scenarios name no workload, so every one of them clones the image."""
    manager = _manager(settings, store, repo, incus, driver, _catalog(settings))
    assert manager.golden_image_required() is True


def test_a_range_of_catalog_workloads_alone_does_not(settings, store, incus, driver, tmp_path):
    only_workloads = _catalogue_without_scenarios_that_use_the_golden_image(settings, tmp_path)
    loaded = only_workloads.list()
    assert loaded, "the Linux-only copy loaded no scenarios at all"
    assert all(scenario.platform_workloads for scenario in loaded)
    manager = _manager(settings, store, only_workloads, incus, driver, _catalog(settings))
    assert manager.golden_image_required() is False


def test_a_range_with_no_catalog_falls_back_to_the_golden_image(settings, store, repo, incus, driver):
    """Without a catalog nothing can resolve to a workload image, so the image is needed.

    This is the conservative direction on purpose: an unreadable or absent catalog
    must not be read as "nothing needs the golden image".
    """
    manager = _manager(settings, store, repo, incus, driver, catalog=None)
    assert manager.golden_image_required() is True
