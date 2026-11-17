"""DHIS2 mapping: model elements and age bands -> data element and category option combo UIDs.

The mapping YAML can refer to DHIS2 objects by UID or by exact name, so a mapping can be
written without looking UIDs up by hand:

    dataSet: {name: "Module 1 - EPI"}          # optional; checked, and added to the payload
    attributeOptionCombo: HllvX50cXC0           # optional; a UID, or {id: ...} / {name: ...}
    skip_zeros: false                           # leave zero values out of the payload
    elements:
      suspected: {dataElement: {name: "EPI - Nombre de cas suspect de paludisme"}}
      presumed:  {dataElement: {id: BKxUq6e6hik}}
    age_bands:                                  # every band of rates.age_bands
      u5:    {name: "< 5ans"}
      5plus: {id: k1QbgGU80Ss}

A plain string is read as a UID. Names are resolved against the instance (`resolve_mapping`
with a client); a mapping that uses only UIDs also resolves without one.
"""

from __future__ import annotations

import logging
from typing import Any

import yaml

from .config import ConfigError
from .model import ELEMENTS

log = logging.getLogger(__name__)

DE_FIELDS = "id,name,valueType,categoryCombo[id,name,categoryOptionCombos[id,name]],dataSetElements[dataSet[id,name]]"
DS_FIELDS = "id,name,periodType,dataSetElements[dataElement[id]]"


def _ref(value: Any, where: str) -> dict[str, str]:
    """Normalise a reference to {id: ...} or {name: ...}."""
    if isinstance(value, str) and value:
        return {"id": value}
    if isinstance(value, dict) and len(value) == 1 and next(iter(value)) in ("id", "name") and next(iter(value.values())):
        return {k: str(v) for k, v in value.items()}
    raise ConfigError(f"{where} must be a UID, {{id: ...}} or {{name: ...}}, got {value!r}")


def load_mapping(path: str) -> dict[str, Any]:
    """Read and check the structure of a mapping YAML (no DHIS2 lookups)."""
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    allowed = {"dataSet", "attributeOptionCombo", "skip_zeros", "elements", "age_bands"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"mapping has unknown key(s) {unknown}; allowed: {sorted(allowed)}")
    elements = raw.get("elements") or {}
    unknown = sorted(set(elements) - set(ELEMENTS))
    if unknown:
        raise ConfigError(f"mapping has unknown element(s) {unknown}; known: {ELEMENTS}")
    if not elements:
        raise ConfigError("mapping.elements is empty")
    mapping: dict[str, Any] = {
        "dataSet": _ref(raw["dataSet"], "mapping.dataSet") if raw.get("dataSet") else None,
        "attributeOptionCombo": (_ref(raw["attributeOptionCombo"], "mapping.attributeOptionCombo")
                                 if raw.get("attributeOptionCombo") else None),
        "skip_zeros": bool(raw.get("skip_zeros", False)),
        "elements": {},
        "age_bands": {},
    }
    for name, m in elements.items():
        if not isinstance(m, dict) or not m.get("dataElement"):
            raise ConfigError(f"mapping.elements.{name} needs a dataElement")
        mapping["elements"][name] = _ref(m["dataElement"], f"mapping.elements.{name}.dataElement")
    for band, ref in (raw.get("age_bands") or {}).items():
        mapping["age_bands"][str(band)] = _ref(ref, f"mapping.age_bands.{band}")
    return mapping


def _one(client, endpoint: str, ref: dict[str, str], fields: str, where: str) -> dict[str, Any]:
    found = client.find(endpoint, ref, fields)
    if not found:
        raise ConfigError(f"{where}: no {endpoint} with {ref} on the instance")
    if len(found) > 1:
        raise ConfigError(f"{where}: {len(found)} {endpoint} match {ref}; use {{id: ...}} instead")
    return found[0]


