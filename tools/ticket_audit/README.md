# Auditoría de integridad Carts ↔ Tickets

Detecta referencias de pago donde la cantidad de tickets emitidos no coincide con lo que
el carrito compró. Nace de un caso real: en agosto de 2026 se emitieron más de 2.500
boletas de más sobre compras web.

**La herramienta es de solo lectura sobre la data de negocio.** No borra, no modifica y no
corrige tickets. Lo único que escribe es su propio caché (`audit_cache.json`) y las
resoluciones del dashboard. Corregir la data es siempre una decisión humana.

## Piezas

| Archivo | Qué hace |
|---|---|
| `detector.py` | Motor de detección. Única fuente de la lógica de clasificación. |
| `backfill.py` | Barrido histórico por CLI + genera el caché del dashboard. |
| `export_excel.py` | Reporte en Excel de los casos de sobre-emisión. |
| `alerta.py` | Chequeo periódico + alerta por WhatsApp. Lo corren los workflows. |
| `export_merchant.py` | Conciliación por evento para un merchant. |
| `export_ajuste_tickets.py` | Tabla boleta por boleta del servicio e IVA sin grabar. |
| `test_detector.py` | 31 tests de la clasificación, con fixtures de cada caso y de los falsos positivos. |

La vista vive en `/auditoria`. El dashboard **no calcula nada**: lee el caché desde Vercel
KV, y si no hay, cae al archivo local. Así el barrido, el Excel y la pantalla no pueden
divergir.

## Cómo correr el barrido

```bash
python3 tools/ticket_audit/backfill.py --days 90 --cache
```

Toma unos 40 segundos para 90 días. Refresca lo que ve el dashboard. Opciones:

| Flag | Para qué |
|---|---|
| `--from` / `--to` | Rango explícito (`YYYY-MM-DD`). Por defecto, `--days 90`. |
| `--merchant BL001` | Filtrar un merchant. Por defecto, todos. |
| `--cart-status APPROVED` | Solo carritos aprobados. |
| `--batch-days 15` | Tamaño del lote. Procesa por ventanas, no carga los 90 días en RAM. |
| `--sleep 2` | Pausa entre lotes, si se quiere ser aún más suave con producción. |
| `--export csv\|json` | Exporta los hallazgos a archivo. |
| `--dry-run` | Reporta sin escribir nada. |
| `--cache` | Refresca `audit_cache.json`, el respaldo local del tablero. |

Para el Excel:

```bash
python3 tools/ticket_audit/export_excel.py --days 90
```

Tests:

```bash
python3 -m unittest tools.ticket_audit.test_detector -v
```

## Cómo leer cada estado

| Estado | Significa | Qué hacer |
|---|---|---|
| `OVER_ISSUED` | Hay más tickets vigentes que los comprados. | El caso reportado. Revisar en el dashboard. |
| `OVER_ISSUED_CORREGIDO` | Se emitieron de más y alguien ya anuló el excedente. Hoy cuadra. | Sin riesgo en puerta, pero **prueba que el bug disparó**. No ignorar. |
| `UNDER_ISSUED` | El cliente pagó y nunca se emitieron todos sus tickets. | Igual de grave, pero para el cliente. Contactarlo. |
| `PARTIALLY_CANCELLED` | Se emitieron todos y luego se anularon algunos. | Reembolso parcial legítimo. Se excluye. |
| `ORPHAN_TICKETS` | Tickets cuya referencia no existe en ningún carrito. | Revisión manual: puede ser data vieja o migrada. |
| `AMBIGUOUS` | Varios carritos con la misma referencia, un act sin `ticketGroupAmount`, o excedente anulado horas después. | No se puede afirmar duplicidad. Revisión manual. |
| `BACKOFFICE_ISSUED` | Emitido desde el panel (cortesías, boletería física). | Se excluye. No es compra web. |
| `ALL_CANCELLED` | Todos los tickets fueron anulados. | Reembolso legítimo. Se excluye. |
| `OK` | Cuadra. | Nada. |

