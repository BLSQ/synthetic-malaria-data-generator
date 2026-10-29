"""Org unit pyramid: load org units and facility types, map facilities to population units."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field, replace

import pandas as pd

from .config import AllocationConfig, ConfigError, FacilityTypeConfig, PyramidConfig
from .dhis2 import Dhis2Client

log = logging.getLogger(__name__)


@dataclass
class Pyramid:
    org_units: pd.DataFrame  # index uid; columns name, level, parent, closed_date
    facilities: pd.DataFrame  # columns uid, name, pop_unit, facility_type; sorted by pop_unit, uid
    pop_units: list[str]  # population units that have at least one facility, sorted
    pop_units_without_facilities: list[str] = field(default_factory=list)
    facilities_without_pop_unit: list[str] = field(default_factory=list)
    facilities_closed: list[str] = field(default_factory=list)  # closed before the run start
    facilities_zero_weight: list[str] = field(default_factory=list)  # allocation weight 0: not simulated

    def facilities_in(self, pop_unit: str) -> pd.DataFrame:
        return self.facilities[self.facilities["pop_unit"] == pop_unit]


# --- loading -----------------------------------------------------------------

def _org_units_frame(records: list[dict]) -> pd.DataFrame:
    rows = [
        {
            "uid": r["id"],
            "name": r.get("name", r["id"]),
            "level": int(r["level"]),
            "parent": (r.get("parent") or {}).get("id"),
            "closed_date": (r.get("closedDate") or None),
        }
        for r in records
    ]
    df = pd.DataFrame(rows, columns=["uid", "name", "level", "parent", "closed_date"])
    if df["uid"].duplicated().any():
        raise ConfigError(f"duplicate org unit UIDs: {sorted(df.loc[df['uid'].duplicated(), 'uid'])[:10]}")
    return df.set_index("uid").sort_index()


def _groups_from_metadata(meta: dict, group_set_uid: str) -> list[dict]:
    """Groups of a group set from a metadata export, with member UIDs.

    Membership may be listed on the groups or on the org units; both are read.
    """
    sets = {s["id"]: s for s in meta.get("organisationUnitGroupSets", [])}
    if group_set_uid not in sets:
        raise ConfigError(f"group set {group_set_uid} not found in metadata export")
    group_ids = {g["id"] for g in sets[group_set_uid].get("organisationUnitGroups", [])}
    groups = {g["id"]: dict(g) for g in meta.get("organisationUnitGroups", []) if g["id"] in group_ids}
    members: dict[str, set[str]] = {gid: {m["id"] for m in g.get("organisationUnits", [])} for gid, g in groups.items()}
    for ou in meta.get("organisationUnits", []):
        for g in ou.get("organisationUnitGroups", []):
            if g["id"] in members:
                members[g["id"]].add(ou["id"])
    return [{**groups[gid], "members": members[gid]} for gid in sorted(groups)]


def _types_from_groups(groups: list[dict], ft: FacilityTypeConfig) -> pd.Series:
    labels: dict[str, str] = {}
    for g in groups:
        label = g.get(ft.type_key) or g["id"]
        for uid in sorted(g["members"]):
            if uid in labels and labels[uid] != label:
                log.warning("org unit %s is in several type groups (%s, %s); keeping %s",
                            uid, labels[uid], label, labels[uid])
                continue
            labels[uid] = label
    return pd.Series(labels, dtype="object", name="facility_type")


def load_org_units(cfg: PyramidConfig, client: Dhis2Client | None = None) -> tuple[pd.DataFrame, pd.Series | None]:
    """Return (org_units, facility types by uid or None)."""
    ft = cfg.facility_type
    types = None
    if cfg.source == "metadata":
        with open(cfg.path, encoding="utf-8") as fh:
            meta = json.load(fh)
        org_units = _org_units_frame(meta["organisationUnits"])
        if ft is not None and ft.source == "group_set":
            types = _types_from_groups(_groups_from_metadata(meta, ft.group_set), ft)
    elif cfg.source == "csv":
        df = pd.read_csv(cfg.path, dtype={"id": str, "parent": str, "closedDate": str})
        records = [
            {"id": r.id, "name": getattr(r, "name", r.id), "level": r.level,
             "parent": {"id": r.parent} if isinstance(r.parent, str) and r.parent else None,
             "closedDate": c if isinstance(c := getattr(r, "closedDate", None), str) else None}
            for r in df.itertuples(index=False)
        ]
        org_units = _org_units_frame(records)
    elif cfg.source == "api":
        if client is None:
            raise ConfigError("pyramid.source is api but no DHIS2 client is configured")
        org_units = _org_units_frame(client.org_units())
        if ft is not None and ft.source == "group_set":
            gs = client.group_set(ft.group_set)
            groups = [{**g, "members": {m["id"] for m in g.get("organisationUnits", [])}}
                      for g in gs.get("organisationUnitGroups", [])]
            types = _types_from_groups(sorted(groups, key=lambda g: g["id"]), ft)
    else:  # validated earlier
        raise ConfigError(f"unknown pyramid.source {cfg.source}")

    if ft is not None and ft.source == "csv":
        tdf = pd.read_csv(ft.path, dtype=str)
        types = tdf.set_index("ou_uid")["type"].rename("facility_type")
    elif ft is not None and ft.source == "name_rules":
        types = types_from_name_rules(org_units["name"], ft.rules)
    return org_units, types


def _normalize_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(name))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).upper()


def types_from_name_rules(names: pd.Series, rules: list[dict[str, str]]) -> pd.Series:
    """Type of each org unit from the first rule whose pattern matches its name; unmatched are left out."""
    compiled = [(re.compile(r["pattern"], re.IGNORECASE), r["type"]) for r in rules]
    labels = {}
    for uid, name in names.items():
        normalized = _normalize_name(name)
        for pattern, label in compiled:
            if pattern.search(normalized):
                labels[uid] = label
                break
    return pd.Series(labels, dtype="object", name="facility_type")


# --- structure ---------------------------------------------------------------

def _descendants(root: str, org_units: pd.DataFrame) -> set[str]:
    children: dict[str, list[str]] = {}
    for uid, parent in org_units["parent"].items():
        if isinstance(parent, str):
            children.setdefault(parent, []).append(uid)
    out, stack = {root}, [root]
    while stack:
        for child in children.get(stack.pop(), []):
            out.add(child)
            stack.append(child)
    return out


def closed_before(org_units: pd.DataFrame, start: str) -> set[str]:
    """Org units whose closedDate is before the first day of the `start` month (YYYY-MM)."""
    closed = pd.to_datetime(org_units["closed_date"], errors="coerce", utc=True)
    cutoff = pd.Timestamp(pd.Period(start, freq="M").start_time, tz="UTC")
    return set(org_units.index[closed.notna() & (closed < cutoff)])


def build_pyramid(org_units: pd.DataFrame, cfg: PyramidConfig, types: pd.Series | None = None,
                  start: str | None = None) -> Pyramid:
    """Map each facility (unit at facility_level) to its ancestor at population_level.

    With `start` (the run's first month) and `cfg.drop_closed_before_start`, facilities
    closed before that month are dropped.
    """
    if cfg.root is not None:
        if cfg.root not in org_units.index:
            raise ConfigError(f"pyramid.root {cfg.root} not found")
        org_units = org_units.loc[sorted(_descendants(cfg.root, org_units))]

    levels = set(org_units["level"])
    for name, lvl in (("facility_level", cfg.facility_level), ("population_level", cfg.population_level)):
        if lvl not in levels:
            raise ConfigError(f"pyramid.{name}={lvl} has no org units (levels present: {sorted(levels)})")

    default_type = cfg.facility_type.default_type if cfg.facility_type else "all"
    fac = org_units[org_units["level"] == cfg.facility_level]
    closed: list[str] = []
    if start is not None and cfg.drop_closed_before_start:
        closed = sorted(closed_before(fac, start))
        if closed:
            log.warning("%d facilities closed before %s are dropped", len(closed), start)
            fac = fac.drop(index=closed)
    parent = org_units["parent"].to_dict()
    lvl = org_units["level"].to_dict()

    rows, orphans = [], []
    for uid, name in fac["name"].items():
        cur = uid
        while cur is not None and cur in lvl and lvl[cur] > cfg.population_level:
            cur = parent.get(cur)
        if cur is None or lvl.get(cur) != cfg.population_level:
            orphans.append(uid)
            continue
        ftype = default_type
        if types is not None and uid in types.index:
            ftype = str(types[uid])
        rows.append({"uid": uid, "name": name, "pop_unit": cur, "facility_type": ftype})

    if orphans:
        log.warning("%d facilities have no ancestor at level %d and are dropped", len(orphans), cfg.population_level)
    facilities = (
        pd.DataFrame(rows, columns=["uid", "name", "pop_unit", "facility_type"])
        .sort_values(["pop_unit", "uid"])
        .reset_index(drop=True)
    )
    all_pop = sorted(org_units.index[org_units["level"] == cfg.population_level])
    with_fac = sorted(set(facilities["pop_unit"]))
    empty = [u for u in all_pop if u not in set(with_fac)]
    if empty:
        log.warning("%d population units have no facilities and are not simulated", len(empty))
    if not with_fac:
        raise ConfigError("no facilities could be mapped to a population unit")
    return Pyramid(org_units, facilities, with_fac, empty, sorted(orphans), closed)


def drop_zero_weight(pyramid: Pyramid, weights: AllocationConfig) -> Pyramid:
    """Leave out facilities whose type has allocation weight 0 (e.g. not malaria providers).

    They would only report zeros; population units left with no facility are skipped.
    """
    w = pyramid.facilities["facility_type"].map(weights.get)
    zero = sorted(pyramid.facilities.loc[w == 0, "uid"])
    if not zero:
        return pyramid
    log.info("%d facilities with allocation weight 0 are not simulated", len(zero))
    facilities = pyramid.facilities[w != 0].reset_index(drop=True)
    with_fac = sorted(set(facilities["pop_unit"]))
    if not with_fac:
        raise ConfigError("every facility has allocation weight 0")
    newly_empty = [u for u in pyramid.pop_units if u not in set(with_fac)]
    if newly_empty:
        log.warning("%d population units have only zero-weight facilities and are not simulated", len(newly_empty))
    return replace(pyramid, facilities=facilities, pop_units=with_fac,
                   pop_units_without_facilities=sorted(pyramid.pop_units_without_facilities + newly_empty),
                   facilities_zero_weight=zero)
