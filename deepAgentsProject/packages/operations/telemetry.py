"""Application-owned telemetry. Payloads, URLs and credentials are never attributes."""
from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import os
import re
import secrets
import time
from datetime import datetime
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily

from packages.secrets import read_secret


_active = contextvars.ContextVar('deepagent_telemetry', default=None)
_correlation = contextvars.ContextVar('deepagent_correlation', default={})
OPERATIONS = frozenset({'runtime.attempt', 'knowledge.ingestion', 'runtime.cancellation',
    'model.call', 'sandbox.command', 'knowledge.parse', 'knowledge.embed', 'knowledge.scan'})
OUTCOMES = frozenset({'ok', 'error', 'cancelled'})
IDENTIFIER = re.compile(r'[a-zA-Z0-9_-]{1,80}\Z')


@dataclass(frozen=True)
class TelemetrySettings:
    role: str = 'api'
    sample_rate: float = 0.1
    metrics_token: str = field(default='', repr=False)
    endpoint: str = ''
    export_token: str = field(default='', repr=False)
    ca_file: str = ''
    production: bool = False

    def __post_init__(self):
        if self.role not in {'api', 'worker', 'all', 'sandbox-service', 'migrate'}:
            raise ValueError('Unsupported telemetry service role')
        if not math.isfinite(self.sample_rate) or not 0 <= self.sample_rate <= 1:
            raise ValueError('Trace sample rate must be between zero and one')
        for value in (self.metrics_token, self.export_token):
            if value and (len(value) < 32 or len(value) > 512 or not re.fullmatch(r'[A-Za-z0-9._~-]+', value)):
                raise ValueError('Telemetry tokens must contain 32–512 URL-safe characters')
        if self.endpoint:
            parsed = urlsplit(self.endpoint)
            if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or parsed.path != '/v1/traces' or parsed.scheme not in {'http', 'https'}):
                raise ValueError('OTLP endpoint must be a credential-free /v1/traces URL')
            if parsed.scheme != 'https' and (self.production or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}):
                raise ValueError('OTLP requires HTTPS; HTTP is limited to development loopback')
            if self.production and not self.export_token:
                raise ValueError('Production OTLP requires a file-backed collector credential')
        elif self.export_token or self.ca_file:
            raise ValueError('Collector credentials require an explicit OTLP endpoint')

    @classmethod
    def from_environment(cls, role, values=None, *, production=False):
        def value(name, default=''):
            return os.environ.get(name, (values or {}).get(name, default)).strip()
        # Do not let SDK auto-detection add host/environment metadata, headers,
        # proxy credentials, exporters or an alternate destination behind our policy.
        internal_metrics = 'OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED'
        if any(name.startswith('OTEL_') and not (name == internal_metrics and os.environ[name] == 'true') for name in os.environ):
            raise ValueError('Use explicit DEEPAGENT telemetry settings, not ambient OTEL_* variables')
        settings = cls(role=role, production=production,
            sample_rate=float(value('DEEPAGENT_TRACE_SAMPLE_RATE', '0.1')),
            endpoint=value('DEEPAGENT_OTLP_TRACES_ENDPOINT'), ca_file=value('DEEPAGENT_OTLP_CA_FILE'),
            metrics_token=read_secret('DEEPAGENT_METRICS_TOKEN', values=values, production=production),
            export_token=read_secret('DEEPAGENT_OTLP_TOKEN', values=values, production=production))
        if production and role in {'api', 'worker'} and (not settings.metrics_token or not settings.endpoint):
            raise ValueError('Production API/Worker requires metrics authentication and an explicit OTLP collector')
        if production and role in {'api', 'worker'} and os.getenv(internal_metrics) != 'true':
            raise ValueError('Production requires OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED=true for queue-loss visibility')
        return settings


def correlation():
    result = dict(_correlation.get())
    span = trace.get_current_span().get_span_context()
    if span.is_valid:
        result.update(trace_id=f'{span.trace_id:032x}', span_id=f'{span.span_id:016x}')
    return result


def current_telemetry():
    return _active.get()


def persist_origin(db, kind, entity_id):
    """Called inside the business transaction; idempotent retries keep provenance."""
    table = {'run': 'run_trace_origins', 'ingestion': 'ingestion_trace_origins'}[kind]
    current = trace.get_current_span().get_span_context()
    observer = getattr(db, 'telemetry', None)
    if not current.is_valid and observer is not None:
        with observer.span('operation.submit', context=Context()):
            return persist_origin(db, kind, entity_id)
    request_id = _correlation.get().get('request_id') or secrets.token_hex(16)
    db.execute(f'''INSERT INTO {table}
        (entity_id,trace_id,parent_span_id,sampled,request_id,created_at) VALUES(?,?,?,?,?,?)
        ON CONFLICT(entity_id) DO NOTHING''',
        (entity_id, f'{current.trace_id:032x}' if current.is_valid else secrets.token_hex(16),
         f'{current.span_id:016x}' if current.is_valid else secrets.token_hex(8),
         int(current.trace_flags.sampled) if current.is_valid else 0, request_id, db.current_time().isoformat()))


