"""Wrapper para Vercel: expone la app de FastAPI."""
import sys
import os
from pathlib import Path

# Debug: muestra variables de entorno
print("DEBUG: Variables de entorno disponibles:", sorted(os.environ.keys()))
print("DEBUG: MONGODB_URI present?", "MONGODB_URI" in os.environ)

# Añade el directorio padre al path para que pueda importar server.py
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from server import app
    print("DEBUG: server.py importado exitosamente")
except Exception as e:
    print(f"ERROR importando server.py: {type(e).__name__}: {e}")
    raise

# Vercel espera que haya un objeto 'app' o una función ASGI
__all__ = ["app"]
