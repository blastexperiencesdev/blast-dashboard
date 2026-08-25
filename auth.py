"""Inicio de sesión de Customer Lab.

El tablero muestra datos personales de compradores (nombre, email, celular), así
que no puede quedar abierto en una URL pública que cualquiera adivine.

La sesión viaja en una cookie firmada con HMAC, no en memoria del proceso: en
Vercel cada petición puede caer en una instancia distinta, así que una sesión
guardada en RAM se perdería entre un clic y el siguiente.

Usuarios — variable de entorno CUSTOMER_LAB_USERS, formato:
    usuario:clave,otro:otraclave

Si no hay usuarios configurados:
  * en local (sin la variable VERCEL) el guardia se apaga, para no estorbar el
    desarrollo;
  * en producción se bloquea todo, porque un despliegue sin la variable deja el
    tablero abierto a internet y eso es peor que un tablero caído.
"""
import base64
import hashlib
import hmac
import html
import os
import time
import urllib.parse

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE = "cl_sesion"
#: Cuánto dura la sesión antes de volver a pedir clave.
DURACION = 30 * 24 * 3600
#: Rutas que se sirven sin sesión. El logo entra porque lo usa la propia
#: pantalla de login, que por definición se ve antes de tener sesión.
PUBLICAS = {"/login", "/logout", "/static/logo.png", "/favicon.ico"}

EN_VERCEL = bool(os.environ.get("VERCEL"))


def usuarios(env: dict) -> dict:
    """Lee el mapa usuario -> clave desde la configuración."""
    crudo = (env.get("CUSTOMER_LAB_USERS") or "").strip()
    out = {}
    for par in crudo.split(","):
        if ":" in par:
            u, _, c = par.partition(":")
            if u.strip() and c.strip():
                out[u.strip()] = c.strip()
    return out


def _secreto(env: dict) -> bytes:
    """Llave para firmar cookies.

    Si no se define SESSION_SECRET se deriva de las credenciales: así, cambiar
    una clave invalida automáticamente las sesiones que seguían abiertas.
    """
    explicito = (env.get("SESSION_SECRET") or "").strip()
    if explicito:
        return explicito.encode()
    base = (env.get("CUSTOMER_LAB_USERS") or "sin-usuarios").encode()
    return hashlib.sha256(b"customer-lab/" + base).digest()


def _firma(usuario: str, expira: int, secreto: bytes) -> str:
    msg = f"{usuario}|{expira}".encode()
    return hmac.new(secreto, msg, hashlib.sha256).hexdigest()


def crear_cookie(usuario: str, secreto: bytes) -> str:
    expira = int(time.time()) + DURACION
    u64 = base64.urlsafe_b64encode(usuario.encode()).decode().rstrip("=")
    return f"{u64}.{expira}.{_firma(usuario, expira, secreto)}"


def leer_cookie(valor: str, env: dict):
    """Devuelve el usuario si la cookie es válida y vigente; si no, None."""
    if not valor or valor.count(".") != 2:
        return None
    u64, exp, firma = valor.split(".")
    try:
        relleno = "=" * (-len(u64) % 4)
        usuario = base64.urlsafe_b64decode(u64 + relleno).decode()
        expira = int(exp)
    except Exception:
        return None
    if expira < time.time():
        return None
    if not hmac.compare_digest(firma, _firma(usuario, expira, _secreto(env))):
        return None
    # Si al usuario se le quitó el acceso, su cookie deja de servir aunque siga
    # bien firmada y sin vencer.
    if usuario not in usuarios(env):
        return None
    return usuario


def _destino_seguro(valor: str) -> str:
    """Evita que ?next= mande al visitante a otro dominio tras iniciar sesión."""
    if valor.startswith("/") and not valor.startswith("//"):
        return valor
    return "/"


