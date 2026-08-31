import asyncio
import json
import logging
import multiprocessing
import secrets
import socket
import ssl
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from prometheus_client.parser import text_string_to_metric_families

from apps import production_entrypoint
from packages.operations.http import TelemetryMiddleware, WorkerMetricsApp
from packages.operations.logging import SafeJsonFormatter, LOG_CONFIG
from packages.operations.telemetry import Telemetry, TelemetrySettings, correlation, operation, persist_origin, task_operation
from packages.operations.worker_metrics import worker_metrics
from packages.persistence import create_database
from packages.runtime.run_lease import RunLeaseManager
from test_runtime_concurrency import runtime, new_thread, new_run


@pytest.fixture
def observer():
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(TelemetrySettings(sample_rate=1, metrics_token='synthetic-metrics-' + 'x'*32), exporter=exporter)
    try:
        yield telemetry, exporter
    finally:
        telemetry.close()


def samples(observer):
    return [sample for family in text_string_to_metric_families(observer.render().decode()) for sample in family.samples]


def app_for(observer):
    app = FastAPI()
    app.state.telemetry = observer
    app.add_middleware(TelemetryMiddleware, application=app)

    @app.get('/items/{item_id}')
    async def item(item_id: str):
        await asyncio.sleep(0)
        return correlation()

    @app.get('/explode')
    async def explode():
        raise ValueError('private-prompt-and-credential')

    return app


def test_request_ids_ignore_public_provenance_and_metrics_are_low_cardinality(observer):
    telemetry, exporter = observer
    with TestClient(app_for(telemetry)) as client:
        ids = []
        for index in range(20):
            response = client.get(f'/items/private-document-{index}?token=secret', headers={
                'X-Request-ID': 'untrusted-id', 'traceparent': '00-'+'1'*32+'-'+'2'*16+'-01',
                'baggage': 'user=private-person', 'Authorization': 'Bearer private-token'})
            assert response.status_code == 200
            assert response.json()['request_id'] == response.headers['x-request-id']
            assert response.json()['trace_id'] == response.headers['x-trace-id']
            assert response.json()['trace_id'] != '1'*32
            ids.append(response.headers['x-request-id'])
    assert len(set(ids)) == 20
    assert correlation() == {}
    records = exporter.get_finished_spans()
    assert all(item.name == 'GET /items/{item_id}' and item.parent is None for item in records)
    serialized = repr([(item.name, dict(item.attributes), item.events, item.resource.attributes) for item in records])
    assert 'private-' not in serialized and 'secret' not in serialized
    counters = [item for item in samples(telemetry) if item.name == 'deepagent_http_requests_total']
    assert len(counters) == 1 and counters[0].value == 20
    assert counters[0].labels == {'method':'GET','route':'/items/{item_id}','status':'200'}


def test_unhandled_errors_keep_safe_correlation_headers_without_exception_text(observer):
    telemetry, exporter = observer
    with TestClient(app_for(telemetry), raise_server_exceptions=False) as client:
        response = client.get('/explode')
    assert response.status_code == 500 and response.headers['x-request-id']
    assert 'private-' not in response.text
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes['error.type'] == 'ValueError' and not span.events
    assert correlation() == {}


@pytest.mark.asyncio
async def test_concurrent_requests_keep_separate_contexts(observer):
    telemetry, exporter = observer
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(telemetry)), base_url='http://testserver') as client:
        responses = await asyncio.gather(*(client.get('/items/' + str(index)) for index in range(30)))
    assert len({response.json()['trace_id'] for response in responses}) == 30
    assert len({response.json()['request_id'] for response in responses}) == 30
    assert correlation() == {}
    assert len(exporter.get_finished_spans()) == 30


