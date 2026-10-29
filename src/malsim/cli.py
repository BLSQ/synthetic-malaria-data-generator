"""Command line: `malsim generate --config x.yaml` and `malsim inspect --config x.yaml`."""

from __future__ import annotations

import argparse
import logging
import sys

import yaml

from .config import ConfigError
from .invariants import InvariantError
from .run import generate, resolve_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="malsim", description="Synthetic malaria routine data for DHIS2 pyramids")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="generate synthetic data from a config")
    ins = sub.add_parser("inspect", help="read-only check of the config's inputs (levels, population, raster)")
    for p in (gen, ins):
        p.add_argument("--config", required=True, help="YAML config file")
        p.add_argument("--population-de", help="population data element UID (sets population.source to dhis2)")
        p.add_argument("-v", "--verbose", action="store_true")
    gen.add_argument("--seed", type=int, help="override run.seed")
    gen.add_argument("--out", help="override output.dir")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        if args.command == "inspect":
            from .inspect import inspect
            report = inspect(resolve_config(args.config, population_de=args.population_de))
            print(yaml.safe_dump(report, sort_keys=False, allow_unicode=True))
            return 0
        files = generate(args.config, seed=args.seed, out_dir=args.out, population_de=args.population_de)
    except (ConfigError, InvariantError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for name, path in files.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
