import os
from pathlib import Path

import pytest
import yaml
from prometheus_client import REGISTRY

from vigil_agent.catalog import Catalog, CatalogError, Flags, ReloadingFile, load_flags

ROOT = Path(__file__).parent.parent


def test_shipped_catalog_loads():
    catalog = Catalog.load(ROOT / "catalog" / "services.yaml")
    assert catalog.by_namespace("oci-cost").name == "oci-cost"
    assert catalog.by_name("vigil-agent").vigil_enabled is False


def test_missing_field_names_service_and_field():
    with pytest.raises(CatalogError, match=r"service checkout: invalid field\(s\): team"):
        Catalog.from_dict({"services": [{"name": "checkout", "vigil_enabled": True, "slack_channel": "#c"}]})


def test_unknown_pd_service_is_none():
    assert Catalog([]).get("PNOPE") is None


def test_namespace_belongs_to_one_service():
    svc = {"vigil_enabled": True, "team": "t", "slack_channel": "#c", "namespaces": ["a"]}
    with pytest.raises(CatalogError, match="already belongs"):
        Catalog.from_dict({"services": [dict(svc, name="x"), dict(svc, name="y")]})


def test_a_bad_reload_keeps_the_last_good_flags(tmp_path):
    path = tmp_path / "flags.yaml"
    path.write_text("enabled: false\n")
    flags = ReloadingFile(str(path), load_flags, Flags())
    assert flags.get().enabled is False
    path.write_text("enabled: [not, a, bool\n")
    os.utime(path, (1, 1))
    assert flags.get().enabled is False
    assert REGISTRY.get_sample_value("vigil_config_reload_errors", {"file": "flags.yaml"}) == 1
    path.write_text("enabled: true\n")
    os.utime(path, (2, 2))
    assert flags.get().enabled is True
    assert REGISTRY.get_sample_value("vigil_config_reload_errors", {"file": "flags.yaml"}) == 0


def test_a_bad_first_read_still_fails_fast(tmp_path):
    path = tmp_path / "flags.yaml"
    path.write_text("enabled: [broken\n")
    with pytest.raises(yaml.YAMLError):
        ReloadingFile(str(path), load_flags, Flags()).get()


def test_flags():
    assert Flags().allows("c", "s")
    assert not Flags(enabled=False).allows("c", "s")
    assert not Flags(disabled_clusters=["c"]).allows("c", "s")
    assert not Flags(disabled_services=["s"]).allows("c", "s")