class HealthCollector:
    def __init__(self):
        self.monitor = None

    def collect(self):
        data = self.monitor.snapshot(details=True) if self.monitor else {}
        fresh = bool(self.monitor and 'observation' not in data.get('checks', {}))
        yield GaugeMetricFamily('deepagent_observation_fresh', 'Cached dependency observation is current', value=int(fresh))
        yield GaugeMetricFamily('deepagent_ready', 'Dependencies and consumers are ready', value=int(data.get('status') == 'healthy'))
        if not fresh:
            return  # Unknown is absent, never a fabricated zero queue or healthy worker.
        yield GaugeMetricFamily('deepagent_observation_timestamp_seconds', 'Last completed observation Unix timestamp',
            value=datetime.fromisoformat(data['checked_at']).timestamp())
        workers = GaugeMetricFamily('deepagent_workers_online', 'Live consumers across the database cluster', labels=['kind'])
        depth = GaugeMetricFamily('deepagent_queue_depth', 'Queued work across the database cluster', labels=['kind'])
        age = GaugeMetricFamily('deepagent_queue_oldest_seconds', 'Oldest queued work age', labels=['kind'])
        for kind in ('runtime', 'knowledge'):
            workers.add_metric([kind], data.get('workers', {}).get('by_type', {}).get(kind, 0))
            queue = data.get('queues', {}).get(kind, {})
            depth.add_metric([kind], queue.get('depth', 0))
            age.add_metric([kind], queue.get('oldest_wait_seconds', 0))
        yield workers
        yield depth
        yield age
        cancellation = data.get('cancellations', {})
        yield GaugeMetricFamily('deepagent_cancellations_pending', 'Runs awaiting durable cancellation completion', value=cancellation.get('pending', 0))
        yield GaugeMetricFamily('deepagent_cancellation_oldest_seconds', 'Oldest incomplete cancellation age', value=cancellation.get('oldest_seconds', 0))


class ObservedExporter(SpanExporter):
    def __init__(self, inner, registry):
        self.inner = inner
        self.results = Counter('deepagent_trace_export_batches', 'OTLP batch export outcomes', ['outcome'], registry=registry)

    def export(self, spans):
        try:
            result = self.inner.export(spans)
        except Exception:
            result = SpanExportResult.FAILURE
        self.results.labels('ok' if result == SpanExportResult.SUCCESS else 'error').inc()
        return result

    def shutdown(self):
        self.inner.shutdown()


class SdkQueueCollector:
    def __init__(self, reader):
        self.reader = reader

    def collect(self):
        data = self.reader.get_metrics_data()
        metrics = [metric for resource in (data.resource_metrics if data else [])
            for scope in resource.scope_metrics for metric in scope.metrics]
        enabled = any(metric.name == 'otel.sdk.processor.span.queue.capacity' for metric in metrics)
        yield GaugeMetricFamily('deepagent_trace_queue_observation_enabled', 'SDK bounded queue observations are enabled', value=int(enabled))
        for source, target in (('otel.sdk.processor.span.queue.size','deepagent_trace_queue_size'),
                               ('otel.sdk.processor.span.queue.capacity','deepagent_trace_queue_capacity')):
            matches = [point.value for metric in metrics if metric.name == source for point in metric.data.data_points]
            if matches:
                yield GaugeMetricFamily(target, 'SDK bounded trace export queue', value=sum(matches))
        dropped = [point.value for metric in metrics if metric.name == 'otel.sdk.processor.span.processed'
            for point in metric.data.data_points if point.attributes.get('error.type') == 'queue_full']
        if enabled:
            yield CounterMetricFamily('deepagent_trace_queue_dropped', 'Spans dropped because the bounded export queue was full', value=sum(dropped))


def http_exporter(settings):
    import requests
    import ssl
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    if settings.endpoint.startswith('https://'):
        ssl.create_default_context(cafile=settings.ca_file or None)

    class CollectorSession(requests.Session):
        def __init__(self):
            super().__init__()
            self.trust_env = False

        def request(self, method, url, **kwargs):
            if method.upper() != 'POST' or url != settings.endpoint:
                raise ValueError('Unexpected telemetry destination')
            timeout = min(3, kwargs.get('timeout', 3))
            if timeout <= 0:
                raise requests.exceptions.Timeout('Collector deadline exceeded')
            kwargs.update(allow_redirects=False, verify=settings.ca_file or True, timeout=timeout)
            response = super().request(method, url, **kwargs)
            if 300 <= response.status_code < 400:
                response.close()
                raise requests.exceptions.InvalidURL('Collector redirect refused')
            return response

    return OTLPSpanExporter(endpoint=settings.endpoint, timeout=3,
        headers={'Authorization': 'Bearer ' + settings.export_token} if settings.export_token else {},
        session=CollectorSession(), certificate_file=settings.ca_file or None)


class OperationSpan:
    def __init__(self, span):
        self.span, self.failed = span, False

    def set_status(self, status):
        self.failed = status == trace.StatusCode.ERROR
        self.span.set_status(status)

    def __getattr__(self, name):
        return getattr(self.span, name)


