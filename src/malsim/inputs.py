"""Population and baseline incidence inputs for population-level units."""

from __future__ import annotations

import json
import logging

import pandas as pd

from .config import ConfigError, IncidenceConfig, PopulationConfig
from .dhis2 import Dhis2Client

log = logging.getLogger(__name__)


# --- population --------------------------------------------------------------

def read_population(cfg: PopulationConfig, pop_units: list[str], years: list[int],
                    client: Dhis2Client | None = None) -> pd.DataFrame:
    """Observed population as a long frame: ou_uid, year, pop."""
    if cfg.source == "csv":
        df = pd.read_csv(cfg.path, dtype={"ou_uid": str})
    elif cfg.source == "dhis2":
        if client is None:
            raise ConfigError("population.source is dhis2 but no DHIS2 client is configured")
        if cfg.dhis2_method == "raw":
            df = _population_from_raw_values(cfg, client, pop_units, years)
        else:
            df = _population_from_analytics(cfg, client, pop_units, years)
    else:
        raise ConfigError(f"unknown population.source {cfg.source}")
    missing_cols = {"ou_uid", "year", "pop"} - set(df.columns)
    if missing_cols:
        raise ConfigError(f"population input is missing column(s) {sorted(missing_cols)}")
    df = df[["ou_uid", "year", "pop"]].dropna()
    df["year"] = df["year"].astype(int)
    df["pop"] = df["pop"].astype(float)
    if (df["pop"] < 0).any():
        raise ConfigError("population input has negative values")
    return df[df["ou_uid"].isin(pop_units)].reset_index(drop=True)


def _window(years: list[int]) -> list[int]:
    # A wider window than the run, so that projection has anchors close by.
    return list(range(min(years) - 5, max(years) + 1))


def _population_from_raw_values(cfg, client, pop_units, years) -> pd.DataFrame:
    """Yearly values stored at the population units, summed over category option combos.

    Values with other period types (monthly, weekly...) are ignored and logged.
    """
    window = _window(years)
    values = client.data_values(cfg.data_element, pop_units, f"{window[0]}-01-01", f"{window[-1]}-12-31")
    wanted = set(pop_units)
    rows, skipped = [], 0
    for v in values:
        period = str(v.get("period", ""))
        if v.get("orgUnit") not in wanted or not (len(period) == 4 and period.isdigit()):
            skipped += 1
            continue
        rows.append({"ou_uid": v["orgUnit"], "year": int(period), "pop": float(v["value"])})
    if skipped:
        log.info("population: ignored %d values that are not yearly or not at the population level", skipped)
    df = pd.DataFrame(rows, columns=["ou_uid", "year", "pop"])
    return df.groupby(["ou_uid", "year"], as_index=False)["pop"].sum()


def _population_from_analytics(cfg, client, pop_units, years) -> pd.DataFrame:
    window = _window(years)
    # Query the population units directly, in chunks to keep URLs short.
    rows: list[dict] = []
    for i in range(0, len(pop_units), 200):
        chunk = pop_units[i:i + 200]
        rows += client.analytics([
            f"dx:{cfg.data_element}",
            f"pe:{';'.join(str(y) for y in window)}",
            f"ou:{';'.join(chunk)}",
        ])
    return pd.DataFrame(
        [{"ou_uid": r["ou"], "year": int(r["pe"]), "pop": float(r["value"])} for r in rows],
        columns=["ou_uid", "year", "pop"],
    )


