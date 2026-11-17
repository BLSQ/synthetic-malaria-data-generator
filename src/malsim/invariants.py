"""Consistency checks on a simulation. Run on every generation and in the tests."""

from __future__ import annotations

import pandas as pd

from .model import SimulationResult


class InvariantError(AssertionError):
    pass


def _bad(mask: pd.Series, df: pd.DataFrame, rule: str) -> list[str]:
    n = int(mask.sum())
    if not n:
        return []
    first = df.loc[mask].iloc[0]
    return [f"{rule}: {n} row(s) violate it, e.g. {first['ou_uid']} {first['period']}"]


def _band_names(f: pd.DataFrame) -> list[str]:
    prefix = "confirmed__"
    return [c[len(prefix):] for c in f.columns if c.startswith(prefix)]


def _counts(f: pd.DataFrame, band: str | None) -> dict[str, pd.Series]:
    """The case counts for one age band, or the facility totals when band is None."""
    def col(name: str, total: str | None = None) -> pd.Series:
        return f[f"{name}__{band}"] if band is not None else f[total or name]
    return {
        "confirmed": col("confirmed"),
        "tested": col("tested", "tested_total"),
        "suspected": col("suspected"),
        "presumed": col("presumed"),
        "simple": col("confirmed_simple"),
        "severe": col("confirmed_severe"),
    }


def violations(result: SimulationResult) -> list[str]:
    f = result.facilities.sort_values(["ou_uid", "period"], kind="stable")
    u = result.units
    errs: list[str] = []
    bands = _band_names(f)
    for band in [None, *bands]:
        c = _counts(f, band)
        tag = f" [{band}]" if band is not None else ""
        errs += _bad(c["simple"] + c["severe"] != c["confirmed"], f, f"confirmed_simple + confirmed_severe = confirmed{tag}")
        errs += _bad(c["confirmed"] > c["tested"], f, f"confirmed <= tested{tag}")
        errs += _bad(c["tested"] > c["suspected"], f, f"tested <= suspected{tag}")
        errs += _bad(c["presumed"] > c["suspected"] - c["tested"], f, f"presumed <= suspected - tested{tag}")
    for name, total in [("confirmed", "confirmed"), ("tested", "tested_total"), ("suspected", "suspected"),
                        ("presumed", "presumed"), ("confirmed_simple", "confirmed_simple"),
                        ("confirmed_severe", "confirmed_severe")]:
        band_sum = sum((f[f"{name}__{b}"] for b in bands), pd.Series(0, index=f.index))
        errs += _bad(band_sum != f[total], f, f"sum of {name} over age bands = {total}")
    errs += _bad(f["tested_rdt"] + f["tested_microscopy"] != f["tested_total"], f, "tested_rdt + tested_microscopy = tested_total")
    errs += _bad(f["act_treated"] > f["confirmed"], f, "act_treated <= confirmed")
    errs += _bad(f["deaths"] > f["confirmed_severe"], f, "deaths <= confirmed_severe")
    count_cols = [c for c in f.columns if f[c].dtype.kind == "i"]
    neg = (f[count_cols] < 0).any(axis=1)
    errs += _bad(neg, f, "all counts >= 0")

    allocated = f.groupby(["pop_unit", "period"])["confirmed"].sum()
    drawn = u.set_index(["ou_uid", "period"])["confirmed"]
    drawn.index.names = ["pop_unit", "period"]
    diff = allocated.reindex(drawn.index, fill_value=0) != drawn
    if diff.any():
        pu, per = diff[diff].index[0]
        errs.append(f"sum of facility confirmed = unit confirmed: {int(diff.sum())} unit-month(s) differ, e.g. {pu} {per}")
    return errs


def check(result: SimulationResult) -> None:
    errs = violations(result)
    if errs:
        raise InvariantError("invariant violations:\n  " + "\n  ".join(errs))
