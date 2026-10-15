"""Content-free observations of local inference; never an estimated percentage."""

from threading import Event, Lock, Thread
import time
import uuid


class GenerationTelemetry:
    def __init__(self, clock=time.monotonic):
        self.clock, self.lock, self.data = clock, Lock(), {}

    def begin(self, request_id, input_tokens):
        with self.lock:
            self.started = self.clock()
            self.first_token = None
            self.data = {
                'request_id': request_id,
                'phase': 'prefill',
                'input_tokens': input_tokens,
                'generated_tokens': 0,
                'first_token_seconds': None,
                'time_basis': 'wall_including_pauses',
            }

    def advance(self, count):
        with self.lock:
            if count > self.data.get('generated_tokens', 0):
                if self.first_token is None:
                    self.first_token = self.clock()
                    self.data['first_token_seconds'] = round(self.first_token - self.started, 2)
                self.data.update(phase='generating', generated_tokens=count)

    def finish(self, phase):
        with self.lock:
            if self.data:
                self.data.update(phase=phase, elapsed_seconds=round(self.clock() - self.started, 2))

    def snapshot(self):
        with self.lock:
            result = dict(self.data)
            if result and result['phase'] not in {'completed', 'failed'}:
                result['elapsed_seconds'] = round(self.clock() - self.started, 2)
            return result


class TokenObservation:
    """StoppingCriteriaList-compatible observer, with no tensor data transfer."""

    def __init__(self, generation, input_length):
        self.generation, self.input_length = generation, input_length

    def __call__(self, input_ids, scores, **kwargs):
        self.generation.advance(int(input_ids.shape[1]) - self.input_length)
        return False


def generate_with_telemetry(request, payload, observe, poll_seconds=2):
    """Health polling cannot abort, repeat, or replace the generation request."""
    request_id = uuid.uuid4().hex
    stopped = Event()

    def emit(generation):
        if isinstance(generation, dict) and generation.get('request_id') == request_id:
            try:
                observe(generation)
            except Exception:
                pass  # Diagnostics cannot change a successful inference result.

    def monitor():
        while not stopped.wait(poll_seconds):
            try:
                emit(request('/health', timeout=2).get('generation'))
            except Exception:
                # An unavailable observation is not a failed generation.
                continue

    thread = Thread(target=monitor, daemon=True)
    thread.start()
    try:
        response = request('/generate', {**payload, 'request_id': request_id})
    finally:
        stopped.set()
        thread.join(timeout=3)
    emit(response.get('generation'))
    return response
