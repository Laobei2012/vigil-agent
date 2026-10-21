"""Service catalog and kill-switch flags.

The catalog is Git-managed YAML. It maps PagerDuty services and Kubernetes
namespaces to a team, a Slack channel, repositories, and context packs. The
flags file is a ConfigMap that switches the agent off without a deploy.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError


class CatalogError(ValueError):
    pass


class ContextPack(BaseModel):
    """Queries and docs for one alert rule, run by the pre-fetch."""

    alert_name: str
    metric_queries: list[str] = Field(default_factory=list)
    log_query: str | None = None
    dashboards: list[str] = Field(default_factory=list)
    runbook: str | None = None


class ServiceEntry(BaseModel):
    name: str
    vigil_enabled: bool
    vigil_pr: bool = False
    pd_service_ids: list[str] = Field(default_factory=list)
    team: str
    slack_channel: str
    repos: list[str] = Field(default_factory=list)
    namespaces: list[str] = Field(default_factory=list)
    log_index: str | None = None
    dashboards: list[str] = Field(default_factory=list)
    runbook: str | None = None
    low_urgency_skip: bool = False
    context_packs: list[ContextPack] = Field(default_factory=list)

    def context_pack(self, alert_name: str | None) -> ContextPack | None:
        for pack in self.context_packs:
            if alert_name and pack.alert_name == alert_name:
                return pack
        return None


class CatalogDefaults(BaseModel):
    fallback_channel: str = "#vigil-test"
    # Unmapped alerts fall back to their labels. Off by default, so only
    # onboarded services reach the agent.
    unmapped_enabled: bool = False


class Catalog:
    def __init__(self, services: list[ServiceEntry], defaults: CatalogDefaults | None = None):
        self.services = services
        self.defaults = defaults or CatalogDefaults()
        self._by_pd: dict[str, ServiceEntry] = {}
        self._by_ns: dict[str, ServiceEntry] = {}
        self._by_name: dict[str, ServiceEntry] = {}
        for svc in services:
            if svc.name in self._by_name:
                raise CatalogError(f"service {svc.name}: duplicate name")
            self._by_name[svc.name] = svc
            for pd_id in svc.pd_service_ids:
                self._by_pd[pd_id] = svc
            for ns in svc.namespaces:
                if ns in self._by_ns:
                    raise CatalogError(
                        f"service {svc.name}: namespace {ns} already belongs to "
                        f"{self._by_ns[ns].name}"
                    )
                self._by_ns[ns] = svc

    @classmethod
    def load(cls, path: str | os.PathLike) -> Catalog:
        data = yaml.safe_load(Path(path).read_text()) or {}
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> Catalog:
        services = []
        for i, raw in enumerate(data.get("services") or []):
            name = raw.get("name", f"#{i}") if isinstance(raw, dict) else f"#{i}"
            try:
                services.append(ServiceEntry.model_validate(raw))
            except ValidationError as exc:
                fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())
                raise CatalogError(f"service {name}: invalid field(s): {fields}") from exc
        defaults = CatalogDefaults.model_validate(data.get("defaults") or {})
        return cls(services, defaults)

    def get(self, pd_service_id: str | None) -> ServiceEntry | None:
        return self._by_pd.get(pd_service_id) if pd_service_id else None

    def by_name(self, name: str) -> ServiceEntry | None:
        return self._by_name.get(name)

    def by_namespace(self, namespace: str | None) -> ServiceEntry | None:
        return self._by_ns.get(namespace) if namespace else None

    def channel_for(self, service: ServiceEntry | None) -> str:
        return service.slack_channel if service else self.defaults.fallback_channel


class Flags(BaseModel):
    """The kill switch. Any False here stops new work at intake."""

    enabled: bool = True
    disabled_clusters: list[str] = Field(default_factory=list)
    disabled_services: list[str] = Field(default_factory=list)

    def allows(self, cluster: str, service: str | None) -> bool:
        if not self.enabled or cluster in self.disabled_clusters:
            return False
        return not (service and service in self.disabled_services)


class ReloadingFile:
    """Re-reads a YAML-backed object when the file changes, so a ConfigMap edit
    applies without a restart."""

    def __init__(self, path: str | None, parse, default):
        self.path = path
        self._parse = parse
        self._value = default
        self._mtime: float | None = None

    def get(self):
        if not self.path:
            return self._value
        try:
            mtime = os.stat(self.path).st_mtime
        except FileNotFoundError:
            return self._value
        if mtime != self._mtime:
            self._value = self._parse(self.path)
            self._mtime = mtime
        return self._value


def load_flags(path: str) -> Flags:
    return Flags.model_validate(yaml.safe_load(Path(path).read_text()) or {})