@pytest.mark.asyncio
async def test_streaming_disconnect_closes_context_and_inflight(observer):
    telemetry, exporter = observer
    sent = []

    async def stream(scope, receive, send):
        scope['route'] = SimpleNamespace(path='/stream/{run_id}')
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':b'private-stream-token','more_body':True})
        raise asyncio.CancelledError()

    async def send(message):
        sent.append(message)

    wrapped = TelemetryMiddleware(stream, SimpleNamespace(state=SimpleNamespace(telemetry=telemetry)))
    with pytest.raises(asyncio.CancelledError):
        await wrapped({'type':'http','method':'GET','path':'/stream/private-id','headers':[]}, None, send)
    assert sent[-1]['more_body'] is True
    assert next(item.value for item in samples(telemetry) if item.name == 'deepagent_http_inflight') == 0
    assert next(item for item in samples(telemetry) if item.name == 'deepagent_http_requests_total').labels['status'] == '499'
    assert correlation() == {}
    assert not exporter.get_finished_spans()[0].events


def test_formatter_never_formats_message_args_exception_or_request_payload(observer):
    telemetry, _ = observer

    class HostileMessage:
        def __str__(self):
            raise AssertionError('Formatter must never call arbitrary __str__')

    try:
        raise ValueError('private-prompt-and-password')
    except ValueError:
        record = logging.LogRecord('packages.runtime.orchestrator', logging.ERROR, __file__, 42,
            HostileMessage(), ('private-argument',), sys.exc_info())
    record.authorization = 'private-token'
    record.body = 'private-document'
    record.telemetry_event = 'http.completed'
    record.status = 500
    with telemetry.span('http.request', fields={'request_id':'a'*32}):
        result = SafeJsonFormatter().format(record)
    assert 'private-' not in result
    decoded = json.loads(result)
    assert decoded['request_id'] == 'a'*32 and decoded['trace_id']
    assert decoded['error_type'] == 'ValueError' and decoded['frames']
    assert decoded['event'] == 'http.completed'


@pytest.mark.parametrize('options', [
    {'sample_rate':float('nan')}, {'sample_rate':1.1}, {'sample_rate':-1},
    {'metrics_token':'short'}, {'metrics_token':'x'*32+'\r\nBad: value'},
    {'endpoint':'https://user:password@collector/v1/traces'},
    {'endpoint':'https://collector/v1/traces?token=secret'}, {'endpoint':'https://collector/v1/traces#secret'},
    {'endpoint':'http://collector/v1/traces'}, {'endpoint':'https://collector/wrong'},
    {'endpoint':'http://127.0.0.1/v1/traces','production':True},
    {'endpoint':'https://collector/v1/traces','production':True}, {'export_token':'x'*32},
])
def test_invalid_telemetry_config_fails_closed(options):
    with pytest.raises(ValueError):
        TelemetrySettings(**options)


def test_ambient_sdk_settings_and_inline_production_credentials_are_rejected(monkeypatch):
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_HEADERS', 'Authorization=private-token')
    with pytest.raises(ValueError, match='ambient'):
        TelemetrySettings.from_environment('api')
    monkeypatch.delenv('OTEL_EXPORTER_OTLP_HEADERS')
    monkeypatch.setenv('DEEPAGENT_METRICS_TOKEN', 'x'*32)
    with pytest.raises(RuntimeError, match='FILE'):
        TelemetrySettings.from_environment('api', production=True)


def test_metrics_requires_distinct_token_and_never_reads_database_on_scrape(runtime, monkeypatch):
    client, services, *_ = runtime
    telemetry = services.db.telemetry
    telemetry.settings = replace(telemetry.settings, metrics_token='metrics-'+'x'*40)
    monkeypatch.setattr(services.health, 'collect', lambda: pytest.fail('Scrapes must use cached observations'))
    assert client.get('/metrics').status_code == 401
    assert client.get('/metrics', headers={'Authorization':'Bearer user-session'}).status_code == 401
    response = client.get('/metrics', headers={'Authorization':'Bearer '+telemetry.settings.metrics_token})
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert 'deepagent_queue_depth' in response.text
    assert 'metrics-'+ 'x'*40 not in response.text
    duplicated = client.get('/metrics', headers=[('Authorization','Bearer '+telemetry.settings.metrics_token),
        ('Authorization','Bearer '+telemetry.settings.metrics_token)])
    assert duplicated.status_code == 401
    telemetry.settings = replace(telemetry.settings, metrics_token='')
    assert client.get('/metrics').status_code == 503


