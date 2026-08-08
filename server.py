"""Dashboard en tiempo real por merchant para Blast Tickets.

Fuentes:
- MongoDB blast-prod (usuario solo lectura, MONGODB_URI en .env): ventas y funnel.
- Microsoft Clarity (comportamiento): se sirve desde clarity_cache.json. Si hay
  CLARITY_API_TOKEN en .env se refresca solo desde la Data Export API de Clarity
  (límite oficial: 10 requests/día, máximo 3 días hacia atrás), por eso el TTL
  del caché es de 3 horas.

Notas del esquema aprendidas:
- carts/paymentIntents solo tienen índice _id; los filtros por periodo usan el
  timestamp embebido en el ObjectId, que sí aprovecha ese índice.
- dateCreation/date están en hora local de Colombia (naive); _id es UTC real.
- paymentIntents no tiene campo de fecha.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from bson import ObjectId
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pymongo import MongoClient

BASE = Path(__file__).resolve().parent
CLARITY_CACHE = BASE / "clarity_cache.json"
CLARITY_TTL_SECONDS = 3 * 3600

# Merchants que ya no son clientes (churn): fuera del selector y de los totales.
CHURNED = ["VPP028"]

# Dominio Clarity por merchant (derivado de mainWebsite en la colección merchants).
MERCHANT_DOMAINS = {
    "BL001": "blasttickets.com",
    "DIAZ301": "tickets.3diazproducciones.com",
    "SEPD07": "sinerror.com",
    "SEVT01": "supereventosticket.com",
    "MTI010": "lamtickets.com",
    "TQY013": "taquiya.co",
    "ES029": "elsellotickets.com",
    "TP023": "ventas.ticketplatino.com",
    "RB031": "reboletos.com",
    "AUG021": "augetickets.com",
    "FTK25": "fanatick.co",
    "AST020": "astickets.online",
    "GZP026": "tickets.gerizimproducciones.com",
    "TQ030": "taquillaone.com",
}


def load_env():
    out = {}
    # Primero lee desde variables de entorno del sistema (para Vercel)
    for key in (
        "MONGODB_URI", "CLARITY_API_TOKEN", "WATI_API_TOKEN",
        "KV_REST_API_URL", "KV_REST_API_TOKEN",
    ):
        if key in os.environ:
            out[key] = os.environ[key]
    # Luego intenta leer archivos .env locales (para desarrollo)
    for env in (BASE / ".env", BASE.parent / ".env"):
        if env.exists():
            for line in env.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    out.setdefault(k.strip(), v.strip())
    return out


ENV = load_env()
if "MONGODB_URI" not in ENV:
    raise RuntimeError("MONGODB_URI no encontrado en variables de entorno o .env")

# Lazy connection: no conecta hasta que se usa (evita timeouts al cargar el módulo)
client = MongoClient(ENV["MONGODB_URI"], serverSelectionTimeoutMS=15000, connect=False)
db = client["blast-prod"]
app = FastAPI(title="Blast Tickets Dashboard")

FAILED_PAYMENT_STATUSES = ["DECLINED", "REJECTED", "ERROR"]
APPROVED_PAYMENT_STATUSES = ["APPROVED", "VALIDATED"]
REACHED_PAYMENT_CART = [
    "APPROVED", "PAYMENT_FAILED", "DECLINED", "ERROR",
    "WAITING-PAYMENT-RESPONSE", "PENDING", "REJECTED", "BACKEND_ERROR",
]

_merchants_cache = {"ts": 0.0, "data": []}


def oid_since(hours: float) -> ObjectId:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    return ObjectId.from_datetime(since)


def merchant_filter(field: str, merchant: str) -> dict:
    if merchant == "ALL":
        return {field: {"$nin": CHURNED}}
    return {field: merchant}


def norm_method(m):
    if not m:
        return "OTRO"
    m = str(m).strip().upper()
    aliases = {
        "BANCOLOMBIA": "BANCOLOMBIA_TRANSFER",
        "CARD_WEB": "CARD",
        "CORRESPONSAL BANCARIO": "CORRESPONSAL",
    }
    return aliases.get(m, m)


def merchant_names() -> dict:
    return {m["ref"]: m["name"] for m in merchants()}


@app.get("/api/merchants")
def merchants():
    if time.time() - _merchants_cache["ts"] > 300:
        docs = db.merchants.find(
            {"merchantRef": {"$nin": CHURNED + [None]}},
            {"merchantRef": 1, "name": 1, "active": 1, "currency": 1,
             "paymentGateway": 1, "primaryColor": 1},
        )
        data = [
            {
                "ref": d["merchantRef"],
                "name": (d.get("name") or d["merchantRef"]).strip(),
                "active": bool(d.get("active")),
                "currency": d.get("currency") or "COP",
                "gateway": d.get("paymentGateway") or "",
                "color": d.get("primaryColor") or "#378ADD",
            }
            for d in docs
        ]
        data.sort(key=lambda x: (not x["active"], x["name"].lower()))
        _merchants_cache.update(ts=time.time(), data=data)
    return _merchants_cache["data"]


@app.get("/api/dashboard")
def dashboard(merchant: str = "ALL", hours: int = 168):
    if hours not in (24, 168, 720):
        raise HTTPException(400, "hours debe ser 24, 168 o 720")
    since = oid_since(hours)
    cart_match = {"_id": {"$gte": since}, **merchant_filter("merchantRef", merchant)}
    pi_match = {"_id": {"$gte": since}, **merchant_filter("merchantReference", merchant)}
    tk_match = {"_id": {"$gte": since}, **merchant_filter("merchantReference", merchant)}

    by_status = {
        r["_id"]: r
        for r in db.carts.aggregate([
            {"$match": cart_match},
            {"$group": {
                "_id": "$status", "n": {"$sum": 1},
                "total": {"$sum": {"$ifNull": ["$total", 0]}},
                "qty": {"$sum": {"$ifNull": ["$quantity", 0]}},
            }},
        ])
    }
    carts_total = sum(r["n"] for r in by_status.values())
    approved = by_status.get("APPROVED", {"n": 0, "total": 0, "qty": 0})
    reached = sum(by_status.get(s, {"n": 0})["n"] for s in REACHED_PAYMENT_CART)
    abandoned = {
        "expirado": by_status.get("CREATED-TIME-OUT", {"n": 0})["n"],
        "borrado_por_usuario": by_status.get("DELETED_BY_USER", {"n": 0})["n"],
        "pago_fallido": sum(
            by_status.get(s, {"n": 0})["n"]
            for s in ("PAYMENT_FAILED", "DECLINED", "ERROR", "REJECTED", "BACKEND_ERROR")
        ),
    }

    bucket = "%Y-%m-%d %H:00" if hours == 24 else "%Y-%m-%d"
    daily = list(db.carts.aggregate([
        {"$match": {**cart_match, "status": "APPROVED"}},
        {"$group": {
            "_id": {"$dateToString": {"format": bucket, "date": "$dateCreation"}},
            "revenue": {"$sum": {"$ifNull": ["$total", 0]}},
            "orders": {"$sum": 1},
        }},
        {"$sort": {"_id": 1}},
    ]))

    methods_raw = list(db.paymentIntents.aggregate([
        {"$match": pi_match},
        {"$group": {"_id": {"m": "$paymentMethod", "s": "$paymentIntentStatus"}, "n": {"$sum": 1}}},
    ]))
    methods = {}
    pi_ok = pi_fail = 0
    for r in methods_raw:
        m = norm_method(r["_id"]["m"])
        s = r["_id"]["s"] or ""
        entry = methods.setdefault(m, {"aprobados": 0, "fallidos": 0})
        if s in APPROVED_PAYMENT_STATUSES:
            entry["aprobados"] += r["n"]
            pi_ok += r["n"]
        elif s in FAILED_PAYMENT_STATUSES:
            entry["fallidos"] += r["n"]
            pi_fail += r["n"]
    methods_list = sorted(
        [{"metodo": k, **v} for k, v in methods.items()],
        key=lambda x: -(x["aprobados"] + x["fallidos"]),
    )[:8]

    tickets_emitted = db.tickets.count_documents(tk_match)
    top_ev = list(db.tickets.aggregate([
        {"$match": tk_match},
        {"$group": {"_id": "$eventId", "tickets": {"$sum": 1}}},
        {"$sort": {"tickets": -1}},
        {"$limit": 7},
    ]))
    ev_ids = [ObjectId(e["_id"]) for e in top_ev if e["_id"] and ObjectId.is_valid(e["_id"])]
    titles = {
        str(e["_id"]): e.get("title", "(sin título)")
        for e in db.events.find({"_id": {"$in": ev_ids}}, {"title": 1})
    }
    top_events = [
        {"id": e["_id"], "evento": titles.get(str(e["_id"]), "(evento desconocido)"), "tickets": e["tickets"]}
        for e in top_ev
    ]

    total_intents = pi_ok + pi_fail
    return {
        "actualizado": datetime.now(timezone.utc).isoformat(),
        "kpis": {
            "ingresos": approved["total"],
            "ordenes": approved["n"],
            "ticket_promedio": approved["total"] / approved["n"] if approved["n"] else 0,
            "boletas_vendidas": approved["qty"],
            "boletas_emitidas": tickets_emitted,
            "conversion_carrito": approved["n"] / carts_total * 100 if carts_total else 0,
            "tasa_fallo_pago": pi_fail / total_intents * 100 if total_intents else 0,
        },
        "funnel": [
            {"etapa": "Carritos creados", "n": carts_total},
            {"etapa": "Llegó a pagar", "n": reached},
            {"etapa": "Compra aprobada", "n": approved["n"]},
        ],
        "abandono": abandoned,
        "serie": [{"fecha": d["_id"], "ingresos": d["revenue"], "ordenes": d["orders"]} for d in daily],
        "metodos": methods_list,
        "top_eventos": top_events,
    }


@app.get("/api/live")
def live(merchant: str = "ALL", minutes: int = 60):
    minutes = max(5, min(minutes, 360))
    since = oid_since(minutes / 60)
    cart_match = {"_id": {"$gte": since}, **merchant_filter("merchantRef", merchant)}
    pi_match = {"_id": {"$gte": since}, **merchant_filter("merchantReference", merchant)}
    names = merchant_names()

    by_status = {
        r["_id"]: r["n"]
        for r in db.carts.aggregate([
            {"$match": cart_match},
            {"$group": {"_id": "$status", "n": {"$sum": 1}}},
        ])
    }
    carts_total = sum(by_status.values())
    reached = sum(by_status.get(s, 0) for s in REACHED_PAYMENT_CART)
    approved = by_status.get("APPROVED", 0)

    feed = []
    for c in db.carts.find(
        cart_match, {"status": 1, "total": 1, "merchantRef": 1, "quantity": 1},
        sort=[("_id", -1)], limit=12,
    ):
        feed.append({
            "t": c["_id"].generation_time.isoformat(),
            "tipo": "carrito",
            "estado": c.get("status") or "",
            "monto": c.get("total") or 0,
            "merchant": names.get(c.get("merchantRef"), c.get("merchantRef") or "?"),
        })
    for p in db.paymentIntents.find(
        pi_match, {"paymentIntentStatus": 1, "finalPrice": 1, "merchantReference": 1, "paymentMethod": 1},
        sort=[("_id", -1)], limit=12,
    ):
        feed.append({
            "t": p["_id"].generation_time.isoformat(),
            "tipo": "pago",
            "estado": p.get("paymentIntentStatus") or "",
            "monto": p.get("finalPrice") or 0,
            "metodo": norm_method(p.get("paymentMethod")),
            "merchant": names.get(p.get("merchantReference"), p.get("merchantReference") or "?"),
        })
    feed.sort(key=lambda x: x["t"], reverse=True)

    return {
        "ahora": datetime.now(timezone.utc).isoformat(),
        "ventana_min": minutes,
        "funnel": [
            {"etapa": "Carritos creados", "n": carts_total},
            {"etapa": "Llegó a pagar", "n": reached},
            {"etapa": "Compra aprobada", "n": approved},
        ],
        "feed": feed[:15],
    }


UTM_CACHE = BASE / "utm_cache.json"


def _read_utm_cache() -> dict:
    if UTM_CACHE.exists():
        return json.loads(UTM_CACHE.read_text())
    return {"sources": {}, "campaigns": {}}


def _full_stats(d: dict) -> dict:
    s = d.get("sesiones", 0)
    return {
        "sesiones": s,
        "checkout": d.get("checkout", 0),
        "compra": d.get("compra", 0),
        "tasa_checkout": round(d.get("checkout", 0) / s * 100, 1) if s else 0,
        "tasa_compra": round(d.get("compra", 0) / s * 100, 1) if s else 0,
    }


def _parse_utm_query(q: str) -> dict:
    """Acepta un link completo con UTMs, un pedazo de querystring o un texto
    suelto (que se interpreta como utm_source)."""
    from urllib.parse import urlparse, parse_qs
    q = q.strip()
    parsed = {"source": None, "medium": None, "campaign": None}
    if "utm_" in q or "?" in q:
        query = urlparse(q if "://" in q else "//x/?" + q.split("?")[-1]).query or q.split("?")[-1]
        params = parse_qs(query)
        parsed["source"] = (params.get("utm_source") or [None])[0]
        parsed["medium"] = (params.get("utm_medium") or [None])[0]
        parsed["campaign"] = (params.get("utm_campaign") or [None])[0]
    if not parsed["source"] and not parsed["campaign"]:
        parsed["source"] = q
    for k in parsed:
        if parsed[k]:
            parsed[k] = parsed[k].strip().lower()
    return parsed


def _sources_for(cache: dict, merchant: str) -> tuple:
    """Devuelve (dict de sources, etiqueta del ámbito) según el merchant."""
    if merchant != "ALL":
        domain = MERCHANT_DOMAINS.get(merchant)
        return (
            {k.lower(): v for k, v in cache.get("by_domain", {}).get(domain, {}).items()},
            domain or "dominio desconocido",
        )
    return ({k.lower(): v for k, v in cache.get("sources", {}).items()}, "todos los dominios")


@app.get("/api/utm/buscar")
def utm_buscar(q: str, merchant: str = "ALL"):
    if not q.strip():
        raise HTTPException(400, "escribe o pega una UTM")
    cache = _read_utm_cache()
    sources, ambito = _sources_for(cache, merchant)
    campaigns = {str(k).lower(): v for k, v in cache.get("campaigns", {}).items()}
    parsed = _parse_utm_query(q)

    resultado_source = None
    similares = []
    if parsed["source"]:
        term = parsed["source"]
        if term in sources:
            resultado_source = {"nombre": term, **_full_stats(sources[term])}
        similares = [
            {"nombre": k, **_full_stats(v)}
            for k, v in sources.items()
            if term in k and k != term
        ]
        similares.sort(key=lambda x: -x["sesiones"])

    resultado_campaign = None
    if parsed["campaign"] and parsed["campaign"] in campaigns:
        resultado_campaign = {"nombre": parsed["campaign"], **_full_stats(campaigns[parsed["campaign"]])}

    return {
        "consulta": parsed,
        "ambito": ambito,
        "source": resultado_source,
        "campaign": resultado_campaign,
        "similares": similares[:8],
        "updated": cache.get("updated"),
        "days": cache.get("days", 3),
    }


@app.get("/api/utm/top")
def utm_top(merchant: str = "ALL", limit: int = 30):
    cache = _read_utm_cache()
    sources, ambito = _sources_for(cache, merchant)
    top = [{"nombre": k, **_full_stats(v)} for k, v in sources.items()]
    top.sort(key=lambda x: -x["sesiones"])
    return {
        "updated": cache.get("updated"),
        "days": cache.get("days", 3),
        "ambito": ambito,
        "top": top[:limit],
    }


GEO_CACHE = BASE / "geo_cache.json"


@app.get("/api/geo")
def geo(merchant: str = "ALL"):
    cache = json.loads(GEO_CACHE.read_text()) if GEO_CACHE.exists() else {"by_domain": {}}
    by = cache.get("by_domain", {})
    if merchant != "ALL":
        domain = MERCHANT_DOMAINS.get(merchant)
        scoped = {domain: by.get(domain, {})} if domain else {}
        ambito = domain or "dominio desconocido"
    else:
        scoped, ambito = by, "todos los dominios"

    ciudades = {}
    for dom in scoped.values():
        for city, v in dom.items():
            d = ciudades.setdefault(city, {"sesiones": 0, "checkout": 0, "compra": 0})
            for k in d:
                d[k] += v.get(k, 0)
    lista = [
        {"ciudad": c, **v,
         "tasa_compra": round(v["compra"] / v["sesiones"] * 100, 1) if v["sesiones"] else 0}
        for c, v in ciudades.items()
    ]
    lista.sort(key=lambda x: -x["sesiones"])
    return {
        "updated": cache.get("updated"),
        "days": cache.get("days", 3),
        "ambito": ambito,
        "ciudades": lista,
    }


@app.get("/api/eventos")
def eventos_lista(merchant: str = "ALL"):
    q = {}
    if merchant != "ALL":
        m = db.merchants.find_one({"merchantRef": merchant}, {"_id": 1})
        if not m:
            raise HTTPException(404, "merchant no encontrado")
        q = {"merchant.$id": m["_id"]}
    mnames = {
        m["_id"]: (m.get("name") or "").strip()
        for m in db.merchants.find({}, {"name": 1})
    }
    now = datetime.now()  # naive, hora local Colombia como las fechas de events
    out = []
    for e in db.events.find(q, {"title": 1, "startsAt": 1, "endAt": 1, "merchant": 1}).sort("startsAt", -1).limit(200):
        mref = e.get("merchant")
        end = e.get("endAt")
        out.append({
            "id": str(e["_id"]),
            "titulo": e.get("title") or "(sin título)",
            "inicio": e.get("startsAt").isoformat() if isinstance(e.get("startsAt"), datetime) else None,
            "merchant": mnames.get(mref.id if mref is not None else None, ""),
            "pasado": end < now if isinstance(end, datetime) else False,
        })
    return out


@app.get("/api/evento")
def evento_detalle(id: str):
    if not ObjectId.is_valid(id):
        raise HTTPException(400, "id inválido")
    oid = ObjectId(id)
    ev = db.events.find_one({"_id": oid})
    if not ev:
        raise HTTPException(404, "evento no encontrado")
    mref = ev.get("merchant")
    mname = ""
    if mref is not None:
        m = db.merchants.find_one({"_id": mref.id}, {"name": 1})
        mname = (m or {}).get("name", "")

    # tickets (indexado por eventId)
    tk_status = {
        (r["_id"] or "").strip(): r["n"]
        for r in db.tickets.aggregate([
            {"$match": {"eventId": id}},
            {"$group": {"_id": "$status", "n": {"$sum": 1}}},
        ])
    }
    vendidas_por_act = {
        r["_id"]: r["n"]
        for r in db.tickets.aggregate([
            {"$match": {"eventId": id, "status": {"$in": ["VALIDATED", "APPROVED"]}}},
            {"$group": {"_id": "$actId", "n": {"$sum": 1}}},
        ])
    }

    # localidades/funciones
    localidades = []
    for a in db.acts.find({"event.$id": oid}, {"label": 1, "capacity": 1, "price": 1, "active": 1}):
        vendidas = vendidas_por_act.get(str(a["_id"]), 0)
        localidades.append({
            "localidad": a.get("label") or "",
            "precio": a.get("price") or 0,
            "vendidas": vendidas,
            "cupos_restantes": a.get("capacity"),
        })
    localidades.sort(key=lambda x: -x["vendidas"])

    # carritos del evento (escaneo por ticketDetails.idEvent, ~0.5s)
    carts = list(db.carts.find(
        {"ticketDetails.idEvent": id},
        {"status": 1, "total": 1, "quantity": 1, "dateCreation": 1},
    ))
    by_status = {}
    daily = {}
    cart_ids = []
    for c in carts:
        s = c.get("status") or "?"
        d = by_status.setdefault(s, {"n": 0, "v": 0.0})
        d["n"] += 1
        d["v"] += c.get("total") or 0
        cart_ids.append(str(c["_id"]))
        if s == "APPROVED" and isinstance(c.get("dateCreation"), datetime):
            day = c["dateCreation"].strftime("%Y-%m-%d")
            dd = daily.setdefault(day, {"ingresos": 0.0, "ordenes": 0})
            dd["ingresos"] += c.get("total") or 0
            dd["ordenes"] += 1
    approved = by_status.get("APPROVED", {"n": 0, "v": 0.0})
    reached = sum(by_status.get(s, {"n": 0})["n"] for s in REACHED_PAYMENT_CART)
    total_carts = sum(d["n"] for d in by_status.values())

    # métodos de pago del evento (paymentIntents por cartId)
    metodos = {}
    pi_ok = pi_fail = 0
    if cart_ids:
        for r in db.paymentIntents.aggregate([
            {"$match": {"cartId": {"$in": cart_ids[:8000]}}},
            {"$group": {"_id": {"m": "$paymentMethod", "s": "$paymentIntentStatus"}, "n": {"$sum": 1}}},
        ]):
            mm = norm_method(r["_id"]["m"])
            s = r["_id"]["s"] or ""
            e = metodos.setdefault(mm, {"aprobados": 0, "fallidos": 0})
            if s in APPROVED_PAYMENT_STATUSES:
                e["aprobados"] += r["n"]; pi_ok += r["n"]
            elif s in FAILED_PAYMENT_STATUSES:
                e["fallidos"] += r["n"]; pi_fail += r["n"]
    metodos_lista = sorted(
        [{"metodo": k, **v} for k, v in metodos.items()],
        key=lambda x: -(x["aprobados"] + x["fallidos"]),
    )

    # ciudades del tráfico del evento (Clarity, solo eventos con visitas recientes)
    evgeo_file = BASE / "evgeo_cache.json"
    evgeo = json.loads(evgeo_file.read_text()) if evgeo_file.exists() else {}
    ciudades_ev = sorted(
        [
            {"ciudad": c, "sesiones": v.get("sesiones", 0), "compras": v.get("compra", 0)}
            for c, v in evgeo.get("by_event", {}).get(id, {}).items()
        ],
        key=lambda x: (-x["compras"], -x["sesiones"]),
    )

    validadas = tk_status.get("VALIDATED", 0)
    aprobadas_tk = tk_status.get("APPROVED", 0)
    vendidas_total = validadas + aprobadas_tk
    total_intents = pi_ok + pi_fail
    return {
        "titulo": ev.get("title"),
        "merchant": mname,
        "inicio": ev.get("startsAt").isoformat() if isinstance(ev.get("startsAt"), datetime) else None,
        "fin": ev.get("endAt").isoformat() if isinstance(ev.get("endAt"), datetime) else None,
        "pasado": ev.get("endAt") < datetime.now() if isinstance(ev.get("endAt"), datetime) else False,
        "kpis": {
            "ingresos": approved["v"],
            "ordenes": approved["n"],
            "boletas_vendidas": vendidas_total,
            "validadas_en_puerta": validadas,
            "asistencia_pct": validadas / vendidas_total * 100 if vendidas_total else 0,
            "conversion_carrito": approved["n"] / total_carts * 100 if total_carts else 0,
            "tasa_fallo_pago": pi_fail / total_intents * 100 if total_intents else 0,
            "canceladas": tk_status.get("CANCELLED", 0),
        },
        "funnel": [
            {"etapa": "Carritos creados", "n": total_carts},
            {"etapa": "Llegó a pagar", "n": reached},
            {"etapa": "Compra aprobada", "n": approved["n"]},
        ],
        "perdido": {
            "expirado": by_status.get("CREATED-TIME-OUT", {"n": 0, "v": 0})["v"],
            "borrado": by_status.get("DELETED_BY_USER", {"n": 0, "v": 0})["v"],
            "pago_fallido": sum(by_status.get(s, {"v": 0})["v"] for s in ("PAYMENT_FAILED", "DECLINED", "ERROR", "REJECTED")),
        },
        "serie": [{"fecha": k, **v} for k, v in sorted(daily.items())],
        "localidades": localidades,
        "metodos": metodos_lista,
        "ciudades": ciudades_ev,
        "ciudades_updated": evgeo.get("updated"),
        "ciudades_days": evgeo.get("days", 3),
    }


TECH_CACHE = BASE / "tech_cache.json"


@app.get("/api/tech")
def tech(merchant: str = "ALL"):
    cache = json.loads(TECH_CACHE.read_text()) if TECH_CACHE.exists() else {"by_domain": {}}
    by = cache.get("by_domain", {})
    if merchant != "ALL":
        domain = MERCHANT_DOMAINS.get(merchant)
        scoped = {domain: by.get(domain, {})} if domain else {}
        ambito = domain or "dominio desconocido"
    else:
        scoped, ambito = by, "todos los dominios"

    out = {"device": {}, "browser": {}, "os": {}}
    for dom in scoped.values():
        for dim in out:
            for k, n in dom.get(dim, {}).items():
                out[dim][k] = out[dim].get(k, 0) + n
    listas = {
        dim: sorted(
            [{"nombre": k, "sesiones": n} for k, n in vals.items()],
            key=lambda x: -x["sesiones"],
        )
        for dim, vals in out.items()
    }
    return {
        "updated": cache.get("updated"),
        "days": cache.get("days", 3),
        "ambito": ambito,
        "dispositivos": listas["device"],
        "navegadores": listas["browser"],
        "sistemas": listas["os"],
    }


ABANDONED_STATUSES = [
    "DELETED_BY_USER", "CREATED-TIME-OUT", "PAYMENT_FAILED",
    "DECLINED", "ERROR", "REJECTED", "BACKEND_ERROR",
]


def _extract_assistant(cart: dict) -> dict:
    td = cart.get("ticketDetails") or []
    first = td[0] if td and isinstance(td[0], dict) else {}
    assistants = first.get("assistants")
    a = assistants[0] if isinstance(assistants, list) and assistants and isinstance(assistants[0], dict) else {}
    return first, a


@app.get("/api/abandonados")
def abandonados(merchant: str = "ALL", hours: int = 168):
    """Carritos abandonados que dejaron datos de contacto en el formulario de
    asistentes (ticketDetails.assistants). Son la lista de remarketing: alta
    intención, se les puede escribir o llamar.

    Excluye a quienes, después de abandonar, sí completaron una compra
    aprobada (mismo email) — ya no son un lead perdido."""
    if hours not in (24, 168, 720):
        raise HTTPException(400, "hours debe ser 24, 168 o 720")
    since = oid_since(hours)
    match = {
        "_id": {"$gte": since},
        "status": {"$in": ABANDONED_STATUSES},
        **merchant_filter("merchantRef", merchant),
    }
    names = merchant_names()

    total_carts = 0
    total_value = 0.0
    by_email = {}
    with_contact_value = 0.0

    cursor = db.carts.find(
        {**match, "ticketDetails.0": {"$exists": True}},
        {"ticketDetails": 1, "total": 1, "quantity": 1, "status": 1,
         "merchantRef": 1},
        sort=[("_id", -1)], limit=1500,
    )
    for c in cursor:
        first, a = _extract_assistant(c)
        email = (a.get("email") or "").strip().lower()
        if not email or "@" not in email:
            continue
        item = {
            "cuando": c["_id"].generation_time,
            "nombre": (a.get("name") or "").strip().title(),
            "email": email,
            "celular": str(a.get("cellphone") or "").strip(),
            "evento": first.get("eventLabel") or "(sin evento)",
            "valor": c.get("total") or 0,
            "boletas": int(c.get("quantity") or 0),
            "estado": c.get("status"),
            "merchant": names.get(c.get("merchantRef"), c.get("merchantRef") or "?"),
            "intentos": 1,
        }
        prev = by_email.get(email)
        if prev:
            prev["intentos"] += 1
            if item["valor"] > prev["valor"]:
                item["intentos"] = prev["intentos"]
                by_email[email] = item
        else:
            by_email[email] = item

    # Quita a quienes ya compraron después de abandonar: mismo email con un
    # carrito APPROVED cuya fecha sea posterior al intento fallido.
    if by_email:
        approved_match = {
            "_id": {"$gte": since},
            "status": "APPROVED",
            "ticketDetails.0": {"$exists": True},
            **merchant_filter("merchantRef", merchant),
        }
        approved_cursor = db.carts.find(
            approved_match,
            {"ticketDetails": 1},
            sort=[("_id", -1)], limit=5000,
        )
        recovered_after = {}
        for c in approved_cursor:
            _, a = _extract_assistant(c)
            email = (a.get("email") or "").strip().lower()
            if email in by_email:
                ts = c["_id"].generation_time
                if email not in recovered_after or ts > recovered_after[email]:
                    recovered_after[email] = ts
        for email, approved_ts in recovered_after.items():
            if approved_ts > by_email[email]["cuando"]:
                del by_email[email]

    for item in by_email.values():
        item["cuando"] = item["cuando"].isoformat()

    # resumen general (con y sin contacto) sobre el mismo periodo
    for r in db.carts.aggregate([
        {"$match": match},
        {"$group": {"_id": None, "n": {"$sum": 1}, "v": {"$sum": {"$ifNull": ["$total", 0]}}}},
    ]):
        total_carts, total_value = r["n"], r["v"]

    lista = sorted(by_email.values(), key=lambda x: -x["valor"])
    with_contact_value = sum(x["valor"] for x in lista)
    return {
        "resumen": {
            "carritos_abandonados": total_carts,
            "valor_abandonado": total_value,
            "compradores_contactables": len(lista),
            "valor_contactable": with_contact_value,
        },
        "lista": lista[:150],
    }


def _refresh_clarity_from_api(cache: dict) -> dict:
    """Refresca métricas base desde la Data Export API oficial de Clarity.

    Esa API solo expone Traffic/DeadClick/RageClick/ScriptError por URL; el
    funnel de comportamiento (checkout/compra) no está disponible ahí, así que
    se conserva el último valor conocido de cada dominio.
    """
    token = ENV.get("CLARITY_API_TOKEN")
    req = urllib.request.Request(
        "https://www.clarity.ms/export-data/api/v1/project-live-insights"
        "?numOfDays=3&dimension1=URL",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.load(r)

    domains = {}
    prev = cache.get("domains", {})
    metric_map = {
        "Traffic": "sesiones",
        "DeadClickCount": "dead_clicks",
        "RageClickCount": "rage_clicks",
        "ScriptErrorCount": "errores_js",
    }
    for metric in payload:
        name = metric.get("metricName")
        if name not in metric_map:
            continue
        for row in metric.get("information", []):
            url = row.get("URL") or row.get("Url") or ""
            host = url.split("//")[-1].split("/")[0].replace("www.", "")
            if not host:
                continue
            d = domains.setdefault(host, {
                "sesiones": 0, "usuarios": 0, "dead_clicks": 0,
                "rage_clicks": 0, "errores_js": 0,
                "checkout": prev.get(host, {}).get("checkout", 0),
                "compra": prev.get(host, {}).get("compra", 0),
            })
            if name == "Traffic":
                d["sesiones"] += int(row.get("totalSessionCount", 0) or 0)
                d["usuarios"] += int(row.get("distinctUserCount", 0) or 0)
            else:
                d[metric_map[name]] += int(row.get("subTotal", 0) or 0)

    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "days": 3,
        "source": "clarity-export-api",
        "domains": domains,
    }


@app.get("/api/clarity")
def clarity(merchant: str = "ALL"):
    cache = json.loads(CLARITY_CACHE.read_text()) if CLARITY_CACHE.exists() else {"domains": {}}
    updated = cache.get("updated")
    age = None
    if updated:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(updated)).total_seconds()

    if ENV.get("CLARITY_API_TOKEN") and (age is None or age > CLARITY_TTL_SECONDS):
        try:
            cache = _refresh_clarity_from_api(cache)
            CLARITY_CACHE.write_text(json.dumps(cache, indent=2))
            age = 0
        except Exception:
            pass  # sirve el caché existente; el token puede haber agotado sus 10 req/día

    domains = cache.get("domains", {})
    if merchant == "ALL":
        agg = {"sesiones": 0, "usuarios": 0, "dead_clicks": 0, "rage_clicks": 0,
               "errores_js": 0, "checkout": 0, "compra": 0}
        for d in domains.values():
            for k in agg:
                agg[k] += d.get(k, 0)
        data, domain = agg, "todos los dominios"
    else:
        domain = MERCHANT_DOMAINS.get(merchant)
        data = domains.get(domain) if domain else None

    return {
        "domain": domain,
        "updated": cache.get("updated"),
        "days": cache.get("days", 3),
        "stale_horas": round(age / 3600, 1) if age else 0,
        "auto_refresh": bool(ENV.get("CLARITY_API_TOKEN")),
        "data": data,
    }


WATI_BASE_URL = "https://live-mt-server.wati.io/10124742"
# Número de WhatsApp Business conectado en WATI (api/v2/whatsapp/phoneNumbers).
WATI_CHANNEL_NUMBER = "573223763994"
# Template aprobado por Meta para contactar en frío (sin conversación abierta).
WATI_RECOVERY_TEMPLATE = "recuperar_carrito_dashboard"
# Cloudflare (frente al servidor de WATI) bloquea el User-Agent por defecto de
# urllib con un 403 "error code: 1010".
WATI_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


class WatiMessageRequest(BaseModel):
    phone: str
    name: str
    event: str
    merchant: str = ""


def _wati_request(url: str, token: str, body: Optional[bytes] = None) -> dict:
    headers = {"Authorization": f"Bearer {token}", "User-Agent": WATI_USER_AGENT}
    if body is not None:
        # sendTemplateMessage exige un Content-Type explícito (application/json,
        # text/json, etc.); sin él responde 415 con cuerpo vacío.
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


@app.post("/api/send-wati-message")
def send_wati_message(req: WatiMessageRequest):
    token = ENV.get("WATI_API_TOKEN")
    if not token:
        raise HTTPException(400, "WATI_API_TOKEN no configurado")

    # Limpiar número: solo dígitos, con prefijo de país 57 (Colombia)
    phone_clean = "".join(c for c in req.phone if c.isdigit())
    if not phone_clean.startswith("57"):
        phone_clean = "57" + phone_clean

    name = (req.name or "").strip() or "cliente"
    message = (
        f"Feliz dia {name}!! Como vas? Mi nombre es Catalina del equipo de Blasttickets! "
        f"vimos que Intentaste realizar una compra para {req.event} y no se pudo concretar. "
        f"¿Necesitas que te ayude con algo concretamente?"
    )

    # WhatsApp solo permite mensaje libre (sendSessionMessage) si el cliente ya
    # escribió en las últimas 24h. Lo intentamos primero porque es instantáneo;
    # si no hay conversación abierta, WATI responde result:false y recurrimos
    # al template aprobado por Meta (sendTemplateMessage), que sí puede
    # contactar en frío a cualquier número.
    try:
        session_url = (
            f"{WATI_BASE_URL}/api/v1/sendSessionMessage/{phone_clean}"
            f"?messageText={urllib.parse.quote(message)}"
        )
        result = _wati_request(session_url, token)
        if result.get("result") is not False:
            return {"success": True, "message": "Mensaje enviado (conversación activa)", "data": result}
    except urllib.error.HTTPError:
        pass  # sin conversación activa: seguimos con el template
    except Exception as e:
        raise HTTPException(500, f"Error al enviar mensaje: {str(e)}")

    template_url = f"{WATI_BASE_URL}/api/v2/sendTemplateMessage?whatsappNumber={phone_clean}"
    body = json.dumps({
        "template_name": WATI_RECOVERY_TEMPLATE,
        "broadcast_name": WATI_RECOVERY_TEMPLATE,
        "channel_number": WATI_CHANNEL_NUMBER,
        "parameters": [
            {"name": "1", "value": name},
            {"name": "2", "value": req.event},
        ],
    }).encode()
    try:
        result = _wati_request(template_url, token, body)
    except urllib.error.HTTPError as e:
        raise HTTPException(e.code, f"WATI respondió con error: {e.read().decode()}")
    except Exception as e:
        raise HTTPException(500, f"Error al enviar mensaje: {str(e)}")

    if result.get("result") is False:
        raise HTTPException(400, result.get("error") or result.get("info") or "WATI rechazó el mensaje")

    return {"success": True, "message": "Mensaje enviado (template)", "data": result}


CONTACTADOS_HASH_KEY = "wati_contactados"


def _kv_command(*parts) -> object:
    base = ENV.get("KV_REST_API_URL")
    token = ENV.get("KV_REST_API_TOKEN")
    if not base or not token:
        raise HTTPException(500, "KV_REST_API_URL/KV_REST_API_TOKEN no configurados")
    url = base.rstrip("/") + "/" + "/".join(urllib.parse.quote(str(p), safe="") for p in parts)
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read())["result"]


class MarcarContactadoRequest(BaseModel):
    email: str


@app.get("/api/contactados")
def get_contactados():
    """Mapa compartido email -> fecha ISO de último contacto por WhatsApp.
    Vive en Vercel KV (Redis), no en Mongo (solo lectura), para que todo el
    equipo vea el mismo estado sin importar desde qué navegador entren."""
    try:
        flat = _kv_command("hgetall", CONTACTADOS_HASH_KEY) or []
    except Exception:
        return {}
    return dict(zip(flat[0::2], flat[1::2]))


@app.post("/api/contactados")
def marcar_contactado(req: MarcarContactadoRequest):
    email = req.email.strip().lower()
    if not email:
        raise HTTPException(400, "email requerido")
    ts = datetime.now(timezone.utc).isoformat()
    _kv_command("hset", CONTACTADOS_HASH_KEY, email, ts)
    return {"success": True, "email": email, "ts": ts}


# ---------------------------------------------------------------------------
# Auditoría de tickets (integridad carrito <-> tickets emitidos)
#
# El dashboard NO calcula nada: solo lee audit_cache.json, que genera
# tools/ticket_audit/backfill.py --cache. La lógica de clasificación vive en un
# único lugar (tools/ticket_audit/detector.py) para que backfill, export y esta
# vista nunca puedan divergir.
#
# Las resoluciones (resuelto / falso positivo) se guardan en Vercel KV, igual
# que los contactados. Nunca tocan la data de negocio: marcar algo aquí no
# modifica ni un ticket.
# ---------------------------------------------------------------------------

AUDIT_CACHE = BASE / "audit_cache.json"
AUDIT_CACHE_KEY = "ticket_audit_cache"
AUDIT_RESOLUTIONS_KEY = "ticket_audit_resolutions"
AUDIT_RESOLUTIONS_FILE = BASE / "audit_resolutions.json"
_audit_cache_mem = {"ts": 0.0, "data": None}


def _leer_audit_cache() -> dict:
    """El barrido corre en GitHub Actions cada hora y deja el resultado en KV,
    así que el tablero se actualiza sin desplegar. El archivo en disco queda
    como respaldo para desarrollo local y para el primer arranque."""
    if time.time() - _audit_cache_mem["ts"] < 60 and _audit_cache_mem["data"]:
        return _audit_cache_mem["data"]

    data = None
    if ENV.get("KV_REST_API_URL") and ENV.get("KV_REST_API_TOKEN"):
        try:
            crudo = _kv_command("get", AUDIT_CACHE_KEY)
            if crudo:
                data = json.loads(crudo)
        except Exception:
            data = None
    if data is None and AUDIT_CACHE.exists():
        data = json.loads(AUDIT_CACHE.read_text())
    if data is None:
        raise HTTPException(
            503,
            "Aún no se ha corrido el barrido. Ejecuta: "
            "python3 tools/ticket_audit/alerta.py --refrescar-cache",
        )
    _audit_cache_mem.update(ts=time.time(), data=_sin_datos_personales(data))
    return _audit_cache_mem["data"]


#: Campos de comprador que jamás deben salir por la API: el tablero es público.
_PII = {"identification", "name", "email", "cellphone", "asistente", "identificacion"}


def _sin_datos_personales(cache: dict) -> dict:
    """Barrido defensivo. El detector ya no propaga la cédula, pero un caché
    generado por una versión anterior sí puede traerla y este tablero se sirve
    sin autenticación."""
    for inc in cache.get("incidencias", []):
        for campo in list(inc):
            if campo.lower() in _PII:
                inc.pop(campo, None)
        for grupo in inc.get("duplicateGroups") or []:
            for campo in list(grupo):
                if campo.lower() in _PII:
                    grupo.pop(campo, None)
    return cache


def _audit_resolutions() -> dict:
    """Mapa paymentReference -> resolución. Usa KV si está configurado; si no
    (desarrollo local), un JSON en disco."""
    if ENV.get("KV_REST_API_URL") and ENV.get("KV_REST_API_TOKEN"):
        try:
            flat = _kv_command("hgetall", AUDIT_RESOLUTIONS_KEY) or []
            return {k: json.loads(v) for k, v in zip(flat[0::2], flat[1::2])}
        except Exception:
            return {}
    if AUDIT_RESOLUTIONS_FILE.exists():
        try:
            return json.loads(AUDIT_RESOLUTIONS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_resolution(reference: str, payload: dict) -> None:
    if ENV.get("KV_REST_API_URL") and ENV.get("KV_REST_API_TOKEN"):
        _kv_command("hset", AUDIT_RESOLUTIONS_KEY, reference, json.dumps(payload))
        return
    data = _audit_resolutions()
    data[reference] = payload
    AUDIT_RESOLUTIONS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1))


@app.get("/api/auditoria")
def auditoria(merchant: str = "ALL", estado: str = "TODOS", severidad: str = "TODAS",
              desde: str = "", hasta: str = "", q: str = "", ver: str = "abiertas"):
    """Incidencias de auditoría con filtros. Todo se resuelve sobre el cache."""
    cache = _leer_audit_cache()
    resoluciones = _audit_resolutions()

    filas = []
    for inc in cache.get("incidencias", []):
        ref = inc.get("paymentReference")
        inc["resolucion"] = resoluciones.get(ref)
        if ver == "abiertas" and inc["resolucion"]:
            continue
        if ver == "resueltas" and not inc["resolucion"]:
            continue
        if merchant != "ALL" and inc.get("merchantRef") != merchant:
            continue
        if estado != "TODOS" and inc.get("status") != estado:
            continue
        if severidad != "TODAS" and inc.get("severity") != severidad:
            continue
        fecha = (inc.get("createdAtLast") or "")[:10]
        if desde and fecha and fecha < desde:
            continue
        if hasta and fecha and fecha > hasta:
            continue
        if q:
            needle = q.strip().lower()
            campos = " ".join(str(inc.get(k) or "") for k in
                              ("paymentReference", "merchantRef", "merchantName",
                               "eventName", "cartId"))
            if needle not in campos.lower():
                continue
        filas.append(inc)

    over = [f for f in filas if f.get("status") == "OVER_ISSUED"]
    return {
        "updated": cache.get("updated"),
        "window": cache.get("window"),
        "totalesGlobales": cache.get("totals", {}),
        "porDia": cache.get("porDia", []),
        "kpis": {
            "incidencias": len(filas),
            "ticketsEnExceso": sum(f.get("delta") or 0 for f in over),
            "valorFacialExpuesto": round(sum(f.get("exposedAmount") or 0 for f in over)),
            "merchantsAfectados": len({f.get("merchantRef") for f in filas}),
            "criticas": sum(1 for f in filas if f.get("severity") == "critical"),
            "resueltas": sum(1 for f in filas if f.get("resolucion")),
        },
        "incidencias": filas,
    }


@app.get("/api/auditoria/csv")
def auditoria_csv(merchant: str = "ALL", estado: str = "TODOS", severidad: str = "TODAS",
                  desde: str = "", hasta: str = "", q: str = "", ver: str = "abiertas"):
    data = auditoria(merchant, estado, severidad, desde, hasta, q, ver)
    cols = ["paymentReference", "status", "severity", "merchantRef", "merchantName",
            "eventName", "expectedTickets", "actualTickets", "delta", "amountPaid",
            "exposedAmount", "paymentMethod", "createdAtFirst", "createdAtLast",
            "createdAtSpread", "cartId", "cartStatus", "reason"]
    def cell(value):
        # `or ""` convertiría un 0 legítimo (ej. spread de 0 s) en celda vacía.
        if value is None:
            return ""
        return str(value).replace(";", ",").replace("\n", " ")

    lines = [";".join(cols)]
    for inc in data["incidencias"]:
        lines.append(";".join(cell(inc.get(c)) for c in cols))
    from fastapi.responses import Response
    return Response(
        "﻿" + "\n".join(lines),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="auditoria_tickets.csv"'},
    )


class ResolverIncidenciaRequest(BaseModel):
    paymentReference: str
    resolucion: str  # "resuelto" | "falso_positivo" | "reabrir"
    nota: str = ""
    usuario: str = ""


@app.post("/api/auditoria/resolver")
def resolver_incidencia(req: ResolverIncidenciaRequest):
    """Marca una incidencia. SOLO afecta el registro de auditoría: no toca
    tickets, carritos ni pagos."""
    ref = req.paymentReference.strip()
    if not ref:
        raise HTTPException(400, "paymentReference requerido")
    if req.resolucion not in ("resuelto", "falso_positivo", "reabrir"):
        raise HTTPException(400, "resolucion inválida")

    if req.resolucion == "reabrir":
        data = _audit_resolutions()
        data.pop(ref, None)
        if ENV.get("KV_REST_API_URL") and ENV.get("KV_REST_API_TOKEN"):
            _kv_command("hdel", AUDIT_RESOLUTIONS_KEY, ref)
        else:
            AUDIT_RESOLUTIONS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        return {"success": True, "paymentReference": ref, "resolucion": None}

    payload = {
        "resolucion": req.resolucion,
        "nota": req.nota.strip(),
        "usuario": req.usuario.strip() or "equipo",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _save_resolution(ref, payload)
    return {"success": True, "paymentReference": ref, **payload}


@app.get("/auditoria")
def auditoria_page():
    return FileResponse(
        BASE / "static" / "auditoria.html",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@app.get("/")
def index():
    # no-store: Vercel preserva timestamps de archivo entre deploys, así que el
    # Last-Modified de FileResponse no cambia y el navegador reusa una versión
    # vieja vía 304 aunque el contenido sí cambió.
    return FileResponse(
        BASE / "static" / "index.html",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
