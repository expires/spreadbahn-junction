"""Repository paths, in one place: code lives in src/bahn, data and runs at the repo root."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # src/bahn/paths.py -> repo
DATA = os.path.join(ROOT, "data")        # saved worlds and reveals (gitignored)
RUNS = os.path.join(ROOT, "runs")        # experiment results, logs, checkpoints (gitignored)
CONFIGS = os.path.join(ROOT, "configs")  # fitted parameters shipped with the code
os.makedirs(RUNS, exist_ok=True)