def test_stale_observations_do_not_export_zero_queue_or_online_workers(runtime):
    _, services, *_ = runtime
    telemetry = services.db.telemetry
    services.health.observed -= services.health.settings.stale_after + 1
    result = telemetry.render().decode()
    assert 'deepagent_observation_fresh 0.0' in result
    assert 'deepagent_workers_online' not in result and 'deepagent_queue_depth' not in result


def test_incomplete_cancellation_is_visible_as_age_and_count(runtime):
    client, services, *_ = runtime
    run = new_run(runtime)
    old = (services.db.current_time()-timedelta(seconds=600)).isoformat()
    services.db.execute("UPDATE runs SET status='CANCELLING',updated_at=? WHERE id=?", (old,run['id']))
    client.portal.call(services.health.refresh)
    values = {item.name:item.value for item in samples(services.db.telemetry) if not item.labels}
    assert values['deepagent_cancellations_pending'] == 1
    assert values['deepagent_cancellation_oldest_seconds'] >= 600


def test_run_trace_origin_matches_response_is_immutable_and_ignores_metadata(runtime):
    client, services, *_ = runtime
    thread = new_thread(runtime)
    url = f"/api/v1/threads/{thread['id']}/runs"
    body = {'input':'synthetic private prompt', 'metadata':{'trace_id':'untrusted-trace', 'request_id':'untrusted-request'}}
    response = client.post(url, json=body, headers={'Idempotency-Key':'trace-retry'})
    assert response.status_code == 202, response.text
    origin = services.db.fetch_one('SELECT * FROM run_trace_origins WHERE entity_id=?', (response.json()['id'],))
    assert origin['trace_id'] == response.headers['x-trace-id']
    assert origin['request_id'] == response.headers['x-request-id']
    assert response.json()['observability'] == {key: origin[key] for key in ('trace_id','request_id')}
    second = client.post(url, json=body, headers={'Idempotency-Key':'trace-retry'})
    assert second.status_code == 202 and second.json()['id'] == response.json()['id']
    assert services.db.fetch_one('SELECT * FROM run_trace_origins WHERE entity_id=?', (response.json()['id'],)) == origin
    assert second.headers['x-trace-id'] != origin['trace_id']


def test_origin_rolls_back_with_run_creation_failure(runtime, monkeypatch):
    client, services, *_ = runtime
    thread = new_thread(runtime)
    original = services.events.append

    def reject(run_id, event, *args, **kwargs):
        if event == 'run.created':
            raise RuntimeError('synthetic failure')
        return original(run_id, event, *args, **kwargs)

    before = services.db.fetch_one('SELECT COUNT(*) AS n FROM run_trace_origins')['n']
    monkeypatch.setattr(services.events, 'append', reject)
    with pytest.raises(RuntimeError, match='synthetic'):
        client.post(f"/api/v1/threads/{thread['id']}/runs", json={'input':'will roll back'})
    assert services.db.fetch_one('SELECT COUNT(*) AS n FROM run_trace_origins')['n'] == before
    assert not services.db.fetch_all('SELECT id FROM runs WHERE thread_id=?', (thread['id'],))


def _trace_child(database_url, run_id, output):
    db = create_database(database_url)
    exporter = InMemorySpanExporter()
    observer = db.telemetry = Telemetry(TelemetrySettings(role='worker', sample_rate=1), exporter=exporter)
    try:
        with task_operation(db, 'run', run_id, 'runtime.attempt'):
            with operation('model.call'):
                data = correlation()
        output.put(data)
    finally:
        observer.close()
        db.close()


def test_trace_origin_survives_real_process_boundary(runtime):
    client, services, *_, database_url = runtime
    thread = new_thread(runtime)
    response = client.post(f"/api/v1/threads/{thread['id']}/runs", json={'input':'trace process boundary'})
    ctx = multiprocessing.get_context('spawn')
    output = ctx.Queue()
    process = ctx.Process(target=_trace_child, args=(database_url, response.json()['id'], output))
    try:
        process.start()
        result = output.get(timeout=15)
        process.join(15)
        assert process.exitcode == 0
        assert result['trace_id'] == response.headers['x-trace-id']
        assert result['request_id'] == response.headers['x-request-id']
        assert result['run_id'] == response.json()['id']
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        output.close()


