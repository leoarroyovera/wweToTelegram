#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WWE.com -> Telegram   |   Telethon, cuenta de usuario (sin bot)

Extrae las imagenes del scroll infinito de la homepage de WWE.com (Drupal)
y las publica en un supergrupo privado organizado por TEMAS (Raw, SmackDown,
NXT, Articulos...), igual que hace c90ToTelegram.

Por que cuenta de usuario y no bot:
  - El supergrupo y sus temas se crean SOLOS; con un bot habria que crearlos
    a mano y anadir el bot como administrador.
  - Un bot NO puede crear temas (CreateForumTopicRequest requiere usuario).
  - Los mensajes salen a tu nombre, no "via @algunbot".
Contrapartida: Telethon no acepta una URL remota, hay que descargar cada
imagen (~1,7 MB) y subirla. Se borra del disco tras subirla.

DOS MODOS DE EJECUCION
----------------------
1) Scheduled task. PythonAnywhere solo admite frecuencia diaria u horaria,
   NO cada 30 min: se crean DOS tareas horarias, a los :00 y a los :30.
   El lockfile impide que se pisen si una se alarga.
2) Always-on task: con --loop se repite solo cada INTERVAL_MINUTES.

PRIMERA EJECUCION
-----------------
Requiere login interactivo UNA vez, en una consola Bash:
    python3.13 wwe_telethon.py --login
Pide telefono, codigo y clave 2FA. Queda en wwe_session.session; a partir de
ahi las tareas programadas no preguntan nada.

Dependencias:  pip3.13 install --user telethon requests
"""

import argparse
import asyncio
import hashlib
import html
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urljoin

import requests
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl import types as tltypes
from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.functions.messages import (CreateForumTopicRequest,
                                            GetForumTopicsRequest)
from telethon.tl.types import ForumTopic

from fast_upload import upload_file_fast

ROOT = Path(__file__).resolve().parent


def load_env():
    """
    Carga el .env de al lado del script (y, si no tiene las credenciales,
    el de c90ToTelegram, que usa las mismas TG_API_ID/TG_API_HASH).

    Tiene que correr ANTES de leer las constantes de abajo. No se usa
    python-dotenv para no anadir una dependencia mas en PythonAnywhere.
    Las variables ya presentes en el entorno mandan, para que las tareas
    programadas puedan sobrescribir el archivo.
    """
    candidatos = [ROOT / ".env", ROOT.parent / "c90ToTelegram" / ".env"]
    for env in candidatos:
        if not env.exists():
            continue
        try:
            for line in env.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                # Comentario al final de linea: VAR=4  # explicacion.
                # Solo fuera de comillas, para no cortar un valor legitimo.
                if v[:1] in ('"', "'"):
                    q = v[0]
                    fin = v.find(q, 1)
                    v = v[1:fin] if fin > 0 else v[1:]
                else:
                    v = v.split("#", 1)[0].strip()
                os.environ.setdefault(k.strip(), v)
        except OSError:
            pass
        # Con las credenciales ya resueltas no hace falta seguir buscando.
        if os.environ.get("TG_API_ID") and os.environ.get("TG_API_HASH"):
            break


load_env()

# ==========================================================================
# CONFIGURACION
# ==========================================================================

# Credenciales de https://my.telegram.org -> API development tools.
# Son las MISMAS que ya usas en c90ToTelegram: puedes copiarlas de su .env.
API_ID = int(os.environ.get("TG_API_ID", "0") or 0)
API_HASH = os.environ.get("TG_API_HASH", "")

# Nombre del supergrupo. Se crea solo si no existe.
CHANNEL_NAME = os.environ.get("WWE_CHANNEL", "WWE")
SESSION_NAME = os.environ.get("WWE_SESSION", "wwe_session")

MAX_PAGES = int(os.environ.get("WWE_MAX_PAGES", "4"))
MAX_SEND_PER_RUN = int(os.environ.get("WWE_MAX_SEND", "12"))
FIRST_RUN_SEND = int(os.environ.get("WWE_FIRST_RUN_SEND", "5"))
SEND_DELAY = float(os.environ.get("WWE_SEND_DELAY", "3"))
INTERVAL_MINUTES = int(os.environ.get("WWE_INTERVAL_MINUTES", "30"))
RETENTION_DAYS = int(os.environ.get("WWE_RETENTION_DAYS", "45"))

# Modo portada: recoger tambien las imagenes sueltas de la home (hero,
# trending, carruseles), no solo las de las feed-cards.
LOOSE_IMAGES = os.environ.get("WWE_LOOSE_IMAGES", "1") == "1"

# Telegram agrupa como maximo 10 medios en un mismo mensaje.
ALBUM_MAX = min(int(os.environ.get("WWE_ALBUM_MAX", "10")), 10)

# ==========================================================================
# Constantes del sitio (verificadas contra wwe.com)
# ==========================================================================

BASE_URL = "https://www.wwe.com"
AJAX_URL = BASE_URL + "/views/ajax"
HOMEPAGE_URL = BASE_URL + "/"

VIEW_NAME = "wwe_homepage"
VIEW_DISPLAY_ID = "block_1"
VIEW_PATH = "/homepage"

# Listado propio de galerias (/photos): otra vista Drupal, misma paginacion.
PHOTOS_VIEW = "photos"
PHOTOS_DISPLAY = "block_1"
PHOTOS_PATH = "/photos"

# Listado de luchadores (/superstars): misma vista Drupal, misma paginacion.
SUPERSTARS_VIEW = "current_superstar"
SUPERSTARS_DISPLAY = "block_1"
SUPERSTARS_PATH = "/superstars"

SHOWS_URL = BASE_URL + "/shows"
EVENTS_URL = BASE_URL + "/events/"

# Preset de respaldo si el original no sirve: 1920x1080, ~380 KB.
IMAGE_STYLE = "wwe_16_9_xl_r"
# Idem para la foto de perfil de un luchador (cuadrada): el original da 503
# a veces (frio de cache en Fastly, no ausente); este preset es el mas
# grande disponible en /superstars, 540x540 sin recomprimir de mas.
SUPERSTAR_IMAGE_STYLE = "wwe_1_1_540__composite"

WORK_DIR = Path(os.environ.get("WWE_WORK_DIR", ROOT / "work"))
DB_PATH = os.environ.get("WWE_DB_PATH", str(ROOT / "wwe_seen.sqlite3"))
LOG_PATH = os.environ.get("WWE_LOG_PATH", str(ROOT / "wwe_telethon.log"))
LOCK_STALE_SECONDS = 3600

MAX_RETRIES = 4
CAPTION_MAX = 1024

# Telethon sube hasta 2GB. Los ZIP de galeria rondan 8-60 MB, muy por debajo.
MAX_UPLOAD = 1900 * 1024 * 1024

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# ---- Temas del supergrupo -------------------------------------------------
# Cada item cae en un tema segun su show. El orden de esta lista es el orden
# en que se crean, que es como aparecen en Telegram (no se puede reordenar
# despues, por eso importa).
TOPIC_ARTICLES = "Articulos"
TOPIC_EVENTS = "Eventos"
TOPIC_OTHER = "Otros"
TOPIC_ICONS = "Iconos"
TOPIC_SUPERSTARS = "Superstars"
TOPIC_SHOWS = "Shows"
# Sub-shows recurrentes que no son PLE ni el programa principal de un show
# grande (Raw Talk, WWE 205 Live, The Bump...): antes cabian todos en Otros
# junto con slugs de superstars individuales, dejandolo con mas de 4.200
# items. Tema propio para que Otros vuelva a ser solo lo que de verdad no
# encaja en ningun lado (superstars sueltos, contenido sin show).
TOPIC_SHOWS_MENORES = "Otros Shows"

TOPICS_ORDER = [TOPIC_ARTICLES, TOPIC_EVENTS, "Raw", "SmackDown", "NXT",
                TOPIC_SHOWS_MENORES, TOPIC_OTHER, TOPIC_ICONS,
                TOPIC_SUPERSTARS, TOPIC_SHOWS]

# Slug de show (clase CSS de la card) -> tema.
SHOW_TO_TOPIC = {
    "raw": "Raw",
    "smackdown": "SmackDown",
    "wwenxt": "NXT", "nxt": "NXT",
    "wwe": TOPIC_OTHER, "wwenow": TOPIC_OTHER,
    "wwetop10": TOPIC_OTHER, "aaa": TOPIC_OTHER,

    # Premium Live Events (PLEs): todos al tema Eventos, sea cual sea su
    # tamano o antiguedad. wrestlemania/summerslam/royalrumble/
    # survivorseries ya estaban; el resto se detecto midiendo el volumen
    # real de 'show' en el archivo historico (>=10 items sin mapear).
    "wrestlemania": TOPIC_EVENTS, "summerslam": TOPIC_EVENTS,
    "royalrumble": TOPIC_EVENTS, "survivorseries": TOPIC_EVENTS,
    "survivorserieswargames": TOPIC_EVENTS, "eliminationchamber": TOPIC_EVENTS,
    "wwecrownjewel": TOPIC_EVENTS, "moneyinthebank": TOPIC_EVENTS,
    "backlash": TOPIC_EVENTS, "sundaynightsmainevent": TOPIC_EVENTS,
    "wwehellinacell": TOPIC_EVENTS, "wweclashatthecastle": TOPIC_EVENTS,
    "nightofchampions": TOPIC_EVENTS, "extremerules": TOPIC_EVENTS,
    "wweclashofchampions": TOPIC_EVENTS, "wwepayback": TOPIC_EVENTS,
    "wweday1": TOPIC_EVENTS, "wwefastlane": TOPIC_EVENTS,
    "nxtpremiumliveevent": TOPIC_EVENTS,

    # Sub-shows recurrentes: programas regulares propios, distintos del
    # show principal (Raw/SmackDown/NXT) pero con volumen suficiente para
    # no perderse dentro de Otros junto a slugs de superstars individuales.
    "wwenetwork": TOPIC_SHOWS_MENORES, "rawtalk": TOPIC_SHOWS_MENORES,
    "wwecommunity": TOPIC_SHOWS_MENORES, "wwetalkingsmack": TOPIC_SHOWS_MENORES,
    "wwe205live": TOPIC_SHOWS_MENORES, "wwesthebump": TOPIC_SHOWS_MENORES,
    "nxtuk": TOPIC_SHOWS_MENORES, "thenewdayfeelthepower": TOPIC_SHOWS_MENORES,
    "wweafterthebell": TOPIC_SHOWS_MENORES,
    "steveaustinsbrokenskullsessions": TOPIC_SHOWS_MENORES,
    "coreygraves": TOPIC_SHOWS_MENORES, "wwehalloffame": TOPIC_SHOWS_MENORES,
    "wweplayback": TOPIC_SHOWS_MENORES, "mckenziemitchell": TOPIC_SHOWS_MENORES,
    "nxtlevelup": TOPIC_SHOWS_MENORES, "wwemattel": TOPIC_SHOWS_MENORES,
    "mizandmrs": TOPIC_SHOWS_MENORES, "wwepopquestion": TOPIC_SHOWS_MENORES,
    "vicjoseph": TOPIC_SHOWS_MENORES, "tributetothetroops": TOPIC_SHOWS_MENORES,
}

# Orden de publicacion dentro de cada pasada. Menor = antes.
# "image" son las sueltas de la home (hero, trending): van tras las cards,
# que llevan titulo y enlace, pero antes de lo desconocido. "icon" son los
# .png/.svg de marca (logos, iconos de nav): van al final, son la prioridad
# mas baja de contenido real. Sin una entrada propia caerian al 9 y con un
# limite bajo no se publicarian nunca.
TYPE_PRIORITY = {"article": 0, "video": 1, "video_playlist": 2,
                 "gallery": 2, "image": 3, "icon": 4, "unknown": 5}
SHOW_PRIORITY = {
    "wrestlemania": 0, "summerslam": 0, "royalrumble": 0, "survivorseries": 0,
    # Resto de PLEs: mismo rango de prioridad que los cuatro grandes de
    # arriba, van todos al tema Eventos y el orden entre ellos ya lo da la
    # posicion original del feed (ver sort_for_channel).
    "survivorserieswargames": 0, "eliminationchamber": 0, "wwecrownjewel": 0,
    "moneyinthebank": 0, "backlash": 0, "sundaynightsmainevent": 0,
    "wwehellinacell": 0, "wweclashatthecastle": 0, "nightofchampions": 0,
    "extremerules": 0, "wweclashofchampions": 0, "wwepayback": 0,
    "wweday1": 0, "wwefastlane": 0, "nxtpremiumliveevent": 0,
    "wwe": 1, "raw": 2, "smackdown": 2,
    "wwenxt": 3, "nxt": 3,
    # Sub-shows recurrentes: por debajo del show principal que los origina,
    # por delante del resto (DEFAULT_SHOW_PRIORITY).
    "wwenetwork": 3, "rawtalk": 3, "wwecommunity": 3, "wwetalkingsmack": 3,
    "wwe205live": 3, "wwesthebump": 3, "nxtuk": 3, "thenewdayfeelthepower": 3,
    "wweafterthebell": 3, "steveaustinsbrokenskullsessions": 3,
    "coreygraves": 3, "wwehalloffame": 3, "wweplayback": 3,
    "mckenziemitchell": 3, "nxtlevelup": 3, "wwemattel": 3, "mizandmrs": 3,
    "wwepopquestion": 3, "vicjoseph": 3, "tributetothetroops": 3,
    "wwenow": 4, "wwetop10": 5, "aaa": 6,
}
DEFAULT_SHOW_PRIORITY = 4

SHOW_LABEL = {
    "wwe": "WWE", "raw": "Raw", "smackdown": "SmackDown",
    "wwenxt": "NXT", "nxt": "NXT", "wwenow": "WWENow",
    "wwetop10": "Top10", "aaa": "AAA",
    "wrestlemania": "WrestleMania", "summerslam": "SummerSlam",
    "royalrumble": "RoyalRumble", "survivorseries": "SurvivorSeries",
    # Shows del hub /shows que no aparecen en el feed de portada.
    "snme": "Sunday Night's Main Event", "nxtple": "NXT PLE",
    "moneyinthebank": "Money in the Bank",
    "survivor-series-wargames": "Survivor Series WarGames",
    "wwe-evolve": "WWE Evolve",
    # PLEs detectados en el archivo historico, sin mapear antes.
    "survivorserieswargames": "Survivor Series WarGames",
    "eliminationchamber": "Elimination Chamber",
    "wwecrownjewel": "Crown Jewel", "backlash": "Backlash",
    "sundaynightsmainevent": "Sunday Night's Main Event",
    "wwehellinacell": "Hell in a Cell",
    "wweclashatthecastle": "Clash at the Castle",
    "nightofchampions": "Night of Champions", "extremerules": "Extreme Rules",
    "wweclashofchampions": "Clash of Champions", "wwepayback": "Payback",
    "wweday1": "Day 1", "wwefastlane": "Fastlane",
    "nxtpremiumliveevent": "NXT PLE",
    # Sub-shows recurrentes.
    "wwenetwork": "WWE Network", "rawtalk": "Raw Talk",
    "wwecommunity": "WWE Community", "wwetalkingsmack": "Talking Smack",
    "wwe205live": "205 Live", "wwesthebump": "The Bump",
    "nxtuk": "NXT UK", "thenewdayfeelthepower": "The New Day: Feel the Power",
    "wweafterthebell": "After the Bell",
    "steveaustinsbrokenskullsessions": "Broken Skull Sessions",
    "coreygraves": "Corey Graves", "wwehalloffame": "Hall of Fame",
    "wweplayback": "Playback", "mckenziemitchell": "McKenzie Mitchell",
    "nxtlevelup": "NXT Level Up", "wwemattel": "WWE Mattel",
    "mizandmrs": "Miz & Mrs.", "wwepopquestion": "Pop Question",
    "vicjoseph": "Vic Joseph", "tributetothetroops": "Tribute to the Troops",
}

log = logging.getLogger("wwe")


def setup_logging(verbose=False):
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.handlers[:] = [fh, sh]
    # Telethon es muy verboso en INFO.
    logging.getLogger("telethon").setLevel(logging.WARNING)


# ==========================================================================
# Lock
# ==========================================================================

class AlreadyRunning(Exception):
    pass


def lock_path(modo="feed"):
    """Un lock por modo: portada y galerias pueden correr a la vez."""
    return ROOT / ("wwe_telethon.%s.lock" % modo)


def acquire_lock(modo="feed"):
    p = lock_path(modo)
    if p.exists():
        age = time.time() - p.stat().st_mtime
        if age < LOCK_STALE_SECONDS:
            raise AlreadyRunning("lock '%s' activo (pid %s, %ds)"
                                 % (modo, p.read_text().strip(), int(age)))
        log.warning("Lock '%s' huerfano de %ds; se ignora.", modo, int(age))
    p.write_text(str(os.getpid()))
    return p


def release_lock(modo="feed"):
    try:
        lock_path(modo).unlink()
    except OSError:
        pass


# ==========================================================================
# Estado persistente
# ==========================================================================

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            cid          TEXT PRIMARY KEY,
            title        TEXT,
            url          TEXT,
            image        TEXT,
            content_type TEXT,
            show         TEXT,
            topic        TEXT,
            first_seen   TEXT,
            sent         INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_first_seen ON seen(first_seen)")
    # Progreso del volcado historico (--backfill), para reanudarlo por tandas.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backfill (
            k TEXT PRIMARY KEY,
            v TEXT
        )
    """)
    conn.commit()
    return conn


