import os
import sys
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

# En el servidor no hay archivo .env: la configuración llega por los "secrets"
# de Streamlit. Se vuelcan a variables de entorno ANTES de importar el resto,
# para que el código funcione igual en la nube y en la Mac sin cambios.
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, str):
            os.environ.setdefault(_k, _v)
except Exception:
    pass  # local: no hay secrets y se usa el .env de siempre

sys.path.insert(0, os.path.dirname(__file__))

from dropi_spy import guia_status as gs
from dropi_spy import guias_db as db

st.set_page_config(page_title="Estado de Guías — Forza/Gintracom GT", page_icon="📦", layout="wide")

st.markdown("""<style>
.block-container { padding-top: 1rem; }
div[data-testid="stMetric"] {
    background: #0e1117; padding: 0.8rem; border-radius: 8px; border: 1px solid #333;
}
div[data-testid="stMetric"] label, div[data-testid="stMetric"] div[data-testid="stMetricLabel"] {
    color: #9ca3af !important;
}
div[data-testid="stMetric"] div[data-testid="stMetricValue"] { color: #ffffff !important; }

/* Tabla copiable: texto seleccionable con el mouse + ⌘C */
.tabla-copiable { overflow-x: auto; max-height: 560px; overflow-y: auto; border: 1px solid #333; border-radius: 8px; }
.tabla-copiable table { border-collapse: collapse; width: 100%; font-size: 0.85rem; user-select: text; -webkit-user-select: text; }
.tabla-copiable th, .tabla-copiable td {
    border-bottom: 1px solid #2a2a2a; padding: 6px 10px; text-align: left;
    white-space: nowrap; user-select: text; -webkit-user-select: text;
}
.tabla-copiable th { position: sticky; top: 0; background: #0e1117; color: #9ca3af; font-weight: 600; z-index: 1; }
.tabla-copiable tr:hover td { background: rgba(255,255,255,0.04); }
.tabla-copiable td { color: inherit; }
</style>""", unsafe_allow_html=True)

# ---------------------------- Ingreso al sistema ----------------------------
def _pantalla_ingreso():
    """Nadie ve datos sin identificarse. Si aún no hay usuarios, el primero que
    se crea es el dueño."""
    if not db.hay_usuarios():
        st.title("📦 Configurar el primer usuario")
        st.caption("Este primer usuario será el dueño: puede verlo y configurarlo todo, "
                   "y más adelante crear la cuenta de quien te ayude.")
        with st.form("crear_dueno"):
            n = st.text_input("Tu nombre")
            u = st.text_input("Usuario para entrar (sin espacios)")
            c1 = st.text_input("Clave", type="password")
            c2 = st.text_input("Repite la clave", type="password")
            if st.form_submit_button("Crear mi usuario", type="primary"):
                if c1 != c2:
                    st.error("Las dos claves no coinciden.")
                else:
                    try:
                        db.crear_usuario(u, n, c1, rol="dueno")
                        st.success("Usuario creado. Ahora entra con él.")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
        st.stop()

    st.title("📦 Estado de Guías")
    st.caption("Entra con tu usuario para continuar.")
    with st.form("ingreso"):
        u = st.text_input("Usuario")
        c = st.text_input("Clave", type="password")
        if st.form_submit_button("Entrar", type="primary"):
            _espera = db.segundos_bloqueo(u)
            if _espera > 0:
                st.error(f"🔒 Demasiados intentos fallidos. Espera "
                         f"{max(1, round(_espera / 60))} minuto(s) y vuelve a intentar.")
            else:
                user = db.verificar_usuario(u, c)
                if user:
                    st.session_state.user = user
                    st.rerun()
                elif db.segundos_bloqueo(u) > 0:
                    st.error(f"🔒 Cuenta bloqueada por {db.MINUTOS_BLOQUEO} minutos "
                             "tras varios intentos fallidos.")
                else:
                    st.error("Usuario o clave incorrectos, o la cuenta está desactivada.")
    st.stop()


if "user" not in st.session_state:
    _pantalla_ingreso()

USER = st.session_state.user
PERMISOS = db.permisos_de(USER)


def puede(p: str) -> bool:
    return p in PERMISOS


def get_client(cuenta: str) -> gs.DropiGTClient:
    """Un cliente por cuenta: cambiar de tienda no pisa la sesión de la otra."""
    cache = st.session_state.setdefault("dropi_clients", {})
    if cuenta not in cache:
        cache[cuenta] = gs.DropiGTClient(cuenta=cuenta)
    return cache[cuenta]


