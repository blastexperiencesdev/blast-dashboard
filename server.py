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
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bson import ObjectId
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
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
    for key in ("MONGODB_URI", "CLARITY_API_TOKEN"):
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

client = MongoClient(ENV["MONGODB_URI"], serverSelectionTimeoutMS=15000)
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


@app.get("/api/abandonados")
def abandonados(merchant: str = "ALL", hours: int = 168):
    """Carritos abandonados que dejaron datos de contacto en el formulario de
    asistentes (ticketDetails.assistants). Son la lista de remarketing: alta
    intención, se les puede escribir o llamar."""
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
        td = c.get("ticketDetails") or []
        first = td[0] if td and isinstance(td[0], dict) else {}
        assistants = first.get("assistants")
        a = assistants[0] if isinstance(assistants, list) and assistants and isinstance(assistants[0], dict) else {}
        email = (a.get("email") or "").strip().lower()
        if not email or "@" not in email:
            continue
        item = {
            "cuando": c["_id"].generation_time.isoformat(),
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


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
