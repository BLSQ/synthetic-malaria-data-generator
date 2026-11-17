import copy

import pytest
import yaml

from malsim.config import ConfigError
from malsim.inspect import inspect
from malsim.mapping import load_mapping, resolve_mapping
from conftest import TINY, FakeDhis2Client, fake_metadata, tiny_with

BANDS = ["u5", "5plus"]


def write(tmp_path, data):
    path = tmp_path / "mapping.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return str(path)


def by_name():
    return {
        "dataSet": {"name": "Monthly cases"},
        "attributeOptionCombo": {"name": "default"},
        "elements": {
            "suspected": {"dataElement": {"name": "Suspected cases"}},
            "presumed": {"dataElement": {"name": "Presumed cases"}},
            "confirmed_simple": {"dataElement": {"name": "Confirmed simple cases"}},
            "confirmed_severe": {"dataElement": {"id": "DeConfSevr1"}},
        },
        "age_bands": {"u5": {"name": "< 5"}, "5plus": {"name": ">= 5"}},
    }


def test_names_resolve_to_uids(tmp_path):
    r = resolve_mapping(load_mapping(write(tmp_path, by_name())), BANDS, FakeDhis2Client())
    assert r["elements"] == {"suspected": "DeSuspectd1", "presumed": "DePresumed1",
                             "confirmed_simple": "DeConfSimp1", "confirmed_severe": "DeConfSevr1"}
    assert r["age_bands"] == {"u5": "CocUnder5Y1", "5plus": "Coc5PlusYr1"}
    assert (r["dataSet"], r["attributeOptionCombo"]) == ("DsMonthly01", "HllvX50cXC0")
    assert r["warnings"] == []


def test_uids_resolve_without_client():
    r = resolve_mapping(load_mapping(str(TINY / "mapping.yaml")), BANDS)
    assert r["elements"]["presumed"] == "DePresumed1" and r["instance"] is None


def test_names_need_a_client(tmp_path):
    with pytest.raises(ConfigError, match="connection"):
        resolve_mapping(load_mapping(write(tmp_path, by_name())), BANDS)


def test_unknown_name(tmp_path):
    m = by_name()
    m["elements"]["presumed"]["dataElement"] = {"name": "Nope"}
    with pytest.raises(ConfigError, match="presumed"):
        resolve_mapping(load_mapping(write(tmp_path, m)), BANDS, FakeDhis2Client())


def test_ambiguous_name(tmp_path):
    meta = fake_metadata()
    dup = copy.deepcopy(meta["dataElements"][0])
    dup["id"] = "DeDuplicat1"
    meta["dataElements"].append(dup)
    with pytest.raises(ConfigError, match="match"):
        resolve_mapping(load_mapping(write(tmp_path, by_name())), BANDS, FakeDhis2Client(metadata=meta))


def test_band_without_coc(tmp_path):
    m = by_name()
    m["age_bands"]["u5"] = {"name": "0-11 mois"}
    with pytest.raises(ConfigError, match="u5"):
        resolve_mapping(load_mapping(write(tmp_path, m)), BANDS, FakeDhis2Client())


def test_bands_must_match_config(tmp_path):
    with pytest.raises(ConfigError, match="age bands"):
        resolve_mapping(load_mapping(write(tmp_path, by_name())), ["u1", "1-4", "5plus"], FakeDhis2Client())


def test_element_on_default_combo_is_rejected(tmp_path):
    meta = fake_metadata()
    meta["dataElements"][0]["categoryCombo"] = {"id": "bjDvmb4bfuf", "name": "default",
                                                "categoryOptionCombos": [{"id": "HllvX50cXC0", "name": "default"}]}
    with pytest.raises(ConfigError, match="category combo"):
        resolve_mapping(load_mapping(write(tmp_path, by_name())), BANDS, FakeDhis2Client(metadata=meta))


def test_element_not_in_data_set(tmp_path):
    meta = fake_metadata()
    meta["dataSets"][0]["dataSetElements"].pop()
    with pytest.raises(ConfigError, match="not in data set"):
        resolve_mapping(load_mapping(write(tmp_path, by_name())), BANDS, FakeDhis2Client(metadata=meta))


def test_unknown_mapping_element(tmp_path):
    m = by_name()
    m["elements"]["confirmed_u5"] = {"dataElement": "X"}
    with pytest.raises(ConfigError, match="unknown element"):
        load_mapping(write(tmp_path, m))


def test_inspect_reports_mapping_and_assignment(tmp_path):
    cfg = tiny_with(tmp_path, {})
    report = inspect(cfg, FakeDhis2Client(assigned=["FaclAAAAAA1", "FaclAAAAAA2"]))
    m = report["mapping"]
    assert m["resolved"]["age_bands"] == {"u5": "CocUnder5Y1", "5plus": "Coc5PlusYr1"}
    assert m["details"]["presumed"]["categoryCombo"]["name"] == "Age 5"
    assert m["data_set_assignment"]["simulated_facilities_not_assigned"] == 4
    assert m["data_values_before_skipping_zeros"] == 6 * 36 * 4 * 2
