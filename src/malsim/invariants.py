"""Consistency checks on a simulation. Run on every generation and in the tests."""

from __future__ import annotations

import numpy as np
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


def _stock_balance(f: pd.DataFrame, item: str) -> list[str]:
    start, end = f[f"stock_{item}_start"], f[f"stock_{item}_end"]
    use = f["tested_rdt"] if item == "rdt" else f["act_treated"]
    errors = _bad(end != np.maximum(0, start + f[f"delivery_{item}"] - use), f,
                  f"stock_{item}_end = max(0, start + delivery - use)")
    # Next month's start equals this month's end, within each facility.
    prev_end = f.groupby("ou_uid", sort=False)[f"stock_{item}_end"].shift(1)
    carried = prev_end.notna()
    errors += _bad(carried & (start != prev_end), f, f"stock_{item}_start = previous stock_{item}_end")
    return errors


def violations(result: SimulationResult) -> list[str]:
    f = result.facilities.sort_values(["ou_uid", "period"], kind="stable")
    u = result.units
    errs: list[str] = []
    errs += _bad(f["confirmed_u5"] + f["confirmed_5plus"] != f["confirmed"], f, "confirmed_u5 + confirmed_5plus = confirmed")
    errs += _bad(f["tested_rdt"] + f["tested_microscopy"] != f["tested_total"], f, "tested_rdt + tested_microscopy = tested_total")
    errs += _bad(f["confirmed"] > f["tested_total"], f, "confirmed <= tested_total")
    errs += _bad(f["tested_total"] > f["suspected"], f, "tested_total <= suspected")
    errs += _bad(f["act_treated"] > f["confirmed"], f, "act_treated <= confirmed")
    errs += _bad(f["deaths"] > f["severe"], f, "deaths <= severe")
    errs += _bad(f["severe"] > f["confirmed"], f, "severe <= confirmed")
    for item in ("rdt", "act"):
        errs += _bad(f[f"stock_{item}_end"] < 0, f, f"stock_{item}_end >= 0")
        errs += _stock_balance(f, item)
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
