"""Push generated data values to DHIS2 (`malsim push`), as a dry run unless asked to commit.

Values are rebuilt from `datavalues.csv` and the resolved mapping `dhis2_mapping.yaml` written by
`generate`, and POSTed to /api/dataValueSets in chunks. CREATE_AND_UPDATE makes re-runs idempotent.
skipAudit is on by default (no audit row per value, faster); it is also sent on dry runs so a
missing F_SKIP_DATA_IMPORT_AUDIT authority shows up before the commit.
"""

from __future__ import annotations

import itertools
import logging
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import pandas as pd
import yaml

from .config import ConfigError
from .outputs import iter_datavalues

log = logging.getLogger(__name__)

COUNT_KEYS = ("imported", "updated", "ignored", "deleted")


def _summary(resp: dict[str, Any]) -> dict[str, Any]:
    """The import summary, whether wrapped in a web message (2.38+) or not."""
    return resp.get("response", resp) if isinstance(resp.get("response"), dict) else resp


def push(out_dir: str | Path, client, *, commit: bool = False, chunk_size: int = 50_000,
         max_conflict_chunks: int = 3, import_strategy: str = "CREATE_AND_UPDATE", skip_audit: bool = True,
         progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    out_dir = Path(out_dir)
    csv_path, mapping_path = out_dir / "datavalues.csv", out_dir / "dhis2_mapping.yaml"
    for p in (csv_path, mapping_path):
        if not p.is_file():
            raise FileNotFoundError(f"{p} not found: run generate with a mapping first")
    resolved = yaml.safe_load(mapping_path.read_text(encoding="utf-8"))
    base = getattr(client, "base", None)
    if resolved.get("instance") and base and urlparse(resolved["instance"]).netloc != urlparse(base).netloc:
        raise ConfigError(f"the data was mapped against {resolved['instance']} but the connection is {base}")

    tidy = pd.read_csv(csv_path, dtype={"ou_uid": str, "period": str, "element": str, "age_band": str})
    values = iter_datavalues(tidy, resolved)
    say = progress or log.info
    mode = "COMMIT" if commit else "dry run"

    totals = {k: 0 for k in COUNT_KEYS}
    conflicts: list[Any] = []
    report: dict[str, Any] = {"instance": base, "commit": commit, "import_strategy": import_strategy,
                              "skip_audit": skip_audit, "chunk_size": chunk_size, "chunks": 0, "values_sent": 0, "conflict_chunks": 0,
                              "stopped_early": False}
    for i in itertools.count(1):
        chunk = list(itertools.islice(values, chunk_size))
        if not chunk:
            break
        payload: dict[str, Any] = {"dataValues": chunk}
        if resolved.get("dataSet"):
            payload = {"dataSet": resolved["dataSet"], **payload}
        resp = client.post_data_values(payload, dry_run=not commit, import_strategy=import_strategy,
                                       skip_audit=skip_audit)
        s = _summary(resp)
        counts = s.get("importCount") or {}
        for k in COUNT_KEYS:
            totals[k] += int(counts.get(k, 0))
        chunk_conflicts = s.get("conflicts") or []
        report["chunks"] = i
        report["values_sent"] += len(chunk)
        say(f"{mode} chunk {i}: {len(chunk)} values, status {s.get('status')}, "
            f"{ {k: counts.get(k, 0) for k in COUNT_KEYS} }, {len(chunk_conflicts)} conflict(s)")
        if chunk_conflicts or s.get("status") == "ERROR":
            report["conflict_chunks"] += 1
            conflicts += chunk_conflicts[: max(0, 50 - len(conflicts))]
            if s.get("status") == "ERROR" and not chunk_conflicts:
                conflicts.append({"chunk": i, "message": s.get("description") or resp.get("message")})
            if report["conflict_chunks"] >= max_conflict_chunks:
                report["stopped_early"] = True
                say(f"stopping: {report['conflict_chunks']} chunk(s) with conflicts")
                break
    report["import_count"] = totals
    report["conflicts"] = conflicts
    path = out_dir / ("push_report.yaml" if commit else "push_report_dryrun.yaml")
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(report, fh, sort_keys=False, allow_unicode=True)
    report["report_path"] = str(path)
    return report
