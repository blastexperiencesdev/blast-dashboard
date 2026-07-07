import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from server import app
except Exception:
    traceback.print_exc()
    raise

import uvicorn

uvicorn.run(app, host="127.0.0.1", port=8899)
