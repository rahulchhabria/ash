"""Optional conversation export. Spec: specs/integrations.md, specs/subsystems.md."""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import TYPE_CHECKING, Any

from ash.integrations.runtime import IntegrationContext, IntegrationContributor
from ash.observability.braintrust import conversation_record

if TYPE_CHECKING:
    from ash.core.session import SessionState

logger = logging.getLogger(__name__)


class BraintrustIntegration(IntegrationContributor):
    name = "braintrust"
    priority = 950

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._client: Any = None

    async def setup(self, context: IntegrationContext) -> None:
        config = context.config.braintrust
        if not config.enabled or context.mode == "eval":
            return
        if not config.api_key:
            raise ValueError("Braintrust enabled without BRAINTRUST_API_KEY")
        sdk = importlib.import_module("braintrust")
        self._client = await asyncio.to_thread(
            sdk.init_logger,
            project=config.project,
            api_key=config.api_key.get_secret_value(),
            async_flush=True,
            set_current=False,
        )
        self._queue = asyncio.Queue(maxsize=config.queue_size)
        self._worker = asyncio.create_task(self._export(), name="braintrust-export")

    async def _export(self) -> None:
        assert self._queue is not None
        while True:
            record = await self._queue.get()
            try:
                await asyncio.to_thread(self._client.log, **record)
                # Drain the SDK buffer per record so our bounded queue is the buffer.
                await asyncio.to_thread(self._client.flush)
            except Exception as exc:
                logger.warning(
                    "braintrust_export_failed", extra={"error.type": type(exc).__name__}
                )
            finally:
                self._queue.task_done()

    async def on_message_postprocess(
        self,
        user_message: str,
        session: SessionState,
        effective_user_id: str,
        context: IntegrationContext,
    ) -> None:
        if self._queue is None:
            return
        model = context.config.get_model("default")
        metadata: dict[str, Any] = {"model": model.model, "mode": context.mode}
        if context.config.sentry:
            metadata["release"] = context.config.sentry.release
            metadata["environment"] = context.config.sentry.environment
        try:
            sentry = importlib.import_module("sentry_sdk")
            span = sentry.get_current_span()
            if span is not None:
                metadata["sentry_trace_id"] = span.trace_id
        except ImportError:
            pass
        record = conversation_record(
            user_message, session, context.config.braintrust, metadata
        )
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            logger.warning("braintrust_queue_full", extra={"count": 1})

    async def on_shutdown(self, context: IntegrationContext) -> None:
        if self._worker is None or self._queue is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=5)
        except TimeoutError:
            logger.warning("braintrust_shutdown_timeout")
        finally:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
            self._queue = None
