import pandas as pd
import pytest

from malsim.config import ConfigError, IncidenceConfig, PyramidConfig
from malsim.inputs import load_incidence, project_population
from malsim.pyramid import _org_units_frame, build_pyramid, load_org_units
from conftest import TINY


def tiny_pyramid(cfg):
    org_units, types = load_org_units(cfg.pyramid)
    return build_pyramid(org_units, cfg.pyramid, types)


def test_tiny_pyramid_structure(tiny_cfg):
    p = tiny_pyramid(tiny_cfg)
    assert p.pop_units == ["DistAAAAAA1", "DistAAAAAA2"]
    assert len(p.facilities) == 6
    by_unit = p.facilities.groupby("pop_unit")["uid"].apply(list).to_dict()
    assert by_unit["DistAAAAAA1"] == ["FaclAAAAAA1", "FaclAAAAAA2", "FaclAAAAAA3", "FaclAAAAAA4"]
    assert by_unit["DistAAAAAA2"] == ["FaclAAAAAA5", "FaclAAAAAA6"]


def test_facility_types_from_group_set(tiny_cfg):
    types = tiny_pyramid(tiny_cfg).facilities.set_index("uid")["facility_type"]
    assert types["FaclAAAAAA1"] == "Hospital"
    assert types["FaclAAAAAA2"] == "Health Centre"
    assert types["FaclAAAAAA4"] == "Health Post"  # membership listed on the org unit side
    assert types["FaclAAAAAA6"] == "unknown"  # in no group of the set; "Public" is ignored


def test_type_key_code(tiny_cfg):
    tiny_cfg.pyramid.facility_type.type_key = "code"
    types = tiny_pyramid(tiny_cfg).facilities.set_index("uid")["facility_type"]
    assert types["FaclAAAAAA1"] == "HOSP"


def test_without_facility_types_all_equal(tiny_cfg):
    tiny_cfg.pyramid.facility_type = None
    assert set(tiny_pyramid(tiny_cfg).facilities["facility_type"]) == {"all"}


def test_orphans_and_empty_units_are_reported():
    ou = _org_units_frame([
        {"id": "C", "level": 1},
        {"id": "D1", "level": 2, "parent": {"id": "C"}},
        {"id": "D2", "level": 2, "parent": {"id": "C"}},
        {"id": "F1", "level": 3, "parent": {"id": "D1"}},
        {"id": "F2", "level": 3, "parent": {"id": "MISSING"}},
    ])
    p = build_pyramid(ou, PyramidConfig(source="csv", facility_level=3, population_level=2))
    assert p.pop_units == ["D1"]
    assert p.pop_units_without_facilities == ["D2"]
    assert p.facilities_without_pop_unit == ["F2"]


def test_root_restricts_subtree(tiny_cfg):
    tiny_cfg.pyramid.root = "DistAAAAAA2"
    p = tiny_pyramid(tiny_cfg)
    assert p.pop_units == ["DistAAAAAA2"]
    assert len(p.facilities) == 2


def test_population_projection():
    obs = pd.DataFrame({"ou_uid": ["A", "A", "B"], "year": [2021, 2023, 2021], "pop": [1000.0, 1100.0, 500.0]})
    pop = project_population(obs, ["A", "B"], [2020, 2021, 2022, 2023, 2024], growth=0.03)
    assert pop.loc["A", 2021] == 1000 and pop.loc["A", 2023] == 1100  # observed years kept
    assert pop.loc["A", 2022] == pytest.approx(1000 * 1.03)  # tie -> earlier anchor
    assert pop.loc["A", 2024] == pytest.approx(1100 * 1.03)
    assert pop.loc["A", 2020] == pytest.approx(1000 / 1.03)
    assert pop.loc["B", 2024] == pytest.approx(500 * 1.03 ** 3)


def test_population_missing_unit_raises():
    obs = pd.DataFrame({"ou_uid": ["A"], "year": [2021], "pop": [1.0]})
    with pytest.raises(ConfigError, match="no population"):
        project_population(obs, ["A", "B"], [2021], 0.03)


def test_incidence_missing_unit_raises():
    cfg = IncidenceConfig(source="csv", path=str(TINY / "incidence.csv"))
    assert load_incidence(cfg, ["DistAAAAAA1"]).tolist() == [250.0]
    with pytest.raises(ConfigError, match="no incidence"):
        load_incidence(cfg, ["DistAAAAAA1", "Elsewhere00"])
