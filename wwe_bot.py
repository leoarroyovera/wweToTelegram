#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot de Telegram para controlar wweToTelegram.

Un bot aparte (cuenta de bot, no la de usuario): no publica nada, solo
lanza/detiene los modos de wwe_telethon.py como subprocesos y lee su estado
de wwe_seen.sqlite3 y wwe_telethon.log. Usa su propia sesion
(wwe_bot.session), asi que nunca compite por wwe_session.session con el
proceso que publica.

Menu (boton "Menu" de Telegram y /menu con botones):
    /estado            progreso de todos los modos, procesos y locks
    /log [n]           las n ultimas imagenes publicadas (defecto 10)
    /tail [n]          las n ultimas lineas de wwe_telethon.log
    /run <modo>        lanza un modo (portada, photos, watch...)
    /stop <modo>       detiene un modo lanzado desde el bot
    /calidad           reintenta el original de lo subido en preset

Configuracion (.env, junto a TG_API_ID/TG_API_HASH):
    WWE_BOT_TOKEN      token de @BotFather (obligatorio)
    WWE_BOT_ADMINS     ids de Telegram autorizados, separados por coma.
                       Si falta, el bot contesta a cualquiera con su id
                       para poder configurarlo, pero no obedece a nadie.
    WWE_BOT_AUTOSTART  modos a lanzar al arrancar el bot (p.ej. "watch"),
                       para dejar el bot como unico always-on task.

Uso:
    python wwe_bot.py
