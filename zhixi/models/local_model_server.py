"""Token-authenticated loopback inference service; importing never loads a model."""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any

from zhixi.models.generation_telemetry import GenerationTelemetry, TokenObservation
from zhixi.models.model_input_budget import check_input_budget, render_messages, model_context_length


class ModelLoadError(RuntimeError):
    """A model could not fit; the CLI preserves its existing error event/exit code."""


@dataclass
class ModelState:
    """Resources and telemetry owned by one resident server, not the module."""

    tokenizer: Any
    model: Any
    device: Any
    torch: Any
    stopping_criteria: Any
    runtime: dict[str, Any]
    generation_lock: Any = field(default_factory=Lock)
    generation: GenerationTelemetry = field(default_factory=GenerationTelemetry)

    def close(self) -> None:
        self.model = self.tokenizer = self.device = None
        if self.torch is not None and self.torch.cuda.is_available():
            self.torch.cuda.empty_cache()
        self.torch = None


def load_model(model_path: Path) -> ModelState:
    """Heavy imports and allocation occur only inside an explicit startup call."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, StoppingCriteriaList

    started = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    device_name = torch.cuda.get_device_name(0) if device.type == 'cuda' else 'CPU'
    try:
        model = AutoModelForCausalLM.from_pretrained(model_path, local_files_only=True, dtype='auto')
        model.to(device)
    except torch.OutOfMemoryError as exc:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        raise ModelLoadError('CUDA 显存不足：该模型无法以当前精度装入显卡，请改用 4-bit 量化模型') from exc
    model.eval()
    runtime: dict[str, Any] = {
        'model_name': model_path.name,
        'model_path': str(model_path),
        'inference_device': str(device),
        'inference_device_name': device_name,
        'inference_dtype': str(next(model.parameters()).dtype).replace('torch.', ''),
        'model_parameters': sum(parameter.numel() for parameter in model.parameters()),
        'resident_since': time.time(),
        'startup_seconds': round(time.perf_counter() - started, 2),
        'context_length': model_context_length(model),
    }
    if device.type == 'cuda':
        free_bytes, total_bytes = torch.cuda.mem_get_info(device)
        runtime.update(
            {
                'gpu_memory_total_mb': round(total_bytes / 1024 / 1024),
                'gpu_memory_free_mb': round(free_bytes / 1024 / 1024),
                'gpu_memory_allocated_mb': round(torch.cuda.memory_allocated(device) / 1024 / 1024),
            }
        )
    return ModelState(tokenizer, model, device, torch, StoppingCriteriaList, runtime)


def create_handler(state: ModelState, auth_token: str) -> type[BaseHTTPRequestHandler]:
    """Bind each HTTP server to its own model and token; no process-global state."""

    class Handler(BaseHTTPRequestHandler):
        server_version = 'LocalModelServer/1.0'

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self) -> bool:
            return bool(auth_token) and self.headers.get('X-Local-Model-Token', '') == auth_token

        def do_GET(self) -> None:  # noqa: N802
            if self.path != '/health' or not self.authorized():
                self.send_json(404, {'error': 'not found'})
                return
            self.send_json(
                200, {'ok': True, 'runtime': state.runtime, 'generation': state.generation.snapshot()}
            )

        def do_POST(self) -> None:  # noqa: N802
            if self.path not in {'/generate', '/measure'} or not self.authorized():
                self.send_json(404, {'error': 'not found'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length <= 0 or length > 2_000_000:
                    raise ValueError('本地模型请求大小无效')
                payload = json.loads(self.rfile.read(length))
                messages = payload.get('messages')
                if not isinstance(messages, list) or not messages:
                    raise ValueError('本地模型请求缺少 messages')
                max_tokens = max(1, min(int(payload.get('max_tokens', 2048)), 40_000))
                rendered = render_messages(state.tokenizer, messages,
                                           enable_thinking=bool(payload.get('enable_thinking', False)))
                inputs = state.tokenizer([rendered], return_tensors='pt')
                input_tokens = int(inputs.input_ids.shape[1])
                if self.path == '/measure':
                    self.send_json(200, {'input_tokens': input_tokens,
                                         'context_length': state.runtime.get('context_length')})
                    return
                if payload.get('require_input_budget'):
                    check_input_budget(input_tokens, max_tokens, state.runtime.get('context_length'))
                inputs = inputs.to(state.device)
                with state.generation_lock, state.torch.inference_mode():
                    state.generation.begin(
                        str(payload.get('request_id') or ''), int(inputs.input_ids.shape[1])
                    )
                    try:
                        output = state.model.generate(
                            **inputs,
                            max_new_tokens=max_tokens,
                            do_sample=True,
                            temperature=float(payload.get('temperature', 0.6)),
                            top_p=float(payload.get('top_p', 0.8)),
                            top_k=int(payload.get('top_k', 20)),
                            stopping_criteria=state.stopping_criteria(
                                [TokenObservation(state.generation, int(inputs.input_ids.shape[1]))]
                            ),
                        )
                        state.generation.finish('completed')
                    except Exception:
                        state.generation.finish('failed')
                        raise
                    observation = state.generation.snapshot()
                text = state.tokenizer.decode(
                    output[0][inputs.input_ids.shape[1] :], skip_special_tokens=True
                )
                self.send_json(200, {'text': text, 'generation': observation})
            except state.torch.OutOfMemoryError:
                if state.torch.cuda.is_available():
                    state.torch.cuda.empty_cache()
                self.send_json(507, {'error': 'CUDA 显存不足：当前输入或输出预算超过可用显存'})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json(400, {'error': str(exc)})
            except Exception as exc:  # keep process resident; return the generation failure to the caller
                self.send_json(500, {'error': f'本地模型生成失败：{exc}'})

    return Handler


def serve(state: ModelState, auth_token: str, port: int = 0) -> None:
    """Own the listening socket; its caller retains ownership of the model."""
    server = ThreadingHTTPServer(('127.0.0.1', port), create_handler(state, auth_token))
    try:
        print(
            json.dumps(
                {'event': 'ready', 'port': server.server_port, 'runtime': state.runtime}, ensure_ascii=False
            ),
            flush=True,
        )
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Start the private loopback model service.')
    parser.add_argument('model_path', type=Path)
    parser.add_argument('port', type=int, nargs='?', default=0)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('port must be between 0 and 65535')
    try:
        state = load_model(args.model_path.resolve())
    except ModelLoadError as exc:
        print(json.dumps({'event': 'error', 'error': str(exc)}, ensure_ascii=False), flush=True)
        return 2
    try:
        serve(state, os.environ.get('LOCAL_MODEL_SERVER_TOKEN', ''), args.port)
    finally:
        state.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