with st.sidebar:
    st.markdown(f"👤 **{USER['nombre']}** · {db.ROLES[USER['rol']]['label']}")
    if st.button("Salir"):
        st.session_state.clear()
        st.rerun()
    st.divider()
    st.header("Sesión Dropi GT")

    disponibles = gs.cuentas_configuradas() or [gs.CUENTA_POR_DEFECTO]
    _opciones = list(gs.CUENTAS.keys())
    cuenta = st.selectbox(
        "Tienda",
        options=_opciones,
        index=_opciones.index(gs.CUENTA_POR_DEFECTO),
        format_func=lambda k: gs.CUENTAS[k]["label"] + ("" if k in disponibles else "  (sin configurar)"),
        key="cuenta_sel",
    )

    if cuenta not in disponibles and not puede("gestionar_accesos"):
        st.warning("Esta tienda no tiene accesos. Pídele al dueño que la conecte.")
    elif cuenta not in disponibles:
        st.warning("Esta tienda todavía no tiene accesos guardados.")
        with st.form(key=f"alta_{cuenta}"):
            st.markdown(f"**Conectar {gs.CUENTAS[cuenta]['label']} con Dropi**")
            st.caption("Llena esto una sola vez. Queda guardado en tu computadora.")
            n_email = st.text_input("Correo con el que entras a Dropi")
            n_pass = st.text_input("Contraseña de Dropi", type="password")
            if st.form_submit_button("Guardar accesos", type="primary"):
                try:
                    gs.guardar_credenciales(cuenta, email=n_email, password=n_pass)
                    st.session_state.pop("dropi_clients", None)
                    st.success("Accesos guardados.")
                    st.rerun()
                except Exception as e:
                    st.error(f"No se pudo guardar: {e}")

client = get_client(st.session_state.get("cuenta_sel", gs.CUENTA_POR_DEFECTO))

st.title(f"📦 Estado de Guías — {client.cuenta_label}")
st.caption("Cada guía traducida a lenguaje simple: qué pasó y qué hacer al respecto.")

with st.sidebar:

    if not client.token:
        st.warning("Sin sesión activa.")

        if st.button("Iniciar sesión", type="primary"):
            try:
                client.login()
                st.success("Login OK")
                st.rerun()
            except gs.Necesita2FA as e:
                st.session_state.needs_2fa = True
                st.session_state.otp_contacts = e.contactos
                st.session_state.otp_sent = False
            except Exception as e:
                st.error(f"Error de login: {e}")

        if st.session_state.get("needs_2fa"):
            contactos = st.session_state.get("otp_contacts") or []
            st.info("Dropi exige un código de verificación en cada inicio de sesión.")

            if contactos:
                elegido = st.selectbox(
                    "¿A dónde enviamos el código?",
                    options=[c["val"] for c in contactos],
                    format_func=lambda v: next(
                        (c.get("text") or v for c in contactos if c["val"] == v), v
                    ),
                )
            else:
                elegido = None

            if st.button("Enviar código"):
                try:
                    client.send_otp(contacto=elegido)
                    st.session_state.otp_sent = True
                    st.success("Código enviado.")
                except Exception as e:
                    st.error(f"No se pudo enviar el código: {e}")

            code = st.text_input("Código de 6 dígitos", max_chars=6)
            if st.button("Confirmar código"):
                try:
                    client.verify_otp(code)
                    st.session_state.needs_2fa = False
                    st.success("Sesión iniciada.")
                    st.rerun()
                except Exception as e:
                    st.error(f"{e}")

        st.divider()
        st.markdown("**Entrar pegando el token**")
        st.caption("Si ya tienes el token copiado, pégalo aquí y listo: no pide usuario, contraseña ni código.")
        manual_token = st.text_input("Pega el token aquí", type="password", key=f"tok_{client.cuenta}")
        if st.button("Entrar con este token"):
            if not manual_token.strip():
                st.error("Pega el token primero.")
            else:
                try:
                    client.set_token(manual_token)
                    gs.get_all_guides(client, auto_relogin=False)
                    st.success("Listo, sesión iniciada.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Ese token no sirvió: {e}")
    else:
        _vence = gs.vencimiento_token(client.token)
        if _vence is None:
            st.success("Sesión activa")
        else:
            _faltan = (_vence - datetime.now()).total_seconds() / 3600
            if _faltan <= 0:
                st.error(f"⛔ El token venció el {_vence.strftime('%d/%m a las %I:%M %p').lower()}.\n\n"
                         "Pega uno nuevo abajo para volver a cargar guías.")
            elif _faltan <= 2:
                st.warning(f"⏳ El token vence hoy a las {_vence.strftime('%I:%M %p').lower()} "
                           f"(faltan {int(_faltan * 60)} min). Conviene renovarlo ya.")
            else:
                st.success(f"Sesión activa · vence a las {_vence.strftime('%I:%M %p').lower()}")

        # Campo de renovación: aparece solo cuando faltan 2 horas o menos.
        if _vence is not None and (_vence - datetime.now()).total_seconds() <= 2 * 3600:
            st.markdown("**Pegar token nuevo**")
            _tok2 = st.text_input("Token", type="password", key=f"renov_{client.cuenta}",
                                  label_visibility="collapsed")
            if st.button("Renovar token", type="primary"):
                if not _tok2.strip():
                    st.error("Pega el token primero.")
                else:
                    try:
                        client.set_token(_tok2)
                        gs.get_all_guides(client, auto_relogin=False, days=1)
                        st.session_state.pop("guides_clave", None)
                        st.success("Token renovado.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Ese token no sirvió: {e}")

        if st.button("Cerrar sesión"):
            client.token = None
            client.session.headers.pop("Authorization", None)
            if client.session_file.exists():
                client.session_file.unlink()
            st.rerun()


    st.divider()
    st.markdown("**📅 Período de pedidos**")
    st.caption("Qué pedidos traer, según su fecha de creación.")
    _hoy = datetime.now().date()
    _rangos = {
        "Últimos 7 días": 7, "Últimos 15 días": 15, "Últimos 30 días": 30,
        "Últimos 60 días": 60, "Últimos 90 días": 90, "Elegir fechas…": None,
    }
    _elegido = st.selectbox("Período", list(_rangos.keys()), index=2, key="rango_sel")
    if _rangos[_elegido] is None:
        _c1, _c2 = st.columns(2)
        with _c1:
            fecha_desde = st.date_input("Desde", value=_hoy - timedelta(days=30),
                                        format="DD/MM/YYYY", key="f_desde")
        with _c2:
            fecha_hasta = st.date_input("Hasta", value=_hoy, format="DD/MM/YYYY", key="f_hasta")
    else:
        fecha_desde = _hoy - timedelta(days=_rangos[_elegido])
        fecha_hasta = _hoy

    if fecha_desde > fecha_hasta:
        st.error("La fecha 'Desde' es posterior a 'Hasta'. Corrígela para poder cargar.")
        st.stop()

    st.divider()
    stale_threshold = st.number_input("Alertar guías sin movimiento por más de (días)", min_value=1, value=2)

