"""Wrapper para Vercel: expone la app de FastAPI."""
import sys
import os
from pathlib import Path
from fastapi import FastAPI

# Placeholder: garantiza que 'app' siempre existe para Vercel
app = FastAPI()

# Añade el directorio padre al path para que pueda importar server.py
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from server import app
except Exception as e:
    print(f"ERROR importando server.py: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()

    # Si falla, usa un app minimal que muestre el error
    @app.get("/")
    def error():
        return {"error": str(e), "type": type(e).__name__}

__all__ = ["app"]
