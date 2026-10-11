"""Agent trace lifecycle. Spec: specs/integrations.md (Tracing)."""

from ash.integrations.runtime import IntegrationContext, IntegrationContributor
from ash.observability.tracing import SentryObserver


class TracingIntegration(IntegrationContributor):
    name = "tracing"
    priority = 900

    def trace_observer(self, context: IntegrationContext) -> SentryObserver | None:
        if (
            context.mode == "eval"
            or not context.config.sentry
            or not context.config.sentry.dsn
        ):
            return None
        return SentryObserver()