if puede("gestionar_usuarios"):
    with st.expander("👥 Usuarios del sistema"):
        st.caption("Cada persona entra con su propio usuario, y todo lo que anote queda a su nombre.")
        _us = db.listar_usuarios()
        st.dataframe(
            pd.DataFrame([{
                "Nombre": u["nombre"], "Usuario": u["usuario"],
                "Rol": db.ROLES[u["rol"]]["label"],
                "Estado": "Activo" if u["activo"] else "Desactivado",
                "Último ingreso": (u["ultimo_ingreso"] or "—")[:16].replace("T", " "),
            } for u in _us]),
            hide_index=True, width="stretch",
        )

        st.markdown("**Crear un usuario nuevo**")
        with st.form("nuevo_usuario", clear_on_submit=True):
            _c1, _c2 = st.columns(2)
            with _c1:
                _nn = st.text_input("Nombre de la persona")
                _nu = st.text_input("Usuario para entrar (sin espacios)")
            with _c2:
                _nr = st.selectbox("Rol", list(db.ROLES.keys()),
                                   index=1, format_func=lambda r: db.ROLES[r]["label"])
                _nc = st.text_input("Clave provisional", type="password")
            if st.form_submit_button("Crear usuario", type="primary"):
                try:
                    db.crear_usuario(_nu, _nn, _nc, rol=_nr)
                    st.success(f"Usuario creado. Entrégale el usuario y la clave a {_nn}.")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

        st.markdown("**Qué puede ver el asesor**")
        _cfg_p = db.get_settings()
        _p1, _p2 = st.columns(2)
        with _p1:
            _vk = st.checkbox("Ver KPIs del equipo", value=_cfg_p.get("asesor_ve_kpis") == "1")
        with _p2:
            _vb = st.checkbox("Ver la bitácora completa", value=_cfg_p.get("asesor_ve_bitacora") == "1")
        if st.button("Guardar permisos"):
            db.save_settings({"asesor_ve_kpis": "1" if _vk else "0",
                              "asesor_ve_bitacora": "1" if _vb else "0"})
            st.success("Permisos actualizados.")
            st.rerun()

        st.markdown("**Cambiar clave o desactivar**")
        _sel_u = st.selectbox("Usuario", [u["usuario"] for u in _us], key="admin_user_sel")
        _q1, _q2 = st.columns([2, 1])
        with _q1:
            _cc = st.text_input("Clave nueva", type="password", key="admin_pass")
            if st.button("Cambiar clave"):
                try:
                    db.cambiar_clave(_sel_u, _cc)
                    st.success("Clave cambiada.")
                except ValueError as e:
                    st.error(str(e))
        with _q2:
            _act = next(u["activo"] for u in _us if u["usuario"] == _sel_u)
            if _sel_u == USER["usuario"]:
                st.caption("No puedes desactivar tu propia cuenta.")
            elif st.button("Desactivar" if _act else "Reactivar"):
                db.activar_usuario(_sel_u, not _act)
                st.rerun()

