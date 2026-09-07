# WWE — Imágenes de portada a Telegram

Extrae las imágenes del scroll infinito de la homepage de
[wwe.com](https://www.wwe.com) (un Drupal) y las publica en un supergrupo
privado **WWE** organizado por temas, igual que hace `c90ToTelegram`.

Sin bot: usa **Telethon con tu cuenta de usuario**. El supergrupo y sus temas
se crean solos en la primera ejecución.

```
WWE (supergrupo privado con temas)
├── 📰 Articulos
├── ⭐ Eventos        PLEs: WrestleMania, SummerSlam, Money in the Bank… + /events/
├── 🔴 Raw
├── 🔵 SmackDown
├── 🟡 NXT
├── 📻 Otros Shows    Raw Talk, The Bump, 205 Live, NXT UK…
├── 📺 Otros          WWE Now, Top 10, AAA, superstars sueltos sin show
├── 🖼️ Iconos
├── 🤼 Superstars     fotos de perfil de /superstars
└── 🎬 Shows          hub estatico de /shows
```

## Configuración

**1. Credenciales** — las mismas que ya usa `c90ToTelegram`:

```
cp .env.example .env      # y edita TG_API_ID / TG_API_HASH
```

El script carga el `.env` solo. Si no lo encuentra, o no trae las credenciales,
cae al `.env` de `c90ToTelegram` (que usa las mismas). Una variable ya presente
en el entorno tiene prioridad sobre el archivo.

**2. Primera ejecución** (login interactivo, una sola vez):

```
python wwe_telethon.py --login
```

Pide teléfono, el código que llega por Telegram y la clave 2FA si la tienes.
Queda en `wwe_session.session` y crea el supergrupo con sus seis temas.

## Cinco ejecuciones independientes

| | Portada | Galerias | Superstars | Shows | Events |
|---|---|---|---|---|---|
| Comando | `wwe_telethon.py` | `--photos` | `--superstars` | `--shows` | `--events` |
| Fuente | `/homepage` (scroll infinito) **+ imagenes sueltas de la home** | `/photos`, listado propio de galerias | `/superstars` (scroll infinito) | `/shows`, hub estatico (9 shows fijos) | `/events/`, landing geolocalizada de proximos eventos |
| Publica | una imagen por item | ZIP + todas las fotos en albumes de 10 por galeria | una foto de perfil por luchador | una imagen por show | una imagen por evento |
| Progreso | `page` | `photos_page` | `superstars_page` | ninguno (revisa todo cada vez) | ninguno (revisa todo cada vez) |
| Lock | `wwe_telethon.feed.lock` | `wwe_telethon.photos.lock` | `wwe_telethon.superstars.lock` | `wwe_telethon.shows.lock` | `wwe_telethon.events.lock` |
| Archivo historico | si, ~22.200 items | si, ~1.600 galerias | si, paginado igual que portada | no: 9 shows fijos | no: solo vigilancia de novedades |

Cada una tiene su propio lock, asi que **pueden correr a la vez** sin
pisarse. Todas comparten el indice `seen`, de modo que nada se publica dos
veces sin importar por que modo entro primero.


## Uso

```
python wwe_telethon.py --dry-run    # scrapea y ordena sin tocar Telegram
python wwe_telethon.py              # PORTADA: publica lo nuevo
python wwe_telethon.py --photos     # GALERIAS: recorre /photos
python wwe_telethon.py --superstars # SUPERSTARS: recorre /superstars
python wwe_telethon.py --shows      # SHOWS: revisa el hub /shows
python wwe_telethon.py --events     # EVENTS: revisa /events/ (novedades)
python wwe_telethon.py --loop       # bucle cada 30 min (always-on task)
python wwe_telethon.py --status     # progreso de todos los modos
```

Es **reanudable**: lo ya publicado vive en `wwe_seen.sqlite3`, así que nunca se
repite. En el primer arranque indexa las 40 noticias del feed pero publica solo
las 5 mejores, para no inundar el canal de golpe.

### Volcado del archivo histórico

El feed no se acaba en la portada: llega hasta la página **2222**, unos
**22.200 items** (~37 GB). Para bajarlo entero:

```
python wwe_telethon.py --backfill --limit 50    # una tanda de 50 páginas
python wwe_telethon.py --backfill               # hasta el final del feed
python wwe_telethon.py --status                 # cuánto llevas
```

Guarda la página alcanzada, así que **se puede cortar con Ctrl+C y reanudar**:
al relanzar sigue donde iba, igual que el `run` de C90. Conviene hacerlo por
tandas con `--limit`.

| a 3 s/envío | a 5 s/envío | a 8 s/envío |
|---|---|---|
| ~19 h | ~31 h | ~49 h |

`--from-page N` revisa una página concreta sin perder el progreso alcanzado.

> El backfill convive con el modo normal: comparten el índice `seen`, así que
> lo que ya publicó la vigilancia no se repite, ni al revés.

`--superstars` funciona igual, con su propio progreso (`superstars_page`):

```
python wwe_telethon.py --superstars --limit 5   # una tanda de 5 páginas
python wwe_telethon.py --superstars             # hasta el final del listado
```

`--shows` y `--events` no tienen progreso por página porque no son un
archivo histórico paginable: cada pasada revisa el listado entero (9 shows
fijos, o los eventos próximos de la landing geolocalizada) y el índice
`seen` evita republicar lo ya visto.

```
python wwe_telethon.py --backfill-all   # portada + galerias + superstars
                                         # hasta el final de las tres,
                                         # intercalando shows y events
```

## Opciones (.env)

| Variable | Defecto | Efecto |
|---|---|---|
| `TG_API_ID` / `TG_API_HASH` | — | **Obligatorias.** De my.telegram.org |
| `WWE_CHANNEL` | `WWE` | Nombre del supergrupo |
| `WWE_MAX_PAGES` | `4` | Páginas del scroll (10 items c/u) |
| `WWE_MAX_SEND` | `12` | Máximo de publicaciones por pasada |
| `WWE_FIRST_RUN_SEND` | `5` | Publicaciones en el primer arranque |
| `WWE_SEND_DELAY` | `3` | Segundos entre publicaciones |
| `WWE_INTERVAL_MINUTES` | `30` | Solo en modo `--loop` |
| `WWE_RETENTION_DAYS` | `45` | Días de historial antes de purgar |
| `WWE_LOOSE_IMAGES` | `1` | Recoger también las imágenes sueltas de la home |
| `WWE_ALBUM_MAX` | `10` | Fotos por álbum de galería (tope de Telegram: 10) |

`WWE_MAX_PAGES` solo dice **cuánto mira** cada pasada (10 items por página);
`WWE_MAX_SEND` dice **cuánto publica**. Lo que se recoge pero no cabe en el
límite se marca como visto sin publicar, para que no se acumule.

> Subir mucho estos valores para el modo normal no sirve: es vigilancia de
> novedades, y las novedades están en las primeras páginas. Para bajar el
> archivo antiguo está `--backfill`, que va por tandas y es reanudable.

---

## Despliegue en PythonAnywhere

### ⚠️ No existe la frecuencia "cada 30 minutos"

Las tareas programadas solo admiten **diaria** u **horaria**. Dos salidas:

| | A — dos tareas horarias | B — always-on task |
|---|---|---|
| Cómo | Una a los `:00`, otra a los `:30` | Un proceso con `--loop` |
| Si falla | La siguiente lo recupera | PythonAnywhere lo reinicia solo |
| Consume | Solo al ejecutar | Un worker permanente |

Un **lockfile** impide que dos pasadas solapadas publiquen lo mismo dos veces.

### Pasos

1. Sube `wwe_telethon.py` a `/home/<usuario>/wweToTelegram/`.

2. En una **Bash console**:
   ```bash
   pip3.13 install --user telethon requests
   ```
   > Ajusta la versión a la tuya. La imagen de sistema *innit* llega a Python
   > 3.13; las antiguas (*glastonbury*, *haggis*) solo a 3.9. Mírala en
   > **Account → System image**.

3. Sube tambien tu `.env` junto al script (el mismo de `c90ToTelegram`).
   El script lo carga solo, no hace falta pasar las variables a mano.

4. **Login, obligatorio antes de programar nada** (necesita ser interactivo):
   ```bash
   cd ~/wweToTelegram && python3.13 wwe_telethon.py --login
   ```

5. Ensayo sin publicar, y luego una pasada real:
   ```bash
   python3.13 wwe_telethon.py --dry-run
   python3.13 wwe_telethon.py
   ```

6. **Tasks** → dos tareas **Hourly**, a los minutos `00` y `30`, con:
   ```bash
   cd /home/<usuario>/wweToTelegram && python3.13 wwe_telethon.py
   ```
   O una **always-on task** con `--loop` al final.

7. Seguimiento:
   ```bash
   tail -f ~/wweToTelegram/wwe_telethon.log
   ```
   El log rota solo (2 MB × 4).

> En **cuenta gratuita** esto no funciona: la whitelist de proxy no incluye
> `www.wwe.com`. Requiere cuenta de pago.

---

## Hallazgos del análisis de wwe.com

Verificado contra el sitio en vivo (2026-08-31):

| Aspecto | Resultado |
|---|---|
| CMS | Drupal (`/themes/custom/wwe_theme`, image styles en `/f/styles/`) |
| Scroll infinito | `views_infinite_scroll` sobre la vista `wwe_homepage`, display `block_1` |
| Items por página | 10 |
| Imagen de máxima calidad | **original en `/f/<ruta>`** → 1920×1080 sin recomprimir (~1,7 MB) |

### Por qué no se usa `/homepage?page=N`

El pager degrada sin JS a `?page=N`, pero **Fastly cachea esa URL ignorando el
parámetro**: las páginas 1, 2 y 3 devolvían los mismos 419.536 bytes y los
mismos 25 `cid`. Un cache-buster tampoco lo evita.

La vía que sí funciona es el endpoint AJAX de Views:

```
GET /views/ajax?view_name=wwe_homepage&view_display_id=block_1&page=N
```

Verificado: **0 solapamiento** de `cid` entre páginas 1, 2 y 5.

Detalle importante: el módulo normalmente usa POST, pero **Fastly responde 405
Method Not Allowed** al POST. Por eso se usa GET.

### Imágenes: llegar al original

Los derivados viven en `/f/styles/<preset>/public/<ruta>`. La clave está en
**eliminar `styles/<preset>/public/` por completo**:

```
/f/styles/wwe_16_9_xl_r/public/video/thumb/2026/08/foo.jpg   → derivado
/f/video/thumb/2026/08/foo.jpg                               → ORIGINAL
```

Ojo con el falso amigo: `/f/public/<ruta>` (la forma habitual en muchos Drupal)
devuelve **404** aquí. Hay que quitar también el `public/`.

| ruta | dimensiones | peso |
|---|---|---|
| `wwe_16_9_l_fc` | 516×290 | 42 KB |
| `wwe_16_9_m` | 720×405 | 71 KB |
| `wwe_16_9_l` | 960×540 | 115 KB |
| `wwe_16_9_xl` | 1125×633 | 150 KB |
| `wwe_16_9_xl_r` | 1920×1080 | 375 KB |
| **`/f/<ruta>` (original)** | **1920×1080** | **1699 KB** |

Mismas dimensiones que `xl_r` pero **sin recomprimir**. Verificado sobre 12
imágenes del feed: **12/12 accesibles**. Cuando el original es más pequeño que
el preset, Drupal no lo amplía y ambos coinciden (una imagen de AAA salió
960×540 en las dos rutas): el original nunca es peor.

El script usa el original y cae al preset `wwe_16_9_xl_r` si la descarga falla.

## Modo portada: todas las imágenes

El feed AJAX no lo trae todo. Medido sobre la home:

| | imágenes únicas |
|---|---|
| solo feed-cards | 38 |
| home completa | **52** |

Las 14 restantes son hero, trending y carruseles. De esas, 5 son logos y
adornos del tema, y **9 son contenido editorial real** que antes se perdía.

`scrape_loose_images()` recoge cualquier `src`/`srcset`/`data-src` con
extensión de imagen — jpg, **png, gif, webp, svg** — y descarta el ruido:
logos, iconos de navegación, promos de plataforma y todo lo que cuelga de
`/public/all/` (material de marca reutilizado), `/themes/` o `/modules/`.

Como no hay card, el identificador para deduplicar es la ruta del fichero
(`img:<ruta>`), que es estable entre ejecuciones. Todas se normalizan al
original antes de comparar, así que las 5 variantes de preset de una misma
foto en un `<picture>` cuentan como una sola.

## Galerías de fotos

Una galería (`/gallery/...`) trae entre 30 y 100 fotos que **no están en el
HTML**: las sirve una API interna del tema, hallada en su JS
(`getGalleryRequestUrl`):

```
GET /api/gallery/<nid>/<porPagina>/<offset>/<fidInicial>
```

### De dónde salen las galerías

El modo `--photos` usa **`/photos`**, el listado propio de galerías: otra
vista Drupal (`photos`/`block_1`) que pagina por el mismo endpoint AJAX, con
0 solapamiento y ~40 galerías por página.

Ojo con una diferencia: ese listado usa otra plantilla
(`landing-page--feed-card`) y **no trae el `cid`**, solo el enlace
`/gallery/<slug>`. Como la API solo acepta el id numérico — el slug devuelve
una lista vacía — hay que resolver el `nid` abriendo el HTML de cada
galería (`resolve_gallery_nid`).

Devuelve JSON con `total_images` y las fotos en orden. Dos detalles que
importan:

- Con `offset=0` el campo `photos` es una **lista**; con `offset>0` es un
  **dict indexado por posición** (`"50"`, `"51"`…). Hay que normalizar ambos
  y ordenar numéricamente, no como texto.
- El `fid` **no siempre es creciente**, así que el orden bueno es el que
  devuelve la API, no ordenar por `fid`.

Las imágenes admiten el mismo truco del original: `/f/<ruta>` da 1920×1080
(~720 KB) frente a los 1200×675 (~250 KB) del preset `gallery_img_l`.

### Cómo se conserva el orden

Dos problemas a la vez: 98 fotos seguidas sepultarían el tema del show, y un
tema por galería tampoco vale — hay **~1.600 galerías** en el archivo
histórico (medido: 7,2 % de los items del feed), y esa lista de temas dejaría
el supergrupo inservible.

La solución: cada galería ocupa **varias posiciones seguidas** en el tema de
su show, en su lugar cronológico:

1. el **ZIP con todas** las fotos en calidad original, primero — así ya
   existe su mensaje (y el enlace a él) para el paso 2;
2. **todas las fotos**, repartidas en tantos álbumes de 10 como haga falta
   (el máximo que Telegram agrupa en un mensaje). Cada álbum lleva su pie
   con título, número de parte y un enlace de vuelta al ZIP, así que
   cualquiera de ellos —no solo el primero— permite llegar a la galería
   completa en calidad original.

Antes solo se subía un álbum de muestra de 10 fotos; ahora se suben todas,
sin perder la referencia al ZIP.

Dentro del ZIP los archivos van numerados (`001_`, `002_`…) para conservar el
orden de la galería, que el nombre original no siempre respeta. Se comprime
con `ZIP_STORED`: los JPEG ya vienen comprimidos y recomprimir solo gastaría
CPU sin ganar tamaño.

Ejemplos reales: 18 fotos → ZIP de 7,5 MB; 98 fotos → ZIP de 63 MB. Muy por
debajo del límite de 2 GB de Telethon, así que no hacen falta volúmenes
partidos como en C90.

## Superstars, Shows y Events

Verificado contra el sitio en vivo (2026-09-06).

### `/superstars`: misma vista Drupal que la portada

Otra vista de `views_infinite_scroll`, distinto nombre:
`current_superstar`/`block_1`. Pagina exactamente igual que `/homepage`
(mismo endpoint `/views/ajax`, mismo truco de leer el `view_dom_id` en vivo):
verificado **0 % de solapamiento** entre páginas, 30 luchadores por página.

Cada fila trae el nombre y una foto de perfil cuadrada en un `<picture>`. El
listado no expone ningún id numérico (a diferencia de las feed-cards, que
llevan `cid`), así que el identificador estable es el slug de
`/superstars/<slug>`.

La foto de perfil admite el mismo truco de siempre
(`/f/styles/<preset>/public/` → `/f/`), pero aquí el original dio **503
intermitente** en las pruebas (nada raro: es lo que ya maneja
`download_to()` con su reintento al preset). El preset de respaldo más
grande disponible es `wwe_1_1_540__composite` (540×540); uno más grande
(`..._xl__composite`) no existe para este campo y da 404.

### `/shows`: hub estático, sin paginación

A diferencia de `/homepage` y `/photos`, `/shows` **no** es una vista
`views_infinite_scroll`: es una página fija con 9 shows (Raw, SmackDown,
NXT, Sunday Night's Main Event, Money in the Bank, Survivor Series
WarGames, AAA, WWE Evolve, NXT PLE), cada uno con un hero y un logo que
comparten el mismo enlace `/shows/<slug>`. No hace falta AJAX ni progreso
por página: `run_shows()` revisa el HTML entero cada vez y el índice `seen`
evita republicar los mismos 9.

Varios de estos shows (los que aún no tienen hero propio) comparten una
imagen placeholder genérica (`Show_Sub_Header`) — es así en el sitio, no un
fallo del scraper.

### `/events/`: landing geolocalizada, sin archivo histórico

`/events/` responde **302** y redirige (Fastly resuelve la geo por IP) a
`/events/results/all-events/all-dates/<lat>/<lng>/<ciudad>/<país>`. Ahí no
hay scroll infinito ni pager: es una landing de "próximos eventos" con un
puñado de tarjetas (8 en la prueba), cada una con fecha, lugar y enlace a
`/event/<slug>`. No es un archivo histórico paginable como `/photos`, así
que `run_events()` no tiene noción de "completo": solo vigila novedades,
igual que el modo normal de portada.

El cid se arma con el slug de `/event/<slug>`; cuando un evento pasa y deja
de aparecer en la landing, ya quedó marcado en `seen` desde la primera vez
que se vio, así que no se pierde ni se reprocesa.

Las imágenes de evento también resuelven con el mismo truco `/f/<ruta>`:
verificado un caso real, 1,1 MB el original contra 75 KB del preset
`large`.

## Orden de publicación

Dentro de cada pasada, antes de repartir por temas:

1. **Tipo**: artículos → vídeos → playlists
2. **Show**: eventos grandes → institucional → Raw/SmackDown → NXT → resto
3. **Posición original** en el feed (que ya viene ordenado por relevancia)

Ajustable en `TYPE_PRIORITY`, `SHOW_PRIORITY` y `SHOW_TO_TOPIC`.

## Notas sobre el volumen

- Cada imagen pesa ~1,7 MB y **se borra del disco tras subirla**: nunca hay más
  de una a la vez, igual que el `CLEANUP` de C90.
- Telethon respeta los `FloodWait` esperando lo que pida el servidor.
- Los temas se crean **todos por adelantado y en orden fijo**, porque Telegram
  no permite reordenarlos después.

## Diagnóstico

`test_parse.py` ejecuta el scraping sin tocar Telegram:

```bash
python3.13 test_parse.py
```

Debe mostrar 10 cards nuevas por página y `items con campos vacios: 0`.

## Notas de fragilidad

Al depender del markup del tema, se rompe si WWE cambia:

- las clases `wwe-feed-cards--card` / el slug de show como última clase
- el atributo `data-tracking-label` con `cid=` y `content_type=`
- el nombre de la vista `wwe_homepage` / display `block_1`
- la ruta del original `/f/<ruta>` (hay fallback al preset `wwe_16_9_xl_r`)
- el nombre de la vista `current_superstar` / display `block_1` (`--superstars`)
- la estructura de fila `.views-row` con el enlace y el `<picture>` debajo
  (`--superstars`)
- las clases `b-link` / `field--name-field-shows-wwe` del hub `/shows`
  (`--shows`)
- las clases `events-upcoming-card` y `event-breaker--meta-location` de la
  landing de eventos (`--events`)

El `view_dom_id` se lee en vivo de cada página en cada ejecución, así que ese
sí se adapta solo.

## Barrido de cobertura (2026-09-06)

Auditoría de todas las rutas scrapeadas para detectar imágenes reales del
sitio que el scraper detecta pero nunca termina de publicar.

**Sin pérdidas de imagen por parseo**: verificado 50/50 cards del feed en 5
páginas seguidas (sin fallos por falta de `cid` o de imagen), 25/25 fotos de
una galería de ejemplo, y 30/30 luchadores de `/superstars`. Todas las rutas
intentan primero el original sin recomprimir y caen al mejor preset
disponible si el original falla (`download_to()`), nunca al revés.

**Hallazgo real — huérfanos permanentes**: `already_seen()` marcaba como
"visto" cualquier fila de `seen`, publicada o no. Un item que el scraper
detectaba pero que quedaba fuera del límite de esa pasada
(`WWE_MAX_SEND`/`WWE_FIRST_RUN_SEND`, o un backfill cortado a mitad de
página) se registraba con `sent=0` y quedaba bloqueado **para siempre**: el
feed lo seguía trayendo en cada pasada futura, pero `already_seen()` ya lo
descartaba antes de intentarlo de nuevo. Medido sobre el índice en
producción: **72 items reales** (60 imágenes sueltas de la home + 12 videos
del archivo histórico) atrapados así.

Corregido: `already_seen()` ahora solo cuenta `sent=1` como visto, así que
cualquier pasada que vuelva a encontrar un `sent=0` en el feed lo reintenta
hasta que se publique. `record()` preserva el `first_seen` original en un
reintento (para que `purge_old()` no pierda la cuenta de antigüedad), y
`purge_old()` ya no necesita distinguir `sent` porque una fila `sent=0`
purgada simplemente vuelve a verse como nueva. No hace falta limpiar a mano
los 72 huérfanos existentes: la próxima pasada normal (o el backfill, cuando
vuelva a pasar por esas páginas) ya los recoge solo.

**Reorganización de temas**: medido el campo `show` sobre las ~10.400 filas
indexadas, el tema `Otros` concentraba 4.224 items (el más grande, muy por
delante de Raw con 1.751) porque agrupaba sin distinción: shows menores
mapeados (`wwe`, `wwenow`, `wwetop10`, `aaa`), sub-shows recurrentes sin
mapear (Raw Talk, 205 Live, The Bump, NXT UK...), Premium Live Events sin
mapear (Money in the Bank, Elimination Chamber, Crown Jewel, Hell in a
Cell...) y slugs de superstars individuales (`romanreigns`, `undertaker`,
`johncena`...). Se agregaron ~30 shows/PLEs al mapeo `SHOW_TO_TOPIC`: los
PLEs van a `Eventos` (pasó de 371 a 771 items) y los sub-shows recurrentes a
un tema nuevo, `Otros Shows` (699 items). `Otros` bajó a 3.187, y lo que le
queda es justamente lo que no encaja en ningún show reconocible: contenido
de superstars individuales y material sin `show`.

> Los ~10.300 items ya publicados con el mapeo anterior **no se
> reclasifican retroactivamente** — quedan en el tema donde ya se enviaron.
> El nuevo mapeo solo aplica a partir de la próxima publicación. Si se
> reinicia el volcado completo desde cero (ver más abajo), sí sale todo
> clasificado con las categorías nuevas desde el principio.

## Problemas frecuentes

| Síntoma | Causa |
|---|---|
| `Falta TG_API_ID / TG_API_HASH` | No hay `.env` junto al script ni en `c90ToTelegram`, o le faltan esas claves |
| Pide teléfono en una tarea programada | Falta el `--login` previo, o falta `wwe_session.session` |
| `'WWE' es un canal, no un supergrupo` | Existe un canal con ese nombre; renómbralo o bórralo |
| `Ya hay una ejecucion en curso` | Normal si se solapan; sale sin hacer nada |
| `Pagina 0 vacia` | WWE cambió el markup (ver *Notas de fragilidad*) |

## Aviso

El contenido de WWE.com está protegido por copyright. Redistribuir sus imágenes
requiere autorización del titular.
