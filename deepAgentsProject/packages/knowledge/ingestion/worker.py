"""One document per isolated interpreter. Executed by absolute path, never imported."""
import json
import math
import os
import signal
import sys


def _limit(resource, name, value):
    kind = getattr(resource, name)
    _, hard = resource.getrlimit(kind)
    bound = value if hard == resource.RLIM_INFINITY else min(hard, value)
    resource.setrlimit(kind, (bound, bound))


def _bootstrap(header):
    import resource

    values = header['limits']
    for key, maximum in (('cpu_seconds', 120), ('memory_bytes', 4 * 1024**3)):
        if type(values[key]) is not int or not 1 <= values[key] <= maximum:
            raise ValueError('Invalid resource limit')
    timeout = values['timeout_seconds']
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 300:
        raise ValueError('Invalid timeout')
    signal.setitimer(signal.ITIMER_REAL, timeout)
    _limit(resource, 'RLIMIT_CORE', 0)
    _limit(resource, 'RLIMIT_FSIZE', 0)
    _limit(resource, 'RLIMIT_NOFILE', 64)
    _limit(resource, 'RLIMIT_CPU', values['cpu_seconds'])
    _limit(resource, 'RLIMIT_NPROC', 0)
    hard_memory = sys.platform.startswith('linux')
    if hard_memory:
        _limit(resource, 'RLIMIT_AS', values['memory_bytes'])
    elif header.get('require_hard_memory'):
        raise RuntimeError('Hard memory isolation is unavailable on this platform')
    return 'address-space' if hard_memory else 'rss-watchdog'


def _guard(event, args):
    # Defence in depth for Python-level APIs, not a substitute for an OS sandbox.
    if event.startswith(('socket.', 'subprocess.', 'os.exec', 'os.spawn', 'os.posix_spawn', 'os.fork', 'ctypes.')):
        raise PermissionError('Parser external operations are forbidden')
    if event in {'os.system', 'resource.setrlimit'}:
        raise PermissionError('Parser external operations are forbidden')
    if event == 'open':
        flags = args[2]
        if isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            raise PermissionError('Parser filesystem writes are forbidden')


def _write(result, maximum):
    output = bytearray()
    for part in json.JSONEncoder(ensure_ascii=False, separators=(',', ':'), allow_nan=False).iterencode(result):
        output.extend(part.encode('utf-8'))
        if len(output) > maximum:
            from packages.knowledge.errors import ParseLimitExceeded
            raise ParseLimitExceeded('Parser result exceeds output limit')
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()


def main():
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(300)  # Bound bootstrap/pipe wait even when the supervisor dies.
    if sys.platform.startswith('linux'):
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            raise RuntimeError('Unable to install parent-death guard')
    if len(sys.argv) != 2 or os.getppid() != int(sys.argv[1]):
        raise RuntimeError('Parser supervisor is no longer alive')
    line = sys.stdin.buffer.readline(8193)
    if len(line) > 8192 or not line.endswith(b'\n'):
        raise ValueError('Invalid parser header')
    header = json.loads(line)
    mode = _bootstrap(header)
    # Only the trusted package root is added; -I excludes cwd/user Python paths.
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from packages.knowledge.ingestion.limits import ParseLimits
    from packages.knowledge.ingestion.parsers import DocumentParser
    from packages.knowledge.ingestion.chunker import StructureAwareChunker
    from packages.knowledge.errors import KnowledgeValidationError, ParseLimitExceeded

    limits = ParseLimits(**header['limits'])
    sandbox = {'mode': 'resource-only'}
    if header.get('require_os_sandbox'):
        from packages.knowledge.ingestion.linux_sandbox import enforce
        sandbox = enforce()
    sys.addaudithook(_guard)
    # No untrusted document byte is consumed before kernel policy/self-tests.
    content = sys.stdin.buffer.read(limits.max_input_bytes + 1)
    if len(content) > limits.max_input_bytes:
        raise ParseLimitExceeded('Parser input exceeds the limit')
    if len(content) != header['size']:
        raise KnowledgeValidationError('Incomplete parser input')
    parser = DocumentParser(limits)
    chunker = StructureAwareChunker(limits.chunk_characters, limits.overlap_characters, limits=limits)
    chunks = chunker.chunk(parser.parse(content, header['content_type'], header['filename']))
    _write({'v': 1, 'status': 'ok', 'parser_version': parser.version, 'chunker_version': chunker.version,
        'memory_mode': mode, 'sandbox': sandbox,
        'chunks': [chunk.model_dump() for chunk in chunks]}, limits.max_output_bytes)


if __name__ == '__main__':
    try:
        main()
    except MemoryError:
        os.write(1, b'{"v":1,"status":"error","code":"KNOWLEDGE_PARSE_LIMIT_EXCEEDED"}')
    except Exception as error:
        code = getattr(error, 'code', 'KNOWLEDGE_PARSE_UNAVAILABLE')
        allowed = {'KNOWLEDGE_VALIDATION_ERROR', 'KNOWLEDGE_PARSE_LIMIT_EXCEEDED', 'KNOWLEDGE_PARSE_UNAVAILABLE'}
        if code not in allowed:
            code = 'KNOWLEDGE_VALIDATION_ERROR'
        # Do not return document contents, library exceptions or paths.
        os.write(1, json.dumps({'v': 1, 'status': 'error', 'code': code}).encode())
