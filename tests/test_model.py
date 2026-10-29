import numpy as np
import pandas as pd
import pytest

from malsim import model
from malsim.invariants import violations
from malsim.run import prepare_and_simulate
from conftest import tiny_with


def test_month_weight_is_flat():
    periods = model.make_periods("2021-01", 24)
    w = model.month_weight_matrix(["A", "B"], periods)
    assert w.shape == (2, 24)
    assert np.allclose(w, 1 / 12)


def test_expected_cases_formula():
    pop = np.array([[120000.0, 120000.0]])
    exp = model.expected_cases(pop, np.array([250.0]), np.full((1, 2), 1 / 12), 0.6)
    assert exp == pytest.approx(np.full((1, 2), 120000 * 0.25 / 12 * 0.6))


def test_tiny_run_ground_truth_matches_inputs(tiny_cfg):
    run = prepare_and_simulate(tiny_cfg)
    u = run.result.units.set_index(["ou_uid", "period"])
    assert u.loc[("DistAAAAAA1", "202101"), "pop"] == 120000
    assert u.loc[("DistAAAAAA1", "202201"), "pop"] == pytest.approx(120000 * 1.03)
    assert u.loc[("DistAAAAAA2", "202101"), "expected_cases"] == pytest.approx(80000 * 0.12 / 12 * 0.6)


def test_allocation_weights_by_type(tiny_cfg):
    run = prepare_and_simulate(tiny_cfg)
    f = run.result.facilities.drop_duplicates("ou_uid").set_index("ou_uid")
    # North: Hospital 3, HC 2, HC 2, HP 1 -> /8. South: HC 2, unknown 1 -> /3.
    assert f.loc["FaclAAAAAA1", "alloc_weight"] == pytest.approx(3 / 8)
    assert f.loc["FaclAAAAAA4", "alloc_weight"] == pytest.approx(1 / 8)
    assert f.loc["FaclAAAAAA6", "alloc_weight"] == pytest.approx(1 / 3)
    assert f.groupby("pop_unit")["alloc_weight"].sum().tolist() == pytest.approx([1.0, 1.0])
    assert f.loc["FaclAAAAAA1", "micro_share"] == 0.3
    assert f.loc["FaclAAAAAA2", "micro_share"] == 0.0


def test_no_microscopy_where_share_is_zero(tiny_cfg):
    f = prepare_and_simulate(tiny_cfg).result.facilities
    assert (f.loc[f["micro_share"] == 0, "tested_microscopy"] == 0).all()
    assert f.loc[f["ou_uid"] == "FaclAAAAAA1", "tested_microscopy"].sum() > 0


def test_invariants_hold_on_tiny_run(tiny_cfg):
    assert violations(prepare_and_simulate(tiny_cfg).result) == []


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_invariants_hold_under_stockouts_and_extremes(tmp_path, seed):
    cfg = tiny_with(tmp_path, {
        "run": {"seed": seed, "months": 60},
        "rates": {"positivity": 0.9, "testing_rate": 0.5, "severe_rate": 0.5, "cfr": 0.5, "act_rate": 1.0},
        "stocks": {"stock_start_months": 0, "delivery_factor": 0.7},
    })
    result = prepare_and_simulate(cfg).result
    assert violations(result) == []
    assert (result.facilities["unmet_act"] > 0).any()  # stockouts actually happen
    assert (result.facilities["stock_act_end"] >= 0).all()


def test_facility_sum_equals_unit_confirmed(tiny_cfg):
    r = prepare_and_simulate(tiny_cfg).result
    fac = r.facilities.groupby(["pop_unit", "period"])["confirmed"].sum()
    unit = r.units.set_index(["ou_uid", "period"])["confirmed"]
    assert fac.to_numpy().tolist() == unit.to_numpy().tolist()


def test_simulate_stock_by_hand():
    use = np.array([[5, 30, 0, 10]])
    expected = np.full((1, 4), 10.0)
    s = model.simulate_stock(use, expected, start_months=2, delivery_factor=1.0)
    assert s["start"].tolist() == [[20, 25, 5, 15]]
    assert s["delivery"].tolist() == [[10, 10, 10, 10]]
    assert s["end"].tolist() == [[25, 5, 15, 15]]
    assert s["unmet"].tolist() == [[0, 0, 0, 0]]
    s = model.simulate_stock(np.array([[50, 0]]), np.full((1, 2), 10.0), 1, 1.0)
    assert s["end"].tolist() == [[0, 10]]
    assert s["unmet"].tolist() == [[30, 0]]


def test_implied_incidence_close_to_input(tmp_path):
    # Large population so Poisson noise is small; still uses the tiny pyramid.
    cfg = tiny_with(tmp_path, {"run": {"months": 60}})
    run = prepare_and_simulate(cfg)
    run.population *= 100
    result = model.simulate(cfg, run.pyramid, run.population, run.incidence)
    u = result.units
    exposure = (u["pop"] * u["month_weight"]).groupby(u["ou_uid"]).sum()
    implied = u.groupby("ou_uid")["confirmed"].sum() / exposure * 1000 / cfg.rates.care_seeking
    assert implied.to_numpy() == pytest.approx(run.incidence.to_numpy(), rel=0.01)


def test_streams_are_independent_across_steps(tmp_path):
    """Changing a later-step parameter must not change earlier draws."""
    a = prepare_and_simulate(tiny_with(tmp_path, {"rates": {"cfr": 0.02}})).result.facilities
    b = prepare_and_simulate(tiny_with(tmp_path, {"rates": {"cfr": 0.5}})).result.facilities
    for col in ["confirmed", "confirmed_u5", "tested_microscopy", "act_treated", "severe"]:
        assert a[col].equals(b[col]), col
    assert not a["deaths"].equals(b["deaths"])
