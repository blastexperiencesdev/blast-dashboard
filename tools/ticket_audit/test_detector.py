"""Tests de la logica de clasificacion del auditor.

No tocan MongoDB: construyen el auditor sin conexion y le inyectan los
catalogos. Cubren cada estado y, sobre todo, los falsos positivos que costaron
descubrir (localidades grupales, cortesias de backoffice, anulaciones).

    python3 -m unittest tools.ticket_audit.test_detector -v
"""
import unittest
from datetime import datetime, timedelta, timezone

from bson import DBRef, ObjectId

from tools.ticket_audit.detector import (
    ALL_CANCELLED, AMBIGUOUS, BACKOFFICE_ISSUED, OK, ORPHAN_TICKETS,
    OVER_ISSUED, OVER_ISSUED_CORREGIDO, PARTIALLY_CANCELLED, UNDER_ISSUED,
    TicketAuditor, norm_status,
)

ACT_SIMPLE = "6a39b97e60a1293707f785e2"   # ticketGroupAmount = 1
ACT_PALCO = "697d1408fe16082beea171ff"    # ticketGroupAmount = 15 (combo x15)
BASE_TS = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)


def oid_at(seconds: int) -> ObjectId:
    return ObjectId.from_datetime(BASE_TS + timedelta(seconds=seconds))


def auditor() -> TicketAuditor:
    """Auditor sin conexion: se saltan __init__ y se inyectan los catalogos."""
    a = TicketAuditor.__new__(TicketAuditor)
    a.high_amount = 1_000_000.0
    a._acts = {ACT_SIMPLE: 1, ACT_PALCO: 15}
    a._merchants = {}
    a._events = {}
    return a


def cart(items, status="APPROVED", total=100000.0, cart_id=None):
    """items: lista de (actId, quantity). actId None simula un act desconocido."""
    return {
        "_id": ObjectId(cart_id) if cart_id else ObjectId(),
        "reference": "AB123456789",
        "status": status,
        "total": total,
        "dateCreation": datetime(2026, 8, 6, 9, 0),
        "merchantRef": "BL001",
        "details": [{"items": [
            {"quantity": q, "act": DBRef("acts", ObjectId(a)) if a else None}
            for a, q in items]}],
    }


def group(n_alive, n_cancelled=0, cart_ids=("6a765019c6074a3be0e05596",),
          act_id=ACT_SIMPLE, ident="80850261", ticket_ref="GN123586614",
          spread_seconds=0, distinct_refs=False):
    """Simula la salida del $group de tickets."""
    total = n_alive + n_cancelled
    keys = []
    for i in range(n_alive):
        keys.append({"act": act_id, "ident": ident,
                     "ref": f"REF{i}" if distinct_refs else ticket_ref,
                     "st": "APPROVED"})
    for i in range(n_cancelled):
        keys.append({"act": act_id, "ident": ident, "ref": f"C{i}", "st": "CANCELLED"})
    return {
        "_id": "AB123456789",
        "ticketIds": [oid_at(i) for i in range(total)],
        "total": total,
        "alive": n_alive,
        "cancelled": n_cancelled,
        "cartIds": list(cart_ids),
        "eventIds": ["686ae592dc26990eb58d4276"],
        "merchantRefs": ["BL001"],
        "dupKeys": keys,
        "firstId": oid_at(0),
        "lastId": oid_at(spread_seconds),
    }


class TestNormalizacion(unittest.TestCase):
    def test_status_con_basura(self):
        # la coleccion trae "CANCELLED\n", minusculas y espacios
        self.assertEqual(norm_status("CANCELLED\n"), "CANCELLED")
        self.assertEqual(norm_status(" approved "), "APPROVED")
        self.assertEqual(norm_status(None), "")
        self.assertEqual(norm_status(123), "")


class TestEsperados(unittest.TestCase):
    def test_item_simple(self):
        n, unknown = auditor().expected_from_cart(cart([(ACT_SIMPLE, 3)]))
        self.assertEqual(n, 3)
        self.assertFalse(unknown)

    def test_localidad_grupal_multiplica(self):
        """1 combo x15 = 15 tickets legitimos. Este es EL falso positivo."""
        n, unknown = auditor().expected_from_cart(cart([(ACT_PALCO, 1)]))
        self.assertEqual(n, 15)
        self.assertFalse(unknown)

    def test_mezcla_de_localidades(self):
        n, _ = auditor().expected_from_cart(cart([(ACT_PALCO, 2), (ACT_SIMPLE, 3)]))
        self.assertEqual(n, 33)

    def test_act_desconocido_se_marca(self):
        n, unknown = auditor().expected_from_cart(cart([(None, 2)]))
        self.assertTrue(unknown)
        self.assertEqual(n, 2)


