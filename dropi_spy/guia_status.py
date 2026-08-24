"""
Tracker de estados de guías (Forza, Gintracom, etc.) usando la API nativa de Dropi GT.

Dropi no siempre refleja las novedades/incidencias en ruta en el campo "status"
general de la orden, pero SÍ las guarda en el historial de movimientos de la
transportadora (servientrega_movements). Este módulo lee ese historial completo
para detectar incidencias que la vista normal de Dropi no muestra.
"""
import os
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

API_BASE = "https://api.dropi.gt"
LOGIN_URL = f"{API_BASE}/api/login"
ORDERS_URL = f"{API_BASE}/api/orders/myorders/v2"

SESSION_FILE = BASE_DIR / "dropi_spy" / "data" / "gt_session.json"

# Cuentas de Dropi GT disponibles. Cada una lee sus credenciales del .env con
# su propio prefijo, y guarda su sesión en un archivo aparte, para poder tener
# varias tiendas abiertas sin que una pise a la otra.
CUENTAS = {
    # Maxicombos (antes Nativa/NativaGT) es la tienda principal: abre por defecto.
    "maxicombos": {"label": "Maxicombos", "prefix": "DROPI_GT_MAXICOMBOS"},
    "principal": {"label": "Cuenta anterior", "prefix": "DROPI_GT"},
}

# Cuál se abre al arrancar el tablero.
CUENTA_POR_DEFECTO = "maxicombos"

# Dónde guarda su sesión cada cuenta. Es un mapa fijo a propósito: no depende
# de cuál sea la cuenta por defecto, para que cambiarla no reasigne archivos
# de sesión ya existentes.
ARCHIVOS_SESION = {
    "principal": SESSION_FILE,   # ruta histórica, se respeta
    "maxicombos": BASE_DIR / "dropi_spy" / "data" / "gt_session_maxicombos.json",
}


def session_file_for(cuenta: str):
    return ARCHIVOS_SESION.get(
        cuenta, BASE_DIR / "dropi_spy" / "data" / f"gt_session_{cuenta}.json"
    )


def guardar_credenciales(cuenta: str, email: str = "", password: str = "",
                         integration_key: str = ""):
    """Guarda los accesos de una tienda en el .env desde la propia app, para
    que no haya que editar el archivo a mano. Reescribe la clave si ya existía
    y elimina la línea comentada de ejemplo si sigue ahí."""
    if cuenta not in CUENTAS:
        raise ValueError(f"Cuenta desconocida: {cuenta}")
    prefix = CUENTAS[cuenta]["prefix"]

    valores = {}
    if integration_key.strip():
        valores[f"{prefix}_INTEGRATION_KEY"] = integration_key.strip()
    if email.strip():
        valores[f"{prefix}_EMAIL"] = email.strip()
    if password.strip():
        valores[f"{prefix}_PASSWORD"] = password.strip()
    if not valores:
        raise ValueError("No se recibió ningún dato para guardar.")

    env_path = BASE_DIR / ".env"
    lineas = env_path.read_text().splitlines() if env_path.exists() else []

    for clave, valor in valores.items():
        reemplazada = False
        for i, linea in enumerate(lineas):
            limpia = linea.lstrip("#").strip()
            if limpia.startswith(f"{clave}="):
                lineas[i] = f"{clave}={valor}"
                reemplazada = True
                break
        if not reemplazada:
            lineas.append(f"{clave}={valor}")
        # Que quede disponible de inmediato, sin reiniciar la app.
        os.environ[clave] = valor

    env_path.write_text("\n".join(lineas) + "\n")
    try:
        env_path.chmod(0o600)  # solo el dueño puede leerlo
    except OSError:
        pass
    return sorted(valores)


def cuentas_configuradas():
    """Devuelve las cuentas que ya tienen credenciales o llave en el .env."""
    listas = []
    for key, cfg in CUENTAS.items():
        pre = cfg["prefix"]
        if (
            os.getenv(f"{pre}_INTEGRATION_KEY")
            or (os.getenv(f"{pre}_EMAIL") and os.getenv(f"{pre}_PASSWORD"))
            or session_file_for(key).exists()   # basta con un token ya pegado
        ):
            listas.append(key)
    return listas

# Ventana de seguimiento por defecto (días hacia atrás)
DEFAULT_DAYS = 30

# Se descartan por completo del tablero (ya no se les hace seguimiento).
EXCLUDED_STATUSES = {"ENTREGADO", "CANCELADO"}

# Para compatibilidad con el dashboard (filtro de estancadas).
FINAL_STATUSES = list(EXCLUDED_STATUSES)

# Estados que por sí solos disparan alerta 🔴 (regla de negocio).
ALERT_STATUSES = {
    "NOVEDAD", "INCIDENCIA EN RUTA", "INCIDENCIA VALIDADA",
    "SOLUCION INCORRECTA", "SOLUCIÓN INCORRECTA",
}

