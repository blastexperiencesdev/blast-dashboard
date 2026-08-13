"""Resiembra los cachés de Clarity, incluyendo el desglose por evento.

La clave está en la dimensión URL: cada página de evento vive en
``/eventos/<eventId>``, así que pidiendo ``dimension1=URL`` cruzado con otra
dimensión se obtiene el mismo dato que ya se tenía por dominio, pero separado
por evento. Eso es lo que llena las claves ``by_event`` que lee server.py.

Presupuesto de la API (documentado por Microsoft):
- 10 peticiones por proyecto por día.
- Máximo 3 dimensiones por petición.
- Respuesta tope 1.000 filas, sin paginación.
- Solo 1, 2 o 3 días hacia atrás: esto es una foto móvil, no un histórico.

Este script gasta 4 de las 10 peticiones diarias:
  1. URL                    -> tráfico + dead/rage clicks + errores por evento
  2. URL x Country/Region   -> (ver aviso sobre el mapa)
  3. URL x Device x Browser -> dispositivos y navegadores por evento
  4. URL x Source           -> UTMs por evento

AVISO SOBRE EL MAPA (comprobado 2026-08-13): esta API NO devuelve ciudades. Su
dimensión geográfica más fina es Country/Region, y al combinarla con URL la
ignora en silencio: responde 200, con las filas de siempre, sin columna de
país, y gastando el cupo igual. El desglose por ciudad (Bogotá, Tunja, Ibagué…)
solo se consigue con el MCP de Clarity, que sí tiene dimensión `City` — de ahí
el "source": "claude-mcp-seed" de los cachés del mapa. La contra del MCP es que
trunca a 10 filas por consulta, así que sembrar el mapa exige varias consultas
acotadas por dominio o por evento.

Uso:
    python3 tools/clarity_refresh.py           # escribe los cachés
    python3 tools/clarity_refresh.py --dry-run # muestra qué traería, sin escribir
"""
import json
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

API = "https://www.clarity.ms/export-data/api/v1/project-live-insights"
DIAS = 3

# Las URLs de evento son /eventos/<ObjectId de 24 hex>. El id que aparece ahí es
# el mismo _id de la colección events, que es como se filtra en Mongo.
RE_EVENTO = re.compile(r"/eventos/([0-9a-f]{24})", re.I)

METRICAS = {
    "Traffic": "sesiones",
    "DeadClickCount": "dead_clicks",
    "RageClickCount": "rage_clicks",
    "ScriptErrorCount": "errores_js",
}


def cargar_token() -> str:
    for env in (BASE / ".env", BASE.parent / ".env"):
        if not env.exists():
            continue
        for line in env.read_text().splitlines():
            if line.startswith("CLARITY_API_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise SystemExit(
        "Falta CLARITY_API_TOKEN en .env.\n"
        "Genéralo en Clarity: Settings -> Data Export -> Generate new API token."
    )


def pedir_dias(token: str, dias: int, *dimensiones) -> list:
    # safe="" es obligatorio: quote() deja pasar "/" por defecto, y entonces
    # "Country/Region" viaja crudo en el querystring. Clarity no lo reconoce,
    # ignora la dimensión en silencio y responde solo con la URL — sin error,
    # con 200, y con el cupo del día igualmente gastado.
    params = f"?numOfDays={dias}" + "".join(
        f"&dimension{i + 1}={urllib.parse.quote(d, safe='')}" for i, d in enumerate(dimensiones)
    )
    req = urllib.request.Request(API + params, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)


def pedir(token: str, *dimensiones) -> list:
    """Ventana por defecto (3 días), la que usa el refresco de los cachés."""
    return pedir_dias(token, DIAS, *dimensiones)


def host_de(url: str) -> str:
    return url.split("//")[-1].split("/")[0].replace("www.", "")


def evento_de(url: str) -> str:
    m = RE_EVENTO.search(url or "")
    return m.group(1).lower() if m else ""


def fila_url(fila: dict) -> str:
    return fila.get("URL") or fila.get("Url") or fila.get("VisitedUrl") or ""


def sesiones_de(fila: dict) -> int:
    return int(fila.get("totalSessionCount") or fila.get("subTotal") or 0)