def bf_get(conn, k, default=None):
    r = conn.execute("SELECT v FROM backfill WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def bf_set(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO backfill (k,v) VALUES (?,?)",
                 (k, str(v)))
    conn.commit()


def save_progress(conn, page):
    """
    Guarda el avance, pero nunca hacia atras: un --from-page para revisar
    una pagina antigua no debe hacer perder el progreso ya alcanzado.
    """
    if page > int(bf_get(conn, "page", "0")):
        bf_set(conn, "page", page)


def is_first_run(conn):
    return conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0


def already_seen(conn, cid):
    """
    Solo cuenta como visto lo que YA SE PUBLICO (sent=1).

    Antes cualquier fila en 'seen' bloqueaba el reintento, aunque fuera un
    item que se indexo pero nunca se subio por quedar fuera del limite de
    esa pasada (MAX_SEND_PER_RUN/FIRST_RUN_SEND, o un backfill cortado a
    medio camino). Eso los dejaba huerfanos para siempre: el feed los sigue
    trayendo, pero already_seen() los descartaba antes de intentarlos de
    nuevo. Con sent=0 excluido de este chequeo, la proxima pasada que los
    vuelva a ver los reintenta hasta que se publiquen de verdad.
    """
    r = conn.execute("SELECT sent FROM seen WHERE cid=?", (cid,)).fetchone()
    return r is not None and r[0] == 1


def record(conn, item, sent):
    """
    Guarda el resultado de intentar publicar un item.

    first_seen se preserva si ya existia una fila previa (un reintento tras
    un sent=0 no debe resetear su fecha de primera vista, o purge_old()
    nunca lo alcanzaria si el reintento lo sigue posponiendo indefinidamente).
    """
    previo = conn.execute("SELECT first_seen FROM seen WHERE cid=?",
                          (item["cid"],)).fetchone()
    first_seen = previo[0] if previo else datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO seen "
        "(cid,title,url,image,content_type,show,topic,first_seen,sent) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (item["cid"], item["title"], item["url"], item["image"],
         item["content_type"], item["show"], item["topic"],
         first_seen, 1 if sent else 0))
    conn.commit()