# Estados donde el paquete ya va con la transportadora y debería avanzar:
# si lleva ≥2 días sin moverse en el mismo estado, se escala a alerta.
CARRIER_ACTIVE_STATUSES = {
    "RECOLECTADO", "EN TRANSITO", "EN TRÁNSITO", "EN REPARTO", "EN BODEGA ORIGEN",
    "EN RUTA",
}

# Nº de días en el mismo estado que dispara alerta por estancamiento.
STUCK_DAYS = 2

# --- Detección de "recoge en agencia/oficina" -------------------------------
# Dropi no tiene un campo para esto (rate_type siempre es "CON RECAUDO"), así
# que la única señal está en el texto de la dirección que escribe el cliente.
# Se separa en dos niveles a propósito: hay direcciones que dicen "oficina"
# porque el cliente trabaja ahí (oficina jurídica, contable...) y ESAS son
# entregas a domicilio normales, no recogidas.
RECOGIDA_SEGURA = re.compile(
    r"(recoj|recog|recib)\w*\s+(lo\s+|el\s+|en\s+|para\s+)*(la\s+)?(agencia|oficina|sucursal|bodega)"
    r"|\bagencia\s+(forza|guatex|cargo|plaza|puerto|atanasio)"
    r"|oficina\s+(de\s+)?cargo"
    r"|cargo\s*expres\w*"
    r"|\bagente\s+autorizado\b"
    r"|\ben\s+agencia\b",
    re.I,
)
RECOGIDA_DUDOSA = re.compile(r"\bagencia\b|\bsucursal\b", re.I)

ENTREGA_LABEL = {
    "agencia": "🏢 Recoge en agencia",
    "revisar": "❓ Posible recogida — revisar",
    "domicilio": "🏠 A domicilio",
}


def clasificar_entrega(order: dict) -> str:
    """'agencia' si el cliente va a recoger a un punto de la transportadora,
    'revisar' si la dirección lo insinúa sin confirmarlo, 'domicilio' si no."""
    texto = " ".join(
        str(order.get(campo) or "") for campo in ("dir", "notes", "colonia")
    )
    if RECOGIDA_SEGURA.search(texto):
        return "agencia"
    if RECOGIDA_DUDOSA.search(texto):
        return "revisar"
    return "domicilio"


SEVERITY_EMOJI = {"urgente": "🔴", "devolucion": "🔄", "atencion": "🟡", "ok": "🟢", "finalizado": "⚪"}

# Traducción de cada estado REAL de Dropi GT a lenguaje simple + acción + severidad base.
STATUS_INFO = {
    "NOVEDAD": {"label": "Novedad declarada por la transportadora", "action": "Abrir el detalle abajo y gestionar con la transportadora o el cliente.", "severity": "urgente"},
    "INCIDENCIA EN RUTA": {"label": "Incidencia en ruta", "action": "Revisar el detalle y gestionar con la transportadora.", "severity": "urgente"},
    "INCIDENCIA VALIDADA": {"label": "Incidencia validada", "action": "Gestionar la solución con la transportadora o el cliente.", "severity": "urgente"},
    "SOLUCION INCORRECTA": {"label": "Solución de novedad marcada como incorrecta", "action": "Revisar por qué falló la solución y volver a gestionar.", "severity": "urgente"},
    "SOLUCIÓN INCORRECTA": {"label": "Solución de novedad marcada como incorrecta", "action": "Revisar por qué falló la solución y volver a gestionar.", "severity": "urgente"},
    "NOVEDAD SOLUCIONADA": {"label": "Novedad resuelta, en camino de nuevo", "action": "Ninguna acción, el problema ya se resolvió.", "severity": "ok"},
    "RECOLECTADO": {"label": "Recolectado por la transportadora", "action": "Ninguna acción, solo esperar que avance.", "severity": "ok"},
    "EN TRANSITO": {"label": "En tránsito a destino", "action": "Ninguna acción, solo esperar.", "severity": "ok"},
    "EN TRÁNSITO": {"label": "En tránsito a destino", "action": "Ninguna acción, solo esperar.", "severity": "ok"},
    "EN REPARTO": {"label": "En reparto (sale a entregar)", "action": "Ninguna acción, va en camino al cliente.", "severity": "ok"},
    "EN BODEGA ORIGEN": {"label": "En bodega de origen", "action": "Ninguna acción, aún no sale a ruta.", "severity": "ok"},
    "PENDIENTE": {"label": "Pendiente de envío (aún no recolectado)", "action": "Verificar que se genere la guía y se despache.", "severity": "atencion"},
    "PENDIENTE CONFIRMACION": {"label": "Pendiente de confirmación del pedido", "action": "Confirmar el pedido con el cliente para poder despacharlo.", "severity": "atencion"},
    "PENDIENTE CONFIRMACIÓN": {"label": "Pendiente de confirmación del pedido", "action": "Confirmar el pedido con el cliente para poder despacharlo.", "severity": "atencion"},
    "DEVOLUCION": {"label": "En devolución a bodega", "action": "Definir si se reintenta el envío o se hace nota de crédito.", "severity": "devolucion"},
    "DEVOLUCIÓN": {"label": "En devolución a bodega", "action": "Definir si se reintenta el envío o se hace nota de crédito.", "severity": "devolucion"},
    "RECHAZADO": {"label": "Rechazado por el cliente", "action": "Confirmar el motivo y gestionar la devolución.", "severity": "atencion"},
    "EN RUTA": {"label": "En ruta hacia el cliente", "action": "Ninguna acción, va en camino.", "severity": "ok"},
    "SOLUCION APROBADA": {"label": "Solución de la novedad aprobada", "action": "Ninguna acción, la novedad se resolvió y el envío continúa.", "severity": "ok"},
    "SOLUCIÓN APROBADA": {"label": "Solución de la novedad aprobada", "action": "Ninguna acción, la novedad se resolvió y el envío continúa.", "severity": "ok"},
    "EN INVENTARIO": {"label": "En inventario de bodega (aún no despachado)", "action": "Verificar que se genere la guía y salga a ruta.", "severity": "atencion"},
}

