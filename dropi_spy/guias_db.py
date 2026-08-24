"""SQLite para trackear el historial de guías y detectar incidencias nuevas
desde la última revisión (diff contra el snapshot anterior)."""
import sqlite3
from pathlib import Path

from . import db_conn
from datetime import datetime, timedelta, timezone

DB_PATH = Path(__file__).resolve().parent / "data" / "guias.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS guides (
    order_id INTEGER PRIMARY KEY,
    guide TEXT,
    carrier TEXT,
    customer TEXT,
    phone TEXT,
    city TEXT,
    dropi_status TEXT,
    last_movement_text TEXT,
    last_movement_at TEXT,
    days_since_movement INTEGER,
    has_incident INTEGER,
    first_seen_at TEXT,
    last_checked_at TEXT
);
CREATE TABLE IF NOT EXISTS discarded_guides (
    order_id INTEGER PRIMARY KEY,
    guide TEXT,
    carrier TEXT,
    customer TEXT,
    phone TEXT,
    city TEXT,
    dropi_status TEXT,
    discarded_at TEXT
);
CREATE TABLE IF NOT EXISTS contact_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    guide TEXT,
    customer TEXT,
    phone TEXT,
    agent TEXT,
    method TEXT,
    result TEXT,
    notes TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS usuarios (
    usuario TEXT PRIMARY KEY,
    nombre TEXT,
    clave_hash TEXT,
    salt TEXT,
    rol TEXT,
    activo INTEGER DEFAULT 1,
    creado_en TEXT,
    ultimo_ingreso TEXT
);
"""

# Qué puede hacer cada rol. "dueño" ve y toca todo; "asesor" solo gestiona y
# anota. Los permisos del asesor se pueden ampliar desde la app sin tocar código.
ROLES = {
    "dueno": {
        "label": "Dueño",
        "permisos": {"gestionar", "anotar", "ver_kpis", "ver_bitacora",
                     "configurar_reglas", "gestionar_accesos", "gestionar_usuarios"},
    },
    "asesor": {
        "label": "Asesor",
        "permisos": {"gestionar", "anotar"},
    },
}

DEFAULT_SETTINGS = {
    "umbral_bajo": "49",
    "umbral_medio": "60",
    "monto_flete_anticipado": "",
    "banco": "",
    "cuenta_bancaria": "",
    "zonas_excluidas": "",
    "excepcion_pedidos_min": "10",
    "excepcion_devolucion_max": "10",
    "asesor_ve_kpis": "0",
    "asesor_ve_bitacora": "0",
}

RESULT_LABELS = {
    "confirmado": "Confirmado",
    "no_contesta": "No contesta",
    "reprogramado": "Reprogramado",
    "cancelado": "Cliente cancela",
    "novedad_resuelta": "Novedad resuelta",
    "abono_solicitado": "Se pidió abono anticipado",
    "otro": "Otro",
}

METHOD_LABELS = {
    "llamada": "Llamada",
    "whatsapp": "WhatsApp",
    "lucidbot": "LucidBot (chat)",
    "otro": "Otro",
}


NEW_COLUMNS = {
    "category": "TEXT",
    "label_es": "TEXT",
    "action_es": "TEXT",
    "severity": "TEXT",
}


def get_conn():
    """Abre la conexión (SQLite local o Postgres compartido) y deja el esquema
    al día. Las migraciones se hacen de forma distinta en cada motor."""
    if not db_conn.ES_POSTGRES:
        db_conn.SQLITE_PATH = DB_PATH  # respetar la ruta que fije quien lo use
    conn = db_conn.conectar()
    conn.executescript(SCHEMA)

    if db_conn.ES_POSTGRES:
        # Postgres sí tiene IF NOT EXISTS para columnas.
        for col, tipo in NEW_COLUMNS.items():
            conn.execute(f"ALTER TABLE guides ADD COLUMN IF NOT EXISTS {col} {tipo}")
        conn.execute("ALTER TABLE discarded_guides ADD COLUMN IF NOT EXISTS phone TEXT")
    else:
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(guides)").fetchall()}
        for col, col_type in NEW_COLUMNS.items():
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE guides ADD COLUMN {col} {col_type}")

        discarded_cols = {row["name"] for row in conn.execute("PRAGMA table_info(discarded_guides)").fetchall()}
        if "phone" not in discarded_cols:
            conn.execute("ALTER TABLE discarded_guides ADD COLUMN phone TEXT")

    conn.commit()
    return conn


def log_discarded(orders: list) -> int:
    """Guarda en el historial las órdenes descartadas por estar ENTREGADAS o
    CANCELADAS. No duplica (order_id es clave). Devuelve cuántas nuevas."""
    if not orders:
        return 0
    conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    nuevas = 0
    for o in orders:
        cur = conn.execute(
            """INSERT OR IGNORE INTO discarded_guides
               (order_id, guide, carrier, customer, phone, city, dropi_status, discarded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                o.get("id"), o.get("shipping_guide"), o.get("shipping_company"),
                f"{o.get('name','')} {o.get('surname','')}".strip(),
                o.get("phone"), o.get("city"), o.get("status"), now,
            ),
        )
        nuevas += cur.rowcount
    conn.commit()
    conn.close()
    return nuevas


