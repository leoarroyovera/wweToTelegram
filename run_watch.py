#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Punto de entrada dedicado para el always-on task de PythonAnywhere, una vez
que el backfill historico (run_backfill_all.py) ya termino.

Corre wwe_telethon.py --watch: en cada vuelta revisa portada (home + scroll
infinito), galerias nuevas de /photos, superstars nuevos de /superstars,
shows, events y events-local (logos en alta calidad). No re-descarga el
archivo historico, solo vigila novedades indefinidamente (ver run_watch en
wwe_telethon.py). Es exactamente wwe_telethon.py --watch, pero como script
propio para que el comando del always-on task sea simple y no dependa de
argparse:

    /home/tu-usuario/.virtualenvs/wwe/bin/python \
        /home/tu-usuario/wweToTelegram/run_watch.py

Requiere haber hecho el login una vez (wwe_telethon.py --login) para que
exista wwe_session.session.

Nunca termina por un error puntual: cada fuente que falla en una vuelta se
loguea y se reintenta en la siguiente (ver run_watch en wwe_telethon.py).
Solo termina si se lo corta manualmente (Ctrl+C / que PythonAnywhere
reinicie el always-on task, en cuyo caso retoma sin perder nada: el indice
de publicado vive en wwe_seen.sqlite3).
"""
import asyncio
import sys

from wwe_telethon import (acquire_lock, release_lock, run_watch,
                          setup_logging, AlreadyRunning)


async def main():
    try:
        acquire_lock("feed")
        acquire_lock("photos")
        acquire_lock("superstars")
    except AlreadyRunning as e:
        print(f"Ya hay una ejecucion en curso: {e}. Salgo.")
        return 0
    try:
        await run_watch()
        return 0
    finally:
        release_lock("feed")
        release_lock("photos")
        release_lock("superstars")


if __name__ == "__main__":
    setup_logging(verbose=True)
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