STATUS_FALLBACK = {"label": "Estado no reconocido — revisar manualmente", "action": "Abrir el texto original abajo; si es un caso nuevo, agrégalo a la tabla.", "severity": "atencion"}

# Traduce status/texto crudo de la transportadora a lenguaje simple + acción
# sugerida + severidad. Orden importa: para texto libre (classify_text_only),
# el primer entry cuyo patrón matchea gana, así que las categorías más
# específicas van antes que las genéricas (ej. "DEV CONFIRMADA" antes que
# "INCIDENCIA").
MOVEMENT_TAXONOMY = [
    {
        "category": "novedad_resuelta",
        "label_es": "Novedad ya resuelta, en camino de nuevo",
        "action_es": "Ninguna acción necesaria; el problema ya se resolvió.",
        "severity": "ok",
        "exact": ["NOVEDAD SOLUCIONADA"],
    },
    {
        "category": "solucion_aprobada",
        "label_es": "Solución de la novedad aprobada, sigue el envío",
        "action_es": "Ninguna acción, la novedad se resolvió.",
        "severity": "ok",
        "exact": ["SOLUCION APROBADA", "SOLUCIÓN APROBADA"],
    },
    {
        "category": "en_ruta",
        "label_es": "En ruta hacia el cliente",
        "action_es": "Ninguna acción, va en camino.",
        "severity": "ok",
        "exact": ["EN RUTA"],
    },
    {
        "category": "en_inventario",
        "label_es": "En inventario de bodega (aún no despachado)",
        "action_es": "Verificar que se genere la guía y salga a ruta.",
        "severity": "atencion",
        "exact": ["EN INVENTARIO"],
    },
    {
        "category": "entregado",
        "label_es": "Entregado al cliente",
        "action_es": "Ninguna acción, entrega completada.",
        "severity": "finalizado",
        "exact": ["ENTREGADO"],
    },
    {
        "category": "devuelto",
        "label_es": "Devuelto a bodega",
        "action_es": "Verificar en bodega y decidir si se reintenta el envío o se hace nota de crédito.",
        "severity": "devolucion",
        "exact": ["DEVOLUCION", "DEVOLUCIÓN"],
    },
    {
        "category": "cancelado",
        "label_es": "Pedido cancelado",
        "action_es": "Ninguna acción de envío; confirmar con ventas si aplica reembolso.",
        "severity": "finalizado",
        "exact": ["CANCELADO"],
    },
    {
        "category": "rechazado_final",
        "label_es": "Rechazado definitivamente por el cliente",
        "action_es": "Confirmar motivo con el cliente para futuras ventas; gestionar devolución de mercancía.",
        "severity": "finalizado",
        "exact": ["RECHAZADO"],
    },
    {
        "category": "cliente_ausente",
        "label_es": "Cliente no se encontraba en la dirección",
        "action_es": "Llamar al cliente para coordinar una nueva entrega y confirmar que va a estar en casa.",
        "severity": "urgente",
        "contains": ["NO SE ENCUENTRA", "AUSENTE"],
    },
    {
        "category": "producto_no_reconocido",
        "label_es": "Cliente dice que no es el producto que pidió",
        "action_es": "Llamar al cliente para aclarar el pedido; si insiste, coordinar devolución.",
        "severity": "urgente",
        "contains": ["NO ES EL PRODUCTO"],
    },
    {
        "category": "cliente_inconforme",
        "label_es": "Cliente inconforme con el producto, no lo recibe",
        "action_es": "Llamar al cliente para entender el reclamo; si lo mantiene, coordinar devolución.",
        "severity": "urgente",
        "contains": ["INCONFORME", "NO RECIBE"],
    },
    {
        "category": "fuera_cobertura",
        "label_es": "Fuera de cobertura: la ciudad no coincide con la dirección",
        "action_es": "Confirmar con el cliente la ciudad y dirección correctas y reenviarlas a la transportadora.",
        "severity": "urgente",
        "contains": ["FUERA DE COBERTURA", "NO COINCIDE LA CIUDAD"],
    },
    {
        "category": "direccion_incorrecta",
        "label_es": "Dirección incorrecta o incompleta",
        "action_es": "Confirmar la dirección exacta con el cliente y reenviarla a la transportadora.",
        "severity": "urgente",
        "contains": ["DIRECCION ERRADA", "DIRECCIÓN ERRADA", "NO UBICADO"],
    },
    {
        "category": "fecha_no_disponible",
        "label_es": "Cliente pidió una fecha que la transportadora no maneja",
        "action_es": "Llamar al cliente para ofrecer una fecha dentro de los días disponibles, o cancelar.",
        "severity": "urgente",
        "contains": ["FUERA DE LOS DÍAS DE GESTIÓN", "FUERA DE LOS DIAS DE GESTION", "RE-PROGRAMA", "REPROGRAMA"],
    },
    {
        "category": "no_contesta",
        "label_es": "Cliente no contesta el teléfono",
        "action_es": "Intentar otra vía de contacto (WhatsApp/otro número) antes de que venza el plazo de intentos.",
        "severity": "urgente",
        "contains": ["NO CONTESTA"],
    },
    {
        "category": "producto_dañado",
        "label_es": "Producto llegó dañado",
        "action_es": "Confirmar el daño con el cliente, pedir fotos, coordinar reposición o devolución.",
        "severity": "urgente",
        "contains": ["DAÑADO", "DANADO"],
    },
    {
        "category": "paquete_extraviado",
        "label_es": "Paquete extraviado por la transportadora",
        "action_es": "Reclamar a la transportadora, avisar al cliente, evaluar reposición.",
        "severity": "urgente",
        "contains": ["EXTRAVIADO"],
    },
    {
        "category": "devolucion_confirmada",
        "label_es": "Devolución confirmada por bodega",
        "action_es": "El paquete va de regreso; no se puede reactivar. Dar de baja el pedido o reprogramar un envío nuevo.",
        "severity": "devolucion",
        "contains": ["DEV CONFIRMADA", "DEVOLUCION CONFIRMADA", "DEVOLUCIÓN CONFIRMADA"],
    },
    {
        "category": "cliente_rechaza",
        "label_es": "Cliente rechazó el pedido / no autoriza",
        "action_es": "Confirmar el motivo con el cliente; si es definitivo, gestionar la devolución.",
        "severity": "urgente",
        "contains": ["RECHAZ", "NO AUTORIZA"],
    },
    {
        "category": "devolucion_entregada",
        "label_es": "Devolución ya entregada en bodega de origen",
        "action_es": "El paquete regresó. Dar de baja el pedido o reprogramar un envío nuevo.",
        "severity": "devolucion",
        "contains": ["DEVOLUCION ENTREGADA", "DEVOLUCIÓN ENTREGADA"],
    },
    {
        "category": "incidencia_validada",
        "label_es": "Incidencia validada por la transportadora",
        "action_es": "Gestionar la solución con la transportadora o el cliente.",
        "severity": "urgente",
        "contains": ["INCIDENCIA VALIDADA"],
    },
    {
        "category": "incidencia_en_ruta",
        "label_es": "Incidencia en ruta",
        "action_es": "Revisar el detalle y gestionar con la transportadora antes de que se devuelva.",
        "severity": "urgente",
        "contains": ["INCIDENCIA EN RUTA", "INCIDENCIA"],
    },
    {
        "category": "recolectado_plano",
        "label_es": "Recolectado, esperando salir a ruta",
        "action_es": "Ninguna acción, solo esperar.",
        "severity": "ok",
        "exact": ["RECOLECTADO"],
    },
    {
        "category": "en_transito_plano",
        "label_es": "En camino a destino",
        "action_es": "Ninguna acción, solo esperar.",
        "severity": "ok",
        "exact": ["EN TRANSITO", "EN TRÁNSITO"],
    },
    {
        "category": "en_reparto",
        "label_es": "En reparto (sale a entregar hoy)",
        "action_es": "Ninguna acción, va en camino al cliente.",
        "severity": "ok",
        "exact": ["EN REPARTO"],
    },
    {
        "category": "en_bodega_origen",
        "label_es": "En bodega de origen",
        "action_es": "Ninguna acción, aún no sale a ruta.",
        "severity": "ok",
        "exact": ["EN BODEGA ORIGEN"],
    },
    {
        "category": "guia_generada",
        "label_es": "Guía generada, esperando recolección",
        "action_es": "Ninguna acción, esperar a que la transportadora recoja.",
        "severity": "ok",
        "exact": ["GENERADA", "GUIA GENERADA", "GUÍA GENERADA"],
    },
]

