"""Output writers: tidy CSV, dataValueSets JSON, ground truth, run params, summary report."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from . import __version__
from .config import Config, ConfigError
from .model import ELEMENTS, SimulationResult
from .pyramid import Pyramid

log = logging.getLogger(__name__)


# --- data values ----------------------------------------------------------------

def tidy_datavalues(result: SimulationResult) -> pd.DataFrame:
    """Facility elements in long form: ou_uid, period, element, value."""
    f = result.facilities.sort_values(["ou_uid", "period"], kind="stable")
    long = f.melt(id_vars=["ou_uid", "period"], value_vars=ELEMENTS, var_name="element", value_name="value")
    long["element"] = pd.Categorical(long["element"], categories=ELEMENTS, ordered=True)
    long = long.sort_values(["ou_uid", "period", "element"], kind="stable").reset_index(drop=True)
    long["element"] = long["element"].astype(str)
    long["value"] = long["value"].astype(np.int64)
    return long


def write_datavalues_csv(tidy: pd.DataFrame, path: Path) -> None:
    tidy.to_csv(path, index=False, lineterminator="\n")


def load_mapping(path: str) -> dict:
    """Mapping YAML:

        attributeOptionCombo: HllvX50cXC0   # optional default
        elements:
          confirmed_u5: {dataElement: abc, categoryOptionCombo: def}
    """
    with open(path, encoding="utf-8") as fh:
        mapping = yaml.safe_load(fh) or {}
    elements = mapping.get("elements") or {}
    unknown = sorted(set(elements) - set(ELEMENTS))
    if unknown:
        raise ConfigError(f"mapping has unknown element(s) {unknown}; known: {ELEMENTS}")
    for name, m in elements.items():
        if not isinstance(m, dict) or not m.get("dataElement"):
            raise ConfigError(f"mapping.elements.{name} needs a dataElement UID")
    return mapping


def datavalueset(tidy: pd.DataFrame, mapping: dict) -> dict:
    elements = mapping.get("elements") or {}
    unmapped = [e for e in ELEMENTS if e not in elements]
    if unmapped:
        log.info("not exported to dataValueSets (unmapped): %s", ", ".join(unmapped))
    default_aoc = mapping.get("attributeOptionCombo")
    values = []
    for row in tidy[tidy["element"].isin(list(elements))].itertuples(index=False):
        m = elements[row.element]
        dv = {"dataElement": m["dataElement"], "period": row.period, "orgUnit": row.ou_uid}
        if m.get("categoryOptionCombo"):
            dv["categoryOptionCombo"] = m["categoryOptionCombo"]
        aoc = m.get("attributeOptionCombo", default_aoc)
        if aoc:
            dv["attributeOptionCombo"] = aoc
        dv["value"] = str(row.value)
        values.append(dv)
    out = {"dataValues": values}
    if mapping.get("dataSet"):
        out = {"dataSet": mapping["dataSet"], **out}
    return out


def write_datavalues_json(tidy: pd.DataFrame, mapping: dict, path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(datavalueset(tidy, mapping), fh, indent=1)


# --- ground truth -----------------------------------------------------------------

def ground_truth(result: SimulationResult) -> pd.DataFrame:
    units = result.units.assign(role="population_unit", pop_unit=result.units["ou_uid"])
    facs = result.facilities.assign(role="facility")
    cols = ["role", "ou_uid", "period", "pop_unit"]
    gt = pd.concat([units, facs], ignore_index=True, sort=False)
    return gt[cols + [c for c in gt.columns if c not in cols]]


def write_ground_truth(result: SimulationResult, path: Path) -> None:
    ground_truth(result).to_parquet(path, index=False)


# --- run params -------------------------------------------------------------------

def _sha256(path: str | None) -> str | None:
    if not path or not Path(path).is_file():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def code_version() -> dict:
    version = {"package": "malsim", "version": __version__, "git_commit": None}
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent,
                                capture_output=True, text=True, timeout=5)
        if commit.returncode == 0:
            version["git_commit"] = commit.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return version


def write_input_snapshots(out_dir: Path, pyramid: Pyramid, observed_population: pd.DataFrame,
                          incidence: pd.Series, geometries: dict | None) -> dict[str, Path]:
    """Write the inputs as actually used, so runs from the API can be compared and reproduced."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "org_units": out_dir / "org_units.csv",
        "facilities": out_dir / "facilities.csv",
        "population_observed": out_dir / "population_observed.csv",
        "incidence": out_dir / "incidence.csv",
    }
    pyramid.org_units.reset_index().sort_values("uid").to_csv(files["org_units"], index=False, lineterminator="\n")
    pyramid.facilities.to_csv(files["facilities"], index=False, lineterminator="\n")
    (observed_population.sort_values(["ou_uid", "year"])
     .to_csv(files["population_observed"], index=False, lineterminator="\n"))
    incidence.rename_axis("ou_uid").reset_index().to_csv(files["incidence"], index=False, lineterminator="\n")
    if geometries is not None:
        files["population_units_geojson"] = out_dir / "population_units.geojson"
        with files["population_units_geojson"].open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(geometries, fh)
    return files