def project_population(observed: pd.DataFrame, pop_units: list[str], years: list[int],
                       growth: float) -> pd.DataFrame:
    """Population per unit (rows) and year (columns).

    Observed years are kept as is. A missing year is projected from the nearest
    observed year y0 (the earlier one on ties) as pop[y0] * (1 + growth) ** (y - y0).
    """
    obs = observed.groupby(["ou_uid", "year"])["pop"].sum()
    missing_units = [u for u in pop_units if u not in obs.index.get_level_values(0)]
    if missing_units:
        raise ConfigError(f"{len(missing_units)} population units have no population at all, e.g. {missing_units[:5]}")
    out = pd.DataFrame(index=pd.Index(pop_units, name="ou_uid"), columns=years, dtype=float)
    projected = 0
    for u in pop_units:
        series = obs.loc[u]
        known = sorted(series.index)
        for y in years:
            if y in series.index:
                out.loc[u, y] = series[y]
                continue
            y0 = min(known, key=lambda k: (abs(k - y), k))
            out.loc[u, y] = series[y0] * (1 + growth) ** (y - y0)
            projected += 1
    if projected:
        log.info("projected %d unit-years of population with growth %.3f", projected, growth)
    return out


# --- incidence ---------------------------------------------------------------

def load_incidence(cfg: IncidenceConfig, pop_units: list[str], geometries: dict | None = None) -> pd.Series:
    """Baseline annual incidence per 1000 population, indexed by population unit.

    For a raster, `geometries` is the FeatureCollection of population units
    (see load_unit_geometries).
    """
    if cfg.source == "csv":
        df = pd.read_csv(cfg.path, dtype={"ou_uid": str})
        if {"ou_uid", "incidence_per_1000"} - set(df.columns):
            raise ConfigError("incidence CSV needs columns ou_uid, incidence_per_1000")
        inc = df.set_index("ou_uid")["incidence_per_1000"].astype(float)
    elif cfg.source == "raster":
        if geometries is None:
            raise ConfigError("raster incidence needs population unit geometries")
        inc = zonal_means(cfg, geometries, pop_units)
    else:
        raise ConfigError(f"unknown incidence.source {cfg.source}")
    if inc.index.duplicated().any():
        raise ConfigError("incidence input has duplicate ou_uid rows")
    missing = [u for u in pop_units if u not in inc.index or pd.isna(inc.get(u))]
    if missing:
        raise ConfigError(f"{len(missing)} population units have no incidence, e.g. {missing[:5]}")
    inc = inc.loc[pop_units]
    if (inc < 0).any():
        raise ConfigError("incidence values must be >= 0")
    return inc.rename("incidence_per_1000")


def _feature_uid(feature: dict, prop: str):
    uid = (feature.get("properties") or {}).get(prop)
    if uid is None and prop == "id":
        uid = feature.get("id")  # DHIS2 GeoJSON exports put the UID on the feature itself
    return uid


def load_unit_geometries(cfg: IncidenceConfig, pop_units: list[str],
                         client: Dhis2Client | None = None) -> dict:
    """FeatureCollection of the population units, from a GeoJSON file or the DHIS2 API."""
    if cfg.geometry_source == "dhis2":
        if client is None:
            raise ConfigError("incidence.geometry_source is dhis2 but no DHIS2 client is configured")
        return client.org_unit_geometries(pop_units)
    with open(cfg.geometry_path, encoding="utf-8") as fh:
        gj = json.load(fh)
    wanted = set(pop_units)
    features = [f for f in gj["features"] if _feature_uid(f, cfg.id_property) in wanted]
    return {"type": "FeatureCollection", "features": features}


def zonal_means(cfg: IncidenceConfig, geometries: dict, pop_units: list[str]) -> pd.Series:
    """Mean raster value x raster_scale per unit. all_touched so small units still get pixels."""
    try:
        from rasterstats import zonal_stats
    except ImportError as exc:  # optional dependency
        raise ConfigError("raster incidence needs the raster extra: pip install 'malsim[raster]'") from exc
    prop = "id" if cfg.geometry_source == "dhis2" else cfg.id_property
    wanted = set(pop_units)
    features = [f for f in geometries["features"] if _feature_uid(f, prop) in wanted]
    stats = zonal_stats(features, cfg.raster_path, stats=["mean"], all_touched=True)
    values = {
        _feature_uid(f, prop): (s["mean"] * cfg.raster_scale if s["mean"] is not None else None)
        for f, s in zip(features, stats)
    }
    return pd.Series(values, dtype=float)
