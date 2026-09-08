"""Fail-closed JSON logs: never format arbitrary messages, args or traceback text."""
import json
import logging
import logging.config
import os
from datetime import datetime, timezone
from pathlib import Path

from packages.operations.telemetry import correlation, IDENTIFIER


class SafeJsonFormatter(logging.Formatter):
    def format(self, record):
        # Unknown/library messages retain diagnostic call-site and exception type,
        # not a guessed regex-redaction of provider payloads or request URLs.
        data = {'timestamp': datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            'level': record.levelname, 'logger': record.name[:120],
            'event': 'diagnostic', 'line': record.lineno,
            'service': 'deepagent-' + os.getenv('DEEPAGENT_PROCESS_ROLE', 'api')}
        safe_event = getattr(record, 'telemetry_event', None)
        if isinstance(safe_event, str) and safe_event in {
                'http.completed', 'worker.started', 'worker.stopped', 'worker.failed', 'telemetry.started'}:
            data['event'] = safe_event
        data.update(correlation())
        if record.exc_info and record.exc_info[0]:
            data['error_type'] = record.exc_info[0].__name__[:80]
            frames = []
            tb = record.exc_info[2]
            while tb is not None and len(frames) < 12:
                # Basename, function and line only: no source lines or locals.
                frames.append({'file': Path(tb.tb_frame.f_code.co_filename).name,
                    'function': tb.tb_frame.f_code.co_name, 'line': tb.tb_lineno})
                tb = tb.tb_next
            data['frames'] = frames
        for key in ('status', 'duration_ms'):
            value = getattr(record, key, None)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 1e12:
                data[key] = value
        return json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


LOG_CONFIG = {
    'version': 1, 'disable_existing_loggers': False,
    'formatters': {'safe': {'()': 'packages.operations.logging.SafeJsonFormatter'}},
    'handlers': {'safe': {'class': 'logging.StreamHandler', 'stream': 'ext://sys.stderr', 'formatter': 'safe'},
                 'discard': {'class': 'logging.NullHandler'}},
    'root': {'handlers': ['safe'], 'level': 'INFO'},
    'loggers': {'uvicorn': {'handlers': ['safe'], 'level': 'INFO', 'propagate': False},
                'uvicorn.error': {'handlers': ['safe'], 'level': 'INFO', 'propagate': False},
                'uvicorn.access': {'handlers': ['discard'], 'propagate': False},
                'httpx': {'level': 'WARNING'}, 'httpcore': {'level': 'WARNING'}},
}


def configure_logging():
    logging.config.dictConfig(LOG_CONFIG)
