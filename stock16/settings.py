import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env(path=None):
    """Small literal env loader: no expansion, commands or secret output."""
    path = Path(path or ROOT / '.env')
    if path.exists():
        for line in path.read_text().splitlines():
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip())


def data_dir():
    path = Path(os.environ.get('STOCK16_DATA_DIR', ROOT / 'data')).resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path