def save_snapshot(guides: list) -> dict:
    """Guarda el estado actual de las guías y devuelve qué cambió desde
    la última vez: incidencias nuevas y guías que ya no aparecen (resueltas)."""
    conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()

    previous = {
        row["order_id"]: dict(row)
        for row in conn.execute("SELECT * FROM guides").fetchall()
    }

    new_incidents = []
    current_ids = set()

    for g in guides:
        order_id = g["order_id"]
        current_ids.add(order_id)
        prev = previous.get(order_id)
        was_urgent = (prev.get("severity") == "urgente") if prev else False
        first_seen_at = prev["first_seen_at"] if prev else now

        if g["severity"] == "urgente" and not was_urgent:
            new_incidents.append(g)

        conn.execute(
            """INSERT INTO guides
               (order_id, guide, carrier, customer, phone, city, dropi_status,
                last_movement_text, last_movement_at, days_since_movement,
                has_incident, category, label_es, action_es, severity,
                first_seen_at, last_checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(order_id) DO UPDATE SET
                guide=excluded.guide, carrier=excluded.carrier,
                customer=excluded.customer, phone=excluded.phone, city=excluded.city,
                dropi_status=excluded.dropi_status,
                last_movement_text=excluded.last_movement_text,
                last_movement_at=excluded.last_movement_at,
                days_since_movement=excluded.days_since_movement,
                has_incident=excluded.has_incident,
                category=excluded.category, label_es=excluded.label_es,
                action_es=excluded.action_es, severity=excluded.severity,
                last_checked_at=excluded.last_checked_at
            """,
            (
                order_id, g["guide"], g["carrier"], g["customer"], g["phone"],
                g["city"], g["dropi_status"], g["last_movement_text"],
                g["last_movement_at"], g["days_since_movement"],
                int(g["has_incident"]), g["category"], g["label_es"],
                g["action_es"], g["severity"], first_seen_at, now,
            ),
        )

    resolved_ids = set(previous.keys()) - current_ids
    conn.commit()
    conn.close()

    return {
        "new_incidents": new_incidents,
        "resolved_order_ids": list(resolved_ids),
    }


