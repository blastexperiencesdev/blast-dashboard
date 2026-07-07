"""Wrapper para Vercel: expone la app de FastAPI."""
import sys
from pathlib import Path

# Añade el directorio padre al path para que pueda importar server.py
sys.path.insert(0, str(Path(__file__).parent.parent))

from server import app

# Vercel espera que haya un objeto 'app' o una función ASGI
__all__ = ["app"]