def resolve_mapping(mapping: dict[str, Any], bands: list[str], client=None) -> dict[str, Any]:
    """Resolve names to UIDs and check the mapping against the instance.

    Returns {instance, dataSet, attributeOptionCombo, skip_zeros, elements: {element: uid},
    age_bands: {band: coc uid}, details, warnings}. Raises ConfigError for anything that would
    make the import fail: unknown names, a band with no COC in an element's category combo,
    an element missing from the data set, a data set that is not monthly.
    """
    missing = [b for b in bands if b not in mapping["age_bands"]]
    extra = [b for b in mapping["age_bands"] if b not in bands]
    if missing or extra:
        raise ConfigError(f"mapping.age_bands must cover exactly the age bands {bands}: "
                          f"missing {missing}, unknown {extra}")
    resolved: dict[str, Any] = {
        "instance": getattr(client, "base", None),
        "dataSet": None, "attributeOptionCombo": None, "skip_zeros": mapping["skip_zeros"],
        "elements": {}, "age_bands": {}, "details": {}, "warnings": [],
    }
    unmapped = [e for e in ELEMENTS if e not in mapping["elements"]]
    if unmapped:
        resolved["warnings"].append(f"not exported (unmapped): {unmapped}")

    if client is None:
        refs = [*mapping["elements"].values(), *mapping["age_bands"].values(),
                *(r for r in (mapping["dataSet"], mapping["attributeOptionCombo"]) if r)]
        named = [r["name"] for r in refs if "name" in r]
        if named:
            raise ConfigError(f"the mapping refers to DHIS2 objects by name ({named[:3]}...), "
                              "which needs a DHIS2 connection to resolve")
        resolved["elements"] = {e: r["id"] for e, r in mapping["elements"].items()}
        resolved["age_bands"] = {b: mapping["age_bands"][b]["id"] for b in bands}
        resolved["dataSet"] = mapping["dataSet"]["id"] if mapping["dataSet"] else None
        aoc = mapping["attributeOptionCombo"]
        resolved["attributeOptionCombo"] = aoc["id"] if aoc else None
        return resolved

    data_elements = {}
    for element, ref in mapping["elements"].items():
        de = _one(client, "dataElements", ref, DE_FIELDS, f"mapping.elements.{element}")
        data_elements[element] = de
        resolved["elements"][element] = de["id"]
        combo = de.get("categoryCombo") or {}
        resolved["details"][element] = {
            "dataElement": {"id": de["id"], "name": de.get("name")},
            "categoryCombo": {"id": combo.get("id"), "name": combo.get("name")},
            "categoryOptionCombos": [{"id": c["id"], "name": c["name"]} for c in combo.get("categoryOptionCombos", [])],
            "dataSets": [d["dataSet"]["id"] for d in de.get("dataSetElements", [])],
        }

    for band in bands:
        ref = mapping["age_bands"][band]
        key = "id" if "id" in ref else "name"
        uids = set()
        for element, de in data_elements.items():
            cocs = (de.get("categoryCombo") or {}).get("categoryOptionCombos", [])
            match = [c["id"] for c in cocs if c.get(key) == ref[key]]
            if len(match) != 1:
                combo = (de.get("categoryCombo") or {}).get("name")
                raise ConfigError(f"mapping.age_bands.{band}: {ref} matches {len(match)} category option combo(s) "
                                  f"of {element} (category combo {combo!r}; options "
                                  f"{[c['name'] for c in cocs]})")
            uids.add(match[0])
        if len(uids) != 1:
            raise ConfigError(f"mapping.age_bands.{band}: resolves to different COCs across elements: {sorted(uids)}")
        resolved["age_bands"][band] = uids.pop()
    used = set(resolved["age_bands"].values())
    for element, d in resolved["details"].items():
        left = [c["name"] for c in d["categoryOptionCombos"] if c["id"] not in used]
        if left:
            resolved["warnings"].append(f"{element}: category option combo(s) {left} get no values")

    if mapping["dataSet"]:
        ds = _one(client, "dataSets", mapping["dataSet"], DS_FIELDS, "mapping.dataSet")
        resolved["dataSet"] = ds["id"]
        if ds.get("periodType") != "Monthly":
            raise ConfigError(f"data set {ds.get('name')!r} has period type {ds.get('periodType')}, not Monthly")
        in_set = {d["dataElement"]["id"] for d in ds.get("dataSetElements", [])}
        outside = [e for e, uid in resolved["elements"].items() if uid not in in_set]
        if outside:
            raise ConfigError(f"element(s) {outside} are not in data set {ds.get('name')!r}")
        resolved["details"]["dataSet"] = {"id": ds["id"], "name": ds.get("name"), "periodType": ds.get("periodType")}

    if mapping["attributeOptionCombo"]:
        aoc = _one(client, "categoryOptionCombos", mapping["attributeOptionCombo"], "id,name",
                   "mapping.attributeOptionCombo")
        resolved["attributeOptionCombo"] = aoc["id"]

    for w in resolved["warnings"]:
        log.warning("mapping: %s", w)
    return resolved
