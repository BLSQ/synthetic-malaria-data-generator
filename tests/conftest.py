import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from malsim.config import load_config
from malsim.pyramid import _groups_from_metadata

TINY = Path(__file__).resolve().parents[1] / "examples" / "tiny"
TINY_CONFIG = TINY / "config.yaml"


@pytest.fixture
def tiny_cfg(tmp_path):
    return load_config(TINY_CONFIG, {"output": {"dir": str(tmp_path / "out")}})


def tiny_with(tmp_path, overrides):
    base = {"output": {"dir": str(tmp_path / "out")}}
    for key, value in overrides.items():
        base[key] = value
    return load_config(TINY_CONFIG, base)


def square(x0, x1, y0=0.05, y1=0.95):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


# Districts sit side by side over lon 0-2, lat 0-1, inset so no pixel is shared.
TINY_POLYGONS = {"DistAAAAAA1": square(0.05, 0.95), "DistAAAAAA2": square(1.05, 1.95)}


class FakeDhis2Client:
    """Serves the tiny pyramid as the DHIS2 Web API would. No network."""

    base = "https://fake.dhis2.test/api"
    credential_source = "fake"

    def __init__(self, polygons=None, extra_org_units=()):
        self.meta = json.loads((TINY / "metadata.json").read_text(encoding="utf-8"))
        self.meta["organisationUnits"] += list(extra_org_units)
        self.population = pd.read_csv(TINY / "population.csv")
        self.polygons = TINY_POLYGONS if polygons is None else polygons

    def org_units(self):
        keep = ("id", "name", "level", "parent", "closedDate")
        return [{k: v for k, v in ou.items() if k in keep} for ou in self.meta["organisationUnits"]]

    def org_unit_levels(self):
        return [{"level": i, "name": n} for i, n in enumerate(["Country", "District", "Area", "Facility"], 1)]

    def group_set(self, uid):
        groups = _groups_from_metadata(self.meta, uid)
        return {"id": uid, "organisationUnitGroups": [
            {"id": g["id"], "name": g["name"], "code": g.get("code"),
             "organisationUnits": [{"id": m} for m in sorted(g["members"])]} for g in groups]}

    def group_sets(self):
        out = []
        for gs in self.meta["organisationUnitGroupSets"]:
            groups = self.group_set(gs["id"])["organisationUnitGroups"]
            out.append({"id": gs["id"], "name": gs["name"], "organisationUnitGroups": [
                {"id": g["id"], "name": g["name"], "code": g["code"], "members": len(g["organisationUnits"])}
                for g in groups]})
        return out

    def org_unit_geometries(self, uids):
        return {"type": "FeatureCollection", "features": [
            {"type": "Feature", "id": u, "properties": {"id": u}, "geometry": self.polygons[u]}
            for u in sorted(uids) if u in self.polygons]}

    def data_values(self, data_element, org_units, start_date, end_date):
        """Raw values like dataValueSets, with the kinds of noise seen on real instances."""
        years = range(int(start_date[:4]), int(end_date[:4]) + 1)
        p = self.population
        values = []
        for r in p[p["ou_uid"].isin(org_units) & p["year"].isin(years)].itertuples(index=False):
            if r.ou_uid == "DistAAAAAA2":  # disaggregated: two category option combos
                values += [{"orgUnit": r.ou_uid, "period": str(r.year), "categoryOptionCombo": c, "value": str(v)}
                           for c, v in (("CocA", r.pop * 0.625), ("CocB", r.pop * 0.375))]
            else:
                values.append({"orgUnit": r.ou_uid, "period": str(r.year), "categoryOptionCombo": "Def", "value": str(r.pop)})
        values += [
            {"orgUnit": "DistAAAAAA1", "period": "202103", "categoryOptionCombo": "Def", "value": "999999"},  # monthly
            {"orgUnit": "FaclAAAAAA1", "period": "2021", "categoryOptionCombo": "Def", "value": "5000"},  # wrong level
        ]
        return values

    def analytics(self, dimensions):
        dims = dict(d.split(":", 1) for d in dimensions)
        years = {int(y) for y in dims["pe"].split(";")}
        units = set(dims["ou"].split(";"))
        p = self.population
        p = p[p["ou_uid"].isin(units) & p["year"].isin(years)]
        return [{"dx": dims["dx"], "pe": str(r.year), "ou": r.ou_uid, "value": str(float(r.pop))}
                for r in p.itertuples(index=False)]


def write_tiny_raster(path, left_value=0.25, right_value=0.125):
    """GeoTIFF over lon 0-2, lat 0-1 (EPSG:4326): left half one value, right half another."""
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_bounds

    data = np.full((10, 20), right_value, dtype="float64")
    data[:, :10] = left_value
    with rasterio.open(path, "w", driver="GTiff", height=10, width=20, count=1, dtype="float64",
                       crs="EPSG:4326", transform=from_bounds(0, 0, 2, 1, 20, 10), nodata=-9999) as dst:
        dst.write(data, 1)
    return path
