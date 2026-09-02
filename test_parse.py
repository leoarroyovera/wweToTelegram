"""Diagnostico: scrapea y ordena sin tocar Telegram.

Util cuando WWE cambia el markup. Debe mostrar 10 cards nuevas por pagina,
0 campos vacios y ningun tema desconocido.
"""
import collections

import wwe_telethon as w

s = w.new_session()
dom = w.get_view_dom_id(s)
print("dom_id:", dom[:24], "...")

items, seen = [], set()
for p in range(3):
    cards = w.parse_cards(w.fetch_page(s, dom, p))
    fresh = [c for c in cards if c["cid"] not in seen]
    seen.update(c["cid"] for c in fresh)
    items += fresh
    print("pagina %d -> %d cards, %d nuevas" % (p, len(cards), len(fresh)))

print("\nTOTAL:", len(items))

vacios = [i for i in items
          if not i["image"] or i["title"] == "(sin titulo)"]
print("items con campos vacios:", len(vacios))

print("reparto por tema:", dict(collections.Counter(i["topic"] for i in items)))
desconocidos = {i["topic"] for i in items} - set(w.TOPICS_ORDER)
print("temas desconocidos:", desconocidos or "ninguno")

print("\n--- ORDEN DE PUBLICACION (primeros 10) ---")
for it in w.sort_for_channel(items)[:10]:
    print("[%-10s] %s" % (it["topic"], it["title"][:60]))
    print("            ", it["image"][:100])