class TestClasificacion(unittest.TestCase):
    def clasificar(self, g, c):
        return auditor()._classify(g, c, {})

    # -- casos correctos --

    def test_ok(self):
        r = self.clasificar(group(2, distinct_refs=True), [cart([(ACT_SIMPLE, 2)])])
        self.assertEqual(r.status, OK)
        self.assertEqual(r.delta, 0)

    def test_ok_palco_no_es_duplicado(self):
        """15 tickets contra 1 item de combo x15: correcto, no duplicado."""
        r = self.clasificar(group(15, distinct_refs=True), [cart([(ACT_PALCO, 1)])])
        self.assertEqual(r.status, OK)
        self.assertEqual(r.expectedTickets, 15)

    # -- sobre-emision --

    def test_over_issued(self):
        r = self.clasificar(group(30), [cart([(ACT_SIMPLE, 1)], total=4285600.0)])
        self.assertEqual(r.status, OVER_ISSUED)
        self.assertEqual(r.delta, 29)
        self.assertEqual(r.severity, "critical")

    def test_over_issued_delta_uno_es_high(self):
        r = self.clasificar(group(2), [cart([(ACT_SIMPLE, 1)], total=1000.0)])
        self.assertEqual(r.status, OVER_ISSUED)
        self.assertEqual(r.delta, 1)
        self.assertEqual(r.severity, "high")

    def test_over_issued_monto_alto_escala_a_critical(self):
        r = self.clasificar(group(2), [cart([(ACT_SIMPLE, 1)], total=5_000_000.0)])
        self.assertEqual(r.severity, "critical")

    def test_monto_expuesto_se_prorratea(self):
        r = self.clasificar(group(4), [cart([(ACT_SIMPLE, 2)], total=100000.0)])
        self.assertEqual(r.delta, 2)
        self.assertEqual(r.exposedAmount, 100000.0)  # 100000/2 * 2

    def test_detecta_firma_de_clonacion(self):
        r = self.clasificar(group(30), [cart([(ACT_SIMPLE, 1)])])
        clones = [g for g in r.duplicateGroups if g["type"] == "cloned_ticket_reference"]
        self.assertTrue(clones)
        self.assertEqual(clones[0]["count"], 30)

    def test_sin_clonacion_no_reporta_firma(self):
        r = self.clasificar(group(3, distinct_refs=True, ident=None),
                            [cart([(ACT_SIMPLE, 1)])])
        self.assertEqual([g for g in r.duplicateGroups
                          if g["type"] == "cloned_ticket_reference"], [])

    def test_spread_temporal(self):
        r = self.clasificar(group(3, spread_seconds=120), [cart([(ACT_SIMPLE, 1)])])
        self.assertEqual(r.createdAtSpread, 120.0)

    # -- sub-emision --

    def test_under_issued(self):
        r = self.clasificar(group(1, distinct_refs=True), [cart([(ACT_SIMPLE, 3)])])
        self.assertEqual(r.status, UNDER_ISSUED)
        self.assertEqual(r.delta, -2)
        self.assertEqual(r.severity, "medium")

    # -- falsos positivos que NO deben clasificarse como incidencia --

    def test_todos_cancelados_es_reembolso_no_under_issued(self):
        r = self.clasificar(group(0, n_cancelled=4), [cart([(ACT_SIMPLE, 4)])])
        self.assertEqual(r.status, ALL_CANCELLED)

    def test_reembolso_parcial_no_es_sub_emision(self):
        """4 comprados, 4 emitidos, 2 anulados después. Al cliente no le
        debemos nada: pidió que le anularan dos."""
        r = self.clasificar(group(2, n_cancelled=2, distinct_refs=True),
                            [cart([(ACT_SIMPLE, 4)])])
        self.assertEqual(r.status, PARTIALLY_CANCELLED)
        self.assertNotEqual(r.status, UNDER_ISSUED)

    def test_sub_emision_real_es_la_que_nunca_se_emitio(self):
        """3 comprados y solo 1 ticket existe: ese sí es un cliente sin boletas."""
        r = self.clasificar(group(1, distinct_refs=True), [cart([(ACT_SIMPLE, 3)])])
        self.assertEqual(r.status, UNDER_ISSUED)

    def test_backoffice_se_excluye(self):
        """Cortesias y boleteria fisica: sin carrito por diseno."""
        r = self.clasificar(group(10, cart_ids=("BO-GENERATED-AA285535406",)), None)
        self.assertEqual(r.status, BACKOFFICE_ISSUED)

    def test_backoffice_sin_sufijo(self):
        r = self.clasificar(group(3, cart_ids=("BO-GENERATED",)), None)
        self.assertEqual(r.status, BACKOFFICE_ISSUED)

    def test_backoffice_otras_variantes(self):
        """En producción conviven varias formas de marcar una emisión manual.
        Reconocer solo 'BO-GENERATED' dejaba entrar abonos y emisiones a mano."""
        for cid in ("BO-ABONO-GENERATED-OQ611961254", "BO GENERATED 67adfa287e0e5574e13",
                    "b0 GENERATED67b15a691bd95f33de2e", "SDL EVENTOS", "CAPPA RECORDS"):
            r = self.clasificar(group(3, cart_ids=(cid,)), None)
            self.assertEqual(r.status, BACKOFFICE_ISSUED, f"cartId={cid!r}")

    def test_cartid_objectid_sin_carrito_sigue_siendo_huerfano(self):
        r = self.clasificar(group(2, cart_ids=("6a765019c6074a3be0e05596",)), None)
        self.assertEqual(r.status, ORPHAN_TICKETS)

    # -- sobre-emision que alguien ya limpio a mano --

    def test_sobre_emision_corregida_a_mano(self):
        """Caso MK102342013: 3 comprados, 6 emitidos en el mismo segundo, 3
        anulados desde el backoffice. Hoy cuadra, pero la doble emision pasó."""
        r = self.clasificar(group(3, n_cancelled=3, distinct_refs=True),
                            [cart([(ACT_SIMPLE, 3)])])
        self.assertEqual(r.status, OVER_ISSUED_CORREGIDO)
        self.assertEqual(r.correctedExcess, 3)
        self.assertEqual(r.delta, 0)
        self.assertEqual(r.actualTickets, 3)

    def test_corregida_no_se_cuenta_como_duplicado_vivo(self):
        r = self.clasificar(group(2, n_cancelled=2, distinct_refs=True),
                            [cart([(ACT_SIMPLE, 2)])])
        self.assertEqual(r.status, OVER_ISSUED_CORREGIDO)
        self.assertNotEqual(r.status, OVER_ISSUED)
        self.assertEqual(r.exposedAmount, 0.0)  # no hay boleta viva de mas

    def test_anulacion_con_reemision_tardia_es_ambigua(self):
        """Mismos conteos, pero los tickets nacen con horas de diferencia: por
        conteos no se distingue de una reemision legitima."""
        r = self.clasificar(group(3, n_cancelled=3, distinct_refs=True, spread_seconds=7200),
                            [cart([(ACT_SIMPLE, 3)])])
        self.assertEqual(r.status, AMBIGUOUS)
        self.assertEqual(r.correctedExcess, 3)

    def test_anulacion_sin_exceso_sigue_siendo_ok(self):
        """2 comprados, 2 vigentes, 2 anulados = 4 totales > 2: sí hubo exceso.
        Pero si solo hay 1 anulado y 2 vigentes contra 3 comprados, no."""
        r = self.clasificar(group(3, n_cancelled=0, distinct_refs=True),
                            [cart([(ACT_SIMPLE, 3)])])
        self.assertEqual(r.status, OK)
        self.assertEqual(r.correctedExcess, 0)

    # -- casos que no se pueden afirmar --

    def test_huerfano_real(self):
        r = self.clasificar(group(2, cart_ids=("6a765019c6074a3be0e05596",)), None)
        self.assertEqual(r.status, ORPHAN_TICKETS)

    def test_varios_carritos_misma_referencia_es_ambiguo(self):
        r = self.clasificar(group(5), [cart([(ACT_SIMPLE, 1)]), cart([(ACT_SIMPLE, 4)])])
        self.assertEqual(r.status, AMBIGUOUS)
        self.assertIn("carritos", r.reason)

    def test_act_desconocido_es_ambiguo_no_duplicado(self):
        """Preferimos un falso negativo declarado a un falso positivo silencioso."""
        r = self.clasificar(group(10), [cart([(None, 1)])])
        self.assertEqual(r.status, AMBIGUOUS)
        self.assertNotEqual(r.status, OVER_ISSUED)

    # -- metadata --

    def test_conserva_ids_y_montos(self):
        c = cart([(ACT_SIMPLE, 1)], total=4285600.0)
        r = self.clasificar(group(3), [c])
        self.assertEqual(r.cartId, str(c["_id"]))
        self.assertEqual(r.amountPaid, 4285600.0)
        self.assertEqual(len(r.ticketIds), 3)
        self.assertEqual(r.cartStatus, "APPROVED")

    def test_referencia_vacia_se_ignora(self):
        g = group(2)
        g["_id"] = None
        self.assertIsNone(self.clasificar(g, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
