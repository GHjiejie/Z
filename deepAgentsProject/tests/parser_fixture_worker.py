"""Adversarial child behaviours used only by isolated-parser tests."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time

mode = sys.argv[1]
header = json.loads(sys.stdin.buffer.readline())
if mode != 'blocked-input':
    sys.stdin.buffer.read()

if mode in {'hang', 'blocked-input'}:
    time.sleep(60)
elif mode in {'child', 'orphan'}:
    if os.fork() == 0:
        time.sleep(60)
    elif mode == 'orphan':
        os._exit(0)
    else:
        time.sleep(60)
elif mode == 'memory':
    values = []
    while True:
        values.append(bytearray(4 * 1024**2))
        time.sleep(0.03)
elif mode == 'cpu':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from packages.knowledge.ingestion.worker import _bootstrap
    _bootstrap(header)
    while True:
        pass
elif mode == 'stdout':
    sys.stdout.write('x' * 100_000)
elif mode == 'stderr':
    sys.stderr.write('private-document-sentinel' * 5000)
elif mode == 'crash':
    os._exit(7)
elif mode == 'protocol':
    print('{"v":1,"status":"ok","chunks":[]}')
elif mode == 'inspect':
    descriptor = int(sys.argv[2])
    try:
        os.fstat(descriptor)
        inherited_fd = True
    except OSError:
        inherited_fd = False
    text = json.dumps({'environment': sorted(os.environ), 'cwd': os.getcwd(), 'inherited_fd': inherited_fd,
        'isolated': sys.flags.isolated, 'no_bytecode': sys.dont_write_bytecode})
    print(json.dumps({'v': 1, 'status': 'ok', 'parser_version': 'structure-parser-2.0',
        'chunker_version': 'structure-chunker-2.0', 'memory_mode': 'rss-watchdog', 'chunks': [
            {'position': 0, 'text': text, 'token_count': max(1, len(text) // 4),
             'content_hash': hashlib.sha256(text.encode()).hexdigest(), 'locator': {}}]}))
