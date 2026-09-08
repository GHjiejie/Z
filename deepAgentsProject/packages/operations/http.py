"""Pure ASGI telemetry preserves streaming and context across async work."""
import asyncio
import hmac
import logging
import secrets
import time

from opentelemetry import trace
from opentelemetry.context import Context
from starlette.responses import PlainTextResponse, Response

logger = logging.getLogger(__name__)
PROBES = frozenset({'/metrics', '/livez', '/readyz', '/health'})
METHODS = frozenset({'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'})


async def metrics_response(observer, scope, receive, send):
    token = observer.settings.metrics_token
    values = [value for key, value in scope.get('headers', []) if key.lower() == b'authorization']
    if not token:
        response = PlainTextResponse('Metrics are not configured', status_code=503)
    elif len(values) != 1 or not hmac.compare_digest(values[0], ('Bearer ' + token).encode('ascii')):
        response = PlainTextResponse('Unauthorized', status_code=401, headers={'WWW-Authenticate': 'Bearer'})
    else:
        response = Response(observer.render(), media_type='text/plain; version=0.0.4; charset=utf-8')
    response.headers['Cache-Control'] = 'no-store'
    await response(scope, receive, send)


class MetricsResponse(Response):
    def __init__(self, observer):
        super().__init__()
        self.observer = observer

    async def __call__(self, scope, receive, send):
        await metrics_response(self.observer, scope, receive, send)


class TelemetryMiddleware:
    def __init__(self, app, application):
        self.app, self.application = app, application

    async def __call__(self, scope, receive, send):
        observer = getattr(self.application.state, 'telemetry', None)
        if scope['type'] != 'http' or observer is None or scope.get('path') in PROBES:
            return await self.app(scope, receive, send)
        method = scope.get('method', '')
        method = method if method in METHODS else 'OTHER'
        status, started, response_started = 500, time.monotonic(), False
        request_id = secrets.token_hex(16)  # Never accept public tracing/baggage/ID headers as trusted provenance.
        observer.inflight.inc()
        with observer.span('http.request', context=Context(), fields={'request_id': request_id}, kind=trace.SpanKind.SERVER) as span:
            async def wrapped_send(message):
                nonlocal status, response_started
                if message['type'] == 'http.response.start':
                    response_started = True
                    status = message['status']
                    headers = [(key, value) for key, value in message.get('headers', [])
                        if key.lower() not in {b'x-request-id', b'x-trace-id'}]
                    context = span.get_span_context()
                    headers.extend([(b'x-request-id', request_id.encode()), (b'x-trace-id', f'{context.trace_id:032x}'.encode())])
                    message = {**message, 'headers': headers}
                await send(message)
            try:
                await self.app(scope, receive, wrapped_send)
            except asyncio.CancelledError:
                status = 499
                raise
            except Exception:
                status = 500
                if not response_started:
                    await PlainTextResponse('Internal Server Error', status_code=500)(scope, receive, wrapped_send)
                raise
            finally:
                route = getattr(scope.get('route'), 'path', '__unmatched__')
                # The route is a code-defined template; no request path/query/header/body is exported.
                span.update_name(method + ' ' + route)
                span.set_attribute('http.request.method', method)
                span.set_attribute('http.route', route)
                span.set_attribute('http.response.status_code', status)
                if status >= 500:
                    span.set_status(trace.StatusCode.ERROR)
                duration = time.monotonic() - started
                observer.requests.labels(method, route, str(status)).inc()
                observer.latency.labels(method, route).observe(duration)
                observer.inflight.dec()
                logger.info('http.completed', extra={'telemetry_event': 'http.completed',
                    'status': status, 'duration_ms': round(duration * 1000, 3)})


class WorkerMetricsApp:
    """Dedicated worker management surface: no business APIs or identity bypass."""
    def __init__(self, observer):
        self.observer = observer

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return
        if scope.get('method') == 'GET' and scope.get('path') == '/metrics':
            await metrics_response(self.observer, scope, receive, send)
        else:
            await PlainTextResponse('Not Found', status_code=404)(scope, receive, send)
