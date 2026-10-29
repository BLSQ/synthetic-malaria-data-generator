"""End-to-end generation: inputs -> simulation -> invariant checks -> outputs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from . import invariants, outputs
from .config import Config, load_config
from .dhis2 import Dhis2Client, client_for
from .inputs import load_incidence, load_unit_geometries, project_population, read_population
from .model import SimulationResult, make_periods, simulate
from .pyramid import Pyramid, build_pyramid, drop_zero_weight, load_org_units

log = logging.getLogger(__name__)


@dataclass
class Run:
    config: Config
    pyramid: Pyramid
    observed_population: pd.DataFrame
    population: pd.DataFrame
    incidence: pd.Series
    geometries: dict | None
    result: SimulationResult
    dhis2: dict | None = None  # instance URL and credential source used, never credentials


def run_overrides(seed: int | None = None, population_de: str | None = None) -> dict[str, Any] | None:
    """Config overrides given at run time (CLI flags, pipeline parameters)."""
    overrides: dict[str, Any] = {}
    if seed is not None:
        overrides["run"] = {"seed": seed}
    if population_de:
        overrides["population"] = {"source": "dhis2", "data_element": population_de}
    return overrides or None


def resolve_config(config: str | Path | Config, *, seed: int | None = None,
                   population_de: str | None = None) -> Config:
    if not isinstance(config, Config):
        return load_config(config, run_overrides(seed, population_de))
    if seed is not None:
        config.run.seed = seed
    if population_de:
        config.population.source, config.population.data_element = "dhis2", population_de
    return config


def prepare_and_simulate(cfg: Config, client: Dhis2Client | None = None) -> Run:
    """Load inputs and simulate, without writing anything."""
    client = client_for(cfg, client)

    org_units, types = load_org_units(cfg.pyramid, client)
    pyramid = drop_zero_weight(build_pyramid(org_units, cfg.pyramid, types, start=cfg.run.start),
                               cfg.allocation_weights)
    years = sorted({p.year for p in make_periods(cfg.run.start, cfg.run.months)})
    observed = read_population(cfg.population, pyramid.pop_units, years, client)
    population = project_population(observed, pyramid.pop_units, years, cfg.run.pop_growth)
    geometries = None
    if cfg.incidence.source == "raster":
        geometries = load_unit_geometries(cfg.incidence, pyramid.pop_units, client)
    incidence = load_incidence(cfg.incidence, pyramid.pop_units, geometries)

    result = simulate(cfg, pyramid, population, incidence)
    invariants.check(result)
    dhis2 = None
    if client is not None:
        dhis2 = {"url": getattr(client, "base", None), "credential_source": getattr(client, "credential_source", None)}
    return Run(cfg, pyramid, observed, population, incidence, geometries, result, dhis2)


def write_outputs(run: Run, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg, result = run.config, run.result
    files = {
        "datavalues_csv": out_dir / "datavalues.csv",
        "ground_truth": out_dir / "ground_truth.parquet",
        "run_params": out_dir / "run_params.yaml",
        "report": out_dir / "summary_report.md",
    }
    tidy = outputs.tidy_datavalues(result)
    outputs.write_datavalues_csv(tidy, files["datavalues_csv"])
    if cfg.mapping:
        files["datavalues_json"] = out_dir / "datavalues.json"
        outputs.write_datavalues_json(tidy, outputs.load_mapping(cfg.mapping), files["datavalues_json"])
    outputs.write_ground_truth(result, files["ground_truth"])
    snapshots = outputs.write_input_snapshots(out_dir / "inputs", run.pyramid, run.observed_population,
                                              run.incidence, run.geometries)
    outputs.write_run_params(cfg, run.pyramid, files["run_params"], run.dhis2, snapshots)
    outputs.write_report(cfg, run.pyramid, result, out_dir)
    return files


def generate(config: str | Path | Config, *, seed: int | None = None, out_dir: str | Path | None = None,
             population_de: str | None = None, client: Dhis2Client | None = None) -> dict[str, Path]:
    """Generate all outputs for a config. Returns the written file paths.

    This is the entry point for both the CLI and an OpenHEXA pipeline.
    """
    cfg = resolve_config(config, seed=seed, population_de=population_de)
    if out_dir is not None:
        cfg.output.dir = str(Path(out_dir).resolve())
    run = prepare_and_simulate(cfg, client)
    files = write_outputs(run, Path(cfg.output.dir))
    log.info("wrote %d files to %s", len(files), cfg.output.dir)
    return files