class Telemetry:
    def __init__(self, settings: TelemetrySettings, *, exporter=None):
        self.settings = settings
        self.registry = CollectorRegistry(auto_describe=False)
        self.sdk_reader = InMemoryMetricReader()
        self.sdk_metrics = MeterProvider(metric_readers=[self.sdk_reader], resource=Resource({}), shutdown_on_exit=False)
        self.registry.register(SdkQueueCollector(self.sdk_reader))
        self.health = HealthCollector()
        self.registry.register(self.health)
        self.requests = Counter('deepagent_http_requests', 'Completed API requests, excluding probes and scrapes',
            ['method', 'route', 'status'], registry=self.registry)
        self.latency = Histogram('deepagent_http_duration_seconds', 'API request/stream lifetime', ['method', 'route'],
            buckets=(.01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30, 120, 600), registry=self.registry)
        self.inflight = Gauge('deepagent_http_inflight', 'Currently active API requests/streams', registry=self.registry)
        self.operations = Counter('deepagent_operations', 'Observed operation outcomes in this process', ['operation', 'outcome'], registry=self.registry)
        self.operation_latency = Histogram('deepagent_operation_duration_seconds', 'Operation attempt lifetime', ['operation'],
            buckets=(.01, .1, 1, 5, 15, 60, 120, 600, 3600), registry=self.registry)
        self.provider = TracerProvider(resource=Resource({'service.name': 'deepagent-' + settings.role, 'service.version': '0.1.0'}),
            sampler=ParentBased(TraceIdRatioBased(settings.sample_rate)),
            span_limits=SpanLimits(max_attributes=16, max_events=0, max_links=0), shutdown_on_exit=False)
        if exporter is not None:
            self.provider.add_span_processor(SimpleSpanProcessor(ObservedExporter(exporter, self.registry)))
        elif settings.endpoint:
            self.provider.add_span_processor(BatchSpanProcessor(ObservedExporter(http_exporter(settings), self.registry),
                max_queue_size=2048, max_export_batch_size=128, schedule_delay_millis=1000, export_timeout_millis=3000,
                meter_provider=self.sdk_metrics))
        self.tracer = self.provider.get_tracer('deepagent', '0.1.0')

    @contextmanager
    def span(self, name, *, context=None, fields=None, kind=trace.SpanKind.INTERNAL):
        token = _active.set(self)
        ids = {key: value for key, value in (fields or {}).items()
               if key in {'request_id', 'run_id', 'attempt_id', 'job_id'} and isinstance(value, str) and IDENTIFIER.fullmatch(value)}
        correlation_token = _correlation.set({**_correlation.get(), **ids})
        with self.tracer.start_as_current_span(name, context=context, kind=kind,
                attributes={'deepagent.' + key: value for key, value in ids.items()},
                record_exception=False, set_status_on_exception=False) as span:
            try:
                yield span
            except BaseException as error:
                span.set_status(trace.StatusCode.ERROR)
                span.set_attribute('error.type', type(error).__name__[:80])
                raise
            finally:
                _correlation.reset(correlation_token)
                _active.reset(token)

    @contextmanager
    def operation(self, name, *, context=None, fields=None):
        if name not in OPERATIONS:
            raise ValueError('Unregistered telemetry operation')
        started, outcome = time.monotonic(), 'ok'
        try:
            with self.span(name, context=context, fields=fields) as span:
                operation_span = OperationSpan(span)
                yield operation_span
                if operation_span.failed:
                    outcome = 'error'
        except asyncio.CancelledError:
            outcome = 'cancelled'
            raise
        except BaseException:
            outcome = 'error'
            raise
        finally:
            self.operations.labels(name, outcome).inc()
            self.operation_latency.labels(name).observe(time.monotonic() - started)

    def render(self):
        return generate_latest(self.registry)

    def close(self):
        self.provider.shutdown()
        self.sdk_metrics.shutdown()


@contextmanager
def operation(name):
    current = _active.get()
    if current is None:
        yield None
    else:
        with current.operation(name) as span:
            yield span


@contextmanager
def task_operation(db, kind, entity_id, name, *, attempt_id=None):
    observer = getattr(db, 'telemetry', None)
    if observer is None:
        yield None
        return
    table = {'run': 'run_trace_origins', 'ingestion': 'ingestion_trace_origins'}[kind]
    origin = db.fetch_one(f'SELECT * FROM {table} WHERE entity_id=?', (entity_id,))
    parent = Context()  # A background consumer must not inherit an unrelated request.
    fields = {'run_id' if kind == 'run' else 'job_id': entity_id, 'attempt_id': attempt_id}
    if origin:
        try:
            source = trace.SpanContext(trace_id=int(origin['trace_id'], 16), span_id=int(origin['parent_span_id'], 16),
                is_remote=True, trace_flags=trace.TraceFlags(int(origin['sampled'])))
            if source.is_valid:
                parent = trace.set_span_in_context(trace.NonRecordingSpan(source), parent)
                fields['request_id'] = origin['request_id']
        except (ValueError, TypeError):
            pass  # Legacy/invalid provenance starts a new root; never trusts user metadata.
    with observer.operation(name, context=parent, fields=fields) as span:
        yield span