if puede("configurar_reglas"):
  with st.expander("⚙️ Configuración de reglas de confirmación"):
    st.caption("Estas reglas se aplican solas cuando abres una guía en \"Pendiente confirmación\" más abajo. Guárdalas una vez y quedan para siempre.")
    cfg = db.get_settings()
    c1, c2 = st.columns(2)
    with c1:
        umbral_bajo = st.number_input("Hasta este % de devolución → despachar directo", min_value=0.0, max_value=100.0,
                                       value=float(cfg["umbral_bajo"] or 49), step=1.0)
        umbral_medio = st.number_input("Hasta este % → llamar sin pedir flete (arriba de esto, pedir flete anticipado)",
                                        min_value=0.0, max_value=100.0, value=float(cfg["umbral_medio"] or 60), step=1.0)
        zonas_excluidas = st.text_area("Zonas sin cobertura (separadas por coma)", value=cfg["zonas_excluidas"],
                                        placeholder="ej: Petén, Izabal")
    with c2:
        monto_flete = st.text_input("Monto del flete anticipado (Q)", value=cfg["monto_flete_anticipado"])
        banco = st.text_input("Banco", value=cfg["banco"])
        cuenta_bancaria_val = st.text_input("Cuenta bancaria", value=cfg["cuenta_bancaria"])
    st.caption("Excepción: cliente con muchos pedidos y buen historial que aun así no confirma → no se cancela solo, se escala.")
    c3, c4 = st.columns(2)
    with c3:
        excepcion_pedidos = st.number_input("Mínimo de pedidos históricos para la excepción", min_value=1,
                                             value=int(float(cfg["excepcion_pedidos_min"] or 10)))
    with c4:
        excepcion_dev = st.number_input("% de devolución máximo para la excepción", min_value=0.0, max_value=100.0,
                                         value=float(cfg["excepcion_devolucion_max"] or 10))
    if st.button("Guardar configuración"):
        db.save_settings({
            "umbral_bajo": umbral_bajo, "umbral_medio": umbral_medio,
            "monto_flete_anticipado": monto_flete, "banco": banco, "cuenta_bancaria": cuenta_bancaria_val,
            "zonas_excluidas": zonas_excluidas,
            "excepcion_pedidos_min": excepcion_pedidos, "excepcion_devolucion_max": excepcion_dev,
        })
        st.success("Configuración guardada.")

if not client.token:
    st.info("Inicia sesión en la barra lateral para cargar las guías.")
    st.stop()

_clave_carga = (client.cuenta, str(fecha_desde), str(fecha_hasta))
if st.session_state.get("guides_clave") != _clave_carga:
    st.session_state.pop("guides_df", None)
    st.session_state.pop("guides_raw", None)
    st.session_state.pop("new_incidents", None)

if st.button("🔄 Actualizar guías", type="primary") or "guides_df" not in st.session_state:
    with st.spinner("Consultando órdenes activas en Dropi GT..."):
        try:
            guides, resumen = gs.get_guides_con_resumen(
                client, desde=str(fecha_desde), hasta=str(fecha_hasta))
        except gs.TokenExpirado:
            st.error("⛔ **El token de Dropi venció.** Pega uno nuevo en la barra "
                     "de la izquierda (abajo dice *Pegar token nuevo*) y vuelve a intentar.")
            st.stop()
        except Exception as e:
            _txt = str(e).lower()
            if "token" in _txt or "401" in _txt:
                st.error("⛔ **El token de Dropi ya no sirve.** Pega uno nuevo en la barra de la izquierda.")
            else:
                st.error(f"No se pudieron cargar las guías: {e}")
            st.stop()

        guides = [g for g in guides if g["category"] not in ("entregado", "cancelado")]

        diff = db.save_snapshot(guides)
        st.session_state.guides_raw = guides
        st.session_state.guides_df = pd.DataFrame(guides)
        st.session_state.new_incidents = diff["new_incidents"]
        st.session_state.last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st.session_state.guides_clave = _clave_carga
        st.session_state.resumen = resumen

df = st.session_state.get("guides_df", pd.DataFrame())
new_incidents = st.session_state.get("new_incidents", [])

if df.empty:
    st.warning("No hay guías activas en este momento (o aún no se ha actualizado).")
    st.stop()

st.caption(f"Última actualización: {st.session_state.get('last_update', '—')} · Período: {fecha_desde.strftime('%d/%m/%Y')} → {fecha_hasta.strftime('%d/%m/%Y')}")

# Trazabilidad: última gestión registrada de cada guía (una sola consulta).
_ultimas = db.get_last_contacts(df["order_id"].tolist())


def _resumen_gestion(oid):
    c = _ultimas.get(oid)
    if not c:
        return ""
    cuando = (c["created_at"] or "")[:10]
    quien = c.get("agent") or "?"
    res = db.RESULT_LABELS.get(c.get("result"), c.get("result") or "")
    nota = (c.get("notes") or "").strip()
    txt = f"{cuando} · {quien} · {res}"
    return f"{txt} — {nota}" if nota else txt


df["ultima_gestion"] = df["order_id"].map(_resumen_gestion)
df["gestionada"] = df["order_id"].map(lambda o: "✅" if o in _ultimas else "")
df["entrega_txt"] = df["tipo_entrega"].map(lambda t: gs.ENTREGA_LABEL.get(t, t))

if new_incidents:
    st.error(f"🚨 {len(new_incidents)} guía(s) pasaron a URGENTE desde la última revisión")
    for inc in new_incidents:
        st.markdown(
            f"- **{inc['guide']}** ({inc['carrier']}) — {inc['customer']}: "
            f"**{inc['label_es']}** → _{inc['action_es']}_"
        )

stale_df = df[(df["days_since_movement"] >= stale_threshold) & (~df["dropi_status"].isin(gs.FINAL_STATUSES))]

