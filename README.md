# Blast Tickets Dashboard

Dashboard en tiempo real para Blast Tickets con métricas por merchant: órdenes, ingresos, funnel de conversión, análisis geográfico y comportamiento web.

**Stack:** FastAPI + Chart.js + MongoDB + Microsoft Clarity

## Inicio rápido local

```bash
# Copia .env.example a .env y completa las variables
cp .env.example .env

# Instala dependencias
pip install -r requirements.txt

# Arranca el servidor (puerto 8899)
python3 run.py
```

Visita `http://localhost:8899`

## Variables de entorno

```
MONGODB_URI=mongodb+srv://usuario:contraseña@cluster.mongodb.net/blast-prod
CLARITY_API_TOKEN=  # Opcional; sin él usa caché local
```

## Despliegue en Vercel

### 1. Inicia un repositorio Git

```bash
git init
git add .
git commit -m "Initial commit: Blast Dashboard"
```

### 2. Sube a GitHub

```bash
git remote add origin https://github.com/tu-usuario/blast-dashboard
git branch -M main
git push -u origin main
```

### 3. Conecta a Vercel

- Ve a [vercel.com/new](https://vercel.com/new)
- Selecciona "Import Git Repository"
- Busca y selecciona tu repositorio
- Vercel detectará automáticamente que es un proyecto Python

### 4. Configura variables de entorno en Vercel

En el dashboard de Vercel del proyecto:
- **Settings** → **Environment Variables**
- Añade:
  - `MONGODB_URI`: Tu conexión a MongoDB Atlas (usuario solo lectura)
  - `CLARITY_API_TOKEN` (opcional): Token de la API de Clarity

### 5. Deploy

Vercel desplegará automáticamente. Una vez que finalice:
- Tu dashboard estará disponible en una URL como `https://blast-dashboard.vercel.app`

## Estructura

```
/api/index.py           → Entrada para Vercel (wrapper de FastAPI)
/server.py              → Lógica del servidor (endpoints, queries)
/static/index.html      → Frontend (HTML + Chart.js)
/requirements.txt       → Dependencias de Python
/vercel.json            → Configuración de Vercel
/*.json                 → Caché local (Clarity, UTM, geo, tech, eventos)
```

## Notas de producción

1. **Caché de Clarity**: Los archivos `.json` son cachés locales (sesiones, UTMs, geografía). Sin `CLARITY_API_TOKEN`, se sirven estáticos. Con token, se refrescan automáticamente (límite 10 req/día).

2. **Merchants excluidos**: VIPPASS (VPP028) está marcado como churn y aparece excluido del dashboard por decisión del usuario.

3. **Índices en MongoDB**: Las queries usan los índices nativos de ObjectId. Para producción, se recomienda añadir índices en:
   - `carts`: `merchantRef`, `dateCreation`
   - `paymentIntents`: `merchantReference`

4. **Timeouts**: MongoDB usa `serverSelectionTimeoutMS=15000` (15s). Vercel puede tener límites de ejecución; asegúrate de que tus queries devuelven en menos de 60s.

## Contacto y soporte

- Repo: [tu-usuario/blast-dashboard](https://github.com/tu-usuario/blast-dashboard)
- Email: blastentertainment2603@gmail.com