TAXONOMY_BY_CATEGORY = {entry["category"]: entry for entry in MOVEMENT_TAXONOMY}

# Status dice "NOVEDAD" pero ningún patrón de texto lo reconoció. NO se
# afirma que "no hay motivo" (Dropi casi siempre sí lo pone en el texto):
# se marca urgente y se remite al texto original real, sin inventar.
NOVEDAD_SIN_DETALLE = {
    "category": "novedad_sin_detalle",
    "label_es": "Novedad activa — revisar el detalle en el historial",
    "action_es": "Abrir el historial de abajo para leer el motivo exacto que reportó la transportadora.",
    "severity": "urgente",
}

# Nada matcheó: no se asume "sin problema" por default, se marca para
# revisión manual.
REVISION_MANUAL = {
    "category": "revision_manual",
    "label_es": "Estado no reconocido — revisar manualmente",
    "action_es": "Abrir el texto original abajo; si es un caso nuevo, agrégalo a la tabla de categorías.",
    "severity": "atencion",
}


class Necesita2FA(Exception):
    """Dropi exige 2FA en cada login. `context` trae la lista de contactos a
    los que puede mandar el código (correo/SMS) para que el usuario elija."""

    def __init__(self, context):
        self.context = context
        super().__init__("Dropi solicita código 2FA para completar el login")

    @property
    def contactos(self):
        return (self.context or {}).get("list_contact") or []