# "finalizado" (entregado/cancelado/rechazado) no se lista: esas guías se
# descartan antes de llegar al tablero, así que la categoría siempre da 0.
# La devolución no es un nivel de urgencia: el pedido ya terminó y no hay
# nada que gestionar. Se cuenta aparte y solo se lista si se pide ver.
SEVERITY_LABEL = {
    "urgente": "Urgente", "atencion": "En seguimiento", "ok": "Sin problema",
}
SEVERITY_ORDER = {"urgente": 0, "devolucion": 1, "atencion": 2, "ok": 3, "finalizado": 4}

_res = st.session_state.get("resumen", {})

st.markdown("**Pendientes de gestionar**")
col1, col2, col3, col4 = st.columns(4)
col1.metric("🔴 Necesitan acción hoy", int((df["severity"] == "urgente").sum()))
col2.metric("🟡 En seguimiento", int((df["severity"] == "atencion").sum()))
col3.metric("🟢 En camino sin problema", int((df["severity"] == "ok").sum()))
col4.metric("🏢 Recogen en agencia", int((df["tipo_entrega"] == "agencia").sum()),
            help="El cliente va a recoger a un punto de la transportadora: hay que avisarle cuando llegue. "
                 "Se detecta por el texto de la dirección, porque Dropi no lo marca en ningún campo.")

if _res:
    st.markdown("**Cerrados en el período** (ya no requieren acción)")
    d1, d2, d3, d4, d5 = st.columns(5)
    d1.metric("✅ Entregados", _res.get("entregados", 0))
    d2.metric("❌ Cancelados", _res.get("cancelados", 0))
    d5.metric("🔄 Devueltos", int((df["severity"] == "devolucion").sum()))
    d3.metric("📦 Pedidos del período", _res.get("total", 0))
    _pct = _res.get("pct_entrega")
    d4.metric("% de entrega", f"{_pct}%" if _pct is not None else "—",
              help="Entregados sobre los pedidos que ya terminaron (entregados + cancelados). "
                   "No cuenta los que siguen en camino, para no verse artificialmente bajo.")

st.caption(f"Sin movimiento ≥{stale_threshold}d: {len(stale_df)} guía(s) — ver filtro abajo.")

st.divider()
st.subheader("🗒️ Anotar una gestión")
st.caption("Cada llamada, chat o mensaje queda registrado con fecha, hora y quién lo hizo.")

# Las devoluciones no se anotan aquí: ahí la decisión es reintentar o hacer
# nota de crédito, no gestionar al cliente. Se trabajan por su propia vía.
_anotables = df[df["severity"] != "devolucion"]
_n_dev = int((df["severity"] == "devolucion").sum())

_pend = _anotables[_anotables["gestionada"] == ""]
st.caption(
    f"{len(_pend)} de {len(_anotables)} guías todavía sin ninguna anotación."
    + (f"  ·  {_n_dev} en devolución quedan fuera de esta lista." if _n_dev else "")
)

_busq = st.text_input("Busca la guía por cliente, teléfono, número de guía o ciudad",
                      placeholder="ej: 5512 · Maria · Xela · 240033…", key="busca_guia")

_cand = _anotables.copy()
if _busq.strip():
    _t = _busq.strip().lower()
    _cand = _cand[_cand.apply(
        lambda r: _t in " ".join(str(r.get(c) or "").lower()
                                 for c in ("customer", "phone", "guide", "city", "state")), axis=1)]

_cand = _cand.assign(_o=_cand["severity"].map(SEVERITY_ORDER)).sort_values(["_o", "days_since_movement"],
                                                                           ascending=[True, False])

if _cand.empty:
    st.info("Ninguna guía para anotar con esa búsqueda."
            + (" (Las devoluciones no entran aquí.)" if _n_dev else ""))
else:
    def _rotulo(oid):
        r = df[df["order_id"] == oid].iloc[0]
        marca = "✅" if r["gestionada"] else gs.SEVERITY_EMOJI.get(r["severity"], "")
        return f"{marca} {r['customer']} · {r['phone']} · {r['city']} · {r['label_es'][:38]}"

    _sel = st.selectbox("Guía a anotar", options=_cand["order_id"].tolist()[:300],
                        format_func=_rotulo, key="guia_gestion")
    _row = df[df["order_id"] == _sel].iloc[0]

    _prev = db.get_contact_history(int(_sel))
    if _prev:
        st.markdown("**Lo que ya se hizo con este cliente:**")
        for _p in _prev:
            st.caption(
                f"· {_p['created_at'][:16].replace('T',' ')} — {_p['agent']} — "
                f"{db.METHOD_LABELS.get(_p['method'], _p['method'])} → "
                f"{db.RESULT_LABELS.get(_p['result'], _p['result'])}"
                + (f" — _{_p['notes']}_" if _p.get('notes') else "")
            )
    else:
        st.caption("Todavía no hay ninguna anotación para esta guía.")

    with st.form(key=f"anotar_{_sel}", clear_on_submit=True):
        _ag = USER["nombre"]
        st.caption(f"Se va a registrar a nombre de **{_ag}**.")
        _a2, _a3 = st.columns(2)
        with _a2:
            _me = st.selectbox("¿Por dónde?", list(db.METHOD_LABELS.keys()),
                               format_func=lambda k: db.METHOD_LABELS[k])
        with _a3:
            _re = st.selectbox("¿Qué pasó?", list(db.RESULT_LABELS.keys()),
                               format_func=lambda k: db.RESULT_LABELS[k])
        _no = st.text_area("¿Qué se habló? ¿Qué sigue?", height=80,
                           placeholder="ej: Pidió que le llegue el jueves después de las 2pm.")
        if st.form_submit_button("Guardar anotación", type="primary"):
            db.log_contact(int(_sel), _row["guide"], _row["customer"], _row.get("phone"),
                           _ag, _me, _re, _no.strip())
            st.success("Anotación guardada.")
            st.rerun()