def pagina_login(error: str = "", destino: str = "/") -> str:
    aviso = (
        f'<p class="error">{html.escape(error)}</p>' if error else ""
    )
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Customer Lab — Iniciar sesión</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@700;900&family=Space+Grotesk:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:#0a0812; --card:#120f1e; --border:#2a1f4a; --text:#e8e0ff;
    --muted:#7c6fa0; --accent:#9b5cff; --accent2:#c084fc; --bad:#f43f5e;
  }}
  * {{ box-sizing:border-box; margin:0; }}
  body {{
    font-family:"Space Grotesk",-apple-system,"Segoe UI",sans-serif;
    background:var(--bg);
    background-image:radial-gradient(ellipse at 20% 0%,rgba(100,40,200,.18) 0%,transparent 60%),
                     radial-gradient(ellipse at 80% 100%,rgba(155,92,255,.10) 0%,transparent 60%);
    background-attachment:fixed; color:var(--text);
    min-height:100vh; display:flex; align-items:center; justify-content:center; padding:24px;
  }}
  .caja {{
    width:100%; max-width:380px; background:var(--card);
    border:1px solid var(--border); border-radius:14px; padding:32px 28px;
    box-shadow:0 8px 48px rgba(100,40,200,.18);
  }}
  .marca {{ display:flex; align-items:center; gap:10px; margin-bottom:22px; }}
  .marca img {{ width:26px; height:26px; object-fit:contain; }}
  h1 {{
    font-family:"Orbitron",sans-serif; font-size:19px; font-weight:700; letter-spacing:.05em;
    background:linear-gradient(90deg,#c084fc,#9b5cff);
    -webkit-background-clip:text; -webkit-text-fill-color:transparent;
  }}
  label {{ display:block; font-size:12px; text-transform:uppercase; letter-spacing:.08em;
           color:var(--muted); margin:16px 0 6px; }}
  input {{
    width:100%; padding:11px 13px; font-size:15px; font-family:inherit;
    background:#0d0a17; color:var(--text);
    border:1px solid var(--border); border-radius:9px;
  }}
  input:focus {{ outline:none; border-color:var(--accent); box-shadow:0 0 0 3px rgba(155,92,255,.18); }}
  button {{
    width:100%; margin-top:24px; padding:12px; font-size:15px; font-weight:600; font-family:inherit;
    color:#fff; background:linear-gradient(90deg,var(--accent),var(--accent2));
    border:none; border-radius:9px; cursor:pointer;
  }}
  button:hover {{ filter:brightness(1.1); }}
  .error {{
    margin-top:16px; padding:10px 12px; font-size:13px;
    color:var(--bad); background:rgba(244,63,94,.1);
    border:1px solid rgba(244,63,94,.3); border-radius:8px;
  }}
  .pie {{ margin-top:20px; font-size:12px; color:var(--muted); text-align:center; }}
</style>
</head>
<body>
  <form class="caja" method="post" action="/login">
    <div class="marca">
      <img src="/static/logo.png" alt="">
      <h1>Customer Lab</h1>
    </div>
    <input type="hidden" name="next" value="{html.escape(destino, quote=True)}">
    <label for="usuario">Usuario</label>
    <input id="usuario" name="usuario" autocomplete="username" autofocus required>
    <label for="clave">Contraseña</label>
    <input id="clave" name="clave" type="password" autocomplete="current-password" required>
    <button type="submit">Entrar</button>
    {aviso}
    <p class="pie">Acceso restringido · Blasttickets</p>
  </form>
</body>
</html>"""


def montar(app, env: dict):
    """Engancha el guardia y las rutas de sesión sobre la app de FastAPI."""

    @app.middleware("http")
    async def guardia(request, call_next):
        ruta = request.url.path
        if ruta in PUBLICAS:
            return await call_next(request)

        registrados = usuarios(env)
        if not registrados:
            if not EN_VERCEL:
                # Desarrollo local sin credenciales: se deja pasar.
                return await call_next(request)
            return HTMLResponse(
                "<h1>Customer Lab sin configurar</h1>"
                "<p>Falta la variable CUSTOMER_LAB_USERS en el entorno.</p>",
                status_code=503,
            )

        if leer_cookie(request.cookies.get(COOKIE, ""), env):
            return await call_next(request)

        # El navegador espera una pantalla; el JavaScript espera JSON. Mandarle
        # el HTML del login a un fetch() haría que el tablero muestre un error
        # críptico en vez de llevar a iniciar sesión.
        if ruta.startswith("/api/"):
            return JSONResponse({"detail": "sesion_requerida"}, status_code=401)
        destino = ruta
        if request.url.query:
            destino += "?" + request.url.query
        return RedirectResponse(f"/login?next={destino}", status_code=303)

    @app.get("/login")
    def login_form(next: str = "/"):
        return HTMLResponse(
            pagina_login(destino=_destino_seguro(next)),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/login")
    async def login_enviar(request: Request):
        # Se parsea el cuerpo a mano en vez de request.form() para no depender
        # de python-multipart, que no está en requirements.txt.
        crudo = (await request.body()).decode("utf-8", "replace")
        form = {
            k: v[0]
            for k, v in urllib.parse.parse_qs(crudo, keep_blank_values=True).items()
        }
        usuario = (form.get("usuario") or "").strip()
        clave = form.get("clave") or ""
        destino = _destino_seguro(form.get("next") or "/")

        esperada = usuarios(env).get(usuario)
        # compare_digest en vez de == para no filtrar por tiempo de respuesta
        # cuántos caracteres de la clave iban bien.
        if not esperada or not hmac.compare_digest(clave, esperada):
            return HTMLResponse(
                pagina_login("Usuario o contraseña incorrectos.", destino),
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )

        resp = RedirectResponse(destino, status_code=303)
        seguro = EN_VERCEL or request.headers.get("x-forwarded-proto") == "https"
        resp.set_cookie(
            COOKIE, crear_cookie(usuario, _secreto(env)),
            max_age=DURACION, httponly=True, samesite="lax", secure=seguro, path="/",
        )
        return resp

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp
