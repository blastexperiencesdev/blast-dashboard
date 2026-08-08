"""Motor de deteccion de integridad Carts <-> Tickets (Blast Tickets).

Nucleo unico reutilizable: lo usan el backfill CLI, el export a Excel y el
dashboard. Toda la logica de clasificacion vive aqui y en ningun otro lado.

ES SOLO LECTURA SOBRE LA DATA DE NEGOCIO. Este modulo no escribe, no borra y
no corrige tickets. Nunca.

Reglas de negocio confirmadas con el owner (2026-08-07):

- expected = suma de (item.quantity * act.ticketGroupAmount). ticketGroupAmount
  existe porque una localidad grupal (palco, combo x15) se configura como UN
  act que emite N tickets. Ignorarlo produce ~32% de falsos positivos.
- Los tickets con cartId "BO-GENERATED*" se emiten desde el backoffice
  (cortesias, boleteria fisica) y nunca pasan por carrito. Se excluyen: el
  problema reportado es solo de compras web.
- La anulacion sobrescribe el ticket en sitio (status/label/typeEntrance ->
  "CANCELLED", price -> 0). No hay borrado ni flag. Un carrito cuyos tickets
  estan todos cancelados es un reembolso legitimo, no un under-issued.
- tickets.status trae basura historica ("CANCELLED\\n", "VALIDATES", un email).
  Siempre normalizar con strip+upper.

Esquema (descubierto, no documentado en el backend):

    carts.reference == tickets.paymentReference == paymentIntents.reference
    str(carts._id)  == tickets.cartId           == paymentIntents.cartId

No existe indice sobre ninguno de esos campos de referencia. Ver README.
"""
from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator, Optional

from bson import ObjectId
from pymongo import MongoClient

# --- estados -----------------------------------------------------------------

OK = "OK"
OVER_ISSUED = "OVER_ISSUED"
OVER_ISSUED_CORREGIDO = "OVER_ISSUED_CORREGIDO"
UNDER_ISSUED = "UNDER_ISSUED"
ORPHAN_TICKETS = "ORPHAN_TICKETS"
NO_CART = "NO_CART"
AMBIGUOUS = "AMBIGUOUS"
BACKOFFICE_ISSUED = "BACKOFFICE_ISSUED"
ALL_CANCELLED = "ALL_CANCELLED"
PARTIALLY_CANCELLED = "PARTIALLY_CANCELLED"

#: tickets que cuentan como vigentes contra el aforo
ALIVE_TICKET_STATUS = frozenset({"APPROVED", "VALIDATED"})

#: Una compra web siempre deja en tickets.cartId el _id del carrito, es decir
#: un ObjectId de 24 hex. Todo lo demas lo escribio el backoffice a mano y no
#: tiene carrito contra el cual compararse. En produccion conviven varias
#: formas: "BO-GENERATED-", "BO-ABONO-GENERATED-" (abonos), "BO GENERATED ",
#: "b0 GENERATED", e incluso el nombre de la productora ("SDL EVENTOS").
#: Reconocer solo un prefijo dejaba entrar emisiones de backoffice al tablero.
_OBJECTID_RE = re.compile(r"^[0-9a-fA-F]{24}$")

#: Ventana para considerar que unos tickets nacieron de la misma ejecucion.
#: Se usa para distinguir una emision doble que alguien ya anulo a mano (todos
#: los tickets nacen en el mismo instante) de una anulacion y reemision
#: legitima (el reemplazo nace despues). Por conteos son identicas.
SAME_RUN_SECONDS = 120

DEFAULT_DB = "blast-prod"


def norm_status(value) -> str:
    """Normaliza un status. La data trae saltos de linea y mayusculas mixtas."""
    return (value or "").strip().upper() if isinstance(value, str) else ""