st.divider()

carriers = sorted(df["carrier"].dropna().unique().tolist())
estados = sorted(df["dropi_status"].fillna("").str.strip().str.upper().unique().tolist())


def _nombre_estado(e):
    """Muestra el estado en palabras, con cuántas guías hay en él."""
    info = gs.STATUS_INFO.get(e)
    bonito = info["label"] if info else (e or "(sin estado)")
    n = int((df["dropi_status"].fillna("").str.strip().str.upper() == e).sum())
    return f"{bonito} ({n})"


f1, f2 = st.columns(2)
with f1:
    selected_carriers = st.multiselect("Transportadora", carriers, default=carriers)
with f2:
    selected_severities = st.multiselect(
        "Urgencia", list(SEVERITY_LABEL.keys()),
        default=list(SEVERITY_LABEL.keys()),
        format_func=lambda s: f"{gs.SEVERITY_EMOJI[s]} {SEVERITY_LABEL[s]}",
    )

f3, f4 = st.columns([3, 1])
with f3:
    _TODOS = "__todos__"

    def _n_entrega(t):
        return int((df["tipo_entrega"] == t).sum())

    # Las opciones de "recoge en agencia" van en la misma lista: no son un
    # estado de Dropi, pero es donde se buscan.
    _extras = [f"ent:{t}" for t in ("agencia", "revisar") if _n_entrega(t)]
    _opts_estado = [_TODOS] + _extras + estados

    def _rotulo_estado(e):
        if e == _TODOS:
            return f"Todos ({len(df)})"
        if e.startswith("ent:"):
            t = e[4:]
            return f"{gs.ENTREGA_LABEL[t]} ({_n_entrega(t)})"
        return _nombre_estado(e)

    _estado_uno = st.selectbox(
        "Estado del pedido — elige uno para verlo solo a él",
        _opts_estado, format_func=_rotulo_estado, key="estado_sel",
    )

    if _estado_uno == _TODOS:
        selected_estados, entrega_sel = estados, None
    elif _estado_uno.startswith("ent:"):
        selected_estados, entrega_sel = estados, _estado_uno[4:]
    else:
        selected_estados, entrega_sel = [_estado_uno], None
with f4:
    st.write("")
    only_stale = st.checkbox(f"Solo estancadas (≥{stale_threshold}d)")
    ver_devoluciones = st.checkbox("Ver devoluciones", value=False,
                                   help="Las devoluciones ya no requieren gestión; se ocultan por defecto.")


_est_norm = df["dropi_status"].fillna("").str.strip().str.upper()
_sev_permitidas = list(selected_severities) + (["devolucion"] if ver_devoluciones else [])
view = df[df["carrier"].isin(selected_carriers)
          & df["severity"].isin(_sev_permitidas)
          & _est_norm.isin(selected_estados)]
if entrega_sel:
    view = view[view["tipo_entrega"] == entrega_sel]
if only_stale:
    view = view[view["order_id"].isin(stale_df["order_id"])]

view = view.assign(_orden=view["severity"].map(SEVERITY_ORDER).fillna(9))
view = view.sort_values(["_orden", "days_since_movement"], ascending=[True, False])


# Nombre corto y bien escrito de cada estado de Dropi (su texto viene sin
# tildes y en mayúsculas). Lo que no esté aquí se muestra tal cual.
ESTADO_CORTO = {
    "PENDIENTE CONFIRMACION": "Pendiente confirmación",
    "PENDIENTE CONFIRMACIÓN": "Pendiente confirmación",
    "PENDIENTE": "Pendiente de envío",
    "INCIDENCIA EN RUTA": "Incidencia en ruta",
    "INCIDENCIA VALIDADA": "Incidencia validada",
    "NOVEDAD": "Novedad",
    "NOVEDAD SOLUCIONADA": "Novedad solucionada",
    "SOLUCION APROBADA": "Solución aprobada",
    "SOLUCION INCORRECTA": "Solución incorrecta",
    "EN RUTA": "En ruta",
    "EN REPARTO": "En reparto",
    "EN TRANSITO": "En tránsito",
    "RECOLECTADO": "Recolectado",
    "EN BODEGA ORIGEN": "En bodega origen",
    "EN INVENTARIO": "En inventario",
    "DEVOLUCION": "Devolución",
    "RECHAZADO": "Rechazado",
}


