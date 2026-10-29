"""Minimal read-only DHIS2 Web API client (org units, group sets, geometries, analytics)."""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

import requests

from .config import Config, ConfigError, Dhis2Config, needs_api

log = logging.getLogger(__name__)


class Dhis2Client:
    def __init__(self, url: str, username: str | None = None, password: str | None = None,
                 token: str | None = None, timeout: float = 120, source: str = "direct"):
        self.base = url.rstrip("/")
        if not self.base.endswith("/api"):
            self.base += "/api"
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = f"ApiToken {token}"
        elif username and password:
            self.session.auth = (username, password)
        self.timeout = timeout
        self.credential_source = source  # recorded in run_params.yaml; never the credentials

    @classmethod
    def from_config(cls, cfg: Dhis2Config) -> "Dhis2Client":
        """Credentials come from environment variables named in the config, never from the YAML."""
        if not cfg.url:
            raise ConfigError("dhis2.url is required to read from DHIS2 (or pass a client, e.g. an OpenHEXA connection)")
        token = os.environ.get(cfg.token_env)
        username, password = os.environ.get(cfg.username_env), os.environ.get(cfg.password_env)
        if not token and not (username and password):
            raise ConfigError(
                f"no DHIS2 credentials: set {cfg.token_env}, or {cfg.username_env} and {cfg.password_env}"
            )
        return cls(cfg.url, username=username, password=password, token=token, source="env")

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self.session.get(f"{self.base}/{path.lstrip('/')}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def org_units(self) -> list[dict[str, Any]]:
        data = self.get("organisationUnits", {"paging": "false", "fields": "id,name,level,parent[id],closedDate"})
        return data["organisationUnits"]

    def org_unit_levels(self) -> list[dict[str, Any]]:
        data = self.get("organisationUnitLevels", {"paging": "false", "fields": "level,name"})
        return sorted(data["organisationUnitLevels"], key=lambda lv: lv["level"])

    def group_set(self, uid: str) -> dict[str, Any]:
        return self.get(
            f"organisationUnitGroupSets/{uid}",
            {"fields": "id,organisationUnitGroups[id,name,code,organisationUnits[id]]"},
        )

    def group_sets(self) -> list[dict[str, Any]]:
        data = self.get("organisationUnitGroupSets", {
            "paging": "false",
            "fields": "id,name,organisationUnitGroups[id,name,code,organisationUnits~size~rename(members)]",
        })
        return data["organisationUnitGroupSets"]

    def org_unit_geometries(self, uids: list[str], chunk: int = 100) -> dict[str, Any]:
        """Polygon geometries of the given org units as a GeoJSON FeatureCollection.

        Units without a Polygon/MultiPolygon (no geometry, or a Point) are left out.
        """
        features, skipped = [], []
        for i in range(0, len(uids), chunk):
            part = uids[i:i + chunk]
            data = self.get("organisationUnits", {
                "paging": "false", "fields": "id,geometry", "filter": f"id:in:[{','.join(part)}]",
            })
            for ou in data["organisationUnits"]:
                geom = ou.get("geometry")
                if geom and geom.get("type") in ("Polygon", "MultiPolygon"):
                    features.append({"type": "Feature", "id": ou["id"], "properties": {"id": ou["id"]}, "geometry": geom})
                else:
                    skipped.append(ou["id"])
        if skipped:
            log.warning("%d org units have no polygon geometry, e.g. %s", len(skipped), skipped[:5])
        features.sort(key=lambda f: f["id"])
        return {"type": "FeatureCollection", "features": features}

    def data_values(self, data_element: str, org_units: list[str], start_date: str, end_date: str,
                    chunk: int = 100) -> list[dict[str, Any]]:
        """Raw values stored at exactly these org units (not their children)."""
        values: list[dict[str, Any]] = []
        for i in range(0, len(org_units), chunk):
            data = self.get("dataValueSets", {
                "dataElement": data_element, "orgUnit": org_units[i:i + chunk],
                "startDate": start_date, "endDate": end_date,
            })
            values += data.get("dataValues", [])
        return values

    def analytics(self, dimensions: list[str]) -> list[dict[str, str]]:
        """Run an analytics query and return rows as dicts keyed by header name."""
        data = self.get("analytics", {"dimension": dimensions, "skipMeta": "true"})
        names = [h["name"] for h in data["headers"]]
        return [dict(zip(names, row)) for row in data.get("rows", [])]


def client_for(cfg: Config, client: Dhis2Client | None = None) -> Dhis2Client | None:
    """The client to use: the one passed in (e.g. from an OpenHEXA connection) wins over the config."""
    if client is not None:
        base = getattr(client, "base", None)
        if cfg.dhis2.url and base and urlparse(cfg.dhis2.url).netloc != urlparse(base).netloc:
            log.warning("dhis2.url (%s) differs from the passed connection (%s); using the connection",
                        cfg.dhis2.url, base)
        return client
    return Dhis2Client.from_config(cfg.dhis2) if needs_api(cfg) else None
