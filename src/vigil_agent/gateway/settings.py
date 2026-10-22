"""Tool gateway configuration, read from environment variables with the prefix VIGIL_GW_.

Which backend serves a capability is configuration. The model sees the same
tool names against production (VictoriaMetrics, Elasticsearch, Jira,
Confluence) and against the OCI test cluster (Prometheus, Loki, Flux).
"""

from __future__ import annotations

from functools import cached_property

from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VIGIL_GW_", extra="ignore")

    token: str | None = None
    port: int = 8081
    catalog_path: str | None = None
    pii_redaction: bool = False
    cluster: str = "oci-phx"

    # Metrics: Prometheus HTTP API (vmselect serves the same API).
    metrics_url: str | None = None
    grafana_url: str = "https://monitoring.oci.liwenxin.com"
    grafana_metrics_uid: str = "prometheus"
    grafana_logs_uid: str = "loki"
    metrics_max_range_days: int = 7
    metrics_min_step_seconds: int = 15
    metrics_max_series: int = 50
    metrics_timeout_seconds: float = 20.0

    # Logs: "loki" or "elasticsearch".
    logs_backend: str = "loki"
    loki_url: str | None = None
    logs_max_range_days: int = 14
    es_url: str | None = None
    es_username: str | None = None
    es_password: str | None = None
    es_index_allowlist: str = ""
    es_field_allowlist: str = (
        "@timestamp,message,log.level,service.name,kubernetes.namespace,kubernetes.pod.name,"
        "error.message,http.response.status_code"
    )
    kibana_url: str | None = None
    loki_label_allowlist: str = "namespace,pod,container,node,app,job,level,stream"

    # Kubernetes. In-cluster ServiceAccount when api_url is empty.
    k8s_api_url: str | None = None
    k8s_token_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"  # noqa: S105
    k8s_ca_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    k8s_namespace_allowlist: str = ""

    # GitHub. A read-only token (GitHub App installation token in production).
    github_token: str | None = None
    github_api_url: str = "https://api.github.com"
    github_repo_allowlist: str = ""
    github_terraform_workflow: str | None = None

    # Code reads: "flux" reads the Flux source-controller artifact of the deployed
    # GitOps revision, "git" keeps partial clones of the allowed repositories.
    codebase_backend: str = "flux"
    codebase_cache_dir: str = "/cache/repos"
    git_url_template: str = "https://x-access-token:{token}@github.com/{repo}.git"
    flux_source_namespace: str = "flux-system"
    flux_source_name: str = "flux-system"
    flux_kustomization: str = "flux-system"
    flux_repo: str = "Laobei2012/oci-free-cloud-k8s"

    # Jira and Confluence. Off when the URL is empty.
    jira_url: str | None = None
    jira_user: str | None = None
    jira_token: str | None = None
    jira_project_allowlist: str = ""
    confluence_url: str | None = None
    confluence_user: str | None = None
    confluence_token: str | None = None
    confluence_space_allowlist: str = ""

    # Jenkins. Off when the URL or the token is empty. The user needs only Overall/Read and Job/Read.
    jenkins_url: str | None = None
    jenkins_public_url: str | None = None
    jenkins_user: str | None = None
    jenkins_token: str | None = None
    jenkins_job_allowlist: str = ""

    # Requests per minute for each backend.
    rate_limit_per_minute: int = 60
    rate_limit_burst: int = 20

    @cached_property
    def namespaces(self) -> list[str]:
        return _csv(self.k8s_namespace_allowlist)

    @cached_property
    def repos(self) -> list[str]:
        return _csv(self.github_repo_allowlist)

    @cached_property
    def es_indices(self) -> list[str]:
        return _csv(self.es_index_allowlist)

    @cached_property
    def es_fields(self) -> list[str]:
        return _csv(self.es_field_allowlist)

    @cached_property
    def loki_labels(self) -> list[str]:
        return _csv(self.loki_label_allowlist)

    @cached_property
    def jira_projects(self) -> list[str]:
        return _csv(self.jira_project_allowlist)

    @cached_property
    def jenkins_jobs(self) -> list[str]:
        return _csv(self.jenkins_job_allowlist)

    @cached_property
    def confluence_spaces(self) -> list[str]:
        return _csv(self.confluence_space_allowlist)