#: Campos que son métricas o la propia URL: todo lo demás en una fila es el
#: valor de una dimensión. Clarity no documenta con qué nombre exacto devuelve
#: cada dimensión (pide "URL" y responde "VisitedUrl", pide "Country/Region" y
#: puede responder "Country"), así que se detecta por descarte en vez de
#: confiar en el nombre — si no, la fila se descarta en silencio.
NO_DIMENSION = {
    "totalSessionCount", "totalBotSessionCount", "distinctUserCount",
    "distantUserCount", "PagesPerSessionPercentage", "subTotal",
    "URL", "Url", "VisitedUrl",
}


def valor_dimension(fila: dict, preferido: str) -> str:
    """Valor de la dimensión pedida, tolerando que Clarity la renombre."""
    if fila.get(preferido):
        return str(fila[preferido])
    # "Country/Region" -> acepta "Country", "Region", "CountryRegion"…
    partes = [p for p in re.split(r"[/\s]+", preferido) if p]
    for clave, valor in fila.items():
        if clave in NO_DIMENSION or not valor:
            continue
        limpia = clave.replace("/", "").replace(" ", "").lower()
        if any(p.lower() in limpia or limpia in p.lower() for p in partes):
            return str(valor)
    return ""


def acumular(payload: list, dim: str, destino_evento: dict, destino_dominio: dict):
    """Reparte las filas de una respuesta en los dos niveles de agregación."""
    for metrica in payload:
        if metrica.get("metricName") != "Traffic":
            continue
        for fila in metrica.get("information", []):
            url = fila_url(fila)
            valor = valor_dimension(fila, dim)
            if not url or not valor:
                continue
            n = sesiones_de(fila)
            ev = evento_de(url)
            if ev:
                destino_evento[ev][valor] += n
            destino_dominio[host_de(url)][valor] += n


def solo_mapa(token: str, ahora: str):
    """Re-corre únicamente la etapa del mapa (1 petición) y reescribe solo sus
    dos cachés. Existe porque con un cupo de 10 peticiones al día no se puede
    repetir el barrido completo cada vez que falla una sola dimensión."""
    geo = pedir(token, "URL", "Country/Region")
    ev_geo = defaultdict(lambda: defaultdict(int))
    dom_geo = defaultdict(lambda: defaultdict(int))
    acumular(geo, "Country/Region", ev_geo, dom_geo)

    if not ev_geo and not dom_geo:
        muestra = next((f for m in geo if m.get("metricName") == "Traffic"
                        for f in m.get("information", [])), {})
        print("⚠️  La respuesta no trajo ciudades. Claves de una fila real:")
        print("   ", list(muestra.keys()))
        print("   No se escribió nada: se conserva el caché anterior del mapa.")
        return False

    def sesiones_dict(d):
        return {k: {"sesiones": v} for k, v in d.items()}

    (BASE / "evgeo_cache.json").write_text(json.dumps({
        "updated": ahora, "days": DIAS, "source": "clarity-export-api",
        "by_event": {e: sesiones_dict(c) for e, c in ev_geo.items()},
    }, ensure_ascii=False, indent=1))
    (BASE / "geo_cache.json").write_text(json.dumps({
        "updated": ahora, "days": DIAS, "source": "clarity-export-api",
        "by_domain": {d: sesiones_dict(c) for d, c in dom_geo.items()},
    }, ensure_ascii=False, indent=1))
    print(f"mapa actualizado · eventos con ciudades: {len(ev_geo)} · dominios: {len(dom_geo)}")
    return True