def write_run_params(cfg: Config, pyramid: Pyramid, path: Path, dhis2: dict | None = None,
                     snapshots: dict[str, Path] | None = None) -> None:
    """`dhis2` holds the instance URL and credential source actually used, never credentials."""
    inputs = {
        "pyramid": cfg.pyramid.path,
        "facility_types": cfg.pyramid.facility_type.path if cfg.pyramid.facility_type else None,
        "population": cfg.population.path,
        "incidence": cfg.incidence.path,
        "incidence_raster": cfg.incidence.raster_path,
        "incidence_geometry": cfg.incidence.geometry_path,
        "mapping": cfg.mapping,
    }
    doc = {
        "seed": cfg.run.seed,
        "code_version": code_version(),
        "config": cfg.to_dict(),
        "dhis2": dhis2,
        "input_sha256": {k: _sha256(v) for k, v in inputs.items() if v},
        "input_snapshot_sha256": {k: _sha256(str(v)) for k, v in (snapshots or {}).items()},
        "pyramid_summary": {
            "population_units_simulated": len(pyramid.pop_units),
            "facilities": len(pyramid.facilities),
            "population_units_without_facilities": pyramid.pop_units_without_facilities,
            "facilities_without_population_unit": pyramid.facilities_without_pop_unit,
            "facilities_closed_before_start": pyramid.facilities_closed,
            "facilities_zero_weight": pyramid.facilities_zero_weight,
            "facility_types": {str(k): int(v) for k, v in pyramid.facilities["facility_type"].value_counts().items()},
        },
    }
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)


# --- summary report ---------------------------------------------------------------

def summary_by_unit_year(result: SimulationResult, care_seeking: float) -> pd.DataFrame:
    # Exposure = sum of pop x month_weight, i.e. person-years at risk in the period covered.
    u = result.units.assign(year=result.units["period"].str[:4].astype(int),
                            exposure=result.units["pop"] * result.units["month_weight"])
    f = result.facilities.assign(year=result.facilities["period"].str[:4].astype(int))
    unit_tot = u.groupby(["ou_uid", "year"]).agg(
        months=("period", "size"),
        pop=("pop", "mean"),
        incidence_input=("incidence_per_1000", "first"),
        exposure=("exposure", "sum"),
        expected_cases=("expected_cases", "sum"),
        confirmed=("confirmed", "sum"),
    )
    fac_tot = (f.rename(columns={"pop_unit": "unit"})
               .groupby(["unit", "year"])[["tested_total", "suspected", "act_treated", "severe", "deaths"]].sum())
    fac_tot.index.names = ["ou_uid", "year"]
    s = unit_tot.join(fac_tot)
    s["reported_incidence_per_1000"] = s["confirmed"] / s["exposure"] * 1000
    s["implied_incidence_per_1000"] = s["reported_incidence_per_1000"] / care_seeking
    s["implied_vs_input"] = s["implied_incidence_per_1000"] / s["incidence_input"]
    return s.drop(columns="exposure").reset_index()


def zero_month_shares(result: SimulationResult) -> pd.DataFrame:
    f = result.facilities
    return pd.DataFrame({
        "element": ELEMENTS,
        "share_zero_facility_months": [float((f[e] == 0).mean()) for e in ELEMENTS],
    })