def log_contact(order_id, guide, customer, phone, agent, method, result, notes=""):
    """Registra un intento de gestión (llamada/whatsapp/chat) sobre una guía."""
    conn = get_conn()
    conn.execute(
        """INSERT INTO contact_log (order_id, guide, customer, phone, agent, method, result, notes, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (order_id, guide, customer, phone, agent, method, result, notes,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def get_contact_history(order_id):
    """Devuelve todos los intentos de gestión registrados para una guía, más reciente primero."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM contact_log WHERE order_id = ? ORDER BY created_at DESC",
        (order_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_customer_history(phone):
    """Historial local (guías activas + descartadas) para un teléfono dado,
    para estimar a ojo qué tanto entrega/devuelve ese cliente. Se enriquece
    solo con lo que este tablero ha visto — mejora con el uso diario."""
    if not phone:
        return {"activas": [], "descartadas": []}
    conn = get_conn()
    activas = conn.execute(
        "SELECT order_id, guide, dropi_status, last_checked_at FROM guides WHERE phone = ? ORDER BY last_checked_at DESC",
        (phone,),
    ).fetchall()
    descartadas = conn.execute(
        "SELECT order_id, guide, dropi_status, discarded_at FROM discarded_guides WHERE phone = ? ORDER BY discarded_at DESC",
        (phone,),
    ).fetchall()
    conn.close()
    return {"activas": [dict(r) for r in activas], "descartadas": [dict(r) for r in descartadas]}


def get_kpi_summary(days=30):
    """Resumen de gestiones por agente en la ventana dada: total, por resultado,
    y % de confirmación sobre pedidos que estaban pendientes de confirmación."""
    conn = get_conn()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT * FROM contact_log WHERE created_at >= ? ORDER BY created_at DESC",
        (since,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_settings():
    """Reglas de negocio configurables (umbrales de devolución, flete anticipado,
    cuenta bancaria, zonas excluidas). Se guardan en SQLite para que sobrevivan
    entre sesiones del tablero."""
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    values = dict(DEFAULT_SETTINGS)
    values.update({r["key"]: r["value"] for r in rows})
    return values


def save_settings(values: dict):
    conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    for key, value in values.items():
        conn.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, str(value)),
        )
    conn.commit()
    conn.close()


_DEVOLUCION_STATUSES = {"DEVOLUCION", "DEVOLUCIÓN", "RECHAZADO"}


def calcular_perfil_cliente(phone, exclude_order_id=None):
    """Calcula, con el historial local de este teléfono, cuántos pedidos
    anteriores se entregaron vs. se devolvieron. Solo cuenta pedidos que ya
    tuvieron un desenlace final (entregado o devuelto/rechazado) — un pedido
    cancelado antes de despachar no cuenta porque nunca llegó a repartirse.

    Devuelve None si no hay al menos 2 pedidos con desenlace conocido: con
    tan poca info no vale la pena aplicar una regla automática."""
    hist = get_customer_history(phone)
    finalizados = [
        r for r in (hist["activas"] + hist["descartadas"])
        if exclude_order_id is None or r["order_id"] != exclude_order_id
    ]
    entregados = sum(1 for r in finalizados if (r["dropi_status"] or "").strip().upper() == "ENTREGADO")
    devueltos = sum(1 for r in finalizados if (r["dropi_status"] or "").strip().upper() in _DEVOLUCION_STATUSES)
    total = entregados + devueltos
    if total < 2:
        return None
    return {
        "total_finalizados": total,
        "entregados": entregados,
        "devueltos": devueltos,
        "pct_devolucion": round(100 * devueltos / total, 1),
        "total_pedidos_historicos": len(finalizados),
    }


def evaluar_regla_confirmacion(phone, city=None, state=None, exclude_order_id=None):
    """Aplica las reglas configuradas en Configuración a un cliente dado y
    devuelve una recomendación lista para mostrar/usar: qué hacer y por qué,
    más el mensaje de abono ya armado si corresponde."""
    settings = get_settings()
    perfil = calcular_perfil_cliente(phone, exclude_order_id=exclude_order_id)

    zonas = [z.strip().upper() for z in settings["zonas_excluidas"].split(",") if z.strip()]
    zona_excluida = None
    for campo in (city, state):
        if campo and campo.strip().upper() in zonas:
            zona_excluida = campo
            break

    resultado = {
        "perfil": perfil,
        "zona_excluida": zona_excluida,
        "accion": None,
        "motivo": "",
        "mensaje_abono": None,
        "es_excepcion": False,
    }

    if zona_excluida:
        resultado["accion"] = "zona_excluida"
        resultado["motivo"] = f"{zona_excluida} está en la lista de zonas sin cobertura configurada."
        return resultado

    if perfil is None:
        resultado["accion"] = "sin_datos"
        resultado["motivo"] = "Menos de 2 pedidos con desenlace conocido en el historial local. Decide con criterio propio o pide el dato en Dropi."
        return resultado

    pct = perfil["pct_devolucion"]
    try:
        umbral_bajo = float(settings["umbral_bajo"])
        umbral_medio = float(settings["umbral_medio"])
        excepcion_pedidos_min = float(settings["excepcion_pedidos_min"])
        excepcion_dev_max = float(settings["excepcion_devolucion_max"])
    except (TypeError, ValueError):
        umbral_bajo, umbral_medio = 49.0, 60.0
        excepcion_pedidos_min, excepcion_dev_max = 10.0, 10.0

    if perfil["total_pedidos_historicos"] >= excepcion_pedidos_min and pct <= excepcion_dev_max:
        resultado["es_excepcion"] = True
        resultado["accion"] = "escalar_supervisor"
        resultado["motivo"] = (
            f"{perfil['total_pedidos_historicos']} pedidos históricos con solo {pct}% de devolución. "
            "Si no confirma, no se cancela por criterio propio: escalar a supervisor para autorización."
        )
        return resultado

    if pct <= umbral_bajo:
        resultado["accion"] = "despachar_directo"
        resultado["motivo"] = f"{pct}% de devolución histórica (≤{umbral_bajo:g}%) — despachar sin pedir nada por adelantado."
    elif pct <= umbral_medio:
        resultado["accion"] = "llamar_sin_flete"
        resultado["motivo"] = f"{pct}% de devolución histórica ({umbral_bajo:g}–{umbral_medio:g}%) — llamar para confirmar, sin pedir el flete por adelantado."
    else:
        resultado["accion"] = "pedir_flete_anticipado"
        resultado["motivo"] = f"{pct}% de devolución histórica (>{umbral_medio:g}%) — solo despachar con flete anticipado pagado."
        if settings["monto_flete_anticipado"] and settings["cuenta_bancaria"]:
            resultado["mensaje_abono"] = (
                "Hola, hemos revisado tu historial de entregas y para poder continuar con el envío de tu "
                "pedido necesitamos un abono previo del flete de Q{monto}. Este monto se descuenta del valor "
                "final del producto y nos permite asegurar el despacho de tu orden. Si estás de acuerdo, te "
                "compartimos los datos para hacer el abono: {banco} — cuenta {cuenta}."
            ).format(monto=settings["monto_flete_anticipado"], banco=settings["banco"] or "(banco sin configurar)",
                      cuenta=settings["cuenta_bancaria"])

    return resultado

def get_last_contacts(order_ids):
    """Última gestión registrada de cada guía, en una sola consulta, para
    poder mostrar la trazabilidad en la tabla sin abrir una por una."""
    if not order_ids:
        return {}
    conn = get_conn()
    marcas = ",".join("?" * len(order_ids))
    filas = conn.execute(
        f"""SELECT c.* FROM contact_log c
            JOIN (SELECT order_id, MAX(created_at) AS m
                  FROM contact_log WHERE order_id IN ({marcas})
                  GROUP BY order_id) u
              ON u.order_id = c.order_id AND u.m = c.created_at
            WHERE c.order_id IN ({marcas})""",
        list(order_ids) + list(order_ids),
    ).fetchall()
    conn.close()
    return {f["order_id"]: dict(f) for f in filas}


def get_all_contacts(days=None):
    """Bitácora completa de gestiones, más reciente primero."""
    conn = get_conn()
    if days:
        desde = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        filas = conn.execute(
            "SELECT * FROM contact_log WHERE created_at >= ? ORDER BY created_at DESC",
            (desde,),
        ).fetchall()
    else:
        filas = conn.execute("SELECT * FROM contact_log ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(f) for f in filas]


def contar_gestiones_por_guia():
    conn = get_conn()
    filas = conn.execute(
        "SELECT order_id, COUNT(*) n FROM contact_log GROUP BY order_id"
    ).fetchall()
    conn.close()
    return {f["order_id"]: f["n"] for f in filas}

# --------------------------- Usuarios y acceso ---------------------------
import hashlib
import os as _os


def _hash_clave(clave: str, salt: bytes) -> str:
    """PBKDF2 con 200k iteraciones: la clave nunca se guarda en texto plano."""
    return hashlib.pbkdf2_hmac("sha256", clave.encode(), salt, 200_000).hex()


def crear_usuario(usuario: str, nombre: str, clave: str, rol: str = "asesor"):
    usuario = (usuario or "").strip().lower()
    if not usuario or not clave:
        raise ValueError("Usuario y clave son obligatorios.")
    if len(clave) < 6:
        raise ValueError("La clave debe tener al menos 6 caracteres.")
    if rol not in ROLES:
        raise ValueError(f"Rol desconocido: {rol}")

    conn = get_conn()
    existe = conn.execute("SELECT 1 FROM usuarios WHERE usuario = ?", (usuario,)).fetchone()
    if existe:
        conn.close()
        raise ValueError(f"El usuario '{usuario}' ya existe.")
    salt = _os.urandom(16)
    conn.execute(
        """INSERT INTO usuarios (usuario, nombre, clave_hash, salt, rol, activo, creado_en)
           VALUES (?, ?, ?, ?, ?, 1, ?)""",
        (usuario, (nombre or usuario).strip(), _hash_clave(clave, salt), salt.hex(),
         rol, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def verificar_usuario(usuario: str, clave: str):
    """Devuelve el usuario si la clave es correcta y está activo; si no, None."""
    conn = get_conn()
    f = conn.execute("SELECT * FROM usuarios WHERE usuario = ?",
                     ((usuario or "").strip().lower(),)).fetchone()
    if not f or not f["activo"]:
        conn.close()
        return None
    ok = _hash_clave(clave, bytes.fromhex(f["salt"])) == f["clave_hash"]
    if ok:
        conn.execute("UPDATE usuarios SET ultimo_ingreso = ? WHERE usuario = ?",
                     (datetime.now(timezone.utc).isoformat(), f["usuario"]))
        conn.commit()
    conn.close()
    return dict(f) if ok else None


def listar_usuarios():
    conn = get_conn()
    filas = conn.execute(
        "SELECT usuario, nombre, rol, activo, creado_en, ultimo_ingreso FROM usuarios ORDER BY creado_en"
    ).fetchall()
    conn.close()
    return [dict(f) for f in filas]


def hay_usuarios() -> bool:
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) c FROM usuarios").fetchone()["c"]
    conn.close()
    return n > 0


def cambiar_clave(usuario: str, clave_nueva: str):
    if len(clave_nueva or "") < 6:
        raise ValueError("La clave debe tener al menos 6 caracteres.")
    salt = _os.urandom(16)
    conn = get_conn()
    conn.execute("UPDATE usuarios SET clave_hash = ?, salt = ? WHERE usuario = ?",
                 (_hash_clave(clave_nueva, salt), salt.hex(), usuario.strip().lower()))
    conn.commit()
    conn.close()


def activar_usuario(usuario: str, activo: bool):
    conn = get_conn()
    conn.execute("UPDATE usuarios SET activo = ? WHERE usuario = ?",
                 (1 if activo else 0, usuario.strip().lower()))
    conn.commit()
    conn.close()


def permisos_de(user: dict) -> set:
    """Permisos del rol, más los que el dueño le haya habilitado al asesor."""
    if not user:
        return set()
    base = set(ROLES.get(user.get("rol"), {}).get("permisos", set()))
    if user.get("rol") == "asesor":
        cfg = get_settings()
        if cfg.get("asesor_ve_kpis") == "1":
            base.add("ver_kpis")
        if cfg.get("asesor_ve_bitacora") == "1":
            base.add("ver_bitacora")
    return base