class TokenExpirado(Exception):
    pass


class DropiGTClient:
    def __init__(self, email: Optional[str] = None, password: Optional[str] = None,
                 cuenta: str = CUENTA_POR_DEFECTO):
        if cuenta not in CUENTAS:
            raise ValueError(f"Cuenta desconocida: {cuenta}. Disponibles: {list(CUENTAS)}")
        self.cuenta = cuenta
        self.cuenta_label = CUENTAS[cuenta]["label"]
        prefix = CUENTAS[cuenta]["prefix"]
        self.session_file = session_file_for(cuenta)
        self.email = email or os.getenv(f"{prefix}_EMAIL")
        self.password = password or os.getenv(f"{prefix}_PASSWORD")
        self.token = None
        # Estado intermedio del 2FA (token preliminar + datos para pedir el código).
        self._pre_token = None
        self._otp_email = None
        self._user_id = None
        # Llave de integración de Dropi ("Mis Integraciones"): si existe, no
        # caduca y evita por completo el login con 2FA.
        self.integration_key = os.getenv(f"{prefix}_INTEGRATION_KEY")
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Origin": "https://app.dropi.gt",
            "Referer": "https://app.dropi.gt/",
            "Accept-Language": "es-419,es;q=0.9",
            "sec-fetch-site": "same-site",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        })
        if self.integration_key:
            self.session.headers["dropi-integration-key"] = self.integration_key
        self._load_cached_token()

    @staticmethod
    def _clean_token(token):
        """Dropi guarda el token en localStorage como string JSON (entre
        comillas). Al copiarlo manualmente se pueden colar esas comillas o
        espacios, dejando el header 'Bearer' malformado (401). Se limpian
        aquí para que el token funcione sin importar cómo se haya pegado."""
        if not token:
            return None
        token = token.strip().strip('"').strip("'").strip()
        return token or None

    @property
    def _clave_token(self) -> str:
        return f"dropi_token_{self.cuenta}"

    def _load_cached_token(self):
        """Primero la base de datos (sirve tanto local como compartida en la
        nube, donde los archivos se pierden al reiniciar); si no hay nada, se
        cae al archivo de siempre para no perder sesiones ya guardadas."""
        token = None
        try:
            from . import guias_db
            token = guias_db.get_settings().get(self._clave_token)
        except Exception:
            pass

        if not token and self.session_file.exists():
            try:
                token = json.loads(self.session_file.read_text()).get("token")
            except (json.JSONDecodeError, OSError):
                token = None

        token = self._clean_token(token)
        if token:
            self.token = token
            self.session.headers["Authorization"] = f"Bearer {token}"

    def _save_cached_token(self):
        try:
            from . import guias_db
            guias_db.save_settings({self._clave_token: self.token or ""})
        except Exception:
            pass
        try:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            self.session_file.write_text(json.dumps({
                "token": self.token,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }))
        except OSError:
            pass  # en la nube el disco puede ser de solo lectura

    def set_token(self, token: str):
        self.token = self._clean_token(token)
        self.session.headers["Authorization"] = f"Bearer {self.token}"
        self._save_cached_token()

    def login(self, otp_code: Optional[str] = None):
        """Login con email/password.

        Dropi exige 2FA en TODOS los logins: devuelve un token preliminar que
        aún no sirve para consultar (401) más la lista de contactos a los que
        puede enviar el código. En ese caso se levanta Necesita2FA; el flujo
        completo es login() -> send_otp() -> verify_otp()."""
        if not self.email or not self.password:
            pre = CUENTAS[self.cuenta]["prefix"]
            raise ValueError(f"Faltan {pre}_EMAIL / {pre}_PASSWORD en dropi-spy/.env")

        payload = {"email": self.email, "password": self.password, "white_brand_id": 1}
        if otp_code:
            payload["code"] = otp_code
            payload["otp"] = otp_code

        resp = self.session.post(LOGIN_URL, json=payload, timeout=30)
        data = {}
        try:
            data = resp.json()
        except ValueError:
            pass

        # Dropi responde HTTP 200 incluso en errores; el estado real va en el body.
        estado = data.get("status")
        if data.get("isSuccess") is False and estado in (400, 401, 403):
            raise RuntimeError(
                f"Dropi rechazó las credenciales: {data.get('message') or data.get('error')}. "
                "Revisa DROPI_GT_EMAIL / DROPI_GT_PASSWORD en dropi-spy/.env."
            )

        token = (
            data.get("token") or data.get("access_token")
            or (data.get("data") or {}).get("token")
            or (data.get("result") or {}).get("token")
        )

        objects = data.get("objects") or {}
        requiere_2fa = str(data.get("message") or "").lower() == "2fa" or objects.get("required") is True

        if token and requiere_2fa and not otp_code:
            # Token preliminar: sirve solo para pedir/validar el código.
            self._pre_token = token
            # La respuesta de login no siempre trae email_otp; el correo de la
            # cuenta es el respaldo correcto (createcode lo exige).
            self._otp_email = objects.get("email_otp") or self.email
            self._user_id = objects.get("id") or objects.get("user_id")
            raise Necesita2FA(objects)

        if token:
            self.set_token(token)
            return True

        raise RuntimeError(f"Login falló ({resp.status_code}): {data or resp.text[:300]}")

    def send_otp(self, contacto: Optional[str] = None, module: str = "Login"):
        """Pide a Dropi que envíe el código de 6 dígitos. `contacto` es el
        'val' de la lista que devolvió el login (soft1=correo, soft3=SMS...)."""
        if not self._pre_token:
            raise RuntimeError("Primero hay que hacer login() para obtener el token preliminar.")

        payload = {
            "module": module,
            "email": self._otp_email,
            "userId": self._user_id,
        }
        if contacto:
            payload["typeOfRecovery"] = contacto

        resp = self.session.post(
            f"{API_BASE}/api/createcode", json=payload, timeout=30,
            headers={"Authorization": f"Bearer {self._pre_token}"},
        )
        data = resp.json() if resp.content else {}
        if data.get("isSuccess") is False:
            raise RuntimeError(f"No se pudo enviar el código: {data.get('message')}")
        return data

    def verify_otp(self, code: str, module: str = "Login"):
        """Valida el código y deja la sesión lista con el token definitivo."""
        if not self._pre_token:
            raise RuntimeError("Primero hay que hacer login() para obtener el token preliminar.")

        payload = {
            "code": str(code).strip(),
            "module": module,
            "email": self._otp_email,
            "userId": self._user_id,
            "white_brand_id": 1,
            "environment": "production",
        }
        resp = self.session.post(
            f"{API_BASE}/api/segurity/verifyCode", json=payload, timeout=30,
            headers={"Authorization": f"Bearer {self._pre_token}"},
        )
        data = resp.json() if resp.content else {}
        if data.get("isSuccess") is False:
            raise RuntimeError(f"Código inválido: {data.get('message') or data.get('error')}")

        final = (
            data.get("token") or (data.get("objects") or {}).get("token")
            or self._pre_token
        )
        self.set_token(final)
        # Comprobar de verdad que el token ya sirve para consultar.
        self._authed_get(ORDERS_URL, params={
            "orderBy": "id", "orderDirection": "desc", "result_number": 1,
            "start": 0, "textToSearch": "", "status": "",
            "supplier_id": "null", "user_id": "null",
        })
        return True

    def _authed_get(self, url, params=None):
        resp = self.session.get(url, params=params, timeout=30)
        if resp.status_code == 401:
            raise TokenExpirado("El token de Dropi expiró o es inválido")
        resp.raise_for_status()
        return resp.json()

    def fetch_orders(self, status: str, page_size: int = 200, start: int = 0,
                     desde: Optional[str] = None, hasta: Optional[str] = None):
        """`desde`/`hasta` en formato AAAA-MM-DD. Dropi los filtra en el
        servidor (parámetros `from`/`until`), así que no hay que traerse todo
        para luego descartar."""
        params = {
            "orderBy": "id",
            "orderDirection": "desc",
            "result_number": page_size,
            "start": start,
            "textToSearch": "",
            "status": status,
            "supplier_id": "null",
            "user_id": "null",
        }
        if desde:
            params["from"] = desde
        if hasta:
            params["until"] = hasta
        data = self._authed_get(ORDERS_URL, params=params)
        if not data:
            return []
        return data.get("objects", []) or []

    def fetch_orders_range(self, desde: str, hasta: str, page_size: int = 200,
                           max_paginas: int = 60):
        """Trae TODAS las órdenes creadas entre `desde` y `hasta` (AAAA-MM-DD),
        paginando hasta agotar el rango. `max_paginas` es un tope de seguridad
        para no quedarse dando vueltas si la API repite páginas."""
        todas, vistos = [], set()
        start = 0
        for _ in range(max_paginas):
            lote = self.fetch_orders("", page_size=page_size, start=start,
                                     desde=desde, hasta=hasta)
            if not lote:
                break
            nuevos = [o for o in lote if o.get("id") not in vistos]
            if not nuevos:
                break  # la API devolvió lo mismo: no hay más
            for o in nuevos:
                vistos.add(o.get("id"))
            todas.extend(nuevos)
            if len(lote) < page_size:
                break
            start += page_size
            time.sleep(0.3)
        return todas

    def fetch_recent_orders(self, days: int = DEFAULT_DAYS, page_size: int = 200):
        """Atajo: las órdenes de los últimos `days` días."""
        hoy = datetime.now().date()
        return self.fetch_orders_range(
            str(hoy - timedelta(days=days)), str(hoy), page_size=page_size
        )