def is_backoffice(cart_id) -> bool:
    """True si el ticket no viene de una compra web.

    Se decide por la forma del cartId, no por un prefijo: cualquier valor que
    no sea un ObjectId lo escribió el backoffice. Un cartId vacío no permite
    afirmarlo, así que no se marca aquí y termina clasificado como huérfano.
    """
    if not isinstance(cart_id, str) or not cart_id.strip():
        return False
    return not _OBJECTID_RE.match(cart_id.strip())


# --- resultado ----------------------------------------------------------------


@dataclass
class AuditResult:
    paymentReference: str
    status: str
    merchantId: Optional[str] = None
    merchantName: Optional[str] = None
    merchantRef: Optional[str] = None
    eventId: Optional[str] = None
    eventName: Optional[str] = None
    cartId: Optional[str] = None
    cartStatus: Optional[str] = None
    cartDate: Optional[datetime] = None
    expectedTickets: int = 0
    actualTickets: int = 0
    totalTickets: int = 0
    cancelledTickets: int = 0
    delta: int = 0
    correctedExcess: int = 0   # tickets emitidos de mas que ya fueron anulados
    ticketIds: list = field(default_factory=list)
    duplicateGroups: list = field(default_factory=list)
    createdAtFirst: Optional[datetime] = None
    createdAtLast: Optional[datetime] = None
    createdAtSpread: float = 0.0
    amountPaid: float = 0.0
    currency: str = "COP"
    exposedAmount: float = 0.0
    paymentIntents: int = 0
    approvedPaymentIntents: int = 0
    paymentMethod: Optional[str] = None
    severity: str = "low"
    reason: Optional[str] = None
    detectedAt: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict:
        return asdict(self)


def severity_of(status: str, delta: int, exposed: float, high_amount: float) -> str:
    """Severidad. delta>=2 o monto alto -> critical; delta==1 -> high."""
    if status == OVER_ISSUED:
        if delta >= 2 or exposed >= high_amount:
            return "critical"
        return "high"
    if status == UNDER_ISSUED:
        return "medium"
    if status == ORPHAN_TICKETS:
        return "medium"
    return "low"


# --- detector -----------------------------------------------------------------


