"""Bundle the OpenHEXA pipeline: pipeline.py + requirements.txt + the malsim package.

    python scripts/build_openhexa.py [--out build/openhexa]
    openhexa pipelines push build/openhexa
"""

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name in ("pipeline.py", "requirements.txt"):
        shutil.copy2(ROOT / "pipelines" / "openhexa" / name, out / name)
    shutil.copytree(ROOT / "src" / "malsim", out / "malsim", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(ROOT / "build" / "openhexa"))
    print(build(Path(parser.parse_args().out).resolve()))
