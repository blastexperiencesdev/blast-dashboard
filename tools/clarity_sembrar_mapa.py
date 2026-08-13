"""Siembra el mapa de ciudades a partir de consultas al MCP de Clarity.

Por qué existe: la Data Export API (la del token) NO expone ciudades. Su
dimensión geográfica más fina es Country/Region, y combinada con URL la ignora
en silencio. El desglose por ciudad solo lo tiene el MCP de Clarity, que se
consulta de forma asistida — de ahí que este paso no pueda automatizarse en
GitHub Actions como el resto.

El MCP trunca a 10 filas por consulta, así que se pregunta dominio por dominio
y se pegan los resultados aquí. Las filas crudas quedan en
`mcp_mapa_crudo.json` para poder reprocesarlas sin volver a consultar.

Uso:
    python3 tools/clarity_sembrar_mapa.py            # procesa el crudo guardado
    python3 tools/clarity_sembrar_mapa.py --estado   # qué hay en el crudo
"""
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CRUDO = BASE / "mcp_mapa_crudo.json"
sys.path.insert(0, str(BASE / "tools"))

RE_EVENTO = re.compile(r"/eventos/([0-9a-f]{24})", re.I)

#: Clarity reporta algunos nombres sin tilde o con variantes; el mapa del
#: tablero busca la forma canónica en su tabla de coordenadas.
NORMALIZA = {
    "Ibague": "Ibagué", "Bogota": "Bogotá", "Medellin": "Medellín",
    "Cucuta": "Cúcuta", "Monteria": "Montería", "Itagui": "Itagüí",
    "Popayan": "Popayán", "Fusagasuga": "Fusagasugá", "Chia": "Chía",
    "Facatativa": "Facatativá", "Zipaquira": "Zipaquirá", "Cajica": "Cajicá",
    "": "(sin ciudad)",
}


def ciudad_canonica(nombre: str) -> str:
    nombre = (nombre or "").strip()
    return NORMALIZA.get(nombre, nombre or "(sin ciudad)")


def host_de(url: str) -> str:
    return url.split("//")[-1].split("/")[0].replace("www.", "")


def procesar(filas: list) -> tuple:
    """Agrupa las filas crudas del MCP por evento y por dominio."""
    por_evento = defaultdict(lambda: defaultdict(int))
    por_dominio = defaultdict(lambda: defaultdict(int))
    for f in filas:
        url = f.get("VisitedUrl") or ""
        ciudad = ciudad_canonica(f.get("City"))
        n = int(f.get("SessionCount") or 0)
        if not url or not n:
            continue
        por_dominio[host_de(url)][ciudad] += n
        m = RE_EVENTO.search(url)
        if m:
            por_evento[m.group(1).lower()][ciudad] += n
    return por_evento, por_dominio


def main():
    if not CRUDO.exists():
        raise SystemExit(f"No existe {CRUDO}. Pega ahí las filas del MCP.")
    crudo = json.loads(CRUDO.read_text())
    filas = crudo["filas"]

    if "--estado" in sys.argv:
        print(f"{len(filas)} filas crudas · consultado {crudo.get('consultado', '?')}")
        print(f"dominios: {sorted({host_de(f['VisitedUrl']) for f in filas})}")
        return

    por_evento, por_dominio = procesar(filas)
    ahora = crudo.get("consultado") or datetime.now(timezone.utc).isoformat()

    def sesiones_dict(d):
        return {c: {"sesiones": n} for c, n in sorted(d.items(), key=lambda x: -x[1])}

    (BASE / "evgeo_cache.json").write_text(json.dumps({
        "updated": ahora, "days": 3, "source": "claude-mcp-seed",
        "compras_cobertura": "sesiones por ciudad; las compras por ciudad no vienen en esta siembra",
        "by_event": {e: sesiones_dict(c) for e, c in por_evento.items()},
    }, ensure_ascii=False, indent=1))

    (BASE / "geo_cache.json").write_text(json.dumps({
        "updated": ahora, "days": 3, "source": "claude-mcp-seed",
        "granularidad": "tal como la reporta Clarity: ciudades y municipios sin agrupar",
        "by_domain": {d: sesiones_dict(c) for d, c in por_dominio.items()},
    }, ensure_ascii=False, indent=1))

    print(f"mapa sembrado · {len(por_evento)} eventos · {len(por_dominio)} dominios")
    for dom, ciudades in sorted(por_dominio.items(), key=lambda x: -sum(x[1].values())):
        print(f"  {dom:35} {sum(ciudades.values()):>5} sesiones en {len(ciudades)} ciudades")

    # Y al histórico propio, para que esta foto sobreviva a la ventana de 3 días.
    import clarity_historial as ch
    eventos = {e: {"ciudades": dict(c)} for e, c in por_evento.items()}
    dominios = {d: {"ciudades": dict(c)} for d, c in por_dominio.items()}
    dia = ch.registrar(ahora[:10], ventana_dias=3, fuente="mcp-mapa",
                       eventos=eventos, dominios=dominios)
    print(f"\nhistórico {ahora[:10]}: {len(dia['eventos'])} eventos, {len(dia['dominios'])} dominios")


if __name__ == "__main__":
    main()
