"""Barrido historico de integridad Carts <-> Tickets.

Procesa el rango en lotes por ventana temporal (no carga todo en RAM) y con
pausas configurables para no degradar produccion. SOLO LECTURA.

Ejemplos:
    # ultimos 90 dias, refresca el cache del dashboard
    python3 tools/ticket_audit/backfill.py --cache

    # un merchant, exportando CSV, sin persistir nada
    python3 tools/ticket_audit/backfill.py --merchant BL001 --export csv --dry-run

    # rango explicito
    python3 tools/ticket_audit/backfill.py --from 2026-05-01 --to 2026-08-07
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from tools.ticket_audit.detector import (  # noqa: E402
    ALL_CANCELLED, AMBIGUOUS, BACKOFFICE_ISSUED, OK, ORPHAN_TICKETS,
    OVER_ISSUED, OVER_ISSUED_CORREGIDO, PARTIALLY_CANCELLED, UNDER_ISSUED,
    TicketAuditor,
)

DASHBOARD_CACHE = Path.home() / "blast-dashboard" / "audit_cache.json"
FINDING_STATUSES = (OVER_ISSUED, OVER_ISSUED_CORREGIDO, UNDER_ISSUED,
                    ORPHAN_TICKETS, AMBIGUOUS)


def parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def windows(since: datetime, until: datetime, days: int):
    cur = since
    while cur < until:
        nxt = min(cur + timedelta(days=days), until)
        yield cur, nxt
        cur = nxt


def run(args) -> list:
    since = parse_date(args.since) if args.since else \
        datetime.now(timezone.utc) - timedelta(days=args.days)
    until = parse_date(args.until) if args.until else datetime.now(timezone.utc)

    auditor = TicketAuditor(high_amount=args.high_amount)
    results, counts = [], Counter()
    chunks = list(windows(since, until, args.batch_days))
    t0 = time.time()

    for i, (a, b) in enumerate(chunks, start=1):
        found = 0
        for res in auditor.audit(since=a, until=b, merchant_ref=args.merchant,
                                 cart_status=args.cart_status, include_ok=True):
            counts[res.status] += 1
            if res.status in FINDING_STATUSES:
                results.append(res)
                found += 1
        pct = i / len(chunks) * 100
        print(f"  [{pct:5.1f}%] {a:%Y-%m-%d} -> {b:%Y-%m-%d}  hallazgos: {found}",
              flush=True)
        if args.sleep and i < len(chunks):
            time.sleep(args.sleep)

    print(f"\nEnriqueciendo {len(results)} hallazgos...", flush=True)
    for res in results:
        auditor.enrich_payment(res)
    results.sort(key=lambda r: (-abs(r.delta), r.merchantRef or ""))

    summary(results, counts, since, until, time.time() - t0)
    return results


def summary(results, counts, since, until, elapsed):
    over = [r for r in results if r.status == OVER_ISSUED]
    under = [r for r in results if r.status == UNDER_ISSUED]
    total = sum(counts.values())

    print("\n" + "=" * 68)
    print(f"RESUMEN  {since:%Y-%m-%d} -> {until:%Y-%m-%d}   ({elapsed:.0f}s)")
    print("=" * 68)
    print(f"Referencias de pago revisadas: {total}")
    for st in (OK, BACKOFFICE_ISSUED, ALL_CANCELLED, PARTIALLY_CANCELLED,
               OVER_ISSUED, OVER_ISSUED_CORREGIDO, UNDER_ISSUED, ORPHAN_TICKETS,
               AMBIGUOUS):
        if counts.get(st):
            print(f"  {st:24s} {counts[st]:6d}  ({counts[st]/total*100:5.1f}%)")

    corregidos = [r for r in results if r.status == OVER_ISSUED_CORREGIDO]
    if corregidos:
        print(f"\nSobre-emision ya corregida a mano: {len(corregidos)} casos, "
              f"{sum(r.correctedExcess for r in corregidos)} tickets emitidos de mas y anulados")
        print("  (hoy cuadran, pero prueban que el bug disparo)")

    if over:
        print(f"\nSobre-emision: {len(over)} casos, {sum(r.delta for r in over)} tickets de mas")
        print(f"Valor facial expuesto: ${sum(r.exposedAmount for r in over):,.0f}")
        print("\nTop merchants afectados:")
        agg = defaultdict(lambda: [0, 0, 0.0])
        for r in over:
            a = agg[f"{r.merchantRef} - {r.merchantName}"]
            a[0] += 1; a[1] += r.delta; a[2] += r.exposedAmount
        for k, v in sorted(agg.items(), key=lambda x: -x[1][1])[:8]:
            print(f"  {k:38s} {v[0]:4d} casos  {v[1]:5d} tickets  ${v[2]:>15,.0f}")

        byday = Counter(r.createdAtLast.strftime("%Y-%m-%d")
                        for r in over if r.createdAtLast)
        if byday:
            peak, n = byday.most_common(1)[0]
            print(f"\nVentana de mayor incidencia: {peak} ({n} casos) "
                  f"<- correlacionar con deploys de esa fecha")
            byhour = Counter(r.createdAtLast.strftime("%Y-%m-%d %H:00")
                             for r in over if r.createdAtLast
                             and r.createdAtLast.strftime("%Y-%m-%d") == peak)
            for h, c in sorted(byhour.items()):
                print(f"    {h} UTC  {'#' * min(c, 60)} {c}")

    if under:
        print(f"\nSub-emision: {len(under)} casos, "
              f"{sum(-r.delta for r in under)} tickets que el cliente pago y no recibio")


def export(results, fmt: str, path: Path):
    rows = [r.as_dict() for r in results]
    if fmt == "json":
        path.write_text(json.dumps(rows, default=str, ensure_ascii=False, indent=1))
    else:
        cols = ["paymentReference", "status", "severity", "merchantRef", "merchantName",
                "eventName", "cartId", "cartStatus", "expectedTickets", "actualTickets",
                "delta", "totalTickets", "cancelledTickets", "amountPaid", "exposedAmount",
                "paymentMethod", "approvedPaymentIntents", "createdAtFirst",
                "createdAtLast", "createdAtSpread", "reason"]
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k) for k in cols})
    print(f"Export -> {path}")


def write_cache(results, since, until, path: Path):
    """Cache que consume el dashboard. Es la unica salida persistente y vive
    fuera de la base de negocio."""
    over = [r for r in results if r.status == OVER_ISSUED]
    byday = Counter()
    for r in over:
        if r.createdAtLast:
            byday[r.createdAtLast.strftime("%Y-%m-%d")] += r.delta
    payload = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "window": {"from": since.isoformat(), "until": until.isoformat()},
        "totals": {
            "incidencias": len(results),
            "sobreEmision": len(over),
            "ticketsEnExceso": sum(r.delta for r in over),
            "valorFacialExpuesto": round(sum(r.exposedAmount for r in over)),
            "merchantsAfectados": len({r.merchantRef for r in over}),
            "subEmision": sum(1 for r in results if r.status == UNDER_ISSUED),
            "huerfanos": sum(1 for r in results if r.status == ORPHAN_TICKETS),
            "ambiguos": sum(1 for r in results if r.status == AMBIGUOUS),
            "corregidos": sum(1 for r in results if r.status == OVER_ISSUED_CORREGIDO),
            "ticketsCorregidos": sum(r.correctedExcess for r in results
                                     if r.status == OVER_ISSUED_CORREGIDO),
        },
        "porDia": [{"dia": d, "tickets": n} for d, n in sorted(byday.items())],
        "incidencias": [r.as_dict() for r in results],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, default=str, ensure_ascii=False))
    print(f"Cache del dashboard -> {path}")


def main():
    p = argparse.ArgumentParser(description="Barrido de integridad Carts<->Tickets (solo lectura)")
    p.add_argument("--from", dest="since", help="fecha inicio YYYY-MM-DD")
    p.add_argument("--to", dest="until", help="fecha fin YYYY-MM-DD")
    p.add_argument("--days", type=int, default=90, help="dias hacia atras si no hay --from")
    p.add_argument("--merchant", help="merchantRef; por defecto todos")
    p.add_argument("--cart-status", help="filtrar por estado del carrito, ej APPROVED")
    p.add_argument("--batch-days", type=int, default=15, help="tamano del lote en dias")
    p.add_argument("--sleep", type=float, default=0.0, help="pausa entre lotes (s)")
    p.add_argument("--high-amount", type=float, default=1_000_000.0,
                   help="umbral de monto para severidad critical")
    p.add_argument("--export", choices=["csv", "json"])
    p.add_argument("--out", help="ruta del export")
    p.add_argument("--cache", action="store_true", help="refresca el cache del dashboard")
    p.add_argument("--dry-run", action="store_true", help="no escribe cache ni export")
    args = p.parse_args()

    results = run(args)

    if args.dry_run:
        print("\n[dry-run] no se escribio nada.")
        return
    if args.export:
        out = Path(args.out) if args.out else Path.home() / "Downloads" / \
            f"auditoria_tickets_{datetime.now():%Y%m%d}.{args.export}"
        export(results, args.export, out)
    if args.cache:
        since = parse_date(args.since) if args.since else \
            datetime.now(timezone.utc) - timedelta(days=args.days)
        until = parse_date(args.until) if args.until else datetime.now(timezone.utc)
        write_cache(results, since, until, DASHBOARD_CACHE)


if __name__ == "__main__":
    main()
