"""The v1 generative model.

Each step is its own function on plain arrays, so later versions can swap one
step (seasonal month weights, stock feedback on testing, missing reports)
without touching the others. Arrays are shaped (units, months) or
(facilities, months).

Every random step draws from its own stream, seeded from (seed, step id), so
adding a new step later does not change the draws of existing ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from .config import AllocationConfig, Config, ConfigError, RatesConfig
from .pyramid import Pyramid

# Stable stream ids: never renumber, only append.
STREAMS = {
    "confirmed": 1,
    "allocation": 2,
    "age": 3,
    "microscopy": 4,
    "act": 5,
    "severe": 6,
    "deaths": 7,
}

# Facility-level elements, in output order.
ELEMENTS = [
    "confirmed",
    "confirmed_u5",
    "confirmed_5plus",
    "tested_total",
    "tested_microscopy",
    "tested_rdt",
    "suspected",
    "act_treated",
    "severe",
    "deaths",
    "stock_rdt_end",
    "stock_act_end",
]


def rng_for(seed: int, step: str) -> np.random.Generator:
    return np.random.default_rng([seed, STREAMS[step]])


def round_half_up(x: np.ndarray) -> np.ndarray:
    return np.floor(np.asarray(x, dtype=float) + 0.5).astype(np.int64)


def make_periods(start: str, months: int) -> pd.PeriodIndex:
    return pd.period_range(start=pd.Period(start, freq="M"), periods=months, freq="M")


def period_ids(periods: pd.PeriodIndex) -> list[str]:
    """DHIS2 monthly period ids, e.g. 202101."""
    return [p.strftime("%Y%m") for p in periods]


# --- step 1: expected cases per population unit --------------------------------

def month_weight(unit: str, period: pd.Period) -> float:
    """Share of the annual incidence falling in this month. Flat in v1."""
    return 1.0 / 12.0


def month_weight_matrix(units: list[str], periods: pd.PeriodIndex,
                        weight_fn: Callable[[str, pd.Period], float] = month_weight) -> np.ndarray:
    return np.array([[weight_fn(u, p) for p in periods] for u in units], dtype=float)


def population_matrix(pop_by_year: pd.DataFrame, units: list[str], periods: pd.PeriodIndex) -> np.ndarray:
    years = [p.year for p in periods]
    return pop_by_year.loc[units, years].to_numpy(dtype=float)


def expected_cases(pop: np.ndarray, incidence_per_1000: np.ndarray, weights: np.ndarray,
                   care_seeking: float) -> np.ndarray:
    return pop * (incidence_per_1000[:, None] / 1000.0) * weights * care_seeking


# --- step 2: confirmed cases per population unit -------------------------------

def draw_confirmed(expected: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    return rng.poisson(expected).astype(np.int64)


# --- step 3: allocation to facilities ------------------------------------------

def allocation_weights(facilities: pd.DataFrame, cfg: AllocationConfig) -> np.ndarray:
    """Per-facility weights from facility type, normalised to sum to 1 within each population unit."""
    raw = facilities["facility_type"].map(cfg.get).astype(float)
    totals = raw.groupby(facilities["pop_unit"]).transform("sum")
    zero = sorted(set(facilities.loc[totals == 0, "pop_unit"]))
    if zero:
        raise ConfigError(f"allocation weights are all zero for population unit(s) {zero[:5]}")
    return (raw / totals).to_numpy()


def allocate(confirmed: np.ndarray, unit_index: np.ndarray, weights: np.ndarray,
             rng: np.random.Generator) -> np.ndarray:
    """Split each unit-month count across the unit's facilities with a Multinomial."""
    out = np.zeros((len(unit_index), confirmed.shape[1]), dtype=np.int64)
    for u in range(confirmed.shape[0]):
        idx = np.flatnonzero(unit_index == u)
        p = weights[idx] / weights[idx].sum()
        out[idx, :] = rng.multinomial(confirmed[u], p).T
    return out


# --- step 4: facility elements, all derived from confirmed ---------------------

