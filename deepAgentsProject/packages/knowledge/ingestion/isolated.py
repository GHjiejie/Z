from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading

import psutil

from packages.knowledge.errors import KnowledgeValidationError, ParseLimitExceeded, ParseProtocolError, ParseTimeout, ParseUnavailable
from packages.knowledge.models import ChunkRecord
from .chunker import StructureAwareChunker
from .limits import ParseLimits
from .parsers import DocumentParser


_slots_lock = threading.Lock()
_active = 0


async def _acquire(limit):
    global _active
    while True:
        with _slots_lock:
            if _active < limit:
                _active += 1
                return
        await asyncio.sleep(0.02)


def _release():
    global _active
    with _slots_lock:
        _active -= 1


async def _complete_cleanup(task):
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    task.result()
    if interrupted:
        raise asyncio.CancelledError()


class IsolatedDocumentParser:
    """Bounded one-shot child; no business credentials, DB handles or shell.

    Production requires Linux hard memory limits and the Landlock/seccomp
    policy. macOS development uses sampled RSS plus CPU/time limits only.
    """
    parser_version = DocumentParser.version
    chunker_version = StructureAwareChunker.version

    def __init__(self, limits: ParseLimits | None = None):
        self.limits = limits or ParseLimits.from_environment()
        production = os.getenv('DEEPAGENT_ENVIRONMENT', 'development').strip().lower() in {'prod', 'production'}
        setting = os.getenv('DEEPAGENT_PARSER_OS_SANDBOX', '').strip().lower()
        if setting not in {'', 'true', 'false'} or production and setting == 'false':
            raise ParseUnavailable('Production parser OS isolation cannot be disabled; use true/false outside production')
        self.require_os_sandbox = production or setting == 'true'
        self.require_hard_memory = self.require_os_sandbox
        self.runtime_verified = False

    async def validate_runtime(self):
        self.runtime_verified = False
        if self.require_os_sandbox:
            chunks = await self.parse(b'Parser startup self-test.', 'text/plain', 'self-test.txt')
            if len(chunks) != 1 or chunks[0].text != 'Parser startup self-test.':
                raise ParseUnavailable('Parser startup self-test failed')
        self.runtime_verified = True

    def _command(self):
        return [sys.executable, '-I', '-B', str(Path(__file__).with_name('worker.py')), str(os.getpid())]

    async def _spawn(self, directory):
        return await asyncio.create_subprocess_exec(*self._command(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=65536, cwd=directory, start_new_session=True, close_fds=True,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
                 'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1'})

    async def _write(self, stream, header, content):
        try:
            stream.write(header)
            await stream.drain()
            for start in range(0, len(content), 65536):
                stream.write(memoryview(content)[start:start + 65536])
                await stream.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            stream.close()

    @staticmethod
    async def _read(stream, maximum):
        result = bytearray()
        while data := await stream.read(min(65536, maximum - len(result) + 1)):
            result.extend(data)
            if len(result) > maximum:
                raise ParseLimitExceeded('Parser output exceeds the limit')
        return bytes(result)

    async def _watch(self, process):
        try:
            child = psutil.Process(process.pid)
            while process.returncode is None:
                if child.memory_info().rss > self.limits.memory_bytes:
                    raise ParseLimitExceeded('Parser exceeded its resident memory limit')
                await asyncio.sleep(0.02)
        except psutil.NoSuchProcess:
            return
        except psutil.AccessDenied as error:
            raise ParseUnavailable('Parser memory monitoring is unavailable') from error

    @staticmethod
    async def _cleanup(creation, tasks):
        if creation is None:
            return
        try:
            process = await creation
        except Exception:
            return
        # The session/group is owned by this invocation. A crashed leader may
        # leave descendants holding pipes open, so clean up the group as well.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process.stdin:
            process.stdin.close()

        async def discard(stream):
            while await stream.read(65536):
                pass
        # Drain owned pipes as well as reaping: waiting with full PIPE buffers
        # can otherwise deadlock even after the child has been killed.
        await asyncio.gather(discard(process.stdout), discard(process.stderr), process.wait())

    def _decode(self, output, returncode):
        if returncode in {-signal.SIGALRM, -signal.SIGXCPU}:
            raise ParseTimeout('Document parsing exceeded its time limit')
        if returncode:
            raise ParseLimitExceeded('Parser terminated before completing the document')
        try:
            result = json.loads(output)
            if result.get('v') != 1:
                raise ValueError('Invalid protocol version')
            if result.get('status') == 'error':
                errors = {error.code: error for error in (KnowledgeValidationError, ParseLimitExceeded, ParseUnavailable)}
                error = errors.get(result.get('code'))
                if error is None:
                    raise ValueError('Invalid parser error')
                raise error('Document parser rejected the input or could not enforce its resource policy')
            if (result.get('status') != 'ok' or result.get('parser_version') != self.parser_version
                    or result.get('chunker_version') != self.chunker_version
                    or result.get('memory_mode') not in {'address-space', 'rss-watchdog'}
                    or self.require_hard_memory and result['memory_mode'] != 'address-space'):
                raise ValueError('Invalid parser identity')
            if self.require_os_sandbox:
                sandbox = result.get('sandbox')
                if (not isinstance(sandbox, dict) or sandbox.get('mode') != 'landlock-seccomp-v1'
                        or type(sandbox.get('landlock_abi')) is not int or sandbox['landlock_abi'] < 3):
                    raise ValueError('Missing parser OS isolation evidence')
            rows = result['chunks']
            if not isinstance(rows, list) or not 0 < len(rows) <= self.limits.max_chunks:
                raise ValueError('Invalid chunk count')
            chunks = []
            characters = 0
            for index, row in enumerate(rows):
                if not isinstance(row, dict) or set(row) != {'position', 'text', 'token_count', 'content_hash', 'locator'}:
                    raise ValueError('Invalid chunk fields')
                text, locator = row['text'], row['locator']
                if (type(row['position']) is not int or row['position'] != index or not isinstance(text, str)
                        or not 0 < len(text) <= self.limits.chunk_characters
                        or type(row['token_count']) is not int or row['token_count'] != max(1, len(text) // 4)
                        or row['content_hash'] != hashlib.sha256(text.encode()).hexdigest()
                        or not isinstance(locator, dict) or set(locator) - {'page', 'paragraph', 'row', 'table', 'section'}):
                    raise ValueError('Invalid chunk content')
                for key, value in locator.items():
                    if key == 'section':
                        if value is not None and (not isinstance(value, str) or len(value) > 512):
                            raise ValueError('Invalid section')
                    elif type(value) is not int or value < 1:
                        raise ValueError('Invalid locator')
                characters += len(text)
                if characters > self.limits.max_text_characters + self.limits.max_chunks * self.limits.overlap_characters:
                    raise ValueError('Invalid extracted text count')
                chunks.append(ChunkRecord(**row))
            return chunks
        except (KnowledgeValidationError, ParseUnavailable):
            raise
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
            raise ParseProtocolError('Parser returned an invalid result') from error

    async def parse(self, content: bytes, content_type: str, filename: str) -> list[ChunkRecord]:
        if os.name != 'posix':
            raise ParseUnavailable('Isolated parsing requires a POSIX worker')
        if self.require_hard_memory and not sys.platform.startswith('linux'):
            raise ParseUnavailable('Production document parsing requires Linux hard memory limits')
        if len(content) > self.limits.max_input_bytes:
            raise ParseLimitExceeded('Document exceeds parser input limit')
        header = json.dumps({'limits': asdict(self.limits), 'size': len(content), 'content_type': content_type,
            'filename': filename, 'require_hard_memory': self.require_hard_memory,
            'require_os_sandbox': self.require_os_sandbox}).encode() + b'\n'
        if len(header) > 8192:
            raise ParseLimitExceeded('Document metadata exceeds parser header limit')
        creation = None
        tasks = []
        acquired = False
        # The semaphore is process-wide and thread-safe, not bound to one loop.
        with tempfile.TemporaryDirectory(prefix='deepagent-parser-') as directory:
            try:
                async with asyncio.timeout(self.limits.timeout_seconds):
                    await _acquire(self.limits.max_concurrent)
                    acquired = True
                    creation = asyncio.create_task(self._spawn(directory))
                    process = await asyncio.shield(creation)
                    tasks = [asyncio.create_task(self._write(process.stdin, header, content)),
                        asyncio.create_task(self._read(process.stdout, self.limits.max_output_bytes)),
                        asyncio.create_task(self._read(process.stderr, 16384)),
                        asyncio.create_task(process.wait()), asyncio.create_task(self._watch(process))]
                    await asyncio.gather(*tasks)
                    return self._decode(tasks[1].result(), process.returncode)
            except TimeoutError as error:
                raise ParseTimeout('Document parsing exceeded its time limit') from error
            except OSError as error:
                raise ParseUnavailable('Unable to start or supervise document parser') from error
            finally:
                try:
                    await _complete_cleanup(asyncio.create_task(self._cleanup(creation, tasks)))
                finally:
                    if acquired:
                        _release()
