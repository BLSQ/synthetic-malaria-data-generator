"""Read-only check of a config's inputs before a run: levels, mapping, population, geometry, raster.

Call `inspect(cfg, client)` from a notebook, or run `malsim inspect --config x.yaml`.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .config import Config, ConfigError
from .dhis2 import Dhis2Client, client_for
from .inputs import load_unit_geometries, read_population, zonal_means
from .model import make_periods
from .pyramid import build_pyramid, drop_zero_weight, load_org_units


def _stats(values: pd.Series) -> dict[str, float]:
    v = values.dropna()
    if v.empty:
        return {}
    return {"min": round(float(v.min()), 3), "median": round(float(v.median()), 3), "max": round(float(v.max()), 3)}


def inspect(cfg: Config, client: Dhis2Client | None = None) -> dict[str, Any]:
    client = client_for(cfg, client)
    report: dict[str, Any] = {}
    if client is not None:
        report["dhis2"] = {"url": getattr(client, "base", None),
                           "credential_source": getattr(client, "credential_source", None)}

    org_units, types = load_org_units(cfg.pyramid, client)
    counts = org_units["level"].value_counts().sort_index()
    names = {}
    if client is not None and hasattr(client, "org_unit_levels"):
        names = {lv["level"]: lv.get("name") for lv in client.org_unit_levels()}
    report["levels"] = [{"level": int(lv), "name": names.get(int(lv)), "org_units": int(n)} for lv, n in counts.items()]
    if client is not None and hasattr(client, "group_sets"):
        report["group_sets"] = [
            {"id": gs["id"], "name": gs.get("name"),
             "groups": [{"id": g["id"], "name": g.get("name"), "code": g.get("code"), "members": g.get("members")}
                        for g in gs.get("organisationUnitGroups", [])]}
            for gs in client.group_sets()
        ]

    all_types = build_pyramid(org_units, cfg.pyramid, types, start=cfg.run.start)
    pyramid = drop_zero_weight(all_types, cfg.allocation_weights)
    type_counts = all_types.facilities["facility_type"].value_counts()
    report["pyramid"] = {
        "facility_level": cfg.pyramid.facility_level,
        "population_level": cfg.pyramid.population_level,
        "facilities_mapped": len(pyramid.facilities),
        "facilities_closed_before_start": len(pyramid.facilities_closed),
        "facilities_zero_weight": len(pyramid.facilities_zero_weight),
        "facilities_without_population_unit": len(pyramid.facilities_without_pop_unit),
        "population_units_with_facilities": len(pyramid.pop_units),
        "population_units_without_facilities": pyramid.pop_units_without_facilities,
        "facility_types": {
            str(t): {"facilities": int(n), "allocation_weight": cfg.allocation_weights.get(t),
                     "micro_share": cfg.micro_share.get(t)}
            for t, n in type_counts.items()
        },
        "facilities_per_population_unit": _stats(pyramid.facilities.groupby("pop_unit").size()),
    }

    years = sorted({p.year for p in make_periods(cfg.run.start, cfg.run.months)})
    pop: dict[str, Any] = {"source": cfg.population.source, "data_element": cfg.population.data_element}
    try:
        observed = read_population(cfg.population, pyramid.pop_units, years, client)
        by_year = observed.groupby("year")
        pop["units_with_values_by_year"] = {int(y): int(g["ou_uid"].nunique()) for y, g in by_year}
        pop["total_by_year"] = {int(y): round(float(g["pop"].sum())) for y, g in by_year}
        missing = sorted(set(pyramid.pop_units) - set(observed["ou_uid"]))
        pop["units_without_any_value"] = missing
    except ConfigError as exc:
        pop["error"] = str(exc)
    report["population"] = pop

    if cfg.incidence.source == "raster":
        inc: dict[str, Any] = {"raster_path": cfg.incidence.raster_path, "raster_scale": cfg.incidence.raster_scale}
        try:
            import rasterio
            with rasterio.open(cfg.incidence.raster_path) as r:
                inc["crs"] = str(r.crs)
                inc["bounds"] = [round(b, 3) for b in r.bounds]
                inc["resolution"] = [round(x, 5) for x in r.res]
            geometries = load_unit_geometries(cfg.incidence, pyramid.pop_units, client)
            with_geom = {f.get("id") or (f.get("properties") or {}).get(cfg.incidence.id_property)
                         for f in geometries["features"]}
            inc["units_with_polygon"] = len(with_geom & set(pyramid.pop_units))
            inc["units_without_polygon"] = sorted(set(pyramid.pop_units) - with_geom)
            means = zonal_means(cfg.incidence, geometries, pyramid.pop_units)
            inc["incidence_per_1000"] = _stats(means)
            inc["units_without_raster_value"] = sorted(u for u in pyramid.pop_units if pd.isna(means.get(u)))
        except (ConfigError, ImportError, OSError) as exc:
            inc["error"] = str(exc)
        report["incidence"] = inc
    return report
