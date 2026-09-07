#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Punto de entrada dedicado para el always-on task de PythonAnywhere.

Corre el volcado historico completo de WWE.com: portada (/homepage),
galerias (/photos) y superstars (/superstars), alternando tandas hasta que
las tres lleguen al final; de paso intercala shows (/shows) y eventos
(/events/), que no tienen archivo historico pero si novedades propias (ver
run_backfill_all en wwe_telethon.py). Es exactamente
wwe_telethon.py --backfill-all, pero como script propio para que el comando
del always-on task sea simple y no dependa de argparse:

    /home/tu-usuario/.virtualenvs/wwe/bin/python \
        /home/tu-usuario/wweToTelegram/run_backfill_all.py

Requiere haber hecho el login una vez (wwe_telethon.py --login) para que
exista wwe_session.session.

Nunca termina por un error puntual: cada tanda que falla se loguea y se
reintenta en la siguiente vuelta (ver run_backfill_all en wwe_telethon.py).
Solo termina cuando portada, galerias y superstars llegan al final, o si se
lo corta manualmente (Ctrl+C / que PythonAnywhere reinicie el always-on
task, en cuyo caso retoma donde iba: el progreso vive en wwe_seen.sqlite3).
"""
import asyncio
import sys

from wwe_telethon import (acquire_lock, release_lock, run_backfill_all,
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
        await run_backfill_all()
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