def split_age(confirmed: np.ndarray, u5_share: float, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    u5 = rng.binomial(confirmed, u5_share)
    return u5, confirmed - u5


def tested_from_confirmed(confirmed: np.ndarray, positivity: float) -> np.ndarray:
    return round_half_up(confirmed / positivity)


def split_test_method(tested: np.ndarray, micro_share: np.ndarray,
                      rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    micro = rng.binomial(tested, micro_share[:, None])
    return micro, tested - micro


def suspected_from_tested(tested: np.ndarray, testing_rate: float) -> np.ndarray:
    return round_half_up(tested / testing_rate)


def treat(confirmed: np.ndarray, act_rate: float, rng: np.random.Generator) -> np.ndarray:
    return rng.binomial(confirmed, act_rate)


def severe_cases(confirmed: np.ndarray, severe_rate: float, rng: np.random.Generator) -> np.ndarray:
    return rng.binomial(confirmed, severe_rate)


def deaths_from_severe(severe: np.ndarray, cfr: float, rng: np.random.Generator) -> np.ndarray:
    return rng.binomial(severe, cfr)


# --- step 5: stocks -------------------------------------------------------------

def expected_use(expected_fac: np.ndarray, micro_share: np.ndarray, rates: RatesConfig) -> tuple[np.ndarray, np.ndarray]:
    """Expected monthly RDT and ACT consumption per facility (no randomness)."""
    rdt = expected_fac / rates.positivity * (1.0 - micro_share[:, None])
    act = expected_fac * rates.act_rate
    return rdt, act


def simulate_stock(use: np.ndarray, expected: np.ndarray, start_months: float,
                   delivery_factor: float) -> dict[str, np.ndarray]:
    """Stock balance: end = max(0, start + delivery - use); next start = end.

    Starting stock covers `start_months` of the first month's expected use;
    deliveries are deterministic, `delivery_factor` x that month's expected use.
    In v1 stock never constrains use; any shortfall is recorded as `unmet`.
    """
    delivery = round_half_up(expected * delivery_factor)
    start = np.zeros_like(use)
    end = np.zeros_like(use)
    unmet = np.zeros_like(use)
    s = round_half_up(start_months * expected[:, 0])
    for m in range(use.shape[1]):
        start[:, m] = s
        available = s + delivery[:, m]
        end[:, m] = np.maximum(0, available - use[:, m])
        unmet[:, m] = np.maximum(0, use[:, m] - available)
        s = end[:, m]
    return {"start": start, "delivery": delivery, "end": end, "unmet": unmet}


# --- orchestration ---------------------------------------------------------------

@dataclass
class SimulationResult:
    periods: pd.PeriodIndex
    units: pd.DataFrame  # one row per population unit x month
    facilities: pd.DataFrame  # one row per facility x month


def _long(uids: list[str], periods: pd.PeriodIndex, columns: dict[str, np.ndarray]) -> pd.DataFrame:
    n_m = len(periods)
    data = {"ou_uid": np.repeat(np.asarray(uids, dtype=object), n_m),
            "period": np.tile(np.asarray(period_ids(periods), dtype=object), len(uids))}
    for name, arr in columns.items():
        arr = np.asarray(arr)
        data[name] = arr.reshape(-1) if arr.ndim == 2 else np.repeat(arr, n_m)
    return pd.DataFrame(data)


def simulate(cfg: Config, pyramid: Pyramid, pop_by_year: pd.DataFrame, incidence: pd.Series) -> SimulationResult:
    seed, rates = cfg.run.seed, cfg.rates
    periods = make_periods(cfg.run.start, cfg.run.months)
    units = pyramid.pop_units
    fac = pyramid.facilities

    # Population units
    pop = population_matrix(pop_by_year, units, periods)
    inc = incidence.loc[units].to_numpy(dtype=float)
    weights = month_weight_matrix(units, periods)
    expected_u = expected_cases(pop, inc, weights, rates.care_seeking)
    confirmed_u = draw_confirmed(expected_u, rng_for(seed, "confirmed"))

    # Facilities
    unit_pos = {u: i for i, u in enumerate(units)}
    unit_index = fac["pop_unit"].map(unit_pos).to_numpy()
    alloc_w = allocation_weights(fac, cfg.allocation_weights)
    micro_share = fac["facility_type"].map(cfg.micro_share.get).to_numpy(dtype=float)
    confirmed = allocate(confirmed_u, unit_index, alloc_w, rng_for(seed, "allocation"))
    expected_f = expected_u[unit_index] * alloc_w[:, None]

    u5, o5 = split_age(confirmed, rates.u5_share, rng_for(seed, "age"))
    tested = tested_from_confirmed(confirmed, rates.positivity)
    micro, rdt = split_test_method(tested, micro_share, rng_for(seed, "microscopy"))
    suspected = suspected_from_tested(tested, rates.testing_rate)
    act = treat(confirmed, rates.act_rate, rng_for(seed, "act"))
    severe = severe_cases(confirmed, rates.severe_rate, rng_for(seed, "severe"))
    deaths = deaths_from_severe(severe, rates.cfr, rng_for(seed, "deaths"))

    exp_rdt, exp_act = expected_use(expected_f, micro_share, rates)
    st = cfg.stocks
    rdt_stock = simulate_stock(rdt, exp_rdt, st.stock_start_months, st.delivery_factor)
    act_stock = simulate_stock(act, exp_act, st.stock_start_months, st.delivery_factor)

    units_df = _long(units, periods, {
        "pop": pop,
        "incidence_per_1000": inc,
        "month_weight": weights,
        "expected_cases": expected_u,
        "confirmed": confirmed_u,
        "n_facilities": np.bincount(unit_index, minlength=len(units)),
    })
    fac_df = _long(fac["uid"].tolist(), periods, {
        "pop_unit": fac["pop_unit"].to_numpy(dtype=object),
        "facility_type": fac["facility_type"].to_numpy(dtype=object),
        "alloc_weight": alloc_w,
        "micro_share": micro_share,
        "expected_cases": expected_f,
        "confirmed": confirmed,
        "confirmed_u5": u5,
        "confirmed_5plus": o5,
        "tested_total": tested,
        "tested_microscopy": micro,
        "tested_rdt": rdt,
        "suspected": suspected,
        "act_treated": act,
        "severe": severe,
        "deaths": deaths,
        "stock_rdt_start": rdt_stock["start"],
        "delivery_rdt": rdt_stock["delivery"],
        "expected_use_rdt": exp_rdt,
        "stock_rdt_end": rdt_stock["end"],
        "unmet_rdt": rdt_stock["unmet"],
        "stock_act_start": act_stock["start"],
        "delivery_act": act_stock["delivery"],
        "expected_use_act": exp_act,
        "stock_act_end": act_stock["end"],
        "unmet_act": act_stock["unmet"],
    })
    return SimulationResult(periods, units_df, fac_df)