def by_type(result: SimulationResult) -> pd.DataFrame:
    f = result.facilities
    g = f.groupby("facility_type")
    out = pd.DataFrame({
        "facilities": g["ou_uid"].nunique(),
        "mean_share_of_district": g["alloc_weight"].mean(),
        "confirmed_per_facility_month": g["confirmed"].mean(),
        "share_of_confirmed": g["confirmed"].sum() / f["confirmed"].sum(),
        "microscopy_share": g["tested_microscopy"].sum() / g["tested_total"].sum().where(lambda s: s > 0),
        "share_zero_months": g["confirmed"].apply(lambda s: float((s == 0).mean())),
    })
    return out.sort_values("share_of_confirmed", ascending=False).reset_index()


def _md_table(df: pd.DataFrame, floatfmt: str = "{:.3f}") -> str:
    def fmt(v):
        if isinstance(v, (float, np.floating)):
            return floatfmt.format(v)
        return str(v)
    head = "| " + " | ".join(df.columns) + " |"
    sep = "| " + " | ".join("---" for _ in df.columns) + " |"
    rows = ["| " + " | ".join(fmt(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join([head, sep, *rows])


def write_report(cfg: Config, pyramid: Pyramid, result: SimulationResult, out_dir: Path) -> None:
    by_unit_year = summary_by_unit_year(result, cfg.rates.care_seeking)
    by_unit_year.to_csv(out_dir / "summary_by_unit_year.csv", index=False, lineterminator="\n")
    zeros = zero_month_shares(result)
    zeros.to_csv(out_dir / "summary_zero_months.csv", index=False, lineterminator="\n")

    f = result.facilities
    per_fac_zero = (f["confirmed"] == 0).groupby(f["ou_uid"]).mean()
    totals = by_unit_year.groupby("year")[["pop", "expected_cases", "confirmed", "tested_total", "deaths"]].sum().reset_index()
    overall_ratio = (by_unit_year["confirmed"].sum() / by_unit_year["expected_cases"].sum())
    worst = by_unit_year.assign(dev=(by_unit_year["implied_vs_input"] - 1).abs()).nlargest(10, "dev")

    lines = [
        "# Synthetic malaria data: run summary",
        "",
        f"- Seed: {cfg.run.seed}; periods: {result.periods[0].strftime('%Y%m')} to "
        f"{result.periods[-1].strftime('%Y%m')} ({len(result.periods)} months)",
        f"- Population units simulated: {len(pyramid.pop_units)}; facilities: {len(pyramid.facilities)}",
        f"- Population units skipped (no facilities): {len(pyramid.pop_units_without_facilities)}",
        f"- Facilities dropped (no population unit): {len(pyramid.facilities_without_pop_unit)}",
        f"- Facilities dropped (closed before start): {len(pyramid.facilities_closed)}",
        f"- Facilities not simulated (allocation weight 0): {len(pyramid.facilities_zero_weight)}",
        f"- Confirmed / expected cases, all units and months: {overall_ratio:.4f}",
        "",
        "## Totals per year",
        "",
        _md_table(totals, "{:,.1f}"),
        "",
        "## Implied incidence vs input (largest deviations, unit-years)",
        "",
        "Implied incidence = confirmed / person-years / care_seeking x 1000.",
        "",
        _md_table(worst[["ou_uid", "year", "incidence_input", "implied_incidence_per_1000", "implied_vs_input"]]),
        "",
        "Full table: `summary_by_unit_year.csv`.",
        "",
        "## By facility type",
        "",
        _md_table(by_type(result), "{:.2f}"),
        "",
        "## Share of zero facility-months",
        "",
        _md_table(zeros),
        "",
        f"- Facilities with zero confirmed cases in every month: {int((per_fac_zero == 1).sum())} of {len(per_fac_zero)}",
        f"- Median share of zero-confirmed months per facility: {per_fac_zero.median():.3f}",
        "",
        "## Stocks",
        "",
        f"- Facility-months with RDT shortfall (unmet > 0): {float((f['unmet_rdt'] > 0).mean()):.3f}",
        f"- Facility-months with ACT shortfall (unmet > 0): {float((f['unmet_act'] > 0).mean()):.3f}",
        f"- Facility-months ending with zero RDT stock: {float((f['stock_rdt_end'] == 0).mean()):.3f}",
        f"- Facility-months ending with zero ACT stock: {float((f['stock_act_end'] == 0).mean()):.3f}",
        "",
    ]
    (out_dir / "summary_report.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
