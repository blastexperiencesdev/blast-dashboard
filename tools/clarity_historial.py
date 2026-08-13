"""Histórico propio de Clarity: memoria larga sobre una fuente sin memoria.

Clarity solo conserva los últimos 3 días. Este módulo acumula capturas diarias
en `historial_clarity.json` para que el tablero pueda mirar meses atrás sin
depender de esa ventana.

REGLA CENTRAL — capturar con numOfDays=1
    Las ventanas de Clarity se solapan: una foto de 3 días tomada el 13 y otra
    el 14 comparten dos días. Sumarlas contaría el mismo tráfico varias veces.
    Por eso la captura automática pide UN día: cada registro es un día natural
    limpio y la serie sí se puede sumar. Las capturas heredadas de una ventana
    mayor quedan marcadas con `ventana_dias` para que nadie las mezcle.

Se guarda por fecha, no por instante: volver a capturar el mismo día
sobreescribe ese día en vez de duplicarlo, así que el script es idempotente y
se puede reintentar sin ensuciar la serie.

Las ciudades son un caso aparte: la Data Export API no las expone (su dimensión
más fina es Country/Region), solo el MCP de Clarity tiene `City`. Por eso el
registro admite mezclas parciales — la Action escribe comportamiento/tecnología
/UTM y una siembra manual con el MCP agrega las ciudades al mismo día.

Uso:
    python3 tools/clarity_historial.py --importar-julio   # rescata el respaldo
    python3 tools/clarity_historial.py --capturar         # 1 día vía API (1 petición)
    python3 tools/clarity_historial.py --estado           # qué hay guardado
"""
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HISTORIAL = BASE / "historial_clarity.json"

sys.path.insert(0, str(BASE / "tools"))

#: Métricas de comportamiento que se acumulan por evento y por dominio.
CAMPOS_NUM = ("sesiones", "usuarios", "dead_clicks", "rage_clicks", "errores_js")
#: Desgloses tipo {nombre: sesiones}.
CAMPOS_MAPA = ("ciudades", "dispositivos", "navegadores", "sistemas", "utm")


def cargar() -> dict:
    if HISTORIAL.exists():
        return json.loads(HISTORIAL.read_text())
    return {"version": 1, "capturas": {}}


def guardar(hist: dict) -> None:
    HISTORIAL.write_text(json.dumps(hist, ensure_ascii=False, indent=1, sort_keys=True))


def _ficha() -> dict:
    return {**dict.fromkeys(CAMPOS_NUM, 0), **{c: {} for c in CAMPOS_MAPA}}


def _fusionar_ficha(destino: dict, nueva: dict) -> dict:
    """Mezcla una ficha parcial sobre otra.

    Los números se reemplazan solo si vienen con valor: así una siembra de
    ciudades por MCP no pone en cero las sesiones que ya escribió la API.
    """
    out = {**_ficha(), **destino}
    for campo in CAMPOS_NUM:
        if nueva.get(campo):
            out[campo] = nueva[campo]
    for campo in CAMPOS_MAPA:
        if nueva.get(campo):
            out[campo] = {**out.get(campo, {}), **nueva[campo]}
    return out


def registrar(fecha: str, ventana_dias: int, fuente: str,
              eventos: dict = None, dominios: dict = None) -> dict:
    """Escribe (o completa) la captura de un día. Idempotente por fecha."""
    hist = cargar()
    dia = hist["capturas"].get(fecha, {
        "ventana_dias": ventana_dias, "fuentes": [],
        "eventos": {}, "dominios": {},
    })
    dia["ventana_dias"] = ventana_dias
    dia["capturado"] = datetime.now(timezone.utc).isoformat()
    if fuente not in dia["fuentes"]:
        dia["fuentes"].append(fuente)

    for clave, entrantes in (("eventos", eventos or {}), ("dominios", dominios or {})):
        for id_, ficha in entrantes.items():
            dia[clave][id_] = _fusionar_ficha(dia[clave].get(id_, {}), ficha)

    hist["capturas"][fecha] = dia
    guardar(hist)
    return dia