class TicketAuditor:
    """Detector de integridad carts<->tickets.

    Estrategia: el conteo de tickets por referencia se hace con un $group del
    lado del servidor (no se traen los documentos completos a memoria); solo se
    consultan los carritos de las referencias candidatas. El mapa de
    ticketGroupAmount se carga una vez por corrida (proyeccion de 2 campos).
    """

    def __init__(self, uri: Optional[str] = None, db_name: str = DEFAULT_DB,
                 high_amount: float = 1_000_000.0, client: Optional[MongoClient] = None):
        if client is not None:
            self.client = client
        else:
            uri = uri or os.environ.get("MONGODB_URI") or _uri_from_dotenv()
            if not uri:
                raise RuntimeError("MONGODB_URI no encontrado (.env o entorno)")
            self.client = MongoClient(uri, serverSelectionTimeoutMS=20000, connect=False)
        self.db = self.client[db_name]
        self.high_amount = high_amount
        self._acts: dict[str, int] = {}
        self._merchants: dict[str, dict] = {}
        self._events: dict[str, str] = {}

    # -- catalogos (cache por instancia) --

    def _load_acts(self) -> dict[str, int]:
        if not self._acts:
            for a in self.db.acts.find({}, {"ticketGroupAmount": 1}):
                try:
                    n = int(a.get("ticketGroupAmount") or 1)
                except (TypeError, ValueError):
                    n = 1
                self._acts[str(a["_id"])] = n if n > 0 else 1
        return self._acts

    def _load_merchants(self) -> dict[str, dict]:
        if not self._merchants:
            for m in self.db.merchants.find({}, {"merchantRef": 1, "name": 1, "currency": 1}):
                ref = m.get("merchantRef")
                if ref:
                    self._merchants[ref] = {
                        "id": str(m["_id"]),
                        "name": m.get("name"),
                        "currency": m.get("currency") or "COP",
                    }
        return self._merchants

    def event_name(self, event_id: Optional[str]) -> Optional[str]:
        """Titulo del evento, cacheado. Los eventos se consultan de a uno porque
        el universo de eventos afectados por corrida es pequeno."""
        if not event_id:
            return None
        if event_id not in self._events:
            doc = None
            try:
                doc = self.db.events.find_one({"_id": ObjectId(event_id)}, {"title": 1, "name": 1})
            except Exception:
                pass
            self._events[event_id] = (doc or {}).get("title") or (doc or {}).get("name") or None
        return self._events[event_id]

    # -- calculo de esperados --

    def expected_from_cart(self, cart: dict) -> tuple[int, bool]:
        """Devuelve (esperados, hay_act_desconocido).

        Un act desconocido hace la referencia AMBIGUOUS: sin ticketGroupAmount
        no se puede afirmar si sobran tickets. Preferimos un falso negativo
        declarado a un falso positivo silencioso.
        """
        acts = self._load_acts()
        total = 0
        unknown = False
        for detail in cart.get("details") or []:
            for item in detail.get("items") or []:
                try:
                    qty = int(item.get("quantity") or 0)
                except (TypeError, ValueError):
                    qty = 0
                act_ref = item.get("act")
                act_id = str(act_ref.id) if act_ref is not None and hasattr(act_ref, "id") else None
                if act_id and act_id in acts:
                    total += qty * acts[act_id]
                else:
                    unknown = True
                    total += qty
        return total, unknown

    # -- agregacion de tickets --

    def _ticket_groups(self, match: dict) -> Iterator[dict]:
        """$group de tickets por paymentReference, del lado del servidor."""
        pipeline = [
            {"$match": match},
            {"$group": {
                "_id": "$paymentReference",
                "ticketIds": {"$push": "$_id"},
                "total": {"$sum": 1},
                "alive": {"$sum": {"$cond": [
                    {"$in": [{"$toUpper": {"$trim": {"input": {"$ifNull": ["$status", ""]}}}},
                             list(ALIVE_TICKET_STATUS)]}, 1, 0]}},
                "cancelled": {"$sum": {"$cond": [
                    {"$eq": [{"$toUpper": {"$trim": {"input": {"$ifNull": ["$status", ""]}}}},
                             "CANCELLED"]}, 1, 0]}},
                "cartIds": {"$addToSet": "$cartId"},
                "eventIds": {"$addToSet": "$eventId"},
                "merchantRefs": {"$addToSet": "$merchantReference"},
                "dupKeys": {"$push": {
                    "act": "$actId", "ident": "$identification",
                    "ref": "$reference", "st": "$status"}},
                "firstId": {"$min": "$_id"},
                "lastId": {"$max": "$_id"},
            }},
        ]
        return self.db.tickets.aggregate(pipeline, allowDiskUse=True)

    # -- API principal --

    def audit(self, since: Optional[datetime] = None, until: Optional[datetime] = None,
              references: Optional[Iterable[str]] = None,
              merchant_ref: Optional[str] = None,
              cart_status: Optional[str] = None,
              include_ok: bool = False) -> Iterator[AuditResult]:
        """Audita por rango de fechas y/o lista de referencias.

        cart_status filtra por estado del carrito (ej. "APPROVED").
        include_ok=False emite solo hallazgos; True emite tambien los OK.
        """
        match: dict = {}
        if references is not None:
            refs = [r for r in references if r]
            if not refs:
                return
            match["paymentReference"] = {"$in": refs}
        else:
            id_range = {}
            if since:
                id_range["$gte"] = ObjectId.from_datetime(since)
            if until:
                id_range["$lte"] = ObjectId.from_datetime(until)
            if id_range:
                match["_id"] = id_range
        if merchant_ref:
            match["merchantReference"] = merchant_ref

        merchants = self._load_merchants()
        groups = list(self._ticket_groups(match))

        # carritos de las referencias candidatas, en lotes
        refs = [g["_id"] for g in groups if g["_id"]]
        carts: dict[str, list] = defaultdict(list)
        proj = {"reference": 1, "status": 1, "quantity": 1, "details": 1,
                "merchantRef": 1, "total": 1, "dateCreation": 1}
        for chunk in _chunks(refs, 500):
            for c in self.db.carts.find({"reference": {"$in": chunk}}, proj):
                carts[c.get("reference")].append(c)

        for g in groups:
            res = self._classify(g, carts.get(g["_id"]), merchants)
            if res is None:
                continue
            if cart_status and norm_status(res.cartStatus) != norm_status(cart_status):
                continue
            if not include_ok and res.status in (OK, BACKOFFICE_ISSUED, ALL_CANCELLED):
                continue
            yield res

    # -- clasificacion (logica pura, testeable) --

    def _classify(self, group: dict, cart_list: Optional[list],
                  merchants: dict) -> Optional[AuditResult]:
        ref = group["_id"]
        if not ref:
            return None

        first = group["firstId"].generation_time if group.get("firstId") else None
        last = group["lastId"].generation_time if group.get("lastId") else None
        spread = (last - first).total_seconds() if first and last else 0.0
        ticket_ids = [str(x) for x in group.get("ticketIds", [])]
        merchant_ref = next((m for m in group.get("merchantRefs") or [] if m), None)
        minfo = merchants.get(merchant_ref) or {}
        event_id = next((e for e in group.get("eventIds") or [] if e), None)

        res = AuditResult(
            paymentReference=ref,
            status=OK,
            merchantRef=merchant_ref,
            merchantId=minfo.get("id"),
            merchantName=minfo.get("name"),
            currency=minfo.get("currency") or "COP",
            eventId=event_id,
            totalTickets=group.get("total", 0),
            actualTickets=group.get("alive", 0),
            cancelledTickets=group.get("cancelled", 0),
            ticketIds=ticket_ids,
            createdAtFirst=first,
            createdAtLast=last,
            createdAtSpread=spread,
            duplicateGroups=_duplicate_groups(group.get("dupKeys") or []),
        )

        # sin carrito -> backoffice u huerfano
        if not cart_list:
            cart_ids = [c for c in group.get("cartIds") or [] if c]
            if cart_ids and all(is_backoffice(c) for c in cart_ids):
                res.status = BACKOFFICE_ISSUED
                res.reason = "Emitido desde backoffice (cortesia / boleteria fisica)"
                return res
            res.status = ORPHAN_TICKETS
            res.reason = "Tickets sin carrito asociado en ninguna coleccion"
            res.severity = severity_of(ORPHAN_TICKETS, 0, 0, self.high_amount)
            return res

        if len(cart_list) > 1:
            res.status = AMBIGUOUS
            res.reason = f"{len(cart_list)} carritos comparten la misma reference"
            return res

        cart = cart_list[0]
        expected, unknown_act = self.expected_from_cart(cart)
        res.cartId = str(cart["_id"])
        res.cartStatus = cart.get("status")
        res.cartDate = cart.get("dateCreation")
        res.amountPaid = float(cart.get("total") or 0)
        res.expectedTickets = expected
        res.delta = res.actualTickets - expected

        if unknown_act:
            res.status = AMBIGUOUS
            res.reason = "Act sin ticketGroupAmount: no se puede afirmar duplicidad"
            return res

        if res.delta > 0:
            res.status = OVER_ISSUED
            res.exposedAmount = (res.amountPaid / expected * res.delta) if expected else res.amountPaid
            res.reason = f"{res.delta} tickets de más frente a lo comprado"
        elif res.delta < 0:
            # Si se emitieron todos los tickets comprados, el faltante de hoy
            # viene de anulaciones posteriores, no de una emision incompleta.
            # Distinguirlo importa: un reembolso parcial no es un cliente al
            # que le debemos boletas.
            if res.totalTickets >= expected:
                if res.actualTickets == 0:
                    res.status = ALL_CANCELLED
                    res.reason = "Todos los tickets fueron anulados (reembolso legítimo)"
                else:
                    res.status = PARTIALLY_CANCELLED
                    res.reason = (f"Se emitieron los {expected} tickets comprados y luego se "
                                  f"anularon {res.cancelledTickets} (reembolso parcial)")
                return res
            res.status = UNDER_ISSUED
            res.reason = f"Faltan {-res.delta} tickets pagados por el cliente"
        elif res.totalTickets > expected and res.cancelledTickets > 0:
            # Hoy cuadra, pero se emitieron mas tickets de los comprados y el
            # excedente esta anulado. Por conteos esto es indistinguible de una
            # anulacion y reemision legitima: lo que las separa es el tiempo.
            res.correctedExcess = res.totalTickets - expected
            if res.createdAtSpread <= SAME_RUN_SECONDS:
                res.status = OVER_ISSUED_CORREGIDO
                res.reason = (f"Se emitieron {res.correctedExcess} tickets de más y alguien "
                              f"ya los anuló. Hoy el conteo cuadra, pero la doble emisión ocurrió.")
            else:
                res.status = AMBIGUOUS
                res.reason = (f"Hay {res.correctedExcess} tickets anulados por encima de lo "
                              f"comprado, emitidos con horas de diferencia: puede ser una doble "
                              f"emisión corregida o una anulación con reemisión legítima.")
            return res
        else:
            res.status = OK

        res.severity = severity_of(res.status, res.delta, res.exposedAmount, self.high_amount)
        return res

    # -- enriquecimiento opcional (solo para el detalle / export) --

    def enrich_payment(self, res: AuditResult) -> AuditResult:
        """Agrega datos de paymentIntents. Se llama solo sobre hallazgos."""
        intents = list(self.db.paymentIntents.find(
            {"reference": res.paymentReference},
            {"paymentIntentStatus": 1, "paymentMethod": 1}))
        res.paymentIntents = len(intents)
        res.approvedPaymentIntents = sum(
            1 for p in intents if norm_status(p.get("paymentIntentStatus")) == "APPROVED")
        res.paymentMethod = next((p.get("paymentMethod") for p in intents if p.get("paymentMethod")), None)
        res.eventName = self.event_name(res.eventId)
        return res