def _parse_dt(value: Optional[str]):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _as_naive(dt: datetime) -> datetime:
    """Quita tzinfo para comparar con fechas naive sin errores."""
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def classify_text_only(text: Optional[str]) -> Optional[dict]:
    """Clasifica un texto crudo de movimiento contra la taxonomía, sin
    considerar el status de la orden. Usado para traducir cada línea del
    historial y como señal secundaria dentro de classify_movement()."""
    if not text:
        return None
    upper = text.strip().upper()
    for entry in MOVEMENT_TAXONOMY:
        if upper in entry.get("exact", []):
            return entry
        if any(pattern in upper for pattern in entry.get("contains", [])):
            return entry
    return None


def classify_movement(dropi_status: Optional[str], last_movement_text: Optional[str],
                      days_since_movement: Optional[int] = None) -> dict:
    """Clasifica una guía a lenguaje simple + acción + severidad, priorizando
    el ESTADO REAL DE LA TRANSPORTADORA (último movimiento de Gintracom/Forza),
    que es lo que de verdad importa. El status de Dropi solo se usa para lo
    suyo (entregado/cancelado/novedad solucionada) y como respaldo cuando no
    hay movimiento reconocido.

    Reglas de alerta 🔴:
      - Movimiento de la transportadora que sea novedad/incidencia (reprograma,
        inconforme, fuera de cobertura, incidencia en ruta/validada, etc.).
      - Status de Dropi de novedad/incidencia (por si el texto no lo marca).
      - ≥ STUCK_DAYS días sin avanzar en un mismo estado de transportadora.
    """
    status_upper = (dropi_status or "").strip().upper()

    # NOVEDAD SOLUCIONADA en Dropi manda sobre un texto viejo de novedad:
    # ya se resolvió, no debe alertar.
    if status_upper == "NOVEDAD SOLUCIONADA":
        info = dict(TAXONOMY_BY_CATEGORY["novedad_resuelta"])
        info["is_alert"] = False
        return info

    # 1) El movimiento real de la transportadora manda.
    text_match = classify_text_only(last_movement_text)
    if text_match:
        category = text_match["category"]
        label = text_match["label_es"]
        action = text_match["action_es"]
        severity = text_match["severity"]
    else:
        # 2) Sin movimiento reconocido: caer al estado de Dropi.
        base = STATUS_INFO.get(status_upper, STATUS_FALLBACK)
        category = status_upper.lower().replace(" ", "_") or "desconocido"
        label = base["label"]
        action = base["action"]
        severity = base["severity"]

    # 3) Status de Dropi de novedad/incidencia también dispara alerta.
    if status_upper in ALERT_STATUSES and severity != "urgente":
        severity = "urgente"

    is_alert = severity == "urgente"

    # 4) Estancamiento: ≥ STUCK_DAYS días sin avanzar en un estado de transportadora.
    if (days_since_movement is not None and days_since_movement >= STUCK_DAYS
            and status_upper in CARRIER_ACTIVE_STATUSES and severity == "ok"):
        severity = "urgente"
        is_alert = True
        label = f"{label} — estancado {days_since_movement} días sin avanzar"
        action = "Lleva 2+ días sin moverse en el mismo estado. Pedir a la transportadora que lo haga avanzar."

    return {
        "category": category,
        "label_es": label,
        "action_es": action,
        "severity": severity,
        "is_alert": is_alert,
    }


