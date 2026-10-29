"""API sources (fake client), raster incidence, closed facilities, credentials, inspect, bundle."""

import json
import logging
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

from malsim.cli import main
from malsim.config import ConfigError, Dhis2Config, load_config
from malsim.dhis2 import Dhis2Client, client_for
from malsim.inspect import inspect
from malsim.run import generate, prepare_and_simulate, resolve_config
from conftest import TINY_CONFIG, TINY_POLYGONS, FakeDhis2Client, write_tiny_raster

ROOT = Path(__file__).resolve().parents[1]
API = {"pyramid": {"source": "api", "path": None},
       "population": {"source": "dhis2", "data_element": "DePopTotal1", "path": None}}


def cfg_with(tmp_path, overrides, name="out"):
    return load_config(TINY_CONFIG, {"output": {"dir": str(tmp_path / name)}, **overrides})


def raster_overrides(tmp_path, geometry_source="dhis2", **extra):
    raster = write_tiny_raster(tmp_path / "inc.tif")
    return {"incidence": {"source": "raster", "path": None, "raster_path": str(raster),
                          "geometry_source": geometry_source, "raster_scale": 1000, **extra}}


def csv_incidence(tmp_path, d1=250.0, d2=125.0):
    path = tmp_path / "incidence.csv"
    path.write_text(f"ou_uid,incidence_per_1000\nDistAAAAAA1,{d1}\nDistAAAAAA2,{d2}\n", encoding="utf-8")
    return {"incidence": {"source": "csv", "path": str(path)}}


def same_datavalues(a: Path, b: Path):
    assert (a / "datavalues.csv").read_bytes() == (b / "datavalues.csv").read_bytes()
    assert (a / "datavalues.json").read_bytes() == (b / "datavalues.json").read_bytes()


# --- API sources ------------------------------------------------------------------

def test_api_sources_match_file_sources(tmp_path):
    generate(cfg_with(tmp_path, {}, "file"))
    generate(cfg_with(tmp_path, API, "api"), client=FakeDhis2Client())
    same_datavalues(tmp_path / "file", tmp_path / "api")


@pytest.mark.parametrize("method", ["raw", "analytics"])
def test_population_methods_read_clean_yearly_values(tmp_path, method):
    cfg = cfg_with(tmp_path, {**API, "population": {**API["population"], "dhis2_method": method}})
    obs = prepare_and_simulate(cfg, FakeDhis2Client()).observed_population
    got = {(r.ou_uid, r.year): r.pop for r in obs.itertuples(index=False)}
    # raw: monthly and facility-level noise ignored, category option combos summed
    assert got == {("DistAAAAAA1", 2021): 120000, ("DistAAAAAA1", 2023): 127000, ("DistAAAAAA2", 2021): 80000}


def test_api_config_needs_no_url_when_client_is_passed(tmp_path):
    cfg = cfg_with(tmp_path, API)
    assert cfg.dhis2.url is None
    run = prepare_and_simulate(cfg, FakeDhis2Client())
    assert len(run.pyramid.facilities) == 6


def test_population_de_override(tmp_path):
    cfg = resolve_config(TINY_CONFIG, population_de="DeOverride1")
    assert (cfg.population.source, cfg.population.data_element) == ("dhis2", "DeOverride1")
    files = generate(cfg_with(tmp_path, {"pyramid": API["pyramid"]}), population_de="DeOverride1",
                     client=FakeDhis2Client())
    params = yaml.safe_load(files["run_params"].read_text(encoding="utf-8"))
    assert params["config"]["population"]["data_element"] == "DeOverride1"


# --- raster incidence ----------------------------------------------------------------

def test_raster_from_dhis2_polygons_matches_csv(tmp_path):
    generate(cfg_with(tmp_path, csv_incidence(tmp_path), "csv"))
    cfg = cfg_with(tmp_path, raster_overrides(tmp_path), "raster")
    run = prepare_and_simulate(cfg, FakeDhis2Client())
    assert run.incidence.tolist() == [250.0, 125.0]
    generate(cfg, client=FakeDhis2Client())
    same_datavalues(tmp_path / "csv", tmp_path / "raster")
    assert (tmp_path / "raster" / "inputs" / "population_units.geojson").is_file()


def test_raster_from_geojson_file(tmp_path):
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"uid": u}, "geometry": g} for u, g in TINY_POLYGONS.items()]}
    path = tmp_path / "districts.geojson"
    path.write_text(json.dumps(gj), encoding="utf-8")
    cfg = cfg_with(tmp_path, raster_overrides(tmp_path, "file", geometry_path=str(path), id_property="uid"))
    assert prepare_and_simulate(cfg).incidence.tolist() == [250.0, 125.0]


def test_district_with_facilities_but_no_polygon_raises(tmp_path):
    cfg = cfg_with(tmp_path, raster_overrides(tmp_path))
    client = FakeDhis2Client(polygons={"DistAAAAAA1": TINY_POLYGONS["DistAAAAAA1"]})
    with pytest.raises(ConfigError, match="no incidence"):
        prepare_and_simulate(cfg, client)


# --- closed facilities ----------------------------------------------------------------

CLOSED = [
    {"id": "FaclClosed1", "name": "Closed 2019", "level": 4, "parent": {"id": "AreaAAAAAA1"},
     "closedDate": "2019-06-01T00:00:00.000"},
    {"id": "FaclClosed2", "name": "Closed 2022", "level": 4, "parent": {"id": "AreaAAAAAA3"},
     "closedDate": "2022-03-01T00:00:00.000"},
]


