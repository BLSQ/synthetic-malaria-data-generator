"""Run configuration: dataclasses with defaults, YAML loading and validation.

Everything country-specific (pyramid, levels, facility types, input files)
lives in the YAML config, never in code.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


class ConfigError(ValueError):
    """Raised for an invalid or incomplete configuration."""


@dataclass
class RunConfig:
    start: str = "2021-01"  # first month, YYYY-MM
    months: int = 60
    seed: int = 1
    pop_growth: float = 0.03  # annual, used to project missing population years


@dataclass
class FacilityTypeConfig:
    source: str = "group_set"  # "group_set" | "csv" | "name_rules"
    group_set: str | None = None  # org unit group set UID (source: group_set)
    path: str | None = None  # CSV with ou_uid,type (source: csv)
    type_key: str = "name"  # group attribute used as the type label: name | code | id
    default_type: str = "unknown"  # for facilities in no group of the set / matching no rule
    # source: name_rules. Ordered [{pattern: regex, type: label}]; the first match wins. Patterns are
    # searched case-insensitively in the facility name with accents removed (MÉDICAL -> MEDICAL).
    rules: list[dict[str, str]] = field(default_factory=list)


@dataclass
class PyramidConfig:
    source: str = ""  # "metadata" (DHIS2 metadata JSON export) | "csv" | "api"
    facility_level: int = 0
    population_level: int = 0
    path: str | None = None
    root: str | None = None  # optional: only use the subtree under this org unit UID
    facility_type: FacilityTypeConfig | None = None
    drop_closed_before_start: bool = True  # drop facilities whose closedDate is before run.start


@dataclass
class PopulationConfig:
    source: str = "csv"  # "csv" (ou_uid,year,pop) | "dhis2" (a data element)
    path: str | None = None
    data_element: str | None = None
    # dhis2 only. "raw": yearly values stored at the population units themselves (dataValueSets);
    # "analytics": analytics totals, which also sum values entered below the unit or for other periods.
    dhis2_method: str = "raw"


@dataclass
class IncidenceConfig:
    source: str = "csv"  # "csv" (ou_uid,incidence_per_1000) | "raster"
    path: str | None = None
    raster_path: str | None = None
    geometry_source: str = "file"  # "file" (geometry_path) | "dhis2" (org unit geometries from the API)
    geometry_path: str | None = None  # GeoJSON of population-level units
    id_property: str = "id"  # feature property holding the org unit UID
    raster_scale: float = 1.0  # multiplier converting raster values to cases per 1000 per year


@dataclass
class RatesConfig:
    care_seeking: float = 0.6
    u5_share: float = 0.4
    positivity: float = 0.45
    testing_rate: float = 0.9
    act_rate: float = 0.95
    severe_rate: float = 0.05
    cfr: float = 0.02


@dataclass
class MicroShareConfig:
    default: float = 0.0
    by_type: dict[str, float] = field(default_factory=dict)

    def get(self, facility_type: str) -> float:
        return float(self.by_type.get(facility_type, self.default))


@dataclass
class AllocationConfig:
    default: float = 1.0
    by_type: dict[str, float] = field(default_factory=dict)

    def get(self, facility_type: str) -> float:
        return float(self.by_type.get(facility_type, self.default))


@dataclass
class StocksConfig:
    stock_start_months: float = 2.0
    delivery_factor: float = 1.0


@dataclass
class Dhis2Config:
    url: str | None = None
    username_env: str = "DHIS2_USERNAME"
    password_env: str = "DHIS2_PASSWORD"
    token_env: str = "DHIS2_TOKEN"


@dataclass
class OutputConfig:
    dir: str = "output"


@dataclass
class Config:
    pyramid: PyramidConfig
    population: PopulationConfig
    incidence: IncidenceConfig
    run: RunConfig = field(default_factory=RunConfig)
    rates: RatesConfig = field(default_factory=RatesConfig)
    micro_share: MicroShareConfig = field(default_factory=MicroShareConfig)
    allocation_weights: AllocationConfig = field(default_factory=AllocationConfig)
    stocks: StocksConfig = field(default_factory=StocksConfig)
    dhis2: Dhis2Config = field(default_factory=Dhis2Config)
    mapping: str | None = None  # YAML: element -> dataElement / categoryOptionCombo UIDs
    output: OutputConfig = field(default_factory=OutputConfig)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Which fields hold a nested section, so the builder can recurse.
_NESTED: dict[tuple[type, str], type] = {
    (Config, "pyramid"): PyramidConfig,
    (Config, "population"): PopulationConfig,
    (Config, "incidence"): IncidenceConfig,
    (Config, "run"): RunConfig,
    (Config, "rates"): RatesConfig,
    (Config, "micro_share"): MicroShareConfig,
    (Config, "allocation_weights"): AllocationConfig,
    (Config, "stocks"): StocksConfig,
    (Config, "dhis2"): Dhis2Config,
    (Config, "output"): OutputConfig,
    (PyramidConfig, "facility_type"): FacilityTypeConfig,
}

_REQUIRED_SECTIONS = ("pyramid", "population", "incidence")


def _build(cls: type, data: Any, where: str):
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(data).__name__}")
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {unknown}; allowed: {sorted(known)}")
    kwargs = {}
    for name, value in data.items():
        sub = _NESTED.get((cls, name))
        kwargs[name] = _build(sub, value, f"{where}.{name}") if sub and value is not None else value
    return cls(**kwargs)


def from_dict(data: dict[str, Any], base_dir: str | Path | None = None) -> Config:
    """Build and validate a Config. Relative paths resolve against base_dir."""
    missing = [s for s in _REQUIRED_SECTIONS if s not in (data or {})]
    if missing:
        raise ConfigError(f"missing required section(s): {missing}")
    cfg = _build(Config, data, "config")
    if base_dir is not None:
        _resolve_paths(cfg, Path(base_dir))
    validate(cfg)
    return cfg


def load_config(path: str | Path, overrides: dict[str, Any] | None = None) -> Config:
    """Load a YAML config. `overrides` is a nested dict merged on top (e.g. from the CLI)."""
    path = Path(path)
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if overrides:
        data = _deep_merge(data, overrides)
    return from_dict(data, base_dir=path.parent)


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for key, value in b.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _resolve_paths(cfg: Config, base: Path) -> None:
    def resolve(p: str | None) -> str | None:
        if p is None:
            return None
        q = Path(p)
        return str(q if q.is_absolute() else (base / q).resolve())

    cfg.pyramid.path = resolve(cfg.pyramid.path)
    if cfg.pyramid.facility_type is not None:
        cfg.pyramid.facility_type.path = resolve(cfg.pyramid.facility_type.path)
    cfg.population.path = resolve(cfg.population.path)
    cfg.incidence.path = resolve(cfg.incidence.path)
    cfg.incidence.raster_path = resolve(cfg.incidence.raster_path)
    cfg.incidence.geometry_path = resolve(cfg.incidence.geometry_path)
    cfg.mapping = resolve(cfg.mapping)
    cfg.output.dir = resolve(cfg.output.dir)


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def validate(cfg: Config) -> None:
    p = cfg.pyramid
    _check(p.source in ("metadata", "csv", "api"), "pyramid.source must be metadata, csv or api")
    _check(p.source == "api" or bool(p.path), "pyramid.path is required unless pyramid.source is api")
    _check(isinstance(p.facility_level, int) and p.facility_level > 0, "pyramid.facility_level must be a positive integer")
    _check(isinstance(p.population_level, int) and p.population_level > 0, "pyramid.population_level must be a positive integer")
    _check(p.population_level < p.facility_level, "pyramid.population_level must be above (smaller than) facility_level")
    if p.facility_type is not None:
        ft = p.facility_type
        _check(ft.source in ("group_set", "csv", "name_rules"),
               "pyramid.facility_type.source must be group_set, csv or name_rules")
        if ft.source == "name_rules":
            _check(bool(ft.rules), "pyramid.facility_type.rules is required for name_rules")
            for i, rule in enumerate(ft.rules):
                _check(isinstance(rule, dict) and set(rule) == {"pattern", "type"},
                       f"pyramid.facility_type.rules[{i}] must have exactly pattern and type")
                try:
                    re.compile(rule["pattern"])
                except re.error as exc:
                    raise ConfigError(f"pyramid.facility_type.rules[{i}].pattern is not a valid regex: {exc}") from exc
        _check(ft.source != "group_set" or bool(ft.group_set), "pyramid.facility_type.group_set is required")
        _check(ft.source != "group_set" or p.source != "csv", "facility types from a group set need a metadata or api pyramid; use a types CSV with a CSV pyramid")
        _check(ft.source != "csv" or bool(ft.path), "pyramid.facility_type.path is required")
        _check(ft.type_key in ("name", "code", "id"), "pyramid.facility_type.type_key must be name, code or id")

    _check(cfg.population.source in ("csv", "dhis2"), "population.source must be csv or dhis2")
    _check(cfg.population.source != "csv" or bool(cfg.population.path), "population.path is required")
    _check(cfg.population.source != "dhis2" or bool(cfg.population.data_element), "population.data_element is required")
    _check(cfg.population.dhis2_method in ("raw", "analytics"), "population.dhis2_method must be raw or analytics")

    inc = cfg.incidence
    _check(inc.source in ("csv", "raster"), "incidence.source must be csv or raster")
    _check(inc.source != "csv" or bool(inc.path), "incidence.path is required")
    _check(inc.geometry_source in ("file", "dhis2"), "incidence.geometry_source must be file or dhis2")
    _check(inc.source != "raster" or bool(inc.raster_path), "incidence.raster_path is required for a raster")
    _check(inc.source != "raster" or inc.geometry_source != "file" or bool(inc.geometry_path),
           "incidence.geometry_path is required when incidence.geometry_source is file")
    _check(inc.raster_scale > 0, "incidence.raster_scale must be > 0")
    # dhis2.url is not required here: a client may be passed in (e.g. an OpenHEXA connection).
    # Dhis2Client.from_config checks the URL and credentials when it is actually needed.

    r = cfg.run
    try:
        pd.Period(r.start, freq="M")
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"run.start must be YYYY-MM, got {r.start!r}") from exc
    _check(isinstance(r.months, int) and r.months >= 1, "run.months must be a positive integer")
    _check(isinstance(r.seed, int) and r.seed >= 0, "run.seed must be a non-negative integer")
    _check(r.pop_growth > -1, "run.pop_growth must be > -1")

    rates = cfg.rates
    for name in ("care_seeking", "positivity", "testing_rate"):
        v = getattr(rates, name)
        _check(0 < v <= 1, f"rates.{name} must be in (0, 1], got {v}")
    for name in ("u5_share", "act_rate", "severe_rate", "cfr"):
        v = getattr(rates, name)
        _check(0 <= v <= 1, f"rates.{name} must be in [0, 1], got {v}")

    for name, v in [("default", cfg.micro_share.default), *cfg.micro_share.by_type.items()]:
        _check(0 <= float(v) <= 1, f"micro_share.{name} must be in [0, 1], got {v}")
    for name, v in [("default", cfg.allocation_weights.default), *cfg.allocation_weights.by_type.items()]:
        _check(float(v) >= 0, f"allocation_weights.{name} must be >= 0, got {v}")

    _check(cfg.stocks.stock_start_months >= 0, "stocks.stock_start_months must be >= 0")
    _check(cfg.stocks.delivery_factor >= 0, "stocks.delivery_factor must be >= 0")


def needs_api(cfg: Config) -> bool:
    """Whether any input is read from the DHIS2 Web API."""
    return (cfg.pyramid.source == "api" or cfg.population.source == "dhis2"
            or (cfg.incidence.source == "raster" and cfg.incidence.geometry_source == "dhis2"))