def parse_order(order: dict) -> dict:
    movements = order.get("servientrega_movements") or []
    movements_sorted = sorted(
        movements,
        key=lambda m: m.get("created_at") or "",
        reverse=True,
    )
    last_movement = movements_sorted[0] if movements_sorted else None
    last_movement_text = last_movement.get("nom_mov") if last_movement else None
    last_movement_at = last_movement.get("created_at") if last_movement else order.get("created_at")

    dropi_status = order.get("status") or ""

    last_dt = _parse_dt(last_movement_at)
    days_since_movement = None
    if last_dt:
        now = datetime.now(last_dt.tzinfo) if last_dt.tzinfo else datetime.now()
        days_since_movement = (now - last_dt).days

    classification = classify_movement(dropi_status, last_movement_text, days_since_movement)

    return {
        "order_id": order.get("id"),
        "guide": order.get("shipping_guide"),
        "carrier": order.get("shipping_company"),
        "customer": f"{order.get('name', '')} {order.get('surname', '')}".strip(),
        "phone": order.get("phone"),
        "city": order.get("city"),
        "state": order.get("state"),
        "direccion": order.get("dir"),
        "tipo_entrega": clasificar_entrega(order),
        "dropi_status": dropi_status,
        "novedad_text": order.get("novedad_servientrega"),
        "last_movement_text": last_movement_text,
        "last_movement_at": last_movement_at,
        "days_since_movement": days_since_movement,
        "has_incident": classification["is_alert"],
        "is_alert": classification["is_alert"],
        "category": classification["category"],
        "label_es": classification["label_es"],
        "action_es": classification["action_es"],
        "severity": classification["severity"],
        "movements": [
            {
                "text": m.get("nom_mov"),
                "at": m.get("created_at"),
                **(
                    {"label_es": mc["label_es"], "severity": mc["severity"]}
                    if (mc := classify_text_only(m.get("nom_mov")))
                    else {"label_es": m.get("nom_mov"), "severity": None}
                ),
            }
            for m in movements_sorted
        ],
        "created_at": order.get("created_at"),
    }


