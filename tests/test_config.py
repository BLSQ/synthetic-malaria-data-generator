from pathlib import Path

import pytest

from malsim.config import ConfigError, from_dict, load_config
from conftest import TINY, TINY_CONFIG


def minimal(**extra):
    data = {
        "pyramid": {"source": "csv", "path": "p.csv", "facility_level": 4, "population_level": 2},
        "population": {"path": "pop.csv"},
        "incidence": {"path": "inc.csv"},
    }
    data.update(extra)
    return data


def test_defaults_fill_in():
    cfg = from_dict(minimal())
    assert cfg.run.months == 60
    assert cfg.run.pop_growth == 0.03
    assert cfg.rates.positivity == 0.45
    assert cfg.micro_share.get("anything") == 0.0
    assert cfg.allocation_weights.get("anything") == 1.0
    assert cfg.stocks.stock_start_months == 2


def test_example_config_resolves_relative_paths():
    cfg = load_config(TINY_CONFIG)
    assert Path(cfg.pyramid.path) == (TINY / "metadata.json").resolve()
    assert Path(cfg.mapping) == (TINY / "mapping.yaml").resolve()
    assert cfg.micro_share.get("Hospital") == 0.3


def test_unknown_key_is_rejected():
    with pytest.raises(ConfigError, match="unknown key"):
        from_dict(minimal(rates={"positivty": 0.5}))


def test_missing_section_is_rejected():
    data = minimal()
    del data["incidence"]
    with pytest.raises(ConfigError, match="incidence"):
        from_dict(data)


@pytest.mark.parametrize("section,key,value", [
    ("rates", "positivity", 0.0),
    ("rates", "testing_rate", 1.2),
    ("rates", "cfr", -0.1),
    ("run", "start", "2021-13"),
    ("run", "months", 0),
    ("stocks", "delivery_factor", -1),
])
def test_invalid_values_are_rejected(section, key, value):
    with pytest.raises(ConfigError):
        from_dict(minimal(**{section: {key: value}}))


def test_population_level_must_be_above_facility_level():
    data = minimal()
    data["pyramid"]["population_level"] = 4
    with pytest.raises(ConfigError, match="population_level"):
        from_dict(data)


def test_api_source_validates_without_url():
    # The URL may come from a passed client (OpenHEXA connection); Dhis2Client.from_config checks it otherwise.
    data = minimal()
    data["pyramid"] = {"source": "api", "facility_level": 4, "population_level": 2}
    assert from_dict(data).dhis2.url is None


def test_raster_with_dhis2_geometry_needs_no_geometry_path():
    cfg = from_dict(minimal(incidence={"source": "raster", "raster_path": "x.tif", "geometry_source": "dhis2"}))
    assert cfg.incidence.geometry_path is None
    with pytest.raises(ConfigError, match="geometry_path"):
        from_dict(minimal(incidence={"source": "raster", "raster_path": "x.tif"}))