def purge_old(conn):
    """
    Purga filas de mas de RETENTION_DAYS, sent=1 o no.

    Una fila sent=0 purgada no se pierde: al desaparecer de 'seen',
    already_seen() vuelve a verla como no vista y una pasada normal que
    todavia la encuentre en el feed la reintenta desde cero.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    cur = conn.execute("DELETE FROM seen WHERE first_seen < ?", (cutoff,))
    conn.commit()
    if cur.rowcount:
        log.info("Purgados %d registros de mas de %d dias.",
                 cur.rowcount, RETENTION_DAYS)


# ==========================================================================
# Scraping
# ==========================================================================

def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT,
                      "Accept-Language": "en-US,en;q=0.9"})
    return s


def get_view_dom_id(session):
    """
    /views/ajax espera el view_dom_id que Drupal genera al renderizar la home.
    Se lee en vivo, asi que sobrevive a los cambios de ese hash.
    """
    try:
        r = session.get(HOMEPAGE_URL, timeout=30)
        r.raise_for_status()
        m = re.search(
            r'views_dom_id:([0-9a-f]{16,})"\s*:\s*\{"view_name":"%s"' % VIEW_NAME,
            r.text)
        if m:
            return m.group(1)
        m = re.search(r"js-view-dom-id-([0-9a-f]{16,})", r.text)
        if m:
            return m.group(1)
    except requests.RequestException as e:
        log.warning("No se pudo leer la home: %s", e)
    log.warning("view_dom_id no encontrado; se continua sin el.")
    return ""


def fetch_page(session, dom_id, page, view=None, display=None, path=None):
    """
    Una pagina del scroll infinito de una vista Drupal.

    Se usa GET: el modulo views_infinite_scroll normalmente hace POST, pero
    Fastly responde 405 al POST. Tampoco vale /homepage?page=N, porque el CDN
    cachea esa URL ignorando el parametro y devuelve siempre la pagina 0.

    Sirve tanto la portada (wwe_homepage/block_1) como el listado de galerias
    (photos/block_1), que pagina igual.
    """
    params = {
        "view_name": view or VIEW_NAME,
        "view_display_id": display or VIEW_DISPLAY_ID,
        "view_args": "", "view_path": path or VIEW_PATH, "view_base_path": "",
        "view_dom_id": dom_id, "pager_element": "0", "page": str(page),
        "_wrapper_format": "drupal_ajax",
    }
    last = None
    for attempt in range(3):
        try:
            r = session.get(AJAX_URL, params=params, timeout=30, headers={
                "X-Requested-With": "XMLHttpRequest", "Referer": HOMEPAGE_URL})
            r.raise_for_status()
            cmds = r.json()
            if isinstance(cmds, dict):
                cmds = list(cmds.values())
            return "\n".join(c.get("data", "") for c in cmds
                             if isinstance(c, dict) and c.get("command") == "insert")
        except (requests.RequestException, ValueError) as e:
            last = e
            wait = 2 ** attempt + random.random()
            log.warning("Pagina %d fallo (intento %d): %s; reintento en %.1fs",
                        page, attempt + 1, e, wait)
            time.sleep(wait)
    raise RuntimeError("No se pudo obtener la pagina %d: %s" % (page, last))


CARD_RE = re.compile(
    r'<div class="wwe-feed-cards--card\b(?P<attrs>[^>]*)>(?P<body>.*?)'
    r'(?=<div class="wwe-feed-cards--card\b|<div class="wwe-feed-cards"|\Z)',
    re.S)


def image_urls(src):
    """
    Devuelve (original, preset) a partir de la URL del HTML.

    En este Drupal el original NO esta en /f/public/<ruta> (404) sino en
    /f/<ruta>: hay que quitar "styles/<preset>/public/" entero. Mismas
    dimensiones que el preset xl_r pero sin recomprimir (~1,7 MB vs ~375 KB).
    """
    src = html.unescape(src).replace("\\/", "/")
    preset = re.sub(r"/f/styles/[a-z0-9_]+/public/",
                    "/f/styles/%s/public/" % IMAGE_STYLE, src)
    original = re.sub(r"/f/styles/[a-z0-9_]+/public/", "/f/", src)
    return urljoin(BASE_URL, original), urljoin(BASE_URL, preset)


def topic_for(item):
    """Tema al que va el item. Los articulos van juntos, sea cual sea el show."""
    if item["content_type"] == "article":
        return TOPIC_ARTICLES
    return SHOW_TO_TOPIC.get(item["show"], TOPIC_OTHER)


def is_gallery(item):
    return (item["content_type"] == "gallery"
            or "/gallery/" in item.get("url", ""))


def gallery_slug(item):
    """Nombre de archivo del ZIP, a partir de la URL de la galeria."""
    slug = (item.get("url", "").rstrip("/").split("/")[-1]
            or ("gallery-%s" % item["cid"]))
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", slug)[:80]
    return "%s.zip" % slug


PHOTOS_CARD_RE = re.compile(
    r'<div class="landing-page--feed-card\b.*?</div>\s*</div>\s*</div>', re.S)


def parse_photos_page(markup):
    """
    Galerias del listado /photos.

    Ese listado usa otra plantilla que la portada ('landing-page--feed-card')
    y NO trae el cid: solo el enlace /gallery/<slug>. El nid hay que
    resolverlo luego, leyendo el HTML de cada galeria (resolve_gallery_nid).
    """
    items, vistos = [], set()
    for bloque in PHOTOS_CARD_RE.findall(markup):
        href = re.search(r'href="(/gallery/[^"]+)"', bloque)
        if not href:
            continue
        url = urljoin(BASE_URL, html.unescape(href.group(1)))
        if url in vistos:
            continue
        vistos.add(url)

        t = re.search(r'<span class="card-title">(.*?)</span>', bloque, re.S)
        titulo = html.unescape(re.sub(r"<[^>]+>", " ", t.group(1))).strip() if t else ""

        # El show aparece como <span class="match-location">Raw</span>.
        loc = re.search(r'<span class="match-location">(.*?)</span>', bloque, re.S)
        show = ""
        if loc:
            crudo = re.sub(r"[^a-z0-9]", "", loc.group(1).lower())
            show = crudo if crudo in SHOW_TO_TOPIC else ""

        img = re.search(r'src="([^"]+\.(?:jpe?g|png))"', bloque, re.I)
        image, fallback = image_urls(img.group(1)) if img else ("", "")

        it = {
            "cid": "", "title": titulo or url.rstrip("/").split("/")[-1],
            "url": url, "image": image, "image_fallback": fallback,
            "content_type": "gallery", "show": show,
        }
        it["topic"] = topic_for(it)
        items.append(it)
    return items


SUPERSTAR_ROW_RE = re.compile(
    r'<div class="views-row">.*?href="(/superstars/[a-z0-9-]+)"[^>]*>'
    r'(.*?)</a>.*?</picture>', re.S)


def parse_superstars_page(markup):
    """
    Luchadores del listado /superstars.

    Misma vista Drupal que la portada (views_infinite_scroll), otro nombre:
    current_superstar/block_1. Cada fila trae el nombre (primer <a>) y, mas
    abajo, un <picture> con la foto de perfil cuadrada. El slug de la URL
    (unico y estable entre ejecuciones) hace de cid, no hay id numerico
    expuesto en el listado.
    """
    items, vistos = [], set()
    for m in SUPERSTAR_ROW_RE.finditer(markup):
        slug = m.group(1).rsplit("/", 1)[-1]
        if slug in vistos:
            continue
        vistos.add(slug)

        nombre = html.unescape(re.sub(r"<[^>]+>", " ", m.group(2))).strip()
        bloque = m.group(0)
        img = re.search(r'<source srcset="([^"\s]+)', bloque)
        if not img:
            continue
        preset = re.sub(r"/f/styles/[a-z0-9_]+(?:__composite)?/public/",
                        "/f/styles/%s/public/" % SUPERSTAR_IMAGE_STYLE,
                        html.unescape(img.group(1)))
        original = re.sub(r"/f/styles/[a-z0-9_]+(?:__composite)?/public/",
                          "/f/", html.unescape(img.group(1)))

        it = {
            "cid": "superstar:" + slug,
            "title": nombre or slug.replace("-", " ").title(),
            "url": urljoin(BASE_URL, "/superstars/" + slug),
            "image": urljoin(BASE_URL, original),
            "image_fallback": urljoin(BASE_URL, preset),
            "content_type": "superstar", "show": "",
            "topic": TOPIC_SUPERSTARS,
        }
        items.append(it)
    return items


SHOW_ITEM_RE = re.compile(
    r'<a href="https://www\.wwe\.com(/shows/[a-z0-9-]+)" class="b-link">'
    r'(.*?)</picture>', re.S)


def parse_shows_page(markup):
    """
    Shows fijos del hub /shows: pagina estatica, sin scroll infinito.

    Cada show aparece dos veces (hero + logo, ambos con el mismo enlace):
    se queda con la primera imagen encontrada (el hero, mas grande) por slug.
    """
    items, vistos = [], set()
    for m in SHOW_ITEM_RE.finditer(markup):
        slug = m.group(1).rsplit("/", 1)[-1]
        if slug in vistos:
            continue
        vistos.add(slug)

        bloque = m.group(0)
        img = re.search(r'data-src="([^"]+)"', bloque) or re.search(r'src="([^"]+)"', bloque)
        if not img:
            continue
        original, preset = image_urls(img.group(1))

        it = {
            "cid": "show:" + slug,
            "title": SHOW_LABEL.get(slug, slug.replace("-", " ").title()),
            "url": urljoin(BASE_URL, "/shows/" + slug),
            "image": original, "image_fallback": preset,
            "content_type": "show", "show": slug,
            "topic": TOPIC_SHOWS,
        }
        items.append(it)
    return items


EVENT_CARD_RE = re.compile(
    r'<div class="events-upcoming-card\b.*?</div>\s*</div>\s*</div>\s*</div>',
    re.S)


def parse_events_page(markup):
    """
    Proximos eventos de /events/results/... (landing geolocalizada).

    No hay archivo historico paginable como en /photos: esta landing solo
    muestra los eventos proximos en la zona resuelta por IP. El cid se arma
    con el slug de /event/<slug> (estable, sin id numerico en el listado).
    """
    items, vistos = [], set()
    for bloque in EVENT_CARD_RE.findall(markup):
        href = re.search(r'href="(/event/[^"?]+)"', bloque)
        if not href:
            continue
        slug = href.group(1).rsplit("/", 1)[-1]
        if slug in vistos:
            continue
        vistos.add(slug)

        t = re.search(r'datetime="[^"]*"[^>]*>([^<]+)</time>', bloque)
        fecha = html.unescape(t.group(1)).strip() if t else ""
        loc = re.search(r'event-breaker--meta-location">([^<]+)<', bloque)
        lugar = html.unescape(loc.group(1)).strip() if loc else ""
        titulo = " — ".join(p for p in (fecha, lugar) if p) or slug

        img = re.search(r'data-src="([^"]+\.(?:jpe?g|png))"', bloque, re.I)
        if not img:
            continue
        original, preset = image_urls(img.group(1))

        it = {
            "cid": "event:" + slug,
            "title": titulo,
            "url": urljoin(BASE_URL, "/event/" + slug),
            "image": original, "image_fallback": preset,
            "content_type": "event", "show": "",
            "topic": TOPIC_EVENTS,
        }
        items.append(it)
    return items


def resolve_gallery_nid(session, url):
    """
    nid de una galeria a partir de su URL: la API solo acepta el id numerico,
    el slug devuelve una lista vacia. El nid vive en drupalSettings del HTML.
    """
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("No se pudo abrir %s: %s", url, e)
        return None
    m = (re.search(r'"currentPageId":\s*"(\d+)"', r.text)
         or re.search(r'"nid":\s*"(\d+)"', r.text))
    return m.group(1) if m else None


def fetch_gallery(session, nid, per_page=200):
    """
    Fotos de una galeria, en orden, via la API interna del tema.

    Endpoint hallado en el JS del tema (getGalleryRequestUrl):
        /api/gallery/<nid>/<itemsPerPage>/<offset>/<initialFid>
    El nid es el mismo 'cid' que ya trae la card del feed.

    Ojo con el formato: con offset=0 'photos' es una LISTA, pero con offset>0
    Drupal la devuelve como DICT indexado por posicion ("50", "51"...). Se
    normalizan ambos y se ordena por esa posicion para no alterar el orden.

    Devuelve (titulo, descripcion, [ {fid, image, image_fallback, caption} ]).
    """
    fotos, offset, titulo, descripcion, total = [], 0, "", "", None
    while True:
        url = "%s/api/gallery/%s/%d/%d/0" % (BASE_URL, nid, per_page, offset)
        try:
            r = session.get(url, timeout=60,
                            headers={"X-Requested-With": "XMLHttpRequest",
                                     "Referer": BASE_URL})
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("Galeria %s: fallo en offset %d: %s", nid, offset, e)
            break

        titulo = titulo or data.get("title", "")
        descripcion = descripcion or data.get("description", "")
        if total is None:
            total = int(data.get("total_images") or 0)

        crudas = data.get("photos") or []
        if isinstance(crudas, dict):
            # Las claves son posiciones; ordenar numericamente, no como texto.
            crudas = [crudas[k] for k in sorted(crudas, key=lambda x: int(x))]
        if not crudas:
            break

        for ph in crudas:
            # photo_hires y photo son URLs DISTINTAS del JSON (no dos
            # tamanos de la misma imagen): si la de alta calidad da 503 (a
            # veces no existe en el servidor de WWE.com), photo es el
            # fallback real, no otro preset de la misma URL rota. Antes se
            # derivaban original Y fallback de photo_hires nada mas, asi
            # que el "fallback" terminaba siendo la misma imagen caida.
            markup_hires = (ph.get("photo_hires") or {}).get("photo") or ""
            markup_normal = ph.get("photo", "")
            m_hires = re.search(r'src="([^"]+)"', markup_hires)
            m_normal = re.search(r'src="([^"]+)"', markup_normal)
            if not m_hires and not m_normal:
                continue

            # image_urls() pela el preset (/f/styles/<x>/public/ -> /f/)
            # para pedir la version sin recomprimir; se aplica a la mejor
            # URL disponible (hires si existe) y el fallback usa la otra
            # URL del JSON tal cual, sin pelar preset -- si el original de
            # alta calidad no existe, es mas probable que el preset normal
            # si funcione que otro preset derivado de la misma URL rota.
            if m_hires:
                original, _ = image_urls(m_hires.group(1))
                fallback = (urljoin(BASE_URL, html.unescape(m_normal.group(1)).replace("\\/", "/"))
                           if m_normal else original)
            else:
                original, fallback = image_urls(m_normal.group(1))

            fotos.append({
                "fid": str(ph.get("fid") or ""),
                "image": original,
                "image_fallback": fallback,
                "caption": html.unescape(
                    re.sub(r"<[^>]+>", " ", ph.get("caption") or "")).strip(),
            })

        offset += len(crudas)
        if total and offset >= total:
            break
        if len(crudas) < per_page:
            break
        time.sleep(0.5)

    return titulo, descripcion, fotos


def parse_cards(markup):
    """Extrae los items de una pagina del feed."""
    items = []
    for m in CARD_RE.finditer(markup):
        attrs, body = m.group("attrs"), m.group("body")

        # cid = id de nodo Drupal; clave estable para deduplicar.
        cid = re.search(r"cid=(\d+)", attrs) or re.search(r'data-nid="(\d+)"', body)
        if not cid:
            continue
        cid = cid.group(1)

        ctype = re.search(r"content_type=([a-z_]+)", attrs)
        ctype = ctype.group(1) if ctype else "unknown"

        # El slug del show es la ultima clase de la card:
        #   "... wwe-feed-cards--card__video smackdown "
        show = ""
        cls = re.match(r'([^"]*)"', attrs)
        if cls:
            tok = cls.group(1).split()
            if tok and not tok[-1].startswith("wwe-feed-cards--card"):
                show = tok[-1]

        href = re.search(r'href="([^"]+)"', body)
        url = urljoin(BASE_URL, html.unescape(href.group(1))) if href else ""

        img = re.search(r'<img[^>]+src="([^"]+\.(?:jpg|jpeg|png))"', body, re.I)
        raw = img.group(1) if img else None
        if not raw:
            img = re.search(r"/f/styles/[a-z0-9_]+/public/[^\"&\s\\]+\.jpg", body)
            raw = img.group(0) if img else None
        if not raw:
            continue
        image, fallback = image_urls(raw)

        # Titulo, por fiabilidad decreciente. No se raspa texto del markup:
        # arrastra el widget "More Share Options".
        title = ""
        dv = re.search(r'data-video="([^"]*)"', body)
        if dv and dv.group(1).strip():
            try:
                pl = json.loads(html.unescape(dv.group(1))).get("playlist") or []
                if pl:
                    title = pl[0].get("title") or ""
            except (ValueError, AttributeError):
                pass
        if not title:
            t = (re.search(r'<a\b[^>]*\btitle="([^"]+)"', body)
                 or re.search(r'data-a2a-title="([^"]+)"', body))
            if t:
                title = t.group(1)
        title = html.unescape(re.sub(r"\s+", " ", title)).strip()
        if not title and url:
            title = url.rstrip("/").split("/")[-1].replace("-", " ").title()

        it = {"cid": cid, "title": title or "(sin titulo)", "url": url,
              "image": image, "image_fallback": fallback,
              "content_type": ctype, "show": show}
        it["topic"] = topic_for(it)
        items.append(it)
    return items


def sort_for_channel(items):
    """
    El feed ya viene ordenado por relevancia editorial: ese orden se conserva
    como desempate y solo se reagrupa por tipo de contenido y por show.
    """
    for i, it in enumerate(items):
        it["_pos"] = i
    return sorted(items, key=lambda it: (
        TYPE_PRIORITY.get(it["content_type"], 9),
        SHOW_PRIORITY.get(it["show"], DEFAULT_SHOW_PRIORITY),
        it["_pos"]))


# Cualquier formato de imagen, no solo los .jpg de las feed-cards.
IMG_ANY_RE = re.compile(
    r'(?:src|srcset|data-src|data-lazy-src)="([^"]+)"', re.I)
IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|gif|webp|svg)(?:\?|$)", re.I)
ICON_EXT_RE = re.compile(r"\.(?:png|svg)(?:\?|$)", re.I)

# Logos, iconos de navegacion y promos de plataforma: no son contenido de
# feed, pero si son .png/.svg SI interesan (van al tema Iconos mas abajo en
# vez de descartarse). Filtra ademas assets que ni eso -- sprites CSS
# fragmentados (#) y placeholders vacios.
CHROME_RE = re.compile(
    r"/public/all/|logo|nav-|netflix|watch-wwe|sonyliv|sprite|icon|placeholder"
    r"|/themes/|/modules/|advertis", re.I)
ICON_JUNK_RE = re.compile(r"#|placeholder", re.I)


def scrape_loose_images(markup, source="portada"):
    """
    Todas las imagenes de un HTML, no solo las de las feed-cards.

    La home lleva hero, trending y carruseles que el feed AJAX no cubre:
    medido, 52 imagenes unicas frente a las 38 de las cards. Recoge tambien
    png/gif/webp/svg.

    Las .jpg/.gif/.webp de contenido (fotos) van al tema Otros como antes.
    Las .png/.svg que matchean CHROME_RE (logos, iconos de nav, sprites)
    antes se descartaban del todo; ahora van al tema Iconos en vez de
    perderse -- son justamente los assets de marca que interesa archivar.
    No se filtra por "/f/" para estos: los iconos suelen vivir en rutas de
    tema (/themes/, /sites/.../files/) que ese filtro excluia.

    Devuelve items con la misma forma que parse_cards, para que el resto del
    flujo (orden, dedupe, publicacion) no cambie.
    """
    vistas, items = set(), []
    for m in IMG_ANY_RE.finditer(markup):
        # srcset trae varias candidatas "url 2x, url 1x": valen todas,
        # porque luego se normalizan al mismo original.
        for cand in m.group(1).split(","):
            url = html.unescape(cand.strip().split(" ")[0])
            if not url or not IMG_EXT_RE.search(url):
                continue

            es_icono_png_svg = ICON_EXT_RE.search(url) and CHROME_RE.search(url)
            if es_icono_png_svg:
                if ICON_JUNK_RE.search(url):
                    continue  # sprites con fragment (#simbolo) o placeholders
            else:
                if CHROME_RE.search(url):
                    continue
                if "/f/" not in url:
                    continue  # fuera de /f/ solo hay assets del tema

            original = urljoin(BASE_URL, url) if es_icono_png_svg else None
            preset = original
            if not es_icono_png_svg:
                original, preset = image_urls(url)
            if original in vistas:
                continue
            vistas.add(original)

            # Sin card no hay cid: se usa la ruta del fichero (siempre la
            # misma en cada carga de la home, para un logo/icono fijo), que
            # es estable y unica -- already_seen() la dedupe entre corridas
            # sin volver a subir el mismo icono cada vez.
            ruta = original.split(BASE_URL, 1)[-1].lstrip("/")
            nombre = os.path.basename(ruta.split("?")[0])
            items.append({
                "cid": "img:" + ruta,
                "title": os.path.splitext(nombre)[0].replace("_", " "),
                "url": "", "image": original, "image_fallback": preset,
                "content_type": "icon" if es_icono_png_svg else "image",
                "show": "",
                "topic": TOPIC_ICONS if es_icono_png_svg else TOPIC_OTHER,
                "_source": source,
            })
    return items


def human(n):
    """Tamano legible."""
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return "%.0f %s" % (n, u) if u == "B" else "%.1f %s" % (n, u)
        n /= 1024.0


def download_to(session, item, dest):
    """
    Baja la imagen de 'item' a 'dest'. Intenta el original y cae al preset
    si no esta disponible. Devuelve True si quedo un archivo con contenido.
    """
    for label, url in (("original", item["image"]),
                       ("preset", item.get("image_fallback"))):
        if not url:
            continue
        try:
            with session.get(url, timeout=120, stream=True) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
            if dest.stat().st_size > 0:
                return True
            dest.unlink(missing_ok=True)
        except (requests.RequestException, OSError) as e:
            log.warning("Descarga fallo (%s): %s", label, e)
            dest.unlink(missing_ok=True)
    return False


def safe_name(cid, ext):
    """
    Nombre de archivo plano y unico a partir de un cid.

    El cid de una imagen suelta es 'img:<ruta>' y la ruta lleva barras y su
    propia extension: usarlo tal cual creaba subdirectorios inexistentes y
    una doble extension ('...jpg.jpg').

    Se quita solo la extension final conocida (no cualquier punto: 'www.wwe'
    no es una extension), se aplana el resto y se recorta a 120 caracteres
    por el limite de Windows. Como recortar puede hacer colisionar dos cid
    distintos, se anade un hash corto del cid completo.
    """
    crudo = str(cid)
    raiz = re.sub(r"\.(?:jpe?g|png|gif|webp|svg|zip)$", "", crudo, flags=re.I)
    base = re.sub(r"[^A-Za-z0-9._-]", "_", raiz)
    if len(base) > 120:
        # El hash va siempre que se recorte, para no perder unicidad.
        firma = hashlib.md5(crudo.encode("utf-8")).hexdigest()[:8]
        base = base[:111] + "_" + firma
    return base + ext


def download_image(session, item):
    """
    Descarga la imagen a disco: Telethon necesita un archivo local, no acepta
    una URL remota como si hacia la Bot API. Devuelve la ruta o None.
    """
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    url = item["image"] or item.get("image_fallback") or ""
    ext = os.path.splitext(url.split("?")[0])[1] or ".jpg"
    dest = WORK_DIR / safe_name(item["cid"], ext)
    return dest if download_to(session, item, dest) else None


# ==========================================================================
# Telegram (Telethon)
# ==========================================================================

async def get_client():
    if not API_ID or not API_HASH:
        raise SystemExit(
            "Falta TG_API_ID / TG_API_HASH. Obtenlos en https://my.telegram.org "
            "-> API development tools. Son los mismos que usa c90ToTelegram.")
    client = TelegramClient(str(ROOT / SESSION_NAME), API_ID, API_HASH)
    await client.start()
    return client


async def upload_file_parallel(client, path, progress_callback=None):
    """Sube un archivo con multiples conexiones TCP reales al mismo
    datacenter (ver fast_upload.py). Devuelve un InputFileBig/InputSizedFile
    utilizable en send_file(file=...).
    """
    return await upload_file_fast(client, Path(path), progress_callback=progress_callback)


async def ensure_group(client):
    """
    Busca el supergrupo con temas; lo crea si no existe.

    Es supergrupo (megagroup=True, forum=True) y no canal, porque solo los
    supergrupos admiten temas, que es lo que da la estructura por show.
    """
    async for d in client.iter_dialogs():
        if d.is_channel and d.name == CHANNEL_NAME:
            ent = d.entity
            if not getattr(ent, "megagroup", False):
                log.warning(
                    "'%s' es un canal, no un supergrupo: no admite temas. "
                    "Renombralo o borralo para que se cree el supergrupo.",
                    CHANNEL_NAME)
            return ent
    log.info("creando supergrupo privado %s con temas", CHANNEL_NAME)
    res = await client(CreateChannelRequest(
        title=CHANNEL_NAME,
        about="Imagenes de la portada de WWE.com",
        megagroup=True, forum=True))
    return res.chats[0]


async def ensure_topics(client, group, nombres):
    """
    Crea los temas que falten y devuelve {nombre: topic_id}.

    Si no se pueden crear, aborta: publicar sin ellos dejaria los mensajes
    sueltos en la raiz, y Telegram no permite reubicarlos despues.
    """
    topics = {}
    try:
        res = await client(GetForumTopicsRequest(
            peer=group, offset_date=0, offset_id=0, offset_topic=0, limit=100))
        for t in res.topics:
            if isinstance(t, ForumTopic):
                topics[t.title] = t.id
    except Exception as e:
        raise IOError("no se pudieron listar los temas de %s: %s"
                      % (CHANNEL_NAME, e))

    for n in nombres:
        if n in topics:
            continue
        for _ in range(MAX_RETRIES):
            try:
                r = await client(CreateForumTopicRequest(peer=group, title=n))
                # El id del tema es el id del mensaje de servicio que lo abre.
                mid = next((u.id for u in r.updates if hasattr(u, "id")), None)
                if not mid:
                    raise IOError("la respuesta no trae el id del tema")
                topics[n] = mid
                log.info("tema creado: %s", n)
                await asyncio.sleep(1.0)
                break
            except FloodWaitError as e:
                log.warning("FloodWait al crear tema: %ds", e.seconds)
                await asyncio.sleep(e.seconds + 5)
        else:
            raise IOError("no se pudo crear el tema '%s'" % n)

    faltan = [n for n in nombres if n not in topics]
    if faltan:
        raise IOError("faltan temas por crear: %s" % faltan)
    log.info("%d temas listos", len(topics))
    return topics


def build_caption(item):
    cap = "<b>%s</b>" % html.escape(item["title"])
    tags = []
    if item["show"]:
        tags.append("#" + SHOW_LABEL.get(item["show"], item["show"]))
    if item["content_type"] == "article":
        tags.append("#Articulo")
    elif item["content_type"].startswith("video"):
        tags.append("#Video")
    elif item["content_type"] == "icon":
        tags.append("#Icono")
    elif item["content_type"] == "superstar":
        tags.append("#Superstar")
    elif item["content_type"] == "show":
        tags.append("#Show")
    elif item["content_type"] == "event":
        tags.append("#Evento")
    if tags:
        cap += "\n" + " ".join(tags)
    if item["url"]:
        cap += '\n\n<a href="%s">Ver en WWE.com</a>' % html.escape(item["url"])
    return cap[:CAPTION_MAX]


async def publish(client, group, topic_id, path, item):
    """Sube la imagen al tema correspondiente, respetando los FloodWait."""
    # Telegram no acepta SVG como foto embebida (solo Telethon lo intentaria
    # igual y el servidor lo rechaza o lo entrega sin preview): los iconos
    # van como documento, ademas preserva el archivo original sin recomprimir.
    as_doc = item["content_type"] == "icon"
    for _ in range(MAX_RETRIES):
        try:
            await client.send_file(
                group, str(path), caption=build_caption(item),
                parse_mode="html", reply_to=topic_id,
                force_document=as_doc)
            return True
        except FloodWaitError as e:
            log.warning("FloodWait al publicar: %ds", e.seconds)
            await asyncio.sleep(e.seconds + 5)
        except Exception as e:
            log.warning("Fallo al publicar cid=%s: %s", item["cid"], e)
            await asyncio.sleep(5)
    log.error("No se pudo publicar cid=%s", item["cid"])
    return False


async def send_media(client, group, topic_id, paths, caption,
                     as_document=False):
    """
    Envia una lista de archivos como UN solo mensaje agrupado.

    Telethon agrupa automaticamente cuando se le pasa una lista (maximo 10,
    limite de Telegram para un media group). El pie va en el primer elemento,
    que es el que se ve en el listado del tema.

    Devuelve el mensaje enviado (o el primero, si Telegram devuelve varios
    para un album) para poder enlazarlo despues, o False si fallo.
    """
    paths = [p for p in paths if p and p.exists()]
    if not paths:
        return False

    grande = [p for p in paths if p.stat().st_size > MAX_UPLOAD]
    if grande:
        log.error("Archivo demasiado grande para subir: %s (%s)",
                  grande[0].name, human(grande[0].stat().st_size))
        return False

    # Los ZIP de galeria van solos y como documento: ahi vale la pena la
    # subida paralela (ver upload_file_parallel). Los albumes de fotos van
    # por el camino normal, que necesita la ruta en disco para recomprimir
    # cada imagen antes de subirla.
    if as_document and len(paths) == 1:
        size = paths[0].stat().st_size
        last_pct = [-1]

        def _cb(sent, total):
            # Sin esto, un ZIP grande a ~0.4 MB/s deja la consola muda
            # varios minutos y parece colgada (ver run_backfill_all).
            pct = int(sent * 100 / total) if total else 0
            if pct != last_pct[0] and pct % 20 == 0:
                last_pct[0] = pct
                log.info("  subiendo %s: %d%% de %s",
                         paths[0].name, pct, human(size))

        for _ in range(MAX_RETRIES):
            try:
                handle = await upload_file_parallel(client, paths[0],
                                                    progress_callback=_cb)
                return await client.send_file(
                    group, handle, caption=caption,
                    parse_mode="html", reply_to=topic_id,
                    force_document=True,
                    file_size=paths[0].stat().st_size,
                    attributes=[tltypes.DocumentAttributeFilename(paths[0].name)])
            except FloodWaitError as e:
                log.warning("FloodWait al publicar: %ds", e.seconds)
                await asyncio.sleep(e.seconds + 5)
            except Exception as e:
                log.warning("Fallo al enviar %s: %s", paths[0].name, e)
                await asyncio.sleep(5)
        log.error("No se pudieron enviar %d archivo(s).", len(paths))
        return False

    # Un album no toma la ruta rapida de fast_upload (esa es solo para un
    # documento suelto): Telethon lo sube secuencial, con recompresion de
    # imagen, al ritmo mas lento medido (~0.23 MB/s). Sin progreso aca, un
    # album de fotos pesadas deja la consola muda varios minutos.
    last_done = [-1]

    def _cb(sent, total):
        # send_file con lista reporta 'sent' fraccionario (2.5 = mitad del
        # archivo 3 de N); solo interesa marcar cuando se completa uno.
        done = int(sent)
        if done != last_done[0]:
            last_done[0] = done
            log.info("  album: %d/%d archivo(s) subidos", min(done, len(paths)), len(paths))

    for _ in range(MAX_RETRIES):
        try:
            sent = await client.send_file(
                group, [str(p) for p in paths], caption=caption,
                parse_mode="html", reply_to=topic_id,
                force_document=as_document,
                progress_callback=_cb if len(paths) > 1 else None)
            # Un album devuelve una lista de mensajes; el primero es el que
            # lleva el pie y al que interesa enlazar.
            return sent[0] if isinstance(sent, list) else sent
        except FloodWaitError as e:
            log.warning("FloodWait al publicar: %ds", e.seconds)
            await asyncio.sleep(e.seconds + 5)
        except Exception as e:
            log.warning("Fallo al enviar %d archivo(s): %s", len(paths), e)
            await asyncio.sleep(5)
    log.error("No se pudieron enviar %d archivo(s).", len(paths))
    return False


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


async def publish_gallery(client, group, topics, session, conn, item,
                          dry_run=False):
    """
    Publica una galeria como un post en el tema de su show:

      1. el ZIP con TODAS las fotos en calidad original, primero, y
      2. TODAS las fotos, repartidas en albumes de ALBUM_MAX (el maximo que
         Telegram agrupa en un mismo mensaje), cada uno con un enlace de
         vuelta al mensaje del ZIP.

    Antes solo se subia un album de muestra de ALBUM_MAX fotos; ahora se
    suben todas, sin perder la referencia al ZIP: en vez del pie unico de
    antes, cada album enlaza al mensaje del ZIP (que va primero, para que
    el enlace ya exista cuando se arman los pies de los albumes).

    No se abre un tema por galeria: hay ~1.600 en el archivo historico y esa
    lista de temas dejaria el supergrupo inservible. Asi cada galeria ocupa
    varias posiciones seguidas, en su lugar cronologico del feed.

    Devuelve 1 si se publico, 0 si no.
    """
    titulo, _desc, fotos = fetch_gallery(session, item["cid"])
    if not fotos:
        log.warning("Galeria cid=%s sin fotos; la trato como item normal.",
                    item["cid"])
        return 0

    titulo = titulo or item["title"]
    if dry_run:
        albumes = -(-len(fotos) // ALBUM_MAX)  # ceil
        log.info("[DRY-RUN] galeria '%s': %d fotos en %d album(es) + ZIP",
                 titulo[:50], len(fotos), albumes)
        return 1

    tid = topics[item["topic"]]
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    # safe_name: en --photos el cid puede venir como 'gal:<url>'.
    carpeta = WORK_DIR / safe_name("gal_%s" % item["cid"], "")
    carpeta.mkdir(exist_ok=True)
    descargadas = []
    try:
        # 1) Todas las fotos a disco, numeradas para conservar el orden
        #    dentro del ZIP (el nombre original no siempre lo respeta).
        # Sin este log, una galeria grande con fotos lentas o que fallan
        # (cada download_to() puede tardar hasta 120s antes de caer al
        # preset) deja la consola muda varios minutos y parece colgada.
        log.info("Galeria '%s': descargando %d fotos...", titulo[:45], len(fotos))
        for i, foto in enumerate(fotos, 1):
            ext = os.path.splitext(foto["image"].split("?")[0])[1] or ".jpg"
            destino = carpeta / ("%03d_%s%s" % (i, foto["fid"], ext))
            if download_to(session, foto, destino):
                descargadas.append(destino)
            if i % 10 == 0 or i == len(fotos):
                log.info("  %d/%d fotos descargadas", i, len(fotos))
        if not descargadas:
            log.error("Galeria %s: ninguna foto descargada.", item["cid"])
            return 0

        # 2) ZIP. Ya vienen comprimidas: ZIP_STORED evita gastar CPU
        #    recomprimiendo para no ganar casi nada.
        zip_path = WORK_DIR / gallery_slug(item)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
            for f in descargadas:
                z.write(f, f.name)
        tam = zip_path.stat().st_size

        etiqueta_show = ("\n#" + SHOW_LABEL.get(item["show"], item["show"])
                         if item.get("show") else "")
        enlace_wwe = ('\n\n<a href="%s">Ver galeria en WWE.com</a>'
                     % html.escape(item["url"])) if item.get("url") else ""

        # 3) ZIP primero: asi ya existe su mensaje (y el enlace a el) cuando
        #    se arman los pies de los albumes de fotos, que lo referencian.
        log.info("Galeria '%s': subiendo ZIP de %d fotos (%s)...",
                 titulo[:45], len(descargadas), human(tam))
        zip_pie = ("📦 <b>%s</b> — %d fotos en calidad original%s"
                   % (html.escape(titulo)[:200], len(descargadas), etiqueta_show))
        zip_msg = await send_media(client, group, tid, [zip_path],
                                   zip_pie[:CAPTION_MAX], as_document=True)
        if not zip_msg:
            return 0
        zip_link = "https://t.me/c/%d/%d" % (group.id, zip_msg.id)

        # 4) Todas las fotos, en albumes de ALBUM_MAX. Cada uno enlaza al
        #    ZIP, asi cualquiera de ellos (no solo el primero) permite
        #    llegar a la galeria completa en calidad original.
        grupos = list(_chunks(descargadas, ALBUM_MAX))
        await asyncio.sleep(SEND_DELAY)
        enviadas = 0
        for idx, grupo in enumerate(grupos, 1):
            pie = "<b>%s</b>" % html.escape(titulo)
            if len(grupos) > 1:
                pie += ("\n📸 Parte %d/%d (%d fotos)"
                       % (idx, len(grupos), len(descargadas)))
            else:
                pie += "\n📸 %d fotos" % len(descargadas)
            pie += etiqueta_show
            pie += ('\n\n<a href="%s">📦 ZIP con todas en calidad original</a>'
                   % zip_link)
            pie += enlace_wwe
            pie = pie[:CAPTION_MAX]

            log.info("Galeria '%s': subiendo album %d/%d (%d fotos)...",
                     titulo[:45], idx, len(grupos), len(grupo))
            ok = await send_media(client, group, tid, grupo, pie)
            if ok:
                enviadas += len(grupo)
            if idx < len(grupos):
                await asyncio.sleep(SEND_DELAY)

        log.info("Galeria '%s': %d/%d fotos publicadas en %d album(es) + ZIP (%s)",
                 titulo[:45], enviadas, len(descargadas), len(grupos), human(tam))
        return 1
    finally:
        # El disco no se queda con la galeria descargada.
        for f in descargadas:
            f.unlink(missing_ok=True)
        try:
            zip_path.unlink(missing_ok=True)
        except (NameError, OSError):
            pass
        try:
            carpeta.rmdir()
        except OSError:
            pass


# ==========================================================================

async def run_once(dry_run=False, client=None):
    """Una pasada completa. Devuelve el numero de publicaciones.

    `client`: si se pasa un TelegramClient ya conectado (run_backfill_all lo
    comparte para intercalar novedades sin abrir una segunda conexion sobre
    el mismo archivo de sesion), se reusa y NO se desconecta al salir; si
    no, esta funcion crea y cierra el suyo, como antes.
    """
    conn = db_connect()
    own_client = client is None
    try:
        purge_old(conn)
        first_run = is_first_run(conn)

        session = new_session()
        dom_id = get_view_dom_id(session)

        collected, seen = [], set()

        # La home lleva hero, trending y carruseles que el feed AJAX no
        # devuelve: medido, 52 imagenes unicas frente a las 38 de las cards.
        if LOOSE_IMAGES:
            try:
                home = session.get(HOMEPAGE_URL, timeout=30)
                home.raise_for_status()
                sueltas = scrape_loose_images(home.text)
                nuevas = [i for i in sueltas if i["cid"] not in seen]
                seen.update(i["cid"] for i in nuevas)
                collected.extend(nuevas)
                log.info("Home: %d imagenes sueltas", len(nuevas))
            except requests.RequestException as e:
                log.warning("No se pudieron leer las imagenes de la home: %s", e)

        for page in range(MAX_PAGES):
            try:
                markup = fetch_page(session, dom_id, page)
            except RuntimeError as e:
                log.error("%s", e)
                break
            cards = parse_cards(markup)
            if not cards:
                log.info("Pagina %d vacia; fin del scroll.", page)
                break
            # Las galerias tienen su propio modo (--photos): aqui solo su
            # imagen de portada, como un item mas.
            fresh = [c for c in cards if c["cid"] not in seen]
            seen.update(c["cid"] for c in fresh)
            collected.extend(fresh)

            if LOOSE_IMAGES:
                extra = [i for i in scrape_loose_images(markup, "feed")
                         if i["cid"] not in seen]
                seen.update(i["cid"] for i in extra)
                collected.extend(extra)

            log.info("Pagina %d: %d cards, %d unicas", page, len(cards), len(fresh))
            time.sleep(1.5)  # cortesia con el servidor

        nuevos = [it for it in collected if not already_seen(conn, it["cid"])]
        log.info("Recogidos %d, nuevos %d", len(collected), len(nuevos))
        if not nuevos:
            return 0

        ordenados = sort_for_channel(nuevos)
        limite = FIRST_RUN_SEND if first_run else MAX_SEND_PER_RUN
        if first_run:
            log.info("Primer arranque: indexo %d, publico %d.",
                     len(ordenados), min(limite, len(ordenados)))
        objetivo = {it["cid"] for it in ordenados[:limite]}

        if dry_run:
            for it in ordenados[:limite]:
                log.info("[DRY-RUN] %-12s | %s", it["topic"], it["title"][:60])
            log.info("[DRY-RUN] %d se publicarian, %d solo se indexarian. "
                     "No se toca la BD, asi que otra pasada en seco "
                     "vuelve a listar lo mismo.",
                     len(objetivo), len(ordenados) - len(objetivo))
            return 0

        if client is None:
            client = await get_client()
        group = await ensure_group(client)
        # Se crean todos los temas por adelantado y en orden fijo: Telegram
        # no permite reordenarlos despues.
        topics = await ensure_topics(client, group, TOPICS_ORDER)

        enviados = 0
        for item in ordenados:
            if item["cid"] not in objetivo:
                # Visto pero no publicado: no se acumula para la proxima vez.
                record(conn, item, False)
                continue

            # Las galerias son cosa de --photos: aqui solo va su portada.
            path = download_image(session, item)
            if not path:
                log.error("Sin imagen para cid=%s; se marca como visto.",
                          item["cid"])
                record(conn, item, False)
                continue
            try:
                ok = await publish(client, group, topics[item["topic"]],
                                   path, item)
            finally:
                # El disco nunca guarda mas de una imagen a la vez.
                path.unlink(missing_ok=True)

            record(conn, item, ok)
            if ok:
                enviados += 1
                log.info("Publicado [%s] %s", item["topic"], item["title"][:60])
            await asyncio.sleep(SEND_DELAY)

        log.info("Fin: %d publicados de %d nuevos.", enviados, len(nuevos))
        return enviados
    finally:
        conn.close()
        if own_client and client:
            await client.disconnect()


async def run_photos(limit=0, start_page=None, dry_run=False, client=None):
    """
    Modo GALERIAS: recorre /photos, el listado propio de galerias de WWE.

    Independiente del modo portada: tiene su propio progreso ('photos_page'),
    asi que se pueden lanzar por separado sin pisarse. Comparten el indice
    'seen', asi que una galeria ya publicada por el otro modo no se repite.

    Cada galeria se publica como un solo post (album de muestra + ZIP), en el
    tema de su show.

    `client`: si se pasa un TelegramClient ya conectado (--backfill-all lo
    comparte con run_backfill para no abrir dos conexiones a la vez sobre el
    mismo archivo de sesion), se reusa y NO se desconecta al salir; si no,
    esta funcion crea y cierra el suyo, como antes.
    """
    conn = db_connect()
    own_client = client is None
    session = new_session()
    try:
        page = (int(bf_get(conn, "photos_page", "0"))
                if start_page is None else start_page)
        hechas = int(bf_get(conn, "photos_total", "0"))
        log.info("Galerias desde la pagina %d (llevaba %d).", page, hechas)

        # El dom_id de /photos es distinto al de la portada.
        try:
            r = session.get(BASE_URL + "/photos", timeout=30)
            r.raise_for_status()
            m = re.search(r"js-view-dom-id-([0-9a-f]{16,})", r.text)
            dom_id = m.group(1) if m else ""
        except requests.RequestException as e:
            log.error("No se pudo abrir /photos: %s", e)
            return 0

        topics = None
        if not dry_run:
            if client is None:
                client = await get_client()
            group = await ensure_group(client)
            topics = await ensure_topics(client, group, TOPICS_ORDER)

        procesadas, vacias = 0, 0
        while limit == 0 or procesadas < limit:
            try:
                markup = fetch_page(session, dom_id, page, view=PHOTOS_VIEW,
                                    display=PHOTOS_DISPLAY, path=PHOTOS_PATH)
            except RuntimeError as e:
                log.error("%s. Me detengo; al relanzar sigo aqui.", e)
                break

            galerias = parse_photos_page(markup)
            if not galerias:
                vacias += 1
                if vacias >= 2:
                    log.info("Pagina %d vacia dos veces: fin de /photos.", page)
                    bf_set(conn, "photos_done", "1")
                    break
                page += 1
                continue
            vacias = 0

            nuevas = [g for g in galerias
                      if not already_seen(conn, "gal:" + g["url"])]
            log.info("Pagina %d: %d galerias, %d nuevas (total: %d)",
                     page, len(galerias), len(nuevas), hechas)

            for gal in nuevas:
                if dry_run:
                    log.info("[DRY-RUN] %-11s | %s", gal["topic"],
                             gal["title"][:62])
                    continue

                nid = resolve_gallery_nid(session, gal["url"])
                if not nid:
                    log.warning("Sin nid para %s; la salto.", gal["url"])
                    gal["cid"] = "gal:" + gal["url"]
                    record(conn, gal, False)
                    continue

                gal["cid"] = nid
                ok = await publish_gallery(client, group, topics, session,
                                           conn, gal, dry_run)
                # Se marca por URL y por nid: asi no se repite ni viniendo
                # de /photos ni encontrandola en el feed de portada.
                marca = dict(gal, cid="gal:" + gal["url"])
                record(conn, marca, bool(ok))
                record(conn, gal, bool(ok))
                if ok:
                    hechas += 1
                    bf_set(conn, "photos_total", hechas)
                await asyncio.sleep(SEND_DELAY)

            page += 1
            procesadas += 1
            if not dry_run:
                if page > int(bf_get(conn, "photos_page", "0")):
                    bf_set(conn, "photos_page", page)
            time.sleep(1.0)  # cortesia con el servidor

        log.info("Tanda terminada: %d paginas, %d galerias en total. "
                 "Proxima pagina: %d", procesadas, hechas, page)
        return hechas
    finally:
        conn.close()
        if own_client and client:
            await client.disconnect()


async def run_superstars(limit=0, start_page=None, dry_run=False, client=None):
    """
    Modo SUPERSTARS: recorre /superstars, el listado de luchadores.

    Misma vista Drupal que la portada (views_infinite_scroll), asi que
    pagina igual via /views/ajax; verificado 0% de solapamiento entre
    paginas. Independiente de los otros modos: tiene su propio progreso
    ('superstars_page') y su propio lock, y comparte el indice 'seen'.

    Cada luchador se publica como una sola foto de perfil en el tema
    Superstars.

    `client`: si se pasa un TelegramClient ya conectado (--backfill-all lo
    comparte con los demas modos para no abrir dos conexiones a la vez sobre
    el mismo archivo de sesion), se reusa y NO se desconecta al salir; si
    no, esta funcion crea y cierra el suyo, como antes.
    """
    conn = db_connect()
    own_client = client is None
    session = new_session()
    try:
        page = (int(bf_get(conn, "superstars_page", "0"))
                if start_page is None else start_page)
        hechos = int(bf_get(conn, "superstars_total", "0"))
        log.info("Superstars desde la pagina %d (llevaba %d).", page, hechos)

        try:
            r = session.get(BASE_URL + SUPERSTARS_PATH, timeout=30)
            r.raise_for_status()
            m = re.search(r"js-view-dom-id-([0-9a-f]{16,})", r.text)
            dom_id = m.group(1) if m else ""
        except requests.RequestException as e:
            log.error("No se pudo abrir %s: %s", SUPERSTARS_PATH, e)
            return 0

        topics = None
        if not dry_run:
            if client is None:
                client = await get_client()
            group = await ensure_group(client)
            topics = await ensure_topics(client, group, TOPICS_ORDER)

        procesadas, vacias = 0, 0
        while limit == 0 or procesadas < limit:
            try:
                markup = fetch_page(session, dom_id, page,
                                    view=SUPERSTARS_VIEW,
                                    display=SUPERSTARS_DISPLAY,
                                    path=SUPERSTARS_PATH)
            except RuntimeError as e:
                log.error("%s. Me detengo; al relanzar sigo aqui.", e)
                break

            luchadores = parse_superstars_page(markup)
            if not luchadores:
                vacias += 1
                if vacias >= 2:
                    log.info("Pagina %d vacia dos veces: fin de %s.",
                             page, SUPERSTARS_PATH)
                    bf_set(conn, "superstars_done", "1")
                    break
                page += 1
                continue
            vacias = 0

            nuevos = [it for it in luchadores if not already_seen(conn, it["cid"])]
            log.info("Pagina %d: %d luchadores, %d nuevos (total: %d)",
                     page, len(luchadores), len(nuevos), hechos)

            for it in nuevos:
                if dry_run:
                    log.info("[DRY-RUN] %-11s | %s", it["topic"], it["title"][:62])
                    continue

                path = download_image(session, it)
                if not path:
                    log.error("Sin imagen para %s; se marca como visto.", it["cid"])
                    record(conn, it, False)
                    continue
                try:
                    ok = await publish(client, group, topics[it["topic"]], path, it)
                finally:
                    path.unlink(missing_ok=True)
                record(conn, it, ok)
                if ok:
                    hechos += 1
                    bf_set(conn, "superstars_total", hechos)
                await asyncio.sleep(SEND_DELAY)

            page += 1
            procesadas += 1
            if not dry_run:
                if page > int(bf_get(conn, "superstars_page", "0")):
                    bf_set(conn, "superstars_page", page)
            time.sleep(1.0)  # cortesia con el servidor

        log.info("Tanda terminada: %d paginas, %d superstars en total. "
                 "Proxima pagina: %d", procesadas, hechos, page)
        return hechos
    finally:
        conn.close()
        if own_client and client:
            await client.disconnect()


async def run_shows(dry_run=False, client=None):
    """
    Modo SHOWS: /shows es un hub estatico y pequeno (9 shows fijos: Raw,
    SmackDown, NXT, SNME, Money in the Bank, Survivor Series WarGames, AAA,
    WWE Evolve, NXT PLE), sin scroll infinito ni archivo historico: no hace
    falta paginar ni progreso por pagina, basta con revisarlo entero cada
    vez (el dedupe por cid evita republicar los mismos 9).

    `client`: si se pasa un TelegramClient ya conectado, se reusa y NO se
    desconecta al salir; si no, esta funcion crea y cierra el suyo.
    """
    conn = db_connect()
    own_client = client is None
    session = new_session()
    try:
        try:
            r = session.get(SHOWS_URL, timeout=30)
            r.raise_for_status()
        except requests.RequestException as e:
            log.error("No se pudo abrir /shows: %s", e)
            return 0

        shows = parse_shows_page(r.text)
        nuevos = [it for it in shows if not already_seen(conn, it["cid"])]
        log.info("Shows: %d en el hub, %d nuevos.", len(shows), len(nuevos))
        if not nuevos:
            return 0

        if dry_run:
            for it in nuevos:
                log.info("[DRY-RUN] %-11s | %s", it["topic"], it["title"][:62])
            return 0

        if client is None:
            client = await get_client()
        group = await ensure_group(client)
        topics = await ensure_topics(client, group, TOPICS_ORDER)

        enviados = 0
        for it in nuevos:
            path = download_image(session, it)
            if not path:
                log.error("Sin imagen para %s; se marca como visto.", it["cid"])
                record(conn, it, False)
                continue
            try:
                ok = await publish(client, group, topics[it["topic"]], path, it)
            finally:
                path.unlink(missing_ok=True)
            record(conn, it, ok)
            if ok:
                enviados += 1
                log.info("Publicado [%s] %s", it["topic"], it["title"][:60])
            await asyncio.sleep(SEND_DELAY)

        log.info("Shows: %d publicados de %d nuevos.", enviados, len(nuevos))
        return enviados
    finally:
        conn.close()
        if own_client and client:
            await client.disconnect()


async def run_events(dry_run=False, client=None):
    """
    Modo EVENTS: /events/ redirige (302, geolocalizado por IP) a
    /events/results/all-events/all-dates/<lat>/<lng>/<ciudad>/<pais>, una
    landing de "proximos eventos" sin scroll infinito ni pager: no es un
    archivo historico como /photos, solo vigilancia de novedades. El cid
    se arma con el slug de /event/<slug>, asi que un evento que deja de
    aparecer (porque ya paso) no se vuelve a tocar ni se pierde: queda
    marcado en 'seen' desde la primera vez que se vio.

    `client`: si se pasa un TelegramClient ya conectado, se reusa y NO se
    desconecta al salir; si no, esta funcion crea y cierra el suyo.
    """
    conn = db_connect()
    own_client = client is None
    session = new_session()
    try:
        try:
            # requests sigue el 302 solo; la URL final ya trae la geo
            # resuelta por Fastly a partir de la IP de salida.
            r = session.get(EVENTS_URL, timeout=30)
            r.raise_for_status()
        except requests.RequestException as e:
            log.error("No se pudo abrir /events/: %s", e)
            return 0

        eventos = parse_events_page(r.text)
        nuevos = [it for it in eventos if not already_seen(conn, it["cid"])]
        log.info("Events: %d proximos, %d nuevos.", len(eventos), len(nuevos))
        if not nuevos:
            return 0

        if dry_run:
            for it in nuevos:
                log.info("[DRY-RUN] %-11s | %s", it["topic"], it["title"][:62])
            return 0

        if client is None:
            client = await get_client()
        group = await ensure_group(client)
        topics = await ensure_topics(client, group, TOPICS_ORDER)

        enviados = 0
        for it in nuevos:
            path = download_image(session, it)
            if not path:
                log.error("Sin imagen para %s; se marca como visto.", it["cid"])
                record(conn, it, False)
                continue
            try:
                ok = await publish(client, group, topics[it["topic"]], path, it)
            finally:
                path.unlink(missing_ok=True)
            record(conn, it, ok)
            if ok:
                enviados += 1
                log.info("Publicado [%s] %s", it["topic"], it["title"][:60])
            await asyncio.sleep(SEND_DELAY)

        log.info("Events: %d publicados de %d nuevos.", enviados, len(nuevos))
        return enviados
    finally:
        conn.close()
        if own_client and client:
            await client.disconnect()


async def run_backfill(limit=0, start_page=None, dry_run=False, client=None):
    """
    Volcado del archivo historico, pagina a pagina y REANUDABLE.

    El feed no se agota como el modo normal: llega hasta la pagina ~2222
    (~22.200 items, ~37 GB). Recorrerlo entero son 1-2 dias de subida, asi
    que esto guarda la pagina alcanzada en la tabla 'backfill' y al relanzar
    sigue donde iba, igual que el 'run' de c90ToTelegram.

    Se avanza de mayor a menor prioridad de pagina (0, 1, 2...) y se publica
    todo lo que no este ya en 'seen', asi que convive con el modo normal:
    lo que ya publico la vigilancia no se repite.

    limit: paginas a procesar en esta tanda (0 = hasta el final del feed).

    `client`: si se pasa un TelegramClient ya conectado (--backfill-all lo
    comparte con run_photos para no abrir dos conexiones a la vez sobre el
    mismo archivo de sesion), se reusa y NO se desconecta al salir; si no,
    esta funcion crea y cierra el suyo, como antes.
    """
    conn = db_connect()
    own_client = client is None
    session = new_session()
    try:
        page = int(bf_get(conn, "page", "0")) if start_page is None else start_page
        publicados = int(bf_get(conn, "sent_total", "0"))
        log.info("Backfill desde la pagina %d (llevaba %d publicados).",
                 page, publicados)

        if not dry_run:
            if client is None:
                client = await get_client()
            group = await ensure_group(client)
            topics = await ensure_topics(client, group, TOPICS_ORDER)

        dom_id = get_view_dom_id(session)
        procesadas = 0
        vacias = 0

        while limit == 0 or procesadas < limit:
            try:
                cards = parse_cards(fetch_page(session, dom_id, page))
            except RuntimeError as e:
                log.error("%s. Me detengo; al relanzar sigo en esta pagina.", e)
                break

            if not cards:
                # Dos vacias seguidas = fin real del feed, no un hueco.
                vacias += 1
                if vacias >= 2:
                    log.info("Pagina %d vacia dos veces: fin del archivo.", page)
                    bf_set(conn, "done", "1")
                    break
                page += 1
                save_progress(conn, page)
                continue
            vacias = 0

            nuevos = [c for c in cards if not already_seen(conn, c["cid"])]
            log.info("Pagina %d: %d cards, %d nuevas (total publicado: %d)",
                     page, len(cards), len(nuevos), publicados)

            for item in nuevos:
                if dry_run:
                    marca = " [GALERIA]" if is_gallery(item) else ""
                    log.info("[DRY-RUN] %-12s | %s%s", item["topic"],
                             item["title"][:60], marca)
                    continue

                # Las galerias son cosa de --photos: aqui solo su portada.
                path = download_image(session, item)
                if not path:
                    log.error("Sin imagen para cid=%s; se marca como visto.",
                              item["cid"])
                    record(conn, item, False)
                    continue
                try:
                    ok = await publish(client, group, topics[item["topic"]],
                                       path, item)
                finally:
                    path.unlink(missing_ok=True)
                record(conn, item, ok)
                if ok:
                    publicados += 1
                    bf_set(conn, "sent_total", publicados)
                await asyncio.sleep(SEND_DELAY)

            # Solo se avanza tras terminar la pagina: si esto muere a medias,
            # al relanzar la repite y el dedupe evita republicar.
            page += 1
            procesadas += 1
            if not dry_run:
                save_progress(conn, page)
            time.sleep(1.0)  # cortesia con el servidor

        log.info("Tanda terminada: %d paginas, %d publicados en total. "
                 "Proxima pagina: %d", procesadas, publicados, page)
        return publicados
    finally:
        conn.close()
        if own_client and client:
            await client.disconnect()


# Cada cuantas tandas de backfill se intercala una pasada de run_once
# (novedades del dia a dia, ver run_backfill_all). run_backfill/run_photos/
# run_superstars solo avanzan hacia paginas mas altas y nunca vuelven a la 0:
# sin esto, lo que WWE.com publique mientras el backfill esta en curso no se
# capturaria nunca, porque el backfill ya la paso de largo para cuando llega.
NOVEDADES_CADA_TANDAS = 20

# /shows y /events no son archivo historico paginable (ver run_shows/
# run_events): son un puñado de items estaticos o de vigilancia geolocalizada
# que se revisan enteros cada vez. No hace falta mirarlos en cada tanda como
# el resto -- alcanza con cada N tandas, igual que las novedades de portada.
SHOWS_EVENTS_CADA_TANDAS = 20


async def run_backfill_all(dry_run=False):
    """
    Corre portada, galerias y superstars historicos HASTA EL FINAL de los
    tres, alternando tandas en el mismo proceso con un solo TelegramClient
    compartido, e intercala cada NOVEDADES_CADA_TANDAS vueltas una pasada de
    run_once (novedades de portada) y cada SHOWS_EVENTS_CADA_TANDAS una de
    run_shows + run_events (contenido estatico/geolocalizado, sin archivo
    historico que agotar) para cubrir tambien lo que WWE.com publica ahi
    mientras tanto.

    Pensado para el unico always-on task disponible en PythonAnywhere
    Developer: un solo comando que deja corriendo el volcado completo de
    /homepage (--backfill), /photos (--photos) y /superstars
    (--superstars) sin intervencion, en vez de necesitar tareas separadas
    que el plan no tiene espacio para correr a la vez.

    Por que hace falta intercalar run_once: run_backfill avanza de pagina 0
    hacia arriba y NUNCA vuelve a revisar paginas ya pasadas. Como la pagina
    0 del feed es siempre "lo mas reciente", todo lo que se corre hacia
    paginas mas altas por contenido nuevo publicado despues de que el
    backfill ya avanzo de esas paginas se identifica igual por su cid (id
    de nodo Drupal, estable, no la posicion) asi que no se duplica -- pero
    algo COMPLETAMENTE nuevo que aparece en la pagina 0 mientras el backfill
    esta en la pagina 872, por ejemplo, nunca se veria si nada vuelve a
    mirar la pagina 0. run_once si la mira (recorre MAX_PAGES paginas desde
    la 0 en cada pasada), por eso se intercala. /superstars no tiene ese
    problema (el listado entero se revisa por pagina y el orden no importa
    tanto), pero comparte el mismo patron de avance de solo ida.

    No se usa asyncio.gather para correr los modos en paralelo real porque
    cada uno abre su propio TelegramClient, y dos clientes escribiendo a la
    vez sobre el mismo archivo .session (una base SQLite interna de
    Telethon) puede dar 'database is locked'. En vez de eso se alterna: una
    tanda chica de cada modo, y se repite. El cuello de botella real es la
    subida a Telegram (~0.4 MB/s medido, ver fast_upload.py), no la CPU,
    asi que alternar en vez de paralelizar de verdad no cuesta velocidad
    total apreciable.

    Reanudable como los modos sueltos: el progreso de cada uno vive en sus
    propias claves de la tabla 'backfill' ('page'/'done',
    'photos_page'/'photos_done' y 'superstars_page'/'superstars_done'), asi
    que cortar esto con Ctrl+C o que se caiga el always-on task no pierde
    avance. run_once/run_shows/run_events no tienen una nocion de
    "terminado" (siempre hay novedades por revisar), asi que se siguen
    intercalando incluso despues de que portada, galerias y superstars ya
    llegaron al final de su archivo.
    """
    client = None
    try:
        if not dry_run:
            client = await get_client()

        tanda = 0
        while True:
            tanda += 1
            c = db_connect()
            feed_done = bf_get(c, "done") == "1"
            photos_done = bf_get(c, "photos_done") == "1"
            superstars_done = bf_get(c, "superstars_done") == "1"
            c.close()

            if tanda % NOVEDADES_CADA_TANDAS == 0:
                try:
                    log.info("Revisando novedades del dia a dia (tanda #%d)...", tanda)
                    await run_once(dry_run=dry_run, client=client)
                except Exception:
                    log.exception("Error revisando novedades (#%d); sigo.", tanda)

            if tanda % SHOWS_EVENTS_CADA_TANDAS == 0:
                try:
                    log.info("Revisando shows y eventos (tanda #%d)...", tanda)
                    await run_shows(dry_run=dry_run, client=client)
                    await run_events(dry_run=dry_run, client=client)
                except Exception:
                    log.exception("Error revisando shows/eventos (#%d); sigo.", tanda)

            if feed_done and photos_done and superstars_done:
                # El historico ya termino, pero las novedades siguen
                # llegando: no se corta, solo se espacian mas las vueltas
                # para no golpear el sitio sin necesidad.
                log.info("Backfill historico completo; sigo revisando "
                         "novedades cada %d tandas.", NOVEDADES_CADA_TANDAS)
                await asyncio.sleep(60.0)
                continue

            if not feed_done:
                try:
                    await run_backfill(limit=1, dry_run=dry_run, client=client)
                except Exception:
                    log.exception("Error en tanda de portada (#%d); sigo.", tanda)
            else:
                log.info("Portada ya completa.")

            if not photos_done:
                try:
                    await run_photos(limit=1, dry_run=dry_run, client=client)
                except Exception:
                    log.exception("Error en tanda de galerias (#%d); sigo.", tanda)
            else:
                log.info("Galerias ya completas.")

            if not superstars_done:
                try:
                    await run_superstars(limit=1, dry_run=dry_run, client=client)
                except Exception:
                    log.exception("Error en tanda de superstars (#%d); sigo.", tanda)
            else:
                log.info("Superstars ya completos.")

            await asyncio.sleep(2.0)  # cortesia extra entre tandas alternadas
    finally:
        if client:
            await client.disconnect()


async def do_login():
    """Login interactivo; deja la sesion lista para las tareas programadas."""
    client = await get_client()
    me = await client.get_me()
    log.info("Sesion lista para %s (@%s). Archivo: %s.session",
             me.first_name, me.username, SESSION_NAME)
    group = await ensure_group(client)
    topics = await ensure_topics(client, group, TOPICS_ORDER)
    log.info("Supergrupo '%s' con %d temas: %s",
             CHANNEL_NAME, len(topics), ", ".join(TOPICS_ORDER))
    await client.disconnect()
    return 0


LAST_PAGE_ESTIMATE = 2222  # medido por busqueda binaria contra el feed


def show_status():
    """Progreso del volcado, para seguir una corrida de dias."""
    conn = db_connect()
    try:
        vistos = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        enviados = conn.execute(
            "SELECT COUNT(*) FROM seen WHERE sent=1").fetchone()[0]
        page = int(bf_get(conn, "page", "0"))
        done = bf_get(conn, "done") == "1"

        ph_page = int(bf_get(conn, "photos_page", "0"))
        ph_total = int(bf_get(conn, "photos_total", "0"))
        ph_done = bf_get(conn, "photos_done") == "1"

        ss_page = int(bf_get(conn, "superstars_page", "0"))
        ss_total = int(bf_get(conn, "superstars_total", "0"))
        ss_done = bf_get(conn, "superstars_done") == "1"

        print("Publicados     : %d" % enviados)
        print("Vistos (indice): %d" % vistos)
        print()
        print("PORTADA  pagina %d de ~%d (%.1f%%)%s"
              % (page, LAST_PAGE_ESTIMATE,
                 100.0 * page / LAST_PAGE_ESTIMATE,
                 "  [COMPLETO]" if done else ""))
        if not done:
            faltan = max(0, (LAST_PAGE_ESTIMATE - page)) * 10
            horas = faltan * SEND_DELAY / 3600.0
            print("         faltan ~%d items, %.0f h a %.0fs/envio (%.1f GB)"
                  % (faltan, horas, SEND_DELAY, faltan * 1.7 / 1024))
        print("GALERIAS pagina %d, %d galerias publicadas%s"
              % (ph_page, ph_total, "  [COMPLETO]" if ph_done else ""))
        print("SUPERSTARS pagina %d, %d luchadores publicados%s"
              % (ss_page, ss_total, "  [COMPLETO]" if ss_done else ""))
        por_tema = conn.execute(
            "SELECT topic, COUNT(*) FROM seen WHERE sent=1 "
            "GROUP BY topic ORDER BY 2 DESC").fetchall()
        if por_tema:
            print("Por tema       : " +
                  ", ".join("%s %d" % (t or "-", n) for t, n in por_tema))
    finally:
        conn.close()
    return 0


async def amain(args):
    if args.status:
        return show_status()
    if args.login:
        return await do_login()

    # Un lock por modo: cada uno es independiente y pueden correr a la vez
    # sin pisarse. --backfill-all toma los tres que tienen archivo historico
    # paginable (feed/photos/superstars), porque alterna entre ellos y no
    # deberia convivir con --backfill/--photos/--superstars sueltos pisando
    # el mismo progreso a la vez.
    if args.backfill_all:
        modos = ["feed", "photos", "superstars"]
    elif args.superstars:
        modos = ["superstars"]
    elif args.shows:
        modos = ["shows"]
    elif args.events:
        modos = ["events"]
    else:
        modos = ["photos" if args.photos else "feed"]
    try:
        for m in modos:
            acquire_lock(m)
    except AlreadyRunning as e:
        log.warning("Ya hay una ejecucion en curso: %s. Salgo.", e)
        for m in modos:
            release_lock(m)
        return 0
    try:
        if args.backfill_all:
            log.info("Modo backfill-all: portada + galerias + superstars "
                     "hasta el final de las tres.")
            await run_backfill_all(args.dry_run)
        elif args.superstars:
            await run_superstars(args.limit, args.from_page, args.dry_run)
        elif args.shows:
            await run_shows(args.dry_run)
        elif args.events:
            await run_events(args.dry_run)
        elif args.photos:
            await run_photos(args.limit, args.from_page, args.dry_run)
        elif args.backfill:
            await run_backfill(args.limit, args.from_page, args.dry_run)
        elif args.loop:
            log.info("Modo always-on: cada %d min.", INTERVAL_MINUTES)
            while True:
                try:
                    await run_once(args.dry_run)
                except Exception:
                    # En always-on el bucle nunca muere por un fallo puntual.
                    log.exception("Error en la pasada; sigo.")
                await asyncio.sleep(INTERVAL_MINUTES * 60)
        else:
            await run_once(args.dry_run)
        return 0
    finally:
        for m in modos:
            release_lock(m)


def main():
    ap = argparse.ArgumentParser(description="WWE.com -> Telegram (Telethon)")
    ap.add_argument("--login", action="store_true",
                    help="login interactivo (una sola vez) y crea el supergrupo")
    ap.add_argument("--loop", action="store_true",
                    help="bucle continuo cada INTERVAL_MINUTES (always-on task)")
    ap.add_argument("--dry-run", action="store_true",
                    help="scrapea y ordena sin tocar Telegram")
    ap.add_argument("--photos", action="store_true",
                    help="MODO GALERIAS: recorre /photos y publica cada "
                         "galeria como album + ZIP. Reanudable.")
    ap.add_argument("--superstars", action="store_true",
                    help="MODO SUPERSTARS: recorre /superstars y publica "
                         "la foto de perfil de cada luchador. Reanudable.")
    ap.add_argument("--shows", action="store_true",
                    help="MODO SHOWS: revisa el hub estatico /shows (9 shows "
                         "fijos) y publica los que falten. Sin paginacion.")
    ap.add_argument("--events", action="store_true",
                    help="MODO EVENTS: revisa /events/ (proximos eventos, "
                         "geolocalizado) y publica los que falten. Sin "
                         "archivo historico, solo vigilancia de novedades.")
    ap.add_argument("--backfill", action="store_true",
                    help="volcado del archivo historico (~22.200 items), "
                         "reanudable: al relanzar sigue donde iba")
    ap.add_argument("--backfill-all", action="store_true",
                    help="portada (--backfill) + galerias (--photos) + "
                         "superstars (--superstars) juntos, alternando "
                         "tandas hasta el final de las tres (e intercalando "
                         "shows/events). Pensado para dejarlo como unico "
                         "always-on task, corriendo indefinidamente (nunca "
                         "corta por un error puntual)")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="con --backfill/--photos/--superstars: paginas por "
                         "tanda (0 = sin limite)")
    ap.add_argument("--from-page", type=int, default=None, metavar="N",
                    help="con --backfill/--photos/--superstars: reanuda "
                         "desde esta pagina, ignorando el progreso guardado")
    ap.add_argument("--status", action="store_true",
                    help="muestra el progreso y sale")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        log.info("Interrumpido.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
