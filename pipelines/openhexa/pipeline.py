"""OpenHEXA pipeline wrapper around malsim (inspect or generate).

Build the bundle with `python scripts/build_openhexa.py`, which copies this file,
requirements.txt and the malsim package into build/openhexa/, then push it with
`openhexa pipelines push build/openhexa`.

Credentials come only from the DHIS2 connection parameter; environment variables
and dhis2.url in the config are not used here. Untested outside OpenHEXA.
"""

from pathlib import Path

import yaml
from openhexa.sdk import DHIS2Connection, current_run, parameter, pipeline, workspace

from malsim.dhis2 import Dhis2Client
from malsim.inspect import inspect
from malsim.run import generate, resolve_config


@pipeline("malsim-generate", name="Synthetic malaria routine data")
@parameter("mode", name="Mode", type=str, choices=["inspect", "generate"], default="inspect")
@parameter("config_path", name="Config (path in workspace files)", type=str,
           default="malsim/examples/civ/config.yaml")
@parameter("dhis2", name="DHIS2 connection", type=DHIS2Connection, required=True)
@parameter("population_de", name="Population data element UID", type=str, required=False)
@parameter("output_dir", name="Output directory (in workspace files)", type=str, default="malsim/output")
@parameter("seed", name="Seed (overrides config)", type=int, required=False)
def malsim_generate(mode: str, config_path: str, dhis2: DHIS2Connection, population_de: str | None = None,
                    output_dir: str = "malsim/output", seed: int | None = None):
    files_root = Path(workspace.files_path)
    client = Dhis2Client(dhis2.url, username=dhis2.username, password=dhis2.password,
                         source="openhexa_connection")
    out = files_root / output_dir
    out.mkdir(parents=True, exist_ok=True)

    if mode == "inspect":
        cfg = resolve_config(files_root / config_path, population_de=population_de)
        report = yaml.safe_dump(inspect(cfg, client), sort_keys=False, allow_unicode=True)
        current_run.log_info(report)
        path = out / "inspect.yaml"
        path.write_text(report, encoding="utf-8")
        register_output("inspect", path, files_root)
        return

    files = generate(files_root / config_path, seed=seed, out_dir=out, population_de=population_de, client=client)
    listing = ", ".join(f"{p.name} ({p.stat().st_size / 1e6:.1f} MB)" for p in sorted(out.iterdir()) if p.is_file())
    current_run.log_info(f"files in {out}: {listing}")
    for name, path in files.items():
        register_output(name, path, files_root)
    current_run.log_info(files["report"].read_text(encoding="utf-8"))


def register_output(name: str, path: Path, files_root: Path) -> None:
    """Attach a file to the run using a path anchored to workspace.files_path.

    The output path is rebuilt from workspace.files_path + relative path so that
    symlink resolution in Path.resolve() does not produce a path the SDK cannot
    match against the FUSE mount point.
    """
    try:
        rel = path.resolve().relative_to(files_root.resolve())
    except ValueError:
        current_run.log_warning(f"{name}: path {path} is outside workspace files; skipping")
        return
    # Rebuild the path from the un-resolved workspace.files_path so the SDK
    # recognises it as a workspace file.
    workspace_path = str(files_root / rel)
    try:
        current_run.add_file_output(workspace_path)
    except FileNotFoundError as exc:
        current_run.log_warning(f"{name}: not registered as a run output ({exc}); exists={path.exists()}")
    else:
        current_run.log_info(f"{name}: {workspace_path}")


if __name__ == "__main__":
    malsim_generate()