def main():
    dry = "--dry-run" in sys.argv
    token = cargar_token()
    ahora = datetime.now(timezone.utc).isoformat()

    if "--solo-mapa" in sys.argv:
        solo_mapa(token, ahora)
        return

    # --- 1. URL sola: comportamiento por evento y por dominio -----------------
    base = pedir(token, "URL")
    ev_comp = defaultdict(lambda: dict.fromkeys(
        ["sesiones", "usuarios", "dead_clicks", "rage_clicks", "errores_js"], 0))
    dom_comp = defaultdict(lambda: dict.fromkeys(
        ["sesiones", "usuarios", "dead_clicks", "rage_clicks", "errores_js"], 0))
    for metrica in base:
        campo = METRICAS.get(metrica.get("metricName"))
        if not campo:
            continue
        for fila in metrica.get("information", []):
            url = fila_url(fila)
            if not url:
                continue
            n = sesiones_de(fila)
            for destino, clave in ((ev_comp, evento_de(url)), (dom_comp, host_de(url))):
                if not clave:
                    continue
                destino[clave][campo] += n
                if metrica.get("metricName") == "Traffic":
                    destino[clave]["usuarios"] += int(fila.get("distinctUserCount") or 0)

    # Un --dry-run que gastara las 4 peticiones sería absurdo con un cupo de 10
    # al día: con la primera ya se sabe si el token sirve y si las URLs de
    # evento se están reconociendo.
    if dry:
        print(f"token OK · dominios detectados: {len(dom_comp)} · eventos detectados: {len(ev_comp)}")
        for ev, v in sorted(ev_comp.items(), key=lambda x: -x[1]["sesiones"])[:5]:
            print(f"   {ev}  {v['sesiones']} sesiones")
        print("\n--dry-run: no se escribió nada (gastó 1 de las 10 peticiones diarias)")
        return

    # --- 2. URL x ciudad: el mapa de calor ------------------------------------
    geo = pedir(token, "URL", "Country/Region")
    ev_geo = defaultdict(lambda: defaultdict(int))
    dom_geo = defaultdict(lambda: defaultdict(int))
    acumular(geo, "Country/Region", ev_geo, dom_geo)

    # --- 3. URL x dispositivo x navegador -------------------------------------
    tech = pedir(token, "URL", "Device", "Browser")
    ev_dev, dom_dev = defaultdict(lambda: defaultdict(int)), defaultdict(lambda: defaultdict(int))
    ev_bro, dom_bro = defaultdict(lambda: defaultdict(int)), defaultdict(lambda: defaultdict(int))
    acumular(tech, "Device", ev_dev, dom_dev)
    acumular(tech, "Browser", ev_bro, dom_bro)

    # --- 4. URL x source: UTMs -------------------------------------------------
    utm = pedir(token, "URL", "Source")
    ev_src, dom_src = defaultdict(lambda: defaultdict(int)), defaultdict(lambda: defaultdict(int))
    acumular(utm, "Source", ev_src, dom_src)

    def sesiones_dict(d):
        return {k: {"sesiones": v} for k, v in d.items()}

    globales_src = defaultdict(int)
    for por_source in dom_src.values():
        for source, n in por_source.items():
            globales_src[source] += n

    salidas = {
        "clarity_cache.json": {
            "updated": ahora, "days": DIAS, "source": "clarity-export-api",
            "domains": dict(dom_comp),
            "by_event": dict(ev_comp),
        },
        "evgeo_cache.json": {
            "updated": ahora, "days": DIAS, "source": "clarity-export-api",
            "by_event": {e: sesiones_dict(c) for e, c in ev_geo.items()},
        },
        "geo_cache.json": {
            "updated": ahora, "days": DIAS, "source": "clarity-export-api",
            "by_domain": {d: sesiones_dict(c) for d, c in dom_geo.items()},
        },
        "tech_cache.json": {
            "updated": ahora, "days": DIAS, "source": "clarity-export-api",
            "by_domain": {
                d: {"device": dict(dom_dev[d]), "browser": dict(dom_bro[d]), "os": {}}
                for d in set(dom_dev) | set(dom_bro)
            },
            "by_event": {
                e: {"device": dict(ev_dev[e]), "browser": dict(ev_bro[e]), "os": {}}
                for e in set(ev_dev) | set(ev_bro)
            },
        },
        "utm_cache.json": {
            "updated": ahora, "days": DIAS, "source": "clarity-export-api",
            # 'sources' es el consolidado de todos los dominios: lo que ve el
            # selector en "Todos los merchants".
            "sources": sesiones_dict(globales_src),
            "by_domain": {d: sesiones_dict(s) for d, s in dom_src.items()},
            "by_event": {e: sesiones_dict(s) for e, s in ev_src.items()},
        },
    }

    print(f"eventos con datos: comportamiento={len(ev_comp)} geo={len(ev_geo)} "
          f"tech={len(set(ev_dev) | set(ev_bro))} utm={len(ev_src)}")

    # La Data Export API no expone ciudades (su dimensión más fina es
    # Country/Region); el desglose por ciudad solo llega por el MCP de Clarity.
    # Sin esta guarda, cada corrida sobreescribía el mapa con un objeto vacío y
    # el panel más importante del tablero quedaba en blanco.
    if not ev_geo and not dom_geo:
        for descartado in ("evgeo_cache.json", "geo_cache.json"):
            salidas.pop(descartado)
        print("↳ mapa: la API no devolvió ciudades, se conserva el caché anterior "
              "(el desglose por ciudad se siembra con el MCP, no con esta API)")

    for nombre, data in salidas.items():
        (BASE / nombre).write_text(json.dumps(data, ensure_ascii=False, indent=1))
        print(f"escrito {nombre}")


if __name__ == "__main__":
    main()