"""

import asyncio
import html
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

from telethon import Button, TelegramClient, events
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

# Importar wwe_telethon carga el .env (load_env) y deja las mismas rutas
# y credenciales que usa el proceso que publica.
import wwe_telethon as wwe

BOT_TOKEN = os.environ.get("WWE_BOT_TOKEN", "")
ADMINS = {int(x) for x in os.environ.get("WWE_BOT_ADMINS", "").replace(" ", "").split(",")
          if x.lstrip("-").isdigit()}
AUTOSTART = [x.strip() for x in os.environ.get("WWE_BOT_AUTOSTART", "").split(",")
             if x.strip()]
# Modos que el bot relanza solo si mueren sin que nadie los detuviera: la
# vigilancia tiene que seguir corriendo aunque falle (red, Telegram, OOM).
KEEP_ALIVE = set(AUTOSTART) | {"watch"}
# Espera antes de cada relanzamiento consecutivo; si el proceso aguanto mas
# de RESTART_RESET_SECONDS se considera sano y la cuenta vuelve a empezar.
RESTART_DELAYS = [10, 30, 60, 300, 600]
RESTART_RESET_SECONDS = 600

SCRIPT = wwe.ROOT / "wwe_telethon.py"
BOT_LOG_PATH = wwe.ROOT / "wwe_bot.log"
MSG_MAX = 4000  # Telegram corta en 4096; margen para las etiquetas HTML
LOG_DEFAULT, LOG_MAX = 10, 100
TAIL_DEFAULT, TAIL_MAX = 30, 200
STOP_GRACE_SECONDS = 20

# Modo -> (argumentos de wwe_telethon.py, descripcion). El orden es el del
# submenu "Ejecutar".
MODOS = {
    "portada": ([], "Portada: novedades de la home"),
    "watch": (["--watch"], "Vigilancia continua de todo"),
    "photos": (["--photos"], "Galerias (/photos), reanudable"),
    "superstars": (["--superstars"], "Superstars, reanudable"),
    "shows": (["--shows"], "Hub /shows"),
    "events": (["--events"], "Proximos eventos (/events)"),
    "events-local": (["--events-local"], "Logos de eventos cercanos"),
    "backfill-all": (["--backfill-all"], "Volcado historico completo"),
    "calidad": (["--upgrade-quality"], "Reintentar originales (calidad)"),
    "dry-run": (["--dry-run"], "Ensayo de portada sin publicar"),
}

COMANDOS = [
    ("menu", "Menu con todas las opciones"),
    ("estado", "Progreso, procesos y locks"),
    ("log", "Ultimas N imagenes cargadas (/log 20)"),
    ("tail", "Ultimas N lineas del log (/tail 50)"),
    ("run", "Lanzar un modo (/run watch)"),
    ("stop", "Detener un modo (/stop watch)"),
    ("calidad", "Reintentar originales subidos en preset"),
    ("ayuda", "Lista de comandos"),
]

log = logging.getLogger("wwe.bot")

# Procesos lanzados desde el bot: modo -> {"proc", "start", "err"}.
jobs = {}
# Relanzamientos en espera: modo -> asyncio.Task; y fallos seguidos por modo.
pending_restarts = {}
fallos = {}


def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    fh = RotatingFileHandler(BOT_LOG_PATH, maxBytes=1_000_000, backupCount=2,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.handlers[:] = [fh, sh]
    logging.getLogger("telethon").setLevel(logging.WARNING)


# ==========================================================================
# Procesos
# ==========================================================================

def duracion(segundos):
    s = int(segundos)
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm%02ds" % (s // 60, s % 60)
    return "%dh%02dm" % (s // 3600, s % 3600 // 60)


def start_job(modo):
    """Lanza un modo como subproceso. Devuelve un texto para el usuario."""
    if modo not in MODOS:
        return "Modo desconocido: <code>%s</code>" % html.escape(modo)
    if modo in jobs:
        return "<b>%s</b> ya esta corriendo." % modo
    args, _desc = MODOS[modo]
    # stdout duplica lo que ya va a wwe_telethon.log; solo se guarda stderr
    # (tracebacks no capturados) para poder mostrarlo si el proceso muere.
    err_path = wwe.ROOT / ("wwe_bot.%s.log" % modo)
    err = open(err_path, "w", encoding="utf-8")
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen([sys.executable, str(SCRIPT)] + args,
                            cwd=str(wwe.ROOT), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=err, **kwargs)
    jobs[modo] = {"proc": proc, "start": time.time(), "err": err,
                  "err_path": err_path, "stopping": False}
    log.info("Lanzado %s (pid %d)", modo, proc.pid)
    return "▶️ <b>%s</b> lanzado (pid %d)." % (modo, proc.pid)


def release_locks_of(pid):
    """
    Borra los lockfiles de un pid muerto. Si el proceso se mato sin pasar
    por su finally (terminate en Windows, kill), sus locks quedarian
    bloqueando ese modo durante LOCK_STALE_SECONDS.
    """
    for p in wwe.ROOT.glob("wwe_telethon.*.lock"):
        try:
            if p.read_text().strip() == str(pid):
                p.unlink()
        except OSError:
            pass


async def stop_job(modo):
    espera = pending_restarts.pop(modo, None)
    if espera:
        espera.cancel()
        if modo not in jobs:
            return "⏹ Relanzamiento de <b>%s</b> cancelado." % modo
    job = jobs.get(modo)
    if not job:
        return "<b>%s</b> no esta corriendo (o no se lanzo desde el bot)." % html.escape(modo)
    proc = job["proc"]
    job["stopping"] = True
    # En POSIX, SIGINT = Ctrl+C: el script sale por KeyboardInterrupt y
    # suelta sus locks en el finally. En Windows no hay equivalente fiable
    # para un subproceso: terminate y se limpian los locks a mano.
    if os.name == "nt":
        proc.terminate()
    else:
        proc.send_signal(signal.SIGINT)
    for _ in range(STOP_GRACE_SECONDS * 2):
        if proc.poll() is not None:
            break
        await asyncio.sleep(0.5)
    else:
        proc.kill()
        await asyncio.sleep(1)
    jobs.pop(modo, None)
    job["err"].close()
    release_locks_of(proc.pid)
    log.info("Detenido %s (pid %d)", modo, proc.pid)
    return "⏹ <b>%s</b> detenido." % modo


def active_locks():
    filas = []
    for p in sorted(wwe.ROOT.glob("wwe_telethon.*.lock")):
        try:
            pid = p.read_text().strip()
            edad = time.time() - p.stat().st_mtime
        except OSError:
            continue
        modo = p.name[len("wwe_telethon."):-len(".lock")]
        filas.append("%s (pid %s, %s)" % (modo, pid, duracion(edad)))
    return filas


def err_tail(path, n=15):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:]).strip()
    except OSError:
        return ""


async def notify(client, texto):
    for uid in ADMINS:
        try:
            await client.send_message(uid, texto, parse_mode="html")
        except Exception as e:
            log.warning("No se pudo avisar a %s: %s", uid, e)


async def relaunch_later(client, modo, espera):
    try:
        await asyncio.sleep(espera)
    except asyncio.CancelledError:
        return
    pending_restarts.pop(modo, None)
    if modo not in jobs:
        texto = start_job(modo)
        log.info("Relanzado %s", modo)
        await notify(client, "🔁 " + texto)


async def watch_jobs(client):
    """
    Avisa a los admins cuando termina un proceso lanzado desde el bot, y
    relanza los de KEEP_ALIVE si murieron sin un /stop.
    """
    while True:
        await asyncio.sleep(5)
        for modo, job in list(jobs.items()):
            rc = job["proc"].poll()
            if rc is None:
                continue
            jobs.pop(modo, None)
            job["err"].close()
            release_locks_of(job["proc"].pid)
            if job["stopping"]:
                continue  # stop_job ya respondio
            vivio = time.time() - job["start"]
            texto = ("%s <b>%s</b> termino con codigo %d tras %s."
                     % ("✅" if rc == 0 else "❌", modo, rc, duracion(vivio)))
            cola = err_tail(job["err_path"]) if rc != 0 else ""
            if cola:
                texto += "\n<pre>%s</pre>" % html.escape(cola[-3000:])
            log.info("%s termino con codigo %d", modo, rc)
            if modo in KEEP_ALIVE and modo not in pending_restarts:
                n = 0 if vivio > RESTART_RESET_SECONDS else fallos.get(modo, -1) + 1
                fallos[modo] = n
                espera = RESTART_DELAYS[min(n, len(RESTART_DELAYS) - 1)]
                texto += "\nLo relanzo en %s (/stop %s para cancelar)." % (
                    duracion(espera), modo)
                pending_restarts[modo] = asyncio.ensure_future(
                    relaunch_later(client, modo, espera))
            await notify(client, texto)


# ==========================================================================
# Textos
# ==========================================================================

def chunks(lineas, limite=MSG_MAX):
    """Agrupa lineas en mensajes que no pasen el limite de Telegram."""
    bloque, largo = [], 0
    for ln in lineas:
        if bloque and largo + len(ln) + 1 > limite:
            yield "\n".join(bloque)
            bloque, largo = [], 0
        bloque.append(ln)
        largo += len(ln) + 1
    if bloque:
        yield "\n".join(bloque)


def fecha_local(iso):
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d/%m %H:%M")
    except (TypeError, ValueError):
        return "?"


def recent_text(n):
    gid, filas = wwe.recent_images(n)
    if not filas:
        return ["Todavia no hay imagenes publicadas."]
    lineas = ["🖼 <b>Ultimas %d imagenes cargadas</b>" % len(filas), ""]
    for i, f in enumerate(filas, 1):
        titulo = html.escape((f["title"] or "(sin titulo)")[:80])
        if gid and f["message_id"]:
            titulo = '<a href="https://t.me/c/%d/%d">%s</a>' % (
                gid, f["message_id"], titulo)
        partes = ["%d. <code>%s</code> · %s" % (
            i, fecha_local(f["when"]), html.escape(f["topic"] or "-")), titulo]
        if f["image"]:
            partes.append('<a href="%s">imagen</a>' % html.escape(f["image"]))
        if f["quality"] == "temp":
            partes.append("⚠️ preset")
        lineas.append(" · ".join(partes))
    return lineas


def tail_text(n):
    try:
        with open(wwe.LOG_PATH, "rb") as f:
            f.seek(0, os.SEEK_END)
            # Solo el final del archivo: el log puede pesar 2 MB.
            f.seek(max(0, f.tell() - 200 * 1024))
            lineas = f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError as e:
        return ["No se pudo leer el log: %s" % html.escape(str(e))]
    if not lineas:
        return ["El log esta vacio."]
    # Cada mensaje en su propio <pre>, sin pasar el limite.
    esc = [html.escape(ln) for ln in lineas]
    return ["<pre>%s</pre>" % c for c in chunks(esc, MSG_MAX - 20)]


def status_full():
    partes = ["<pre>%s</pre>" % html.escape(wwe.status_text())]
    if jobs:
        partes.append("<b>Lanzados desde el bot</b>\n" + "\n".join(
            "• %s (pid %d, %s)" % (m, j["proc"].pid, duracion(time.time() - j["start"]))
            for m, j in jobs.items()))
    else:
        partes.append("<b>Lanzados desde el bot</b>: ninguno")
    if pending_restarts:
        partes.append("<b>Relanzamiento pendiente</b>: " + ", ".join(pending_restarts))
    locks = active_locks()
    partes.append("<b>Locks activos</b>: " + (", ".join(locks) if locks else "ninguno"))
    return "\n\n".join(partes)


def help_text():
    return "\n".join(["<b>Comandos</b>"] + [
        "/%s — %s" % (c, html.escape(d)) for c, d in COMANDOS] + [
        "", "<b>Modos</b> para /run y /stop:"] + [
        "<code>%s</code> — %s" % (m, html.escape(d)) for m, (_a, d) in MODOS.items()])


# ==========================================================================
# Menus
# ==========================================================================

def main_menu():
    return [
        [Button.inline("📊 Estado", b"st")],
        [Button.inline("🖼 Ultimas 5", b"log:5"),
         Button.inline("10", b"log:10"),
         Button.inline("25", b"log:25"),
         Button.inline("50", b"log:50")],
        [Button.inline("📜 Log del proceso", b"tail:30")],
        [Button.inline("▶️ Ejecutar...", b"runmenu"),
         Button.inline("⏹ Detener...", b"stopmenu")],
        [Button.inline("✨ Mejorar calidad", b"run:calidad")],
    ]


def run_menu():
    filas = []
    for m, (_a, desc) in MODOS.items():
        marca = "🟢 " if m in jobs else ""
        filas.append([Button.inline("%s%s — %s" % (marca, m, desc), ("run:" + m).encode())])
    filas.append([Button.inline("⬅️ Volver", b"menu")])
    return filas


def stop_menu():
    modos = list(jobs) + [m for m in pending_restarts if m not in jobs]
    filas = [[Button.inline("⏹ %s%s" % (m, "" if m in jobs else " (relanzamiento pendiente)"),
                            ("stop:" + m).encode())] for m in modos]
    filas.append([Button.inline("⬅️ Volver", b"menu")])
    return filas


# ==========================================================================
# Handlers
# ==========================================================================

async def send_lines(event, lineas):
    for texto in chunks(lineas):
        await event.respond(texto, parse_mode="html", link_preview=False)


def parse_n(texto, defecto, maximo):
    try:
        n = int(texto)
    except (TypeError, ValueError):
        return defecto
    return max(1, min(n, maximo))


def register(client):
    def autorizado(event):
        return event.sender_id in ADMINS

    async def rechazar(event):
        texto = ("No autorizado. Tu id de Telegram es <code>%s</code>: agregalo "
                 "a WWE_BOT_ADMINS en el .env y reinicia el bot." % event.sender_id)
        if isinstance(event, events.CallbackQuery.Event):
            await event.answer("No autorizado", alert=True)
        else:
            await event.respond(texto, parse_mode="html")

    def comando(nombre):
        return events.NewMessage(
            pattern=r"^/%s(?:@\w+)?(?:\s+(.*))?$" % nombre, func=lambda e: e.is_private)

    async def responder_menu(event):
        await event.respond("<b>WWE → Telegram</b>\nElige una opcion:",
                            parse_mode="html", buttons=main_menu())

    @client.on(comando("(?:start|menu)"))
    async def _menu(event):
        if not autorizado(event):
            return await rechazar(event)
        await responder_menu(event)

    @client.on(comando("(?:ayuda|help)"))
    async def _ayuda(event):
        if not autorizado(event):
            return await rechazar(event)
        await event.respond(help_text(), parse_mode="html")

    @client.on(comando("estado"))
    async def _estado(event):
        if not autorizado(event):
            return await rechazar(event)
        await event.respond(status_full(), parse_mode="html")

    @client.on(comando("log"))
    async def _log(event):
        if not autorizado(event):
            return await rechazar(event)
        await send_lines(event, recent_text(
            parse_n(event.pattern_match.group(1), LOG_DEFAULT, LOG_MAX)))

    @client.on(comando("tail"))
    async def _tail(event):
        if not autorizado(event):
            return await rechazar(event)
        for texto in tail_text(parse_n(event.pattern_match.group(1),
                                       TAIL_DEFAULT, TAIL_MAX)):
            await event.respond(texto, parse_mode="html")

    @client.on(comando("run"))
    async def _run(event):
        if not autorizado(event):
            return await rechazar(event)
        modo = (event.pattern_match.group(1) or "").strip()
        if not modo:
            return await event.respond("¿Que modo?", buttons=run_menu())
        await event.respond(start_job(modo), parse_mode="html")

    @client.on(comando("stop"))
    async def _stop(event):
        if not autorizado(event):
            return await rechazar(event)
        modo = (event.pattern_match.group(1) or "").strip()
        if not modo:
            if not jobs and not pending_restarts:
                return await event.respond("No hay procesos lanzados desde el bot.")
            return await event.respond("¿Cual detengo?", buttons=stop_menu())
        await event.respond(await stop_job(modo), parse_mode="html")

    @client.on(comando("calidad"))
    async def _calidad(event):
        if not autorizado(event):
            return await rechazar(event)
        await event.respond(start_job("calidad"), parse_mode="html")

    @client.on(events.CallbackQuery())
    async def _boton(event):
        if not autorizado(event):
            return await rechazar(event)
        data = event.data.decode()
        accion, _, arg = data.partition(":")
        if accion == "menu":
            await event.edit("<b>WWE → Telegram</b>\nElige una opcion:",
                             parse_mode="html", buttons=main_menu())
        elif accion == "runmenu":
            await event.edit("<b>Ejecutar</b> (🟢 = corriendo):",
                             parse_mode="html", buttons=run_menu())
        elif accion == "stopmenu":
            if not jobs and not pending_restarts:
                return await event.answer("No hay procesos lanzados desde el bot.")
            await event.edit("<b>Detener</b>:", parse_mode="html", buttons=stop_menu())
        elif accion == "st":
            await event.answer()
            await event.respond(status_full(), parse_mode="html")
        elif accion == "log":
            await event.answer()
            await send_lines(event, recent_text(parse_n(arg, LOG_DEFAULT, LOG_MAX)))
        elif accion == "tail":
            await event.answer()
            for texto in tail_text(parse_n(arg, TAIL_DEFAULT, TAIL_MAX)):
                await event.respond(texto, parse_mode="html")
        elif accion == "run":
            await event.answer()
            await event.respond(start_job(arg), parse_mode="html")
        elif accion == "stop":
            await event.answer("Deteniendo %s..." % arg)
            await event.respond(await stop_job(arg), parse_mode="html")
        else:
            await event.answer()


async def main():
    if not wwe.API_ID or not wwe.API_HASH:
        raise SystemExit("Falta TG_API_ID / TG_API_HASH en el .env.")
    if not BOT_TOKEN:
        raise SystemExit("Falta WWE_BOT_TOKEN en el .env (pidelo a @BotFather).")
    if not ADMINS:
        log.warning("WWE_BOT_ADMINS vacio: el bot no obedecera a nadie, solo "
                    "contestara a cada usuario con su id.")

    client = TelegramClient(str(wwe.ROOT / "wwe_bot"), wwe.API_ID, wwe.API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    register(client)
    # Lista de comandos que Telegram muestra en el boton "Menu" del chat.
    await client(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(), lang_code="",
        commands=[BotCommand(c, d) for c, d in COMANDOS]))
    me = await client.get_me()
    log.info("Bot @%s listo. Admins: %s", me.username,
             ", ".join(map(str, sorted(ADMINS))) or "ninguno")

    for modo in AUTOSTART:
        log.info("Autostart: %s", start_job(modo))

    # systemd para el servicio con SIGTERM: se desconecta para que el
    # finally detenga los subprocesos con SIGINT y suelten sus locks.
    if os.name != "nt":
        asyncio.get_running_loop().add_signal_handler(
            signal.SIGTERM, lambda: asyncio.ensure_future(client.disconnect()))

    vigia = asyncio.ensure_future(watch_jobs(client))
    try:
        await client.run_until_disconnected()
    finally:
        vigia.cancel()
        for modo in list(jobs) + list(pending_restarts):
            await stop_job(modo)
        log.info("Bot detenido.")


if __name__ == "__main__":
    setup_logging()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