def test_facilities_closed_before_start_are_dropped(tmp_path):
    run = prepare_and_simulate(cfg_with(tmp_path, API), FakeDhis2Client(extra_org_units=CLOSED))
    assert run.pyramid.facilities_closed == ["FaclClosed1"]
    uids = set(run.pyramid.facilities["uid"])
    assert "FaclClosed1" not in uids and "FaclClosed2" in uids


def test_closed_facilities_kept_when_disabled(tmp_path):
    cfg = cfg_with(tmp_path, {**API, "pyramid": {**API["pyramid"], "drop_closed_before_start": False}})
    run = prepare_and_simulate(cfg, FakeDhis2Client(extra_org_units=CLOSED))
    assert run.pyramid.facilities_closed == []
    assert {"FaclClosed1", "FaclClosed2"} <= set(run.pyramid.facilities["uid"])


# --- credentials -------------------------------------------------------------------------

def test_from_config_needs_url_and_credentials(monkeypatch):
    with pytest.raises(ConfigError, match="dhis2.url"):
        Dhis2Client.from_config(Dhis2Config())
    cfg = Dhis2Config(url="https://x.test", username_env="T_USER", password_env="T_PASS", token_env="T_TOKEN")
    for name in ("T_USER", "T_PASS", "T_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError, match="T_USER and T_PASS"):
        Dhis2Client.from_config(cfg)
    monkeypatch.setenv("T_USER", "someone")
    monkeypatch.setenv("T_PASS", "pw")
    client = Dhis2Client.from_config(cfg)
    assert client.credential_source == "env"
    assert client.base == "https://x.test/api"


def test_passed_client_wins_with_warning(tmp_path, caplog):
    cfg = cfg_with(tmp_path, {**API, "dhis2": {"url": "https://other.test"}})
    fake = FakeDhis2Client()
    with caplog.at_level(logging.WARNING):
        assert client_for(cfg, fake) is fake
    assert "differs from the passed connection" in caplog.text


def test_run_params_has_source_but_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("T_USER", "someone")
    monkeypatch.setenv("T_PASS", "s3cr3t-pw-value")
    client = Dhis2Client.from_config(Dhis2Config(url="https://fake.dhis2.test", username_env="T_USER",
                                                 password_env="T_PASS", token_env="T_TOKEN_UNSET"))
    fake = FakeDhis2Client()
    for name in ("org_units", "group_set", "data_values", "analytics", "org_unit_geometries"):
        setattr(client, name, getattr(fake, name))  # serve data without network
    files = generate(cfg_with(tmp_path, API), client=client)
    text = files["run_params"].read_text(encoding="utf-8")
    assert "s3cr3t-pw-value" not in text
    params = yaml.safe_load(text)
    assert params["dhis2"] == {"url": "https://fake.dhis2.test/api", "credential_source": "env"}
    snaps = params["input_snapshot_sha256"]
    assert {"org_units", "facilities", "population_observed", "incidence"} <= set(snaps)
    assert all(len(h) == 64 for h in snaps.values())


# --- inspect -------------------------------------------------------------------------------

def test_inspect_with_fake_client(tmp_path):
    cfg = cfg_with(tmp_path, {**API, **raster_overrides(tmp_path)})
    report = inspect(cfg, FakeDhis2Client(extra_org_units=CLOSED))
    assert [(lv["level"], lv["name"], lv["org_units"]) for lv in report["levels"]] == [
        (1, "Country", 1), (2, "District", 2), (3, "Area", 3), (4, "Facility", 8)]
    assert report["group_sets"][0]["id"] == "GsFacType01"
    p = report["pyramid"]
    assert (p["facilities_mapped"], p["facilities_closed_before_start"], p["population_units_with_facilities"]) == (7, 1, 2)
    assert report["population"]["units_without_any_value"] == []
    assert report["population"]["units_with_values_by_year"] == {2021: 2, 2023: 1}
    inc = report["incidence"]
    assert inc["incidence_per_1000"] == {"min": 125.0, "median": 187.5, "max": 250.0}
    assert inc["units_without_polygon"] == [] and inc["crs"] == "EPSG:4326"


def test_cli_inspect_file_sources(capsys):
    assert main(["inspect", "--config", str(TINY_CONFIG)]) == 0
    report = yaml.safe_load(capsys.readouterr().out)
    assert report["pyramid"]["facilities_mapped"] == 6


def test_civ_config_needs_population_de():
    with pytest.raises(ConfigError, match="data_element"):
        load_config(ROOT / "examples" / "civ" / "config.yaml")
    cfg = resolve_config(ROOT / "examples" / "civ" / "config.yaml", population_de="DePopTotal1")
    assert (cfg.pyramid.facility_level, cfg.pyramid.population_level) == (4, 3)
    assert Path(cfg.incidence.raster_path).name.endswith("CIV_2023.tiff")


# --- OpenHEXA bundle ------------------------------------------------------------------------

def test_bundle_imports_malsim_from_bundle(tmp_path):
    out = tmp_path / "bundle"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_openhexa.py"), "--out", str(out)], check=True)
    assert (out / "pipeline.py").is_file() and (out / "requirements.txt").is_file()
    found = subprocess.run([sys.executable, "-c", "import malsim, malsim.inspect; print(malsim.__file__)"],
                           cwd=out, capture_output=True, text=True, check=True).stdout.strip()
    assert Path(found).resolve().is_relative_to(out.resolve())