def test_unsampled_operations_still_count_failures():
    exporter = InMemorySpanExporter()
    observer = Telemetry(TelemetrySettings(sample_rate=0), exporter=exporter)
    try:
        with observer.operation('sandbox.command') as span:
            span.set_status(StatusCode.ERROR)
        assert not exporter.get_finished_spans()
        assert any(item.name == 'deepagent_operations_total' and item.labels['outcome'] == 'error' and item.value == 1
            for item in samples(observer))
    finally:
        observer.close()


@pytest.mark.parametrize('redirect', [False, True])
def test_real_otlp_http_export_is_payload_free_and_never_follows_redirects(monkeypatch, redirect):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            content = self.rfile.read(int(self.headers['Content-Length']))
            requests.append((self.path, content, self.headers.get('Authorization')))
            self.send_response(302 if redirect else 200)
            if redirect:
                self.send_header('Location', '/forbidden-destination')
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('HTTP_PROXY', 'http://not-a-real-proxy.invalid:1')
    observer = Telemetry(TelemetrySettings(sample_rate=1,
        endpoint=f'http://127.0.0.1:{server.server_port}/v1/traces', export_token='collector-'+'x'*32))
    try:
        with observer.operation('model.call'):
            private_prompt = 'private-prompt-must-never-be-exported'
        assert observer.provider.force_flush(timeout_millis=5000)
        assert len(requests) == 1 and requests[0][0] == '/v1/traces'
        assert private_prompt.encode() not in requests[0][1]
        assert requests[0][2] == 'Bearer collector-'+'x'*32
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
        envelope = ExportTraceServiceRequest.FromString(requests[0][1])
        assert envelope.resource_spans[0].scope_spans[0].spans[0].name == 'model.call'
        assert any(item.name == 'deepagent_trace_export_batches_total' and item.value == 1
            and item.labels['outcome'] == ('error' if redirect else 'ok') for item in samples(observer))
    finally:
        observer.close()
        server.shutdown()
        server.server_close()
        thread.join(3)


@pytest.mark.asyncio
async def test_worker_tls_metrics_surface_is_authenticated_and_has_no_business_routes(observer, tmp_path, monkeypatch):
    telemetry, _ = observer
    cert, key = tmp_path/'cert.pem', tmp_path/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
        '-keyout',str(key),'-out',str(cert),'-subj','/CN=127.0.0.1',
        '-addext','subjectAltName=IP:127.0.0.1'], check=True, capture_output=True)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    monkeypatch.setenv('DEEPAGENT_ENVIRONMENT','production')
    monkeypatch.setenv('DEEPAGENT_WORKER_METRICS_PORT',str(port))
    monkeypatch.setenv('DEEPAGENT_METRICS_TLS_CERT_FILE',str(cert))
    monkeypatch.setenv('DEEPAGENT_METRICS_TLS_KEY_FILE',str(key))
    services = SimpleNamespace(db=SimpleNamespace(telemetry=telemetry))
    async with worker_metrics(services):
        assert not services.metrics_task.done()
        async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(cert)),trust_env=False) as client:
            url = f'https://127.0.0.1:{port}'
            assert (await client.get(url+'/metrics')).status_code == 401
            assert (await client.get(url+'/metrics',headers={'Authorization':'Bearer '+telemetry.settings.metrics_token})).status_code == 200
            assert (await client.get(url+'/api/v1/users')).status_code == 404
    assert services.metrics_task is None


def test_production_api_and_worker_share_safe_logging_config():
    config = json.loads((Path(__file__).resolve().parents[1]/'apps/logging.json').read_text())
    assert config == LOG_CONFIG
    command = production_entrypoint.command('api')
    assert '--no-access-log' in command and '--log-config' in command


