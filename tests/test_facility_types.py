"""Facility types from name rules, and zero-weight facilities."""

from pathlib import Path

import pandas as pd
import pytest
import yaml

from malsim.config import ConfigError, load_config
from malsim.inspect import inspect
from malsim.invariants import violations
from malsim.pyramid import types_from_name_rules
from malsim.run import generate, prepare_and_simulate, resolve_config
from conftest import TINY_CONFIG

ROOT = Path(__file__).resolve().parents[1]

RULES = [
    {"pattern": "OPTIQUE|SOUTH CLINIC", "type": "not_malaria"},
    {"pattern": "HOSPITAL", "type": "hospital"},
    {"pattern": "HEALTH (CENTRE|POST)", "type": "primary"},
]


def rules_cfg(tmp_path, weights=None):
    return load_config(TINY_CONFIG, {
        "output": {"dir": str(tmp_path / "out")},
        "pyramid": {"facility_type": {"source": "name_rules", "rules": RULES, "default_type": "other",
                                      "group_set": None}},
        "allocation_weights": {"default": 1.0, "by_type": weights if weights is not None else {"hospital": 3.0}},
        "micro_share": {"by_type": {"hospital": 0.3}},
    })


def test_first_matching_rule_wins_case_and_accent_insensitive():
    names = pd.Series({"a": "Centre Médical X", "b": "CSR-D PUBLIC", "c": "csu-dm public", "d": "Optique Y", "e": "?"})
    rules = [{"pattern": "OPTIQUE", "type": "no"}, {"pattern": "MEDICAL", "type": "clinic"},
             {"pattern": "^CS[RU]\\b", "type": "hc"}, {"pattern": "^CSU", "type": "never"}]
    assert types_from_name_rules(names, rules).to_dict() == {"a": "clinic", "b": "hc", "c": "hc", "d": "no"}


@pytest.mark.parametrize("rule", [{"pattern": "("}, {"pattern": "x"}, {"pattern": "x", "type": "t", "extra": 1}])
def test_invalid_rules_are_rejected(tmp_path, rule):
    with pytest.raises(ConfigError, match="rules"):
        load_config(TINY_CONFIG, {"pyramid": {"facility_type": {"source": "name_rules", "rules": [rule]}}})


def test_name_rule_types_drive_weights_and_microscopy(tmp_path):
    run = prepare_and_simulate(rules_cfg(tmp_path))
    fac = run.pyramid.facilities.set_index("uid")
    assert fac.loc["FaclAAAAAA1", "facility_type"] == "hospital"
    assert fac.loc["FaclAAAAAA2", "facility_type"] == "primary"
    f = run.result.facilities.drop_duplicates("ou_uid").set_index("ou_uid")
    assert f.loc["FaclAAAAAA1", "alloc_weight"] == pytest.approx(3 / 6)  # hospital 3 + three primary 1
    assert f.loc["FaclAAAAAA1", "micro_share"] == 0.3
    assert violations(run.result) == []


def test_zero_weight_facilities_are_not_simulated(tmp_path):
    run = prepare_and_simulate(rules_cfg(tmp_path, {"not_malaria": 0.0}))
    assert run.pyramid.facilities_zero_weight == ["FaclAAAAAA6"]
    assert "FaclAAAAAA6" not in set(run.result.facilities["ou_uid"])
    f = run.result.facilities.drop_duplicates("ou_uid").set_index("ou_uid")
    assert f.loc["FaclAAAAAA5", "alloc_weight"] == 1.0  # last facility left in the south district
    assert violations(run.result) == []


def test_district_with_only_zero_weight_facilities_is_skipped(tmp_path):
    run = prepare_and_simulate(rules_cfg(tmp_path, {"not_malaria": 0.0, "primary": 0.0}))
    assert run.pyramid.pop_units == ["DistAAAAAA1"]
    assert run.pyramid.pop_units_without_facilities == ["DistAAAAAA2"]


def test_zero_weight_listed_in_outputs(tmp_path):
    files = generate(rules_cfg(tmp_path, {"not_malaria": 0.0}))
    summary = yaml.safe_load(files["run_params"].read_text(encoding="utf-8"))["pyramid_summary"]
    assert summary["facilities_zero_weight"] == ["FaclAAAAAA6"]
    assert summary["facility_types"] == {"primary": 4, "hospital": 1}
    report = files["report"].read_text(encoding="utf-8")
    assert "## By facility type" in report and "allocation weight 0): 1" in report


def test_inspect_reports_types_with_weights(tmp_path):
    report = inspect(rules_cfg(tmp_path, {"not_malaria": 0.0}))
    types = report["pyramid"]["facility_types"]
    assert types["not_malaria"] == {"facilities": 1, "allocation_weight": 0.0, "micro_share": 0.0}
    assert report["pyramid"]["facilities_zero_weight"] == 1 and report["pyramid"]["facilities_mapped"] == 5


def test_civ_rules_classify_known_names():
    cfg = resolve_config(ROOT / "examples" / "civ" / "config.yaml", population_de="X")
    names = pd.Series({
        1: "CSR-DM PUBLIC de OUEDALLAH", 2: "CSU-D PUBLIC de BOUNA", 3: "EPHD de BEOUMI", 4: "ESC-HG Protestant de DABOU",
        5: "EPHR de DALOA", 6: "EPHN de COCODY", 7: "CABINET D’OPTICIEN-LUNETIER ATLANTIQUE OPTIQUE",
        8: "CABINET DENTAIRE ORY", 9: "ESP-CENTRE DE SOINS INFIRMIERS SAINT-RAPHAEL", 10: "CLINIQUE MÉDICALE RASSUL",
        11: "CSUS-PMI PUBLIC de TOUMODI", 12: "CSUS-SSSU PUBLIC de ABOISSO", 13: "CSUS-CAT PUBLIC de BOUAFLE",
        14: "ESE-CMS de TAMABO", 15: "ESP-MATERNITE SANTE POUR TOUS de DIVO", 16: "INF-PV PRIVE Mosquée de VAVOUA",
        17: "CENTRE KINESITHERAPIE ET DE READAPTATION D’ANYAMA",
    })
    got = types_from_name_rules(names, cfg.pyramid.facility_type.rules).to_dict()
    assert got == {
        1: "rural_health_centre", 2: "urban_health_centre", 3: "hospital_district", 4: "hospital_district",
        5: "hospital_regional", 6: "hospital_national", 7: "not_malaria", 8: "not_malaria", 9: "nursing_post",
        10: "clinic", 11: "mch_centre", 12: "school_health", 13: "specialised", 14: "occupational",
        15: "maternity", 16: "nursing_post", 17: "not_malaria",
    }
    assert cfg.allocation_weights.get("not_malaria") == 0.0
