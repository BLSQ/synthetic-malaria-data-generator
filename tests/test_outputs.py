import json

import pandas as pd
import yaml

from malsim.cli import main
from malsim.config import load_config
from malsim.model import ELEMENTS
from malsim.run import generate
from conftest import TINY_CONFIG

OUTPUT_FILES = [
    "datavalues.csv",
    "datavalues.json",
    "dhis2_mapping.yaml",
    "ground_truth.parquet",
    "run_params.yaml",
    "summary_report.md",
    "summary_by_unit_year.csv",
    "summary_zero_months.csv",
]


def run_cli(out, *extra):
    assert main(["generate", "--config", str(TINY_CONFIG), "--out", str(out), *extra]) == 0


def test_cli_writes_all_outputs(tmp_path):
    run_cli(tmp_path / "a")
    for name in OUTPUT_FILES:
        assert (tmp_path / "a" / name).is_file(), name

    dv = pd.read_csv(tmp_path / "a" / "datavalues.csv", dtype={"period": str})
    assert list(dv.columns) == ["ou_uid", "period", "element", "age_band", "value"]
    assert len(dv) == 6 * 36 * len(ELEMENTS) * 2
    assert dv["period"].iloc[0] == "202101"
    assert dv["element"].iloc[:8].tolist() == [e for e in ELEMENTS for _ in range(2)]
    assert dv["age_band"].iloc[:2].tolist() == ["u5", "5plus"]

    gt = pd.read_parquet(tmp_path / "a" / "ground_truth.parquet")
    assert set(gt["role"]) == {"population_unit", "facility"}
    assert {"expected_cases", "alloc_weight", "tested_total", "act_treated", "deaths",
            "confirmed__u5", "presumed__5plus"} <= set(gt.columns)
    assert not any(c.startswith(("stock_", "unmet_")) for c in gt.columns)
    assert (gt["role"] == "facility").sum() == 6 * 36

    params = yaml.safe_load((tmp_path / "a" / "run_params.yaml").read_text(encoding="utf-8"))
    assert params["seed"] == 42
    assert params["config"]["rates"]["positivity"] == 0.45
    assert params["code_version"]["version"]
    assert len(params["input_sha256"]["pyramid"]) == 64


def test_same_seed_same_bytes(tmp_path):
    run_cli(tmp_path / "a")
    run_cli(tmp_path / "b")
    for name in OUTPUT_FILES:
        if name == "run_params.yaml":  # embeds the output dir, compared below
            continue
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name
    pa, pb = (yaml.safe_load((tmp_path / d / "run_params.yaml").read_text(encoding="utf-8")) for d in "ab")
    pa["config"]["output"]["dir"] = pb["config"]["output"]["dir"] = None
    assert pa == pb


def test_different_seed_different_data(tmp_path):
    run_cli(tmp_path / "a")
    run_cli(tmp_path / "b", "--seed", "7")
    assert (tmp_path / "a" / "datavalues.csv").read_bytes() != (tmp_path / "b" / "datavalues.csv").read_bytes()


def test_datavalueset_uses_mapping(tmp_path):
    run_cli(tmp_path / "a")
    payload = json.loads((tmp_path / "a" / "datavalues.json").read_text(encoding="utf-8"))
    assert payload["dataSet"] == "DsMonthly01"
    dvs = payload["dataValues"]
    assert len(dvs) == 6 * 36 * 4 * 2  # zeros included (skip_zeros: false)
    assert {d["categoryOptionCombo"] for d in dvs} == {"CocUnder5Y1", "Coc5PlusYr1"}
    assert all(d["attributeOptionCombo"] == "HllvX50cXC0" for d in dvs)
    assert all(isinstance(d["value"], str) for d in dvs)
    csv = pd.read_csv(tmp_path / "a" / "datavalues.csv", dtype={"period": str})
    sel = csv[(csv["element"] == "presumed") & (csv["age_band"] == "u5")]
    got = [d["value"] for d in dvs if d["dataElement"] == "DePresumed1" and d["categoryOptionCombo"] == "CocUnder5Y1"]
    assert got == sel["value"].astype(str).tolist()
    resolved = yaml.safe_load((tmp_path / "a" / "dhis2_mapping.yaml").read_text(encoding="utf-8"))
    assert resolved["age_bands"] == {"u5": "CocUnder5Y1", "5plus": "Coc5PlusYr1"}


def test_skip_zeros(tmp_path):
    mapping = yaml.safe_load((TINY_CONFIG.parent / "mapping.yaml").read_text(encoding="utf-8"))
    mapping["skip_zeros"] = True
    (tmp_path / "m.yaml").write_text(yaml.safe_dump(mapping), encoding="utf-8")
    cfg = load_config(TINY_CONFIG, {"mapping": str(tmp_path / "m.yaml")})
    generate(cfg, out_dir=tmp_path / "o")
    dvs = json.loads((tmp_path / "o" / "datavalues.json").read_text(encoding="utf-8"))["dataValues"]
    csv = pd.read_csv(tmp_path / "o" / "datavalues.csv")
    assert all(d["value"] != "0" for d in dvs)
    assert len(dvs) == int((csv["value"] != 0).sum())


def test_no_mapping_no_json(tmp_path):
    cfg = load_config(TINY_CONFIG, {"mapping": None})
    files = generate(cfg, out_dir=tmp_path / "o")
    assert "datavalues_json" not in files
    assert not (tmp_path / "o" / "datavalues.json").exists()
    assert (tmp_path / "o" / "datavalues.csv").exists()


def test_report_summary(tmp_path):
    run_cli(tmp_path / "a")
    s = pd.read_csv(tmp_path / "a" / "summary_by_unit_year.csv")
    assert len(s) == 2 * 3
    assert {"implied_incidence_per_1000", "implied_vs_input"} <= set(s.columns)
    assert s["implied_vs_input"].between(0.8, 1.2).all()
    report = (tmp_path / "a" / "summary_report.md").read_text(encoding="utf-8")
    assert "Share of zero facility-months" in report
    assert "Reported totals by element and age band" in report
    assert "Stocks" not in report