def test_model_callbacks_close_nested_and_abandoned_spans_without_payloads(observer):
    from packages.operations.model_tracing import ModelTraceCallback
    telemetry, exporter = observer
    callback = ModelTraceCallback()
    with telemetry.operation('runtime.attempt') as parent:
        callback.on_chat_model_start({'private':'provider-key'}, ['private prompt'], run_id='one')
        callback.on_chat_model_start({}, ['private nested prompt'], run_id='two')
        callback.on_llm_end('private completion', run_id='one')
        callback.on_llm_end('late duplicate', run_id='one')
        callback.close()
        callback.on_chat_model_start({}, ['late private prompt'], run_id='three')
    spans = exporter.get_finished_spans()
    calls = [span for span in spans if span.name == 'model.call']
    assert len(calls) == 2 and not callback.calls
    assert all(span.parent.span_id == parent.get_span_context().span_id for span in calls)
    assert all(not span.events and not span.attributes for span in calls)
    values = {item.labels['outcome']:item.value for item in samples(telemetry)
        if item.name == 'deepagent_operations_total' and item.labels['operation'] == 'model.call'}
    assert values == {'ok':1,'cancelled':1}


def test_bounded_export_queue_reports_losses_and_does_not_block_business(monkeypatch):
    import packages.operations.telemetry as module
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
    released, started = threading.Event(), threading.Event()

    class SlowExporter(SpanExporter):
        def export(self, spans):
            started.set()
            assert released.wait(10)
            return SpanExportResult.FAILURE

        def shutdown(self):
            pass

    monkeypatch.setenv('OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED','true')
    monkeypatch.setattr(module,'http_exporter',lambda settings: SlowExporter())
    observer = Telemetry(TelemetrySettings(sample_rate=1,endpoint='http://127.0.0.1/v1/traces'))
    try:
        for _ in range(128):
            with observer.operation('model.call'):
                pass
        assert started.wait(3)
        for _ in range(2200):
            with observer.operation('model.call'):
                pass
        values = {item.name:item.value for item in samples(observer) if not item.labels}
        assert values['deepagent_trace_queue_capacity'] == 2048
        assert values['deepagent_trace_queue_size'] <= 2048
        assert values['deepagent_trace_queue_dropped_total'] > 0
        with TestClient(app_for(observer)) as client:
            assert client.get('/items/still-available').status_code == 200
    finally:
        released.set()
        observer.close()


def test_schema19_preserves_legacy_runs_without_fabricated_provenance(runtime):
    _, services, *_ = runtime
    run = new_run(runtime)
    db = services.db
    before = db.fetch_one('SELECT * FROM runs WHERE id=?',(run['id'],))
    db.execute('DROP TABLE run_trace_origins')
    db.execute('DROP TABLE ingestion_trace_origins')
    db.execute('DELETE FROM schema_migrations WHERE version=19')
    db.initialize()
    assert db.schema_versions()[-1] == 20
    assert db.fetch_one('SELECT * FROM runs WHERE id=?',(run['id'],)) == before
    assert not db.fetch_all('SELECT * FROM run_trace_origins')
    with task_operation(db, 'run', run['id'], 'runtime.attempt'):
        assert correlation()['trace_id']


def test_ingestion_origin_and_parse_embed_spans_follow_completion_request(runtime, monkeypatch, observer):
    from test_knowledge_atomicity import setup_operation
    client, services, *_ = runtime
    telemetry, exporter = observer
    telemetry.health.monitor = services.health
    monkeypatch.setattr(services.db,'telemetry',telemetry)
    monkeypatch.setattr(client.app.state,'telemetry',telemetry)
    prepare, _ = setup_operation(runtime,'upload')
    prepared = prepare('trace-ingest')
    assert client.put(prepared['upload']['url'],content=b'fixture',headers={'Content-Type':'text/plain'}).status_code == 200
    response = client.post(f"/api/v1/knowledge-document-versions/{prepared['document_version_id']}:complete",json={})
    assert response.status_code == 202, response.text
    job_id = response.json()['id']
    origin = services.db.fetch_one('SELECT * FROM ingestion_trace_origins WHERE entity_id=?',(job_id,))
    assert origin['trace_id'] == response.headers['x-trace-id']
    client.portal.call(services.knowledge._process_job,job_id)
    spans = [span for span in exporter.get_finished_spans() if f'{span.context.trace_id:032x}' == origin['trace_id']]
    assert {'knowledge.ingestion','knowledge.scan','knowledge.parse','knowledge.embed'}.issubset({span.name for span in spans})
    assert services.db.fetch_one('SELECT status FROM knowledge_ingestion_jobs WHERE id=?',(job_id,))['status'] == 'SUCCEEDED'