def _duplicate_groups(dup_keys: list) -> list:
    """Agrupa tickets vigentes identicos (mismo act + cedula) y reporta los
    que aparecen mas de una vez. Tambien detecta ticket.reference repetido,
    que es la firma de una clonacion de documento (no de una emision nueva)."""
    alive = [d for d in dup_keys if norm_status(d.get("st")) in ALIVE_TICKET_STATUS]
    by_identity = Counter((d.get("act"), d.get("ident")) for d in alive)
    by_reference = Counter(d.get("ref") for d in alive if d.get("ref"))
    groups = []
    for (act, _ident), n in by_identity.items():
        if n > 1:
            # La cédula del asistente no se propaga: el resultado viaja a un
            # tablero público. Para el diagnóstico basta con saber que el mismo
            # asistente se repite N veces en la misma localidad.
            groups.append({"type": "same_act_and_id", "actId": act, "count": n})
    for ref, n in by_reference.items():
        if n > 1:
            groups.append({"type": "cloned_ticket_reference", "ticketReference": ref,
                           "count": n})
    return groups


def _chunks(seq: list, size: int) -> Iterator[list]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _uri_from_dotenv(path: str = None) -> Optional[str]:
    path = path or os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), ".env")
    if not os.path.exists(path):
        return None
    for line in open(path):
        line = line.strip()
        if line.startswith("MONGODB_URI="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None