def _etiqueta_urgencia(fila):
    """Color de urgencia + el estado REAL de Dropi. No es lo mismo una
    incidencia que un paquete estancado, y antes se veían iguales."""
    emoji = gs.SEVERITY_EMOJI.get(fila["severity"], "•")
    crudo = fila.get("dropi_status")
    # Ojo: pandas convierte los vacíos en NaN (float), no en None.
    crudo = crudo.strip() if isinstance(crudo, str) else ""
    estado = ESTADO_CORTO.get(crudo.upper()) or (
        crudo.capitalize() if crudo else SEVERITY_LABEL.get(fila["severity"], "Sin estado")
    )
    if "estancado" in str(fila.get("label_es") or "").lower():
        estado += " (estancado)"
    return f"{emoji} {estado}"


show = view.copy()
show["Urgencia"] = show.apply(_etiqueta_urgencia, axis=1)
display_cols = {
    "guide": "Guía", "carrier": "Transportadora", "customer": "Cliente",
    "phone": "Teléfono", "state": "Departamento", "city": "Ciudad",
    "label_es": "Qué pasó", "action_es": "Qué hacer",
    "Urgencia": "Urgencia", "days_since_movement": "Días sin mover",
    "entrega_txt": "Entrega", "direccion": "Dirección",
    "gestionada": "¿Gestionada?", "ultima_gestion": "Última gestión",
}
show = show[list(display_cols.keys())].rename(columns=display_cols)

vista = st.radio(
    "Vista de la tabla",
    ["📋 Copiable (selecciona con el mouse y ⌘C)", "⚙️ Interactiva (ordenar, buscar, descargar)"],
    horizontal=True, label_visibility="collapsed",
)

if vista.startswith("📋"):
    st.caption("Selecciona el texto con el mouse y presiona **⌘ C** para copiar, como en cualquier página web.")
    st.markdown(
        f'<div class="tabla-copiable">{show.to_html(index=False, escape=False, border=0)}</div>',
        unsafe_allow_html=True,
    )
else:
    st.caption("Haz clic en una celda (o arrastra para seleccionar varias) y presiona **⌘ C**. Usa los íconos de la esquina para buscar o descargar en CSV.")
    st.dataframe(show, width="stretch", height=500, hide_index=True)

with st.expander("📋 Copiar datos (teléfonos y tabla completa)"):
    solo_alertas = st.checkbox("Solo las que necesitan acción 🔴", value=True, key="copy_alertas")
    base = view[view["severity"] == "urgente"] if solo_alertas else view

    st.markdown("**Teléfonos** (uno por línea — usa el botón de copiar de la esquina):")
    tels = [
        f"{r['phone']}  —  {r['customer']}"
        for _, r in base.iterrows() if r.get("phone")
    ]
    st.code("\n".join(tels) or "(sin teléfonos)", language=None)

    st.markdown("**Tabla completa** (pégala en Excel o Google Sheets):")
    tabla = base.copy()
    tabla["Urgencia"] = tabla.apply(_etiqueta_urgencia, axis=1)
    tabla = tabla[list(display_cols.keys())].rename(columns=display_cols)
    st.code(tabla.to_csv(sep="\t", index=False), language=None)

st.divider()
st.subheader("Ver historial completo de una guía")
guide_options = view["guide"].tolist()
if guide_options:
    picked = st.selectbox("Guía", guide_options)
    row = next(g for g in st.session_state.guides_raw if g["guide"] == picked)
    st.markdown(
        f"**Cliente:** {row['customer']} — **Tel:** {row.get('phone') or '—'} "
        f"— **Transportadora:** {row['carrier']}  \n"
        f"**Ubicación:** {row.get('city') or '—'}, {row.get('state') or '—'}  \n"
        f"{gs.SEVERITY_EMOJI[row['severity']]} **{row['label_es']}** → _{row['action_es']}_"
    )
    st.caption(f"Estado Dropi (crudo): {row['dropi_status']}")
    st.divider()
    for m in row["movements"]:
        icon = gs.SEVERITY_EMOJI.get(m["severity"], "•")
        st.markdown(f"{icon} `{m['at']}` — **{m['label_es']}**")
        st.caption(f"Texto original: {m['text']}")

    st.divider()
    st.markdown("**Historial local de este cliente** (se enriquece con el uso diario del tablero)")
    hist = db.get_customer_history(row.get("phone"))
    n_activas, n_descartadas = len(hist["activas"]), len(hist["descartadas"])
    if n_activas + n_descartadas <= 1:
        st.caption("Sin historial previo registrado todavía para este teléfono.")
    else:
        st.caption(f"{n_activas} guía(s) activa(s) vistas antes, {n_descartadas} finalizada(s) (entregado/cancelado) desde que corre este tablero.")
        for d in hist["descartadas"][:10]:
            st.caption(f"· {d['discarded_at'][:10]} — {d['dropi_status']} — guía {d['guide']}")

    if "PENDIENTE CONFIRMACION" in (row.get("dropi_status") or "").upper():
        st.divider()
        st.markdown("**Sugerencia según las reglas configuradas**")
        rec = db.evaluar_regla_confirmacion(row.get("phone"), city=row.get("city"), state=row.get("state"),
                                             exclude_order_id=row["order_id"])
        ACCION_UI = {
            "despachar_directo": ("🟢", "Despachar directo, sin pedir nada por adelantado."),
            "llamar_sin_flete": ("🟡", "Llamar para confirmar, sin pedir el flete por adelantado."),
            "pedir_flete_anticipado": ("🔴", "Solo despachar con flete anticipado pagado."),
            "escalar_supervisor": ("🟣", "No cancelar por criterio propio: escalar a supervisor."),
            "zona_excluida": ("⚫", "Zona fuera de cobertura configurada."),
            "sin_datos": ("⚪", "Sin suficiente historial — decidir con criterio propio."),
        }
        icon, titulo = ACCION_UI.get(rec["accion"], ("⚪", "—"))
        st.markdown(f"{icon} **{titulo}**")
        st.caption(rec["motivo"])
        if rec["perfil"]:
            p = rec["perfil"]
            st.caption(f"Historial: {p['entregados']} entregado(s), {p['devueltos']} devuelto(s) de {p['total_finalizados']} pedido(s) con desenlace conocido.")
        if rec["mensaje_abono"]:
            st.markdown("**Mensaje para pedir el abono** (copiar y enviar por LucidBot/WhatsApp):")
            st.code(rec["mensaje_abono"], language=None)
        elif rec["accion"] == "pedir_flete_anticipado":
            st.warning("Falta configurar el monto del flete y la cuenta bancaria arriba en \"⚙️ Configuración de reglas de confirmación\" para generar el mensaje automáticamente.")

    st.divider()
    _n_ges = len(db.get_contact_history(row["order_id"]))
    st.caption(
        f"Esta guía tiene {_n_ges} anotación(es). Para agregar una nueva usa "
        "**🗒️ Anotar una gestión**, más arriba en la página."
        if _n_ges else
        "Sin anotaciones todavía. Para agregar una usa **🗒️ Anotar una gestión**, más arriba."
    )

