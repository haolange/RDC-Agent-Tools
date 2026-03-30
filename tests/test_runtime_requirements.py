from __future__ import annotations

from rdx.runtime_requirements import REQUIRED_DEPENDENCIES, should_bundle_site_package


def test_required_dependencies_no_longer_include_pyarrow() -> None:
    assert all(dist_name != "pyarrow" for dist_name, _ in REQUIRED_DEPENDENCIES)


def test_should_bundle_site_package_excludes_pyarrow_variants() -> None:
    assert not should_bundle_site_package("pyarrow")
    assert not should_bundle_site_package("pyarrow.libs")
    assert not should_bundle_site_package("pyarrow-18.0.0.dist-info")