# ---------------------------------------------------------------------------
# Importar el respaldo de julio (lo que existía antes de tener histórico)
# ---------------------------------------------------------------------------

def importar_julio(carpeta: Path = None) -> None:
    """Rescata los cachés congelados del 7 de julio hacia el histórico.

    Eran una ventana de 3 días (no de 1), así que quedan marcados como tal: son
    una foto heredada, no un día comparable con los que vengan después.
    """
    carpeta = carpeta or (BASE / "respaldo_clarity_julio")
    if not carpeta.exists():
        raise SystemExit(f"No existe {carpeta}")

    def leer(nombre):
        f = carpeta / nombre
        return json.loads(f.read_text()) if f.exists() else {}

    evgeo, geo = leer("evgeo_cache.json"), leer("geo_cache.json")
    tech, utm, comp = leer("tech_cache.json"), leer("utm_cache.json"), leer("clarity_cache.json")

    fecha = (evgeo.get("updated") or geo.get("updated") or "2026-07-07")[:10]

    eventos = defaultdict(_ficha)
    for ev, ciudades in (evgeo.get("by_event") or {}).items():
        eventos[ev]["ciudades"] = {c: v.get("sesiones", 0) for c, v in ciudades.items()}
        eventos[ev]["sesiones"] = sum(v.get("sesiones", 0) for v in ciudades.values())

    dominios = defaultdict(_ficha)
    for dom, ciudades in (geo.get("by_domain") or {}).items():
        dominios[dom]["ciudades"] = {c: v.get("sesiones", 0) for c, v in ciudades.items()}
    for dom, dims in (tech.get("by_domain") or {}).items():
        dominios[dom]["dispositivos"] = dict(dims.get("device") or {})
        dominios[dom]["navegadores"] = dict(dims.get("browser") or {})
        dominios[dom]["sistemas"] = dict(dims.get("os") or {})
    for dom, srcs in (utm.get("by_domain") or {}).items():
        dominios[dom]["utm"] = {s: v.get("sesiones", 0) for s, v in srcs.items()}
    for dom, m in (comp.get("domains") or {}).items():
        for campo in CAMPOS_NUM:
            if m.get(campo):
                dominios[dom][campo] = m[campo]

    dia = registrar(fecha, ventana_dias=3, fuente="respaldo-julio-mcp",
                    eventos=dict(eventos), dominios=dict(dominios))
    print(f"julio importado como {fecha} (ventana de {dia['ventana_dias']} días): "
          f"{len(dia['eventos'])} eventos, {len(dia['dominios'])} dominios")


# ---------------------------------------------------------------------------
# Captura diaria automática (Data Export API, 1 día limpio, 1 petición)
# ---------------------------------------------------------------------------

def capturar_dia() -> None:
    import clarity_refresh as cr

    token = cr.cargar_token()
    # numOfDays=1 = las últimas 24 h. Se atribuye al día de AYER porque la
    # captura corre de madrugada y esa ventana cubre casi todo el día anterior.
    payload = cr.pedir_dias(token, 1, "URL")
    fecha = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    eventos, dominios = defaultdict(_ficha), defaultdict(_ficha)
    for metrica in payload:
        campo = cr.METRICAS.get(metrica.get("metricName"))
        if not campo:
            continue
        for fila in metrica.get("information", []):
            url = cr.fila_url(fila)
            if not url:
                continue
            n = cr.sesiones_de(fila)
            for destino, clave in ((eventos, cr.evento_de(url)), (dominios, cr.host_de(url))):
                if not clave:
                    continue
                destino[clave][campo] += n
                if metrica.get("metricName") == "Traffic":
                    destino[clave]["usuarios"] += int(fila.get("distinctUserCount") or 0)

    dia = registrar(fecha, ventana_dias=1, fuente="data-export-api",
                    eventos=dict(eventos), dominios=dict(dominios))
    print(f"capturado {fecha}: {len(dia['eventos'])} eventos, {len(dia['dominios'])} dominios "
          f"(gastó 1 de las 10 peticiones diarias)")


