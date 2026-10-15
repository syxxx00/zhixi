"""Read OPS metadata from legacy results without retaining the report tree.

ijson owns JSON syntax/encoding. Memory follows the largest individual value
and selected metadata, not the whole report; it is not a constant-memory claim.
"""

import os
from contextlib import contextmanager
from decimal import Decimal


_FIELDS = {
    'goal',
    'project_id',
    'process_id',
    'process_started_at_unix',
    'heartbeat_unix',
    'terminal_observation',
    'seconds',
    'error',
    'error_type',
    'completed',
    'total',
}


@contextmanager
def open_snapshot(path):
    """Release each bounded read before parsing, so Windows writers can replace."""
    yield _SnapshotStream(path)


class _SnapshotStream:
    def __init__(self, path):
        self.path = path
        self.name = str(path)
        self.offset = 0
        self.identity = None

    def read(self, size):
        if size == 0:  # ijson probes whether the stream returns bytes.
            return b''
        if size < 0:
            raise ValueError('Snapshot reads must be bounded')
        with self.path.open('rb') as stream:
            stat = os.fstat(stream.fileno())
            identity = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
            if self.identity is not None and self.identity != identity:
                raise OSError('Experiment snapshot changed during read')
            self.identity = identity
            stream.seek(self.offset)
            block = stream.read(size)
        self.offset += len(block)
        return block


def read_snapshot_metadata(stream, chunk_size=64 * 1024):
    # Lazy import: a missing OPS dependency must not prevent the app starting.
    import ijson
    from ijson.common import ObjectBuilder

    metadata = {'snapshots': []}
    builder = None
    capture_key = None
    depth = 0
    try:
        events = iter(ijson.parse(stream, buf_size=chunk_size))
        if next(events, None) != ('', 'start_map', None):
            raise ValueError('Snapshot must be a JSON object')
        for prefix, event, value in events:
            if builder is None:
                if prefix == 'snapshots':
                    if event not in {'start_array', 'end_array', 'null'}:
                        raise ValueError('Snapshots must be an array or null')
                    if event != 'end_array':
                        metadata['snapshots'] = []
                    continue
                if prefix not in _FIELDS and prefix != 'snapshots.item':
                    continue
                if event in {'map_key', 'end_map', 'end_array'}:
                    continue
                capture_key = prefix
                builder = ObjectBuilder()
            builder.event(event, float(value) if isinstance(value, Decimal) else value)
            if event in {'start_map', 'start_array'}:
                depth += 1
            elif event in {'end_map', 'end_array'}:
                depth -= 1
            if depth == 0:
                if capture_key == 'snapshots.item':
                    metadata['snapshots'] = [builder.value]
                else:
                    metadata[capture_key] = builder.value
                builder = None
        # Consume the entire stream: a truncated suffix is never a valid status.
    except ijson.JSONError as exc:
        raise ValueError('Experiment snapshot JSON is incomplete or invalid') from exc
    metadata['snapshots'] = metadata['snapshots'] or [{}]
    return metadata
