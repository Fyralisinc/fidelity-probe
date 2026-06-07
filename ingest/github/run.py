"""GitHub slice orchestration: wire App auth + historical/live and emit the report."""
from __future__ import annotations

from ..config import GitHubConfig, WebhookConfig
from ..fidelity import FidelityReport
from ..schemas import SpecValidator
from ..webhook_server import WebhookServer
from . import auth, historical, live


def _new_report(cfg: GitHubConfig) -> FidelityReport:
    report = FidelityReport("github", cfg.base_url)
    report.note("GitHub's official REST description is OpenAPI 3.0.3 and authoritative "
                "(its own response examples uphold their schemas), so missing/extra "
                "fields and protocol deviations are counted as real divergences.")
    return report


def run_historical(max_repos: int | None = None) -> FidelityReport:
    cfg = GitHubConfig.from_env()
    report = _new_report(cfg)
    sv = SpecValidator("github")
    gh = auth.acquire_client(cfg, sv, report)
    historical.run_historical(gh, report, sv, max_repos=max_repos)
    return report


def run_live(run_seconds: float | None = None) -> FidelityReport:
    cfg = GitHubConfig.from_env()
    report = _new_report(cfg)
    server = WebhookServer("github-events")
    live.register(server, cfg, report)
    wcfg = WebhookConfig.from_env()
    print(f"GitHub webhook listener on http://{wcfg.host}:{wcfg.port}{live.ENDPOINT}")
    if run_seconds is not None:
        print(f"Running for {run_seconds}s, then writing the fidelity report.")
        server.serve_for(wcfg, run_seconds)
        return report
    print("Ctrl-C to stop and write the fidelity report.")
    try:
        server.run(wcfg)
    except KeyboardInterrupt:
        pass
    return report