def importar_cachés_actuales() -> None:
    """Vuelca los cachés vigentes al histórico como la captura de hoy.

    Son una ventana de 3 días (la que usa clarity_refresh.py), así que se
    marcan como tal: sirven de foto, no de día sumable.
    """
    def leer(nombre):
        f = BASE / nombre
        return json.loads(f.read_text()) if f.exists() else {}

    comp, tech, utm = leer("clarity_cache.json"), leer("tech_cache.json"), leer("utm_cache.json")
    evgeo, geo = leer("evgeo_cache.json"), leer("geo_cache.json")
    fecha = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    eventos, dominios = defaultdict(_ficha), defaultdict(_ficha)
    for ev, m in (comp.get("by_event") or {}).items():
        for campo in CAMPOS_NUM:
            if m.get(campo):
                eventos[ev][campo] = m[campo]
    for ev, dims in (tech.get("by_event") or {}).items():
        eventos[ev]["dispositivos"] = dict(dims.get("device") or {})
        eventos[ev]["navegadores"] = dict(dims.get("browser") or {})
        eventos[ev]["sistemas"] = dict(dims.get("os") or {})
    for ev, srcs in (utm.get("by_event") or {}).items():
        eventos[ev]["utm"] = {s: v.get("sesiones", 0) for s, v in srcs.items()}

    for dom, m in (comp.get("domains") or {}).items():
        for campo in CAMPOS_NUM:
            if m.get(campo):
                dominios[dom][campo] = m[campo]
    for dom, dims in (tech.get("by_domain") or {}).items():
        dominios[dom]["dispositivos"] = dict(dims.get("device") or {})
        dominios[dom]["navegadores"] = dict(dims.get("browser") or {})
        dominios[dom]["sistemas"] = dict(dims.get("os") or {})
    for dom, srcs in (utm.get("by_domain") or {}).items():
        dominios[dom]["utm"] = {s: v.get("sesiones", 0) for s, v in srcs.items()}

    # Las ciudades solo se copian si el caché del mapa es de hoy: si sigue el
    # de julio, copiarlo aquí inventaría un mapa que nadie midió hoy.
    if (evgeo.get("updated") or "")[:10] == fecha:
        for ev, ciudades in (evgeo.get("by_event") or {}).items():
            eventos[ev]["ciudades"] = {c: v.get("sesiones", 0) for c, v in ciudades.items()}
    if (geo.get("updated") or "")[:10] == fecha:
        for dom, ciudades in (geo.get("by_domain") or {}).items():
            dominios[dom]["ciudades"] = {c: v.get("sesiones", 0) for c, v in ciudades.items()}

    dia = registrar(fecha, ventana_dias=3, fuente="cachés-vigentes",
                    eventos=dict(eventos), dominios=dict(dominios))
    print(f"cachés de hoy volcados a {fecha}: {len(dia['eventos'])} eventos, "
          f"{len(dia['dominios'])} dominios")


def estado() -> None:
    hist = cargar()
    caps = hist.get("capturas", {})
    if not caps:
        print("Histórico vacío.")
        return
    print(f"{len(caps)} capturas · archivo: {HISTORIAL.name} "
          f"({HISTORIAL.stat().st_size / 1024:.1f} KB)\n")
    print(f"{'FECHA':<12} {'VENTANA':<8} {'EVENTOS':>8} {'DOMINIOS':>9}  FUENTES")
    for fecha in sorted(caps):
        d = caps[fecha]
        print(f"{fecha:<12} {str(d.get('ventana_dias')) + 'd':<8} "
              f"{len(d.get('eventos', {})):>8} {len(d.get('dominios', {})):>9}  "
              f"{', '.join(d.get('fuentes', []))}")
    con_ciudades = sum(
        1 for d in caps.values()
        if any(f.get("ciudades") for f in d.get("eventos", {}).values())
    )
    print(f"\ncapturas con mapa de ciudades: {con_ciudades} de {len(caps)}")


if __name__ == "__main__":
    if "--importar-julio" in sys.argv:
        importar_julio()
    elif "--importar-actuales" in sys.argv:
        importar_cachés_actuales()
    elif "--capturar" in sys.argv:
        capturar_dia()
    elif "--estado" in sys.argv:
        estado()
    else:
        print(__doc__)
