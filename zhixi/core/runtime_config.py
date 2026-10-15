"""Machine-specific paths, with explicit overrides and legacy-compatible defaults.

Reading configuration never creates directories, downloads weights or starts a
process. Per-task model selections remain separate from these server defaults.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from zhixi.paths import PROJECT_ROOT


ROOT = PROJECT_ROOT
LEGACY_MODEL = Path(r'D:\Models\ai_comic\runtime\models\Qwen3.5-9B-GGUF\Qwen3.5-9B-Q4_K_M.gguf')
LEGACY_MODEL_PYTHON = Path(r'D:\comfyUI\.venv\Scripts\python.exe')
DEFAULT_EDGE = Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')


@dataclass(frozen=True)
class RuntimePaths:
    data_dir: Path
    model_path: Path
    model_python: Path
    edge_path: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / 'history.sqlite3'


def load_paths(root: Path = ROOT, env: Mapping[str, str] | None = None) -> RuntimePaths:
    values = os.environ if env is None else env

    def path(name: str, fallback: Path) -> Path:
        configured = values.get(name, '').strip()
        result = Path(configured).expanduser() if configured else fallback
        return result if result.is_absolute() else root / result

    return RuntimePaths(
        data_dir=path('WP_DATA_DIR', root / 'data'),
        model_path=path('WP_MODEL_PATH', LEGACY_MODEL),
        model_python=path(
            'WP_MODEL_PYTHON', LEGACY_MODEL_PYTHON if LEGACY_MODEL_PYTHON.exists() else Path(sys.executable)
        ),
        edge_path=path('WP_EDGE_PATH', DEFAULT_EDGE),
    )
