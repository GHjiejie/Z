"""Payload-free model spans for native LangChain callbacks, including nested calls."""
import threading
import time

from langchain_core.callbacks import BaseCallbackHandler
from opentelemetry.trace import StatusCode

from packages.operations.telemetry import current_telemetry


class ModelTraceCallback(BaseCallbackHandler):
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = {}
        self.closed = False

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        observer = current_telemetry()
        if observer is None:
            return
        with self.lock:
            if self.closed or run_id in self.calls:
                return
            self.calls[run_id] = (observer, observer.tracer.start_span('model.call'), time.monotonic())

    def on_llm_start(self, serialized, prompts, *, run_id, **kwargs):
        self.on_chat_model_start(None, None, run_id=run_id)

    def _finish(self, run_id, outcome):
        with self.lock:
            call = self.calls.pop(run_id, None)
        if call is not None:
            self._end(call, outcome)

    @staticmethod
    def _end(call, outcome):
        observer, span, started = call
        if outcome != 'ok':
            span.set_status(StatusCode.ERROR)
        span.end()
        observer.operations.labels('model.call', outcome).inc()
        observer.operation_latency.labels('model.call').observe(time.monotonic() - started)

    def on_llm_end(self, response, *, run_id, **kwargs):
        self._finish(run_id, 'ok')

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._finish(run_id, 'error')

    def close(self):
        with self.lock:
            self.closed = True
            pending, self.calls = self.calls, {}
        for call in pending.values():
            self._end(call, 'cancelled')
