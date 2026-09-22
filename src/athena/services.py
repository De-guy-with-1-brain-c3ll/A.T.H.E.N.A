"""One place that builds the services every ATHENA interface hands to its tools.

Voice, text chat, Feishu, the dashboard, and the tool CLI all discover the same
tools. If an interface forgets a service, the matching tool silently reports
itself unavailable: alarms failed with "Alarms are unavailable until the local
scheduler starts" in every interface except voice mode. Building the registry
here keeps that from drifting apart again.
"""
from __future__ import annotations

import os
from typing import Any

from athena.alerts import AlertScheduler, Notify
from athena.sleep import SleepRunner
from athena.tools.registry import ToolRegistry
from athena.watches import build_watchers


def teams_graph_if_configured():
    """Return a Teams client only when a Microsoft application ID is configured."""
    if not os.environ.get("MICROSOFT_CLIENT_ID", "").strip():
        return None
    from athena.tools.teams import TeamsGraph
    return TeamsGraph()


def build_registry(settings, notify: Notify | None = None, **extra: Any
                   ) -> tuple[ToolRegistry, AlertScheduler]:
    """Build the tool registry together with the alarm scheduler it needs.

    The caller must ``await alerts.start()`` for alarms to fire and
    ``await alerts.close()`` on shutdown.
    """
    alerts = AlertScheduler(notify or (lambda _text: False),
                            teams_graph=teams_graph_if_configured())
    services: dict[str, Any] = {"settings": settings, "alert_scheduler": alerts}
    services.update(extra)
    # Sleep mode is long-running and shared: the tools, the voice coordinator and
    # the CLI must all see one status record, not three.
    services.setdefault("sleep_runner", SleepRunner())
    registry = ToolRegistry.discover(services=services)
    # Proactive checks reuse the same clients the tools already hold.
    alerts.watchers = build_watchers()
    # The scheduler itself is handed over too: the CJ watcher saves a brief
    # through it, and without it the watcher returned nothing every afternoon.
    alerts.watch_services = {"teams_graph": alerts.teams_graph,
                             "registry": registry,
                             "alert_scheduler": alerts}
    return registry, alerts
