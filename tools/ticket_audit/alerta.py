"""Chequeo periódico + alerta por WhatsApp de casos nuevos.

Pensado para correr cada 15 minutos desde GitHub Actions. Barre una ventana
corta (por defecto 6 horas), compara contra lo ya avisado y solo notifica lo
que no se haya avisado antes.

    python3 tools/ticket_audit/alerta.py --horas 6
    python3 tools/ticket_audit/alerta.py --dry-run          # no envía ni guarda
    python3 tools/ticket_audit/alerta.py --refrescar-cache  # además, 180 días

Variables de entorno:
    MONGODB_URI            base de negocio (solo lectura)
    KV_REST_API_URL        estado compartido: qué ya se avisó + caché del tablero
    KV_REST_API_TOKEN
    WATI_API_ENDPOINT      envío de WhatsApp
    WATI_TOKEN
    AUDIT_ALERT_NUMBERS    destinatarios, separados por coma. Ej: 573001112233
    AUDIT_ALERT_TEMPLATE   plantilla aprobada en WATI (default: alerta_auditoria)
    AUDIT_ALERT_MAX_HORA   tope de mensajes por hora (default 6)
    AUDIT_ALERT_ENABLED    "0" apaga el envío sin tocar el código
    AUDIT_DASHBOARD_URL    link que va en el mensaje

Nunca escribe en la data de negocio.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from tools.ticket_audit.detector import (  # noqa: E402
    AMBIGUOUS, ORPHAN_TICKETS, OVER_ISSUED, OVER_ISSUED_CORREGIDO,
    UNDER_ISSUED, TicketAuditor,
)

#: Estados que disparan aviso. Los ambiguos no: son para revisión manual y
#: llenarían el WhatsApp de ruido.
ALERTAN = (OVER_ISSUED, UNDER_ISSUED)

KEY_AVISADAS = "ticket_audit_avisadas"
KEY_ENVIOS = "ticket_audit_envios"
KEY_CACHE = "ticket_audit_cache"
DASHBOARD = os.environ.get("AUDIT_DASHBOARD_URL", "https://blast-dashboard-kappa.vercel.app/auditoria")


# --- almacenamiento compartido (Vercel KV / Upstash Redis por REST) ---------

class Estado:
    """Guarda qué referencias ya se avisaron. Usa KV si está configurado; si
    no, un archivo local, para poder probar sin credenciales."""

    def __init__(self):
        self.base = (os.environ.get("KV_REST_API_URL") or "").rstrip("/")
        self.token = os.environ.get("KV_REST_API_TOKEN") or ""
        self.archivo = Path(__file__).resolve().parent / ".alerta_estado.json"

    @property
    def remoto(self) -> bool:
        return bool(self.base and self.token)

    def _req(self, path: str, body: bytes | None = None):
        req = urllib.request.Request(
            f"{self.base}/{path}", data=body,
            headers={"Authorization": f"Bearer {self.token}"},
            method="POST" if body is not None else "GET")
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read()).get("result")

    def get(self, key: str, default=None):
        if not self.remoto:
            data = json.loads(self.archivo.read_text()) if self.archivo.exists() else {}
            return data.get(key, default)
        try:
            raw = self._req(f"get/{urllib.parse.quote(key, safe='')}")
            return json.loads(raw) if raw else default
        except Exception as e:
            print(f"  [aviso] no se pudo leer {key} de KV: {e}")
            return default

    def set(self, key: str, value) -> None:
        if not self.remoto:
            data = json.loads(self.archivo.read_text()) if self.archivo.exists() else {}
            data[key] = value
            self.archivo.write_text(json.dumps(data, ensure_ascii=False, default=str))
            return
        # El valor va en el cuerpo: el caché pesa cientos de KB y no cabe en la URL.
        self._req(f"set/{urllib.parse.quote(key, safe='')}",
                  json.dumps(value, ensure_ascii=False, default=str).encode())


# --- WATI -------------------------------------------------------------------

def enviar_whatsapp(numero: str, params: list) -> tuple[bool, str]:
    endpoint = (os.environ.get("WATI_API_ENDPOINT") or "").rstrip("/")
    token = os.environ.get("WATI_TOKEN") or os.environ.get("WATI_API_TOKEN") or ""
    plantilla = os.environ.get("AUDIT_ALERT_TEMPLATE", "alerta_auditoria")
    if not endpoint or not token:
        return False, "faltan WATI_API_ENDPOINT o WATI_TOKEN"
    url = f"{endpoint}/api/v2/sendTemplateMessage?whatsappNumber={numero}"
    cuerpo = json.dumps({
        "template_name": plantilla,
        "broadcast_name": f"{plantilla}_{datetime.now():%Y%m%d%H%M}",
        "parameters": [{"name": str(i + 1), "value": v} for i, v in enumerate(params)],
    }).encode()
    req = urllib.request.Request(url, data=cuerpo, method="POST", headers={
        "Authorization": token if token.startswith("Bearer") else f"Bearer {token}",
        "Content-Type": "application/json"})
    for intento in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                res = json.loads(r.read())
            if res.get("result") is False:
                return False, res.get("info") or res.get("error") or "WATI rechazó el mensaje"
            return True, "enviado"
        except urllib.error.HTTPError as e:
            detalle = e.read().decode()[:200]
            if e.code < 500:
                return False, f"HTTP {e.code}: {detalle}"
            espera = 2 ** intento
            print(f"  reintento en {espera}s (HTTP {e.code})")
            time.sleep(espera)
        except Exception as e:
            espera = 2 ** intento
            print(f"  reintento en {espera}s ({e})")
            time.sleep(espera)
    return False, "WATI no respondió tras 3 intentos"


def destinatarios() -> list:
    crudo = os.environ.get("AUDIT_ALERT_NUMBERS", "")
    return [n.strip() for n in crudo.replace(";", ",").split(",") if n.strip()]


# --- mensajes ---------------------------------------------------------------

def texto_caso(r) -> list:
    """Parámetros de la plantilla para un caso puntual."""
    tipo = "sobre-emisión" if r.status == OVER_ISSUED else "sub-emisión"
    delta = f"+{r.delta}" if r.delta > 0 else str(r.delta)
    return [
        f"{r.merchantName or r.merchantRef}",
        f"{r.paymentReference}",
        f"{r.expectedTickets} compradas vs {r.actualTickets} emitidas ({delta})",
        f"${round(r.exposedAmount or r.amountPaid):,}".replace(",", "."),
        f"{tipo} · {(r.eventName or '')[:40]}",
        DASHBOARD,
    ]


def texto_resumen(casos: list) -> list:
    merchants = sorted({c.merchantName or c.merchantRef for c in casos})
    tickets = sum(abs(c.delta) for c in casos)
    return [
        ", ".join(merchants)[:60],
        f"{len(casos)} casos nuevos",
        f"{tickets} boletas afectadas",
        f"${round(sum(c.exposedAmount for c in casos)):,}".replace(",", "."),
        "varios casos en la última hora",
        DASHBOARD,
    ]


# --- ejecución --------------------------------------------------------------

def revisar(args) -> int:
    estado = Estado()
    auditor = TicketAuditor()
    desde = datetime.now(timezone.utc) - timedelta(hours=args.horas)

    print(f"Barriendo las últimas {args.horas} h "
          f"(estado {'en KV' if estado.remoto else 'en archivo local'})...", flush=True)
    hallazgos = [r for r in auditor.audit(since=desde) if r.status in ALERTAN]
    print(f"  casos en la ventana: {len(hallazgos)}")

    avisadas = estado.get(KEY_AVISADAS, {}) or {}
    nuevos = [r for r in hallazgos if r.paymentReference not in avisadas]
    repetidos = len(hallazgos) - len(nuevos)
    print(f"  nuevos: {len(nuevos)} | ya avisados antes: {repetidos}")

    for r in nuevos:
        auditor.enrich_payment(r)

    if args.refrescar_cache:
        refrescar_cache(auditor, estado, args)

    if not nuevos:
        print("Sin casos nuevos.")
        return 0

    if os.environ.get("AUDIT_ALERT_ENABLED", "1") == "0":
        print("Alertas desactivadas por AUDIT_ALERT_ENABLED=0. Solo se registra el estado.")
        marcar(estado, avisadas, nuevos, args)
        return 0

    numeros = destinatarios()
    if not numeros:
        print("No hay AUDIT_ALERT_NUMBERS configurado: no se envía nada.")
        print("La incidencia queda igual visible en el tablero.")
        marcar(estado, avisadas, nuevos, args)
        return 0

    # Tope por hora: un bug masivo no puede convertirse en 400 WhatsApps.
    tope = int(os.environ.get("AUDIT_ALERT_MAX_HORA", "6"))
    envios = [t for t in (estado.get(KEY_ENVIOS, []) or [])
              if datetime.fromisoformat(t) > datetime.now(timezone.utc) - timedelta(hours=1)]
    disponibles = max(tope - len(envios), 0)

    if disponibles == 0:
        print(f"Tope de {tope} mensajes por hora alcanzado. No se envía; "
              f"los casos quedan en el tablero.")
        marcar(estado, avisadas, nuevos, args)
        return 0

    if len(nuevos) > disponibles:
        print(f"{len(nuevos)} casos nuevos y solo {disponibles} envíos disponibles: "
              f"se manda un único resumen.")
        lotes = [(texto_resumen(nuevos), f"resumen de {len(nuevos)} casos")]
    else:
        lotes = [(texto_caso(r), r.paymentReference) for r in nuevos]

    if args.dry_run:
        print(f"[dry-run] se enviarían {len(lotes)} mensaje(s) a {len(numeros)} número(s):")
        for params, etiqueta in lotes[:3]:
            print(f"   {etiqueta}: {params}")
        if len(lotes) > 3:
            print(f"   ... y {len(lotes) - 3} más")
        return 0

    enviados = 0
    for params, etiqueta in lotes:
        for numero in numeros:
            ok, detalle = enviar_whatsapp(numero, params)
            print(f"  {etiqueta} -> {numero}: {detalle}")
            if ok:
                enviados += 1
        envios.append(datetime.now(timezone.utc).isoformat())

    estado.set(KEY_ENVIOS, envios)
    marcar(estado, avisadas, nuevos, args)
    print(f"Listo: {len(nuevos)} casos nuevos, {enviados} mensajes enviados.")
    return 0


def marcar(estado, avisadas: dict, nuevos: list, args) -> None:
    if args.dry_run:
        print("[dry-run] no se guarda el estado.")
        return
    ahora = datetime.now(timezone.utc).isoformat()
    for r in nuevos:
        avisadas[r.paymentReference] = ahora
    # No dejamos crecer el estado para siempre: 30 días alcanzan de sobra para
    # no repetir un aviso.
    corte = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    avisadas = {k: v for k, v in avisadas.items() if v > corte}
    estado.set(KEY_AVISADAS, avisadas)


def refrescar_cache(auditor, estado, args) -> None:
    """Deja el tablero al día sin necesidad de desplegar."""
    from tools.ticket_audit.backfill import FINDING_STATUSES
    print(f"Refrescando el caché del tablero ({args.dias_cache} días)...", flush=True)
    desde = datetime.now(timezone.utc) - timedelta(days=args.dias_cache)
    hasta = datetime.now(timezone.utc)
    resultados = [r for r in auditor.audit(since=desde) if r.status in FINDING_STATUSES]
    for r in resultados:
        auditor.enrich_payment(r)
    over = [r for r in resultados if r.status == OVER_ISSUED]
    por_dia = {}
    for r in over:
        if r.createdAtLast:
            d = r.createdAtLast.strftime("%Y-%m-%d")
            por_dia[d] = por_dia.get(d, 0) + r.delta
    payload = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "window": {"from": desde.isoformat(), "until": hasta.isoformat()},
        "totals": {
            "incidencias": len(resultados),
            "sobreEmision": len(over),
            "ticketsEnExceso": sum(r.delta for r in over),
            "valorFacialExpuesto": round(sum(r.exposedAmount for r in over)),
            "merchantsAfectados": len({r.merchantRef for r in over}),
            "subEmision": sum(1 for r in resultados if r.status == UNDER_ISSUED),
            "huerfanos": sum(1 for r in resultados if r.status == ORPHAN_TICKETS),
            "ambiguos": sum(1 for r in resultados if r.status == AMBIGUOUS),
            "corregidos": sum(1 for r in resultados if r.status == OVER_ISSUED_CORREGIDO),
            "ticketsCorregidos": sum(r.correctedExcess for r in resultados
                                     if r.status == OVER_ISSUED_CORREGIDO),
        },
        "porDia": [{"dia": d, "tickets": n} for d, n in sorted(por_dia.items())],
        "incidencias": [r.as_dict() for r in resultados],
    }
    if args.dry_run:
        print(f"  [dry-run] caché con {len(resultados)} incidencias, no se guarda.")
        return
    estado.set(KEY_CACHE, payload)
    print(f"  caché actualizado: {len(resultados)} incidencias.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--horas", type=float, default=6,
                   help="ventana a revisar para alertar (default 6)")
    p.add_argument("--refrescar-cache", action="store_true",
                   help="además, recalcula el caché que consume el tablero")
    p.add_argument("--dias-cache", type=int, default=180)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    sys.exit(revisar(args))


if __name__ == "__main__":
    main()
