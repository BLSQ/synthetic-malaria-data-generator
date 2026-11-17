"""The v1 generative model.

Each step is its own function on plain arrays, so later versions can swap one
step (seasonal month weights, missing reports, error injection)
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

from .config import AllocationConfig, Config, ConfigError
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
    "presumed": 8,
}

# Reported elements, in output order. Each is split by age band (a DHIS2 category option combo).
ELEMENTS = [
    "suspected",
    "presumed",
    "confirmed_simple",
    "confirmed_severe",
]

# Quantities simulated per age band (kept in the ground truth as `<name>__<band>`).
BAND_QUANTITIES = ["confirmed", "tested", "suspected", "presumed", "confirmed_simple", "confirmed_severe"]


def band_column(name: str, band: str) -> str:
    return f"{name}__{band}"


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

# Per-band arrays are shaped (bands, facilities, months).

def split_age(confirmed: np.ndarray, shares: list[float], rng: np.random.Generator) -> np.ndarray:
    """Multinomial split of each facility-month count across the age bands."""
    p = np.asarray(shares, dtype=float)
    draws = rng.multinomial(confirmed, p / p.sum())  # shape confirmed.shape + (bands,)
    return np.moveaxis(draws, -1, 0).astype(np.int64)


def severe_cases(confirmed_b: np.ndarray, severe_rates: list[float], rng: np.random.Generator) -> np.ndarray:
    rates = np.asarray(severe_rates, dtype=float).reshape(-1, *([1] * (confirmed_b.ndim - 1)))
    return rng.binomial(confirmed_b, rates)


def tested_from_confirmed(confirmed: np.ndarray, positivity: float) -> np.ndarray:
    return round_half_up(confirmed / positivity)


def split_test_method(tested: np.ndarray, micro_share: np.ndarray,
                      rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    micro = rng.binomial(tested, micro_share[:, None])
    return micro, tested - micro


def suspected_from_tested(tested: np.ndarray, testing_rate: float) -> np.ndarray:
    return round_half_up(tested / testing_rate)


def presumed_cases(suspected: np.ndarray, tested: np.ndarray, presumed_rate: float,
                   rng: np.random.Generator) -> np.ndarray:
    """Untested suspected cases managed as severe malaria without confirmation (présumé)."""
    return rng.binomial(suspected - tested, presumed_rate)


def treat(confirmed: np.ndarray, act_rate: float, rng: np.random.Generator) -> np.ndarray:
    return rng.binomial(confirmed, act_rate)


def deaths_from_severe(severe: np.ndarray, cfr: float, rng: np.random.Generator) -> np.ndarray:
    return rng.binomial(severe, cfr)


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

    # Per age band: everything derives from the band's confirmed cases.
    bands = rates.bands
    by_band: dict[str, np.ndarray] = {}
    by_band["confirmed"] = split_age(confirmed, rates.band_shares, rng_for(seed, "age"))
    by_band["confirmed_severe"] = severe_cases(by_band["confirmed"], [rates.severe_rate_for(b) for b in bands],
                                               rng_for(seed, "severe"))
    by_band["confirmed_simple"] = by_band["confirmed"] - by_band["confirmed_severe"]
    by_band["tested"] = tested_from_confirmed(by_band["confirmed"], rates.positivity)
    by_band["suspected"] = suspected_from_tested(by_band["tested"], rates.testing_rate)
    by_band["presumed"] = presumed_cases(by_band["suspected"], by_band["tested"], rates.presumed_rate,
                                         rng_for(seed, "presumed"))
    totals = {name: arr.sum(axis=0) for name, arr in by_band.items()}

    # Ground truth only (not reported to DHIS2).
    tested = totals["tested"]
    micro, rdt = split_test_method(tested, micro_share, rng_for(seed, "microscopy"))
    act = treat(confirmed, rates.act_rate, rng_for(seed, "act"))
    deaths = deaths_from_severe(totals["confirmed_severe"], rates.cfr, rng_for(seed, "deaths"))
    band_cols = {band_column(name, b): by_band[name][i] for name in BAND_QUANTITIES for i, b in enumerate(bands)}

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
        "confirmed_simple": totals["confirmed_simple"],
        "confirmed_severe": totals["confirmed_severe"],
        "tested_total": tested,
        "tested_microscopy": micro,
        "tested_rdt": rdt,
        "suspected": totals["suspected"],
        "presumed": totals["presumed"],
        "act_treated": act,
        "deaths": deaths,
        **band_cols,
    })
    return SimulationResult(periods, units_df, fac_df)