st.divider()
st.divider()
if puede("ver_bitacora"):
  with st.expander("📖 Bitácora — todo lo gestionado, con fecha y hora"):
    _dias_b = st.selectbox("Mostrar", [7, 15, 30, 90, 0],
                           format_func=lambda d: "Todo el historial" if d == 0 else f"Últimos {d} días",
                           index=2, key="bitacora_dias")
    _bit = db.get_all_contacts(days=None if _dias_b == 0 else _dias_b)
    if not _bit:
        st.caption("Todavía no hay nada anotado. Usa \"🗒️ Anotar una gestión\" más arriba.")
    else:
        _bdf = pd.DataFrame(_bit)
        _bdf["Fecha y hora"] = _bdf["created_at"].str[:16].str.replace("T", " ", regex=False)
        _bdf["Por dónde"] = _bdf["method"].map(lambda m: db.METHOD_LABELS.get(m, m))
        _bdf["Qué pasó"] = _bdf["result"].map(lambda r: db.RESULT_LABELS.get(r, r))
        _vista = _bdf.rename(columns={"guide": "Guía", "customer": "Cliente",
                                       "phone": "Teléfono", "agent": "Quién",
                                       "notes": "Notas"})[
            ["Fecha y hora", "Guía", "Cliente", "Teléfono", "Quién", "Por dónde", "Qué pasó", "Notas"]]
        st.caption(f"{len(_vista)} anotación(es).")
        st.dataframe(_vista, hide_index=True, width="stretch", height=340)
        st.markdown("**Para pegar en Excel** (copia con el botón de la esquina):")
        st.code(_vista.to_csv(sep="\t", index=False), language=None)

if puede("ver_kpis"):
  with st.expander("📊 KPIs del equipo (gestiones registradas)"):
    kpi_days = st.slider("Ventana (días)", min_value=1, max_value=90, value=30, key="kpi_days")
    log = db.get_kpi_summary(days=kpi_days)
    if not log:
        st.caption("Todavía no hay gestiones registradas. Aparecerán aquí en cuanto se use el formulario de arriba.")
    else:
        log_df = pd.DataFrame(log)
        log_df["fecha"] = log_df["created_at"].str[:10]

        resumen = (
            log_df.groupby("agent")
            .agg(gestiones=("id", "count"),
                 confirmados=("result", lambda s: (s == "confirmado").sum()),
                 novedades_resueltas=("result", lambda s: (s == "novedad_resuelta").sum()),
                 no_contesta=("result", lambda s: (s == "no_contesta").sum()))
            .reset_index()
            .rename(columns={"agent": "Agente", "gestiones": "Gestiones",
                              "confirmados": "Confirmados", "novedades_resueltas": "Novedades resueltas",
                              "no_contesta": "No contesta"})
            .sort_values("Gestiones", ascending=False)
        )
        st.dataframe(resumen, hide_index=True, width="stretch")

        por_dia = log_df.groupby(["fecha", "agent"]).size().reset_index(name="Gestiones")
        st.markdown("**Gestiones por día y agente**")
        st.dataframe(
            por_dia.rename(columns={"fecha": "Fecha", "agent": "Agente"}).sort_values("Fecha", ascending=False),
            hide_index=True, width="stretch", height=300,
        )
