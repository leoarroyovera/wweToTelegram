#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Diagnostico de solo lectura para la portada de WWE.com.

No descarga imagenes, no toca Telegram ni la base de datos (wwe_seen.sqlite3).
Muestra en limpio, paso a paso, exactamente lo que hace wwe_telethon.py en
modo portada:

  1) QUE OBTIENE   - imagenes sueltas de la home + cards de cada pagina del
                      scroll infinito (fetch_page + parse_cards +
                      scrape_loose_images).
  2) QUE SELECCIONA - deduplicado por cid, orden editorial (sort_for_channel)
                      y el recorte a MAX_SEND_PER_RUN / FIRST_RUN_SEND que
                      decide que se publica en esta pasada.
  3) RUTAS FINALES  - la URL "original" y la URL "preset" (fallback) que
                      download_image() intentaria bajar para cada item
                      seleccionado, en ese orden.

Uso:
    python3 wwe_inspect.py                  # resumen normal
    python3 wwe_inspect.py --pages 2        # menos paginas (mas rapido)
    python3 wwe_inspect.py --json           # salida machine-readable
    python3 wwe_inspect.py --seen           # compara contra wwe_seen.sqlite3
                                             # (si existe) para marcar lo ya
                                             # publicado antes
"""

import argparse
import json
import sqlite3
import time
from pathlib import Path

from wwe_telethon import (  # reutiliza EXACTAMENTE la logica del script real
    BASE_URL, DB_PATH, FIRST_RUN_SEND, HOMEPAGE_URL, LOOSE_IMAGES,
    MAX_PAGES, MAX_SEND_PER_RUN, already_seen, db_connect, fetch_page,
    get_view_dom_id, is_first_run, new_session, parse_cards,
    scrape_loose_images, sort_for_channel,
)

ROOT = Path(__file__).resolve().parent


def linea(car="-", n=78):
    print(car * n)


def recolectar(session, dom_id, max_pages, loose_images, verbose=True):
    """
    Replica el bucle de recoleccion de run_feed(), pero solo junta y cuenta:
    no descarga nada ni escribe en la base de datos.

    Devuelve (items, fuentes) donde 'fuentes' registra de donde vino cada cid
    (home / pagina N) para poder explicar el origen en el resumen.
    """
    items, seen, fuentes = [], set(), {}

    if loose_images:
        if verbose:
            print("[1] Descargando HTML de la home: %s" % HOMEPAGE_URL)
        try:
            home = session.get(HOMEPAGE_URL, timeout=30)
            home.raise_for_status()
            sueltas = scrape_loose_images(home.text)
            nuevas = [i for i in sueltas if i["cid"] not in seen]
            seen.update(i["cid"] for i in nuevas)
            for i in nuevas:
                fuentes[i["cid"]] = "home (imagen suelta)"
            items.extend(nuevas)
            if verbose:
                print("    -> %d imagenes sueltas (hero/trending/carruseles/iconos)"
                      % len(nuevas))
        except Exception as e:
            print("    !! No se pudo leer la home: %s" % e)

    for page in range(max_pages):
        if verbose:
            print("[%d] Pidiendo pagina %d del scroll infinito (AJAX)"
                  % (page + 2, page))
        try:
            markup = fetch_page(session, dom_id, page)
        except RuntimeError as e:
            print("    !! %s; salto a la siguiente." % e)
            continue

        cards = parse_cards(markup)
        if not cards:
            if verbose:
                print("    -> pagina vacia; fin del scroll.")
            break

        fresh = [c for c in cards if c["cid"] not in seen]
        seen.update(c["cid"] for c in fresh)
        for c in fresh:
            fuentes[c["cid"]] = "pagina %d (card)" % page
        items.extend(fresh)

        extra = []
        if loose_images:
            extra = [i for i in scrape_loose_images(markup, "feed")
                    if i["cid"] not in seen]
            seen.update(i["cid"] for i in extra)
            for i in extra:
                fuentes[i["cid"]] = "pagina %d (imagen suelta)" % page
            items.extend(extra)

        if verbose:
            print("    -> %d cards (%d nuevas), %d imagenes sueltas nuevas"
                  % (len(cards), len(fresh), len(extra)))
        time.sleep(1.5)

    return items, fuentes


def marcar_vistos(items):
    """
    Si existe wwe_seen.sqlite3, indica cuales de los items ya se publicaron
    antes (sent=1). Solo lectura: no crea ni modifica la base de datos.
    """
    if not Path(DB_PATH).exists():
        return {}
    conn = db_connect()
    try:
        return {it["cid"]: already_seen(conn, it["cid"]) for it in items}
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(
        description="Muestra en limpio que trae la portada de WWE.com, que "
                    "se seleccionaria para publicar y las rutas finales de "
                    "imagen. Solo lectura: no descarga nada ni toca Telegram.")
    ap.add_argument("--pages", type=int, default=MAX_PAGES,
                    help="Paginas del scroll a recorrer (default: %d, igual "
                         "que WWE_MAX_PAGES)" % MAX_PAGES)
    ap.add_argument("--no-loose", action="store_true",
                    help="No recoger imagenes sueltas de la home/paginas, "
                         "solo las cards del feed.")
    ap.add_argument("--seen", action="store_true",
                    help="Marcar que items ya estaban publicados segun "
                         "wwe_seen.sqlite3 (si existe).")
    ap.add_argument("--json", action="store_true",
                    help="Salida en JSON en vez de texto legible.")
    ap.add_argument("--top", type=int, default=None,
                    help="Cuantos items listar en el detalle (default: todos "
                         "los seleccionados + primeros 10 descartados).")
    args = ap.parse_args()

    session = new_session()

    if not args.json:
        print()
        linea("=")
        print(" WWE.com -> diagnostico de portada (solo lectura)")
        linea("=")

    if not args.json:
        print("\nResolviendo view_dom_id de la home...")
    dom_id = get_view_dom_id(session)
    if not args.json:
        print("  view_dom_id = %r" % dom_id)
        linea()
        print("PASO 1: QUE OBTIENE (home + %d pagina/s del scroll)\n"
              % args.pages)

    items, fuentes = recolectar(session, dom_id, args.pages,
                                loose_images=not args.no_loose,
                                verbose=not args.json)

    primer_run = is_first_run(db_connect()) if Path(DB_PATH).exists() else True
    vistos = marcar_vistos(items) if args.seen else {}

    if args.seen:
        nuevos = [it for it in items if not vistos.get(it["cid"], False)]
    else:
        nuevos = items

    ordenados = sort_for_channel(list(nuevos))
    limite = FIRST_RUN_SEND if primer_run else MAX_SEND_PER_RUN
    seleccionados = ordenados[:limite]
    descartados = ordenados[limite:]

    if args.json:
        salida = {
            "view_dom_id": dom_id,
            "total_obtenidos": len(items),
            "total_nuevos": len(nuevos) if args.seen else None,
            "primer_run": primer_run,
            "limite_por_pasada": limite,
            "seleccionados": [
                {
                    "cid": it["cid"],
                    "titulo": it["title"],
                    "tipo": it["content_type"],
                    "show": it["show"],
                    "tema": it["topic"],
                    "url_pagina": it["url"],
                    "ruta_imagen_original": it["image"],
                    "ruta_imagen_fallback": it.get("image_fallback", ""),
                    "origen": fuentes.get(it["cid"], "?"),
                }
                for it in seleccionados
            ],
            "descartados_esta_pasada": [
                {"cid": it["cid"], "titulo": it["title"], "tema": it["topic"]}
                for it in descartados
            ],
        }
        print(json.dumps(salida, ensure_ascii=False, indent=2))
        return

    print("\nTotal obtenido en esta pasada: %d items" % len(items))
    if args.seen:
        print("De esos, no publicados todavia (segun wwe_seen.sqlite3): %d"
              % len(nuevos))
    else:
        print("(usa --seen para comparar contra wwe_seen.sqlite3 y ver que "
              "ya se publico antes)")

    linea()
    print("PASO 2: QUE SELECCIONA para publicar en esta pasada\n")
    print("  Modo: %s -> limite %d items"
          % ("PRIMER ARRANQUE" if primer_run else "pasada normal", limite))
    print("  Orden: prioridad de tipo (articulo > video > galeria > imagen "
          "> icono)")
    print("         luego prioridad de show (PLEs > Raw/SmackDown > NXT > "
          "resto)")
    print("         luego el orden original del feed (desempate)\n")

    print("  >> SE PUBLICARIAN (%d de %d):\n" % (len(seleccionados), len(ordenados)))
    top = args.top or len(seleccionados)
    for i, it in enumerate(seleccionados[:top], 1):
        marca = ""
        if args.seen and vistos.get(it["cid"]):
            marca = "  [ya visto antes]"
        print("   %2d. [%s] (%s) %s%s"
              % (i, it["topic"], it["content_type"], it["title"][:55], marca))
        print("       origen : %s" % fuentes.get(it["cid"], "?"))
        print("       cid    : %s" % it["cid"])
        if it["url"]:
            print("       pagina : %s" % it["url"])

    if descartados:
        print("\n  >> NO entran en esta pasada (quedan para la siguiente): %d"
              % len(descartados))
        for it in descartados[:args.top or 10]:
            print("       - [%s] %s" % (it["topic"], it["title"][:60]))
        if len(descartados) > (args.top or 10):
            print("       ... y %d mas" % (len(descartados) - (args.top or 10)))

    linea()
    print("PASO 3: RUTAS FINALES DE IMAGEN (orden de intento: original -> "
          "preset/fallback)\n")
    for i, it in enumerate(seleccionados[:top], 1):
        print("   %2d. %s" % (i, it["title"][:60]))
        print("       original : %s" % (it["image"] or "(vacio)"))
        print("       fallback : %s" % (it.get("image_fallback") or "(vacio)"))
        print()

    linea("=")
    print(" Fin. Nada se descargo, nada se publico, nada se escribio en la "
          "base de datos.")
    linea("=")


if __name__ == "__main__":
    main()