`OVER_ISSUED_CORREGIDO` existe porque la auditoría mide el **estado actual**, no si el bug
ocurrió. Sin ese estado, cada vez que alguien limpia duplicados a mano el caso desaparece del
radar y se pierde la señal de que el problema sigue vivo.

Solo se marca así cuando **todos los tickets nacieron en el mismo instante** (≤120 s, o sea
una doble ejecución). Si el excedente se emitió horas después, por conteos es indistinguible
de una anulación con reemisión legítima, y sale como `AMBIGUOUS`.

> **Ojo:** el estado solo sobrevive si los duplicados se **anulan**. Si se **borran** de la
> colección, la evidencia desaparece por completo y la referencia vuelve a `OK` — no hay
> forma de detectarlo después. Pasó el 7 de agosto de 2026 con 30 referencias de ES029.

**Preferimos un falso negativo declarado a un falso positivo silencioso.** Si el esquema no
alcanza para afirmar que algo es duplicado, sale como `AMBIGUOUS`, no como incidencia.

## Las reglas de negocio que hacen bien la cuenta

Estas tres cosas no están documentadas en el backend y son las que separan un hallazgo real
de un falso positivo:

1. **`acts.ticketGroupAmount` es un multiplicador.** Una localidad grupal (palco, "COMBO VIP
   X15 PERSONAS") se configura como **un** act que emite N tickets. Un carrito con 1 ítem de
   un combo x15 debe tener 15 tickets. Ignorar esto inflaba los hallazgos un 32%.

   ```
   esperados = Σ (item.quantity × act.ticketGroupAmount)
   ```

2. **Los tickets de backoffice no tienen carrito, por diseño.** Son cortesías, boletería
   física y abonos: el 22,3% de las referencias. Contarlos sería puro ruido.

   No se reconocen por un prefijo sino por la **forma del `cartId`**: una compra web
   siempre deja ahí el ObjectId del carrito, y cualquier otra cosa la escribió el panel.
   En producción conviven `BO-GENERATED-`, `BO-ABONO-GENERATED-`, `BO GENERATED `,
   `b0 GENERATED` y hasta el nombre de la productora (`SDL EVENTOS`). Reconocer solo el
   primer prefijo dejaba entrar 30 emisiones manuales al tablero.

3. **La anulación sobrescribe el ticket en sitio.** No hay borrado ni flag: el proceso pone
   `status`, `label` y `typeEntrance` en `"CANCELLED"` y `price` en 0. Un carrito aprobado
   cuyos tickets están todos anulados es un reembolso, no una sub-emisión.

Además, `tickets.status` trae basura histórica (`"CANCELLED\n"` con salto de línea,
`"VALIDATES"`, y un registro con un email en el campo). Siempre se normaliza con
`strip()` + `upper()`.

## Cómo se relacionan las colecciones

```
carts.reference  ==  tickets.paymentReference  ==  paymentIntents.reference
str(carts._id)   ==  tickets.cartId            ==  paymentIntents.cartId
```

En 90 días: cero tickets sin `paymentReference`, cero sin `cartId`, cero referencias
repetidas entre carritos. La referencia es una llave limpia.

Ojo con las fechas: `dateCreation` y `date` están en hora local de Colombia (naive); el
timestamp embebido en el `ObjectId` sí es UTC real, y es el que usa la herramienta.

## Índices — pendiente

Hoy **no existe ningún índice sobre los campos de referencia**:

```
tickets:        _id_, eventId_1, eventId_1_status_1
carts:          _id_
paymentIntents: _id_
```

Toda consulta por referencia es un COLLSCAN sobre 195k–222k documentos. Recomendado:

```javascript
db.tickets.createIndex({ paymentReference: 1 })
db.carts.createIndex({ reference: 1 })
db.paymentIntents.createIndex({ reference: 1 })
```

Y el que además **previene físicamente** la doble emisión (ver el informe de causa raíz):

```javascript
db.tickets.createIndex({ reference: 1 }, { unique: true })
```

Antes de crearlo hay que limpiar los duplicados existentes, porque si no, falla. Es una
decisión de negocio, no técnica: implica decidir qué boleta sobrevive.

## Marcar falsos positivos

En `/auditoria`, clic en una fila → "Falso positivo" o "Marcar resuelto", con nota opcional.
Eso **solo escribe el registro de auditoría**: no toca tickets, carritos ni pagos. Las
resoluciones se guardan en Vercel KV si está configurado (`KV_REST_API_URL` /
`KV_REST_API_TOKEN`) para que todo el equipo vea el mismo estado; en local caen en
`audit_resolutions.json`.

Para devolver una incidencia a la lista de abiertas: abrirla y darle "Reabrir".

## Configuración

`MONGODB_URI` en `.env` (usuario de solo lectura). Nada de credenciales en el código.

## Alertas automáticas

Dos workflows de GitHub Actions cubren lo que no se puede enganchar en la emisión:

| Workflow | Cada | Qué hace |
|---|---|---|
| `auditoria-alerta.yml` | 15 min | Barre las últimas 6 h y avisa por WhatsApp los casos nuevos |
| `auditoria-cache.yml` | 1 hora | Recalcula los 180 días y deja el resultado en Vercel KV |

Los cron solo corren desde la rama por defecto: hasta que el cambio no esté en
`main`, no se dispara nada.

### Secretos que hay que crear

En *Settings > Secrets and variables > Actions*:

| Secreto | De dónde sale |
|---|---|
| `MONGODB_URI` | el mismo de Vercel |
| `KV_REST_API_URL` | Vercel > Storage > el KV del proyecto |
| `KV_REST_API_TOKEN` | igual |
| `WATI_API_ENDPOINT` | panel de WATI |
| `WATI_TOKEN` | panel de WATI |
| `AUDIT_ALERT_NUMBERS` | números de los admin, separados por coma (`573001112233,573009998877`) |

Variables opcionales (misma pantalla, pestaña *Variables*): `AUDIT_ALERT_TEMPLATE`
(default `alerta_auditoria`), `AUDIT_ALERT_MAX_HORA` (default 6),
`AUDIT_ALERT_ENABLED` (poner en `0` apaga los envíos sin desplegar) y
`AUDIT_DASHBOARD_URL`.

### La plantilla de WATI

WhatsApp no deja mandar texto libre en frío, así que la alerta usa una plantilla
aprobada por Meta con **6 parámetros**, en este orden:

1. merchant · 2. payment reference · 3. compradas vs emitidas · 4. monto
5. tipo de caso y evento · 6. enlace al tablero

Si la plantilla no existe o no está aprobada, el envío falla pero **la incidencia
igual queda en el tablero**: la notificación nunca es la única fuente de verdad.

### Cómo no ahogar a nadie en WhatsApp

Hay tope de mensajes por hora (`AUDIT_ALERT_MAX_HORA`, 6 por defecto). Si en una
corrida aparecen más casos que envíos disponibles, se manda **un solo mensaje
resumen** en vez de uno por caso. El incidente del 6 de agosto habría generado 85
mensajes; con esto genera uno.

Cada referencia se avisa **una sola vez**: el estado de lo ya notificado vive en
KV y se limpia a los 30 días.

## Limitaciones conocidas

- **No hay hook en el momento de emitir.** El repositorio Java del backend es otro, así
  que no se pudo enganchar la emisión. El cron de 15 minutos cubre ese hueco: en el peor
  caso el aviso llega con ese retraso.
- **El tablero es público.** Se sirve sin autenticación, por eso el resultado no lleva
  datos del comprador y la API los vuelve a filtrar al leer. Si algún día se agregan
  campos nuevos, revisar que no traigan nombre, cédula, correo ni teléfono.
- **Este repositorio es público.** La data de negocio (referencias de pago, montos por
  merchant) no puede vivir en git: va a Vercel KV y está en `.gitignore`.
- El caché se regenera completo en cada corrida; no hay historial de versiones del barrido.