def get_guides_con_resumen(client: DropiGTClient, days: int = DEFAULT_DAYS,
                           auto_relogin: bool = True, desde: Optional[str] = None,
                           hasta: Optional[str] = None):
    """Devuelve (guias_activas, resumen).

    `guias_activas` son las que aún requieren acción (todo menos ENTREGADO y
    CANCELADO). `resumen` trae los totales del período COMPLETO, incluidos los
    entregados y cancelados, que se cuentan en el mismo recorrido para no
    hacer consultas de más."""
    def _traer():
        if desde or hasta:
            hoy = str(datetime.now().date())
            return client.fetch_orders_range(desde or "2000-01-01", hasta or hoy)
        return client.fetch_recent_orders(days=days)

    try:
        orders = _traer()
    except TokenExpirado:
        if not auto_relogin:
            raise
        client.login()
        orders = _traer()

    kept, discarded = [], []
    entregados = cancelados = 0
    for o in orders:
        estado = (o.get("status") or "").strip().upper()
        if estado in EXCLUDED_STATUSES:
            discarded.append(o)
            if estado == "ENTREGADO":
                entregados += 1
            else:
                cancelados += 1
        else:
            kept.append(parse_order(o))

    # Registrar en el historial las que se descartan (entregadas/canceladas).
    if discarded:
        try:
            from . import guias_db
            guias_db.log_discarded(discarded)
        except Exception:
            pass

    total = len(orders)
    # Tasa de entrega sobre los pedidos que ya tuvieron desenlace: incluir los
    # que siguen en camino la haría ver artificialmente baja.
    con_desenlace = entregados + cancelados
    resumen = {
        "total": total,
        "entregados": entregados,
        "cancelados": cancelados,
        "activas": len(kept),
        "pct_entrega": round(100 * entregados / con_desenlace, 1) if con_desenlace else None,
    }
    return kept, resumen


def get_all_guides(client: DropiGTClient, days: int = DEFAULT_DAYS,
                   auto_relogin: bool = True, desde: Optional[str] = None,
                   hasta: Optional[str] = None) -> list:
    """Solo las guías activas (se mantiene por compatibilidad)."""
    return get_guides_con_resumen(client, days=days, auto_relogin=auto_relogin,
                                  desde=desde, hasta=hasta)[0]
