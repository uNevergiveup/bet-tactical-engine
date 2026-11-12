#!/usr/bin/env python3
"""Bwin: scroll progresivo, expansión y extracción masiva con Playwright.

Patch 5.7.11: preservar la identificación por Event ID y título aunque el
ms-header se transforme en la navegación de mercados; no confundir el botón
cíclico de recomendaciones con
"Mostrar más" de mercados ordinarios; recuperación controlada de acordeones.
El informe distingue cobertura de mercados estándar y recomendaciones.

Ejecutar: .venv/bin/python -u scraper_bwin.py
Opciones: --pausar (inspección visual), --intentos N, --sin-pestanas.
No selecciona cuotas ni interactúa con el talón de apuestas.
"""
import argparse
import json
import os
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.sync_api import sync_playwright
from config.constants import (
    BWIN_EVENT_URL, HEADLESS, SLOW_MO, WAIT_BEFORE_EXTRACTION_MS,
    TEAM_ALIASES,
    PAUSAR_AL_FINAL, OUTPUT_DIR_SCRAPER, MAX_INTENTOS_SCRAPER,
    ESPERA_ENTRE_INTENTOS_SEGUNDOS,
)

def obtener_info_url(url):
    partes = urlsplit(url.strip())
    if partes.scheme != "https" or partes.hostname not in {"bwin.es", "www.bwin.es"}:
        raise ValueError("Se requiere una URL HTTPS de bwin.es")
    segmento = unquote(partes.path.rstrip("/").rsplit("/", 1)[-1])
    match = re.fullmatch(r"(?P<slug>.+)-(?P<tipo>\d+):(?P<event_id>\d+)", segmento)
    if not match or "/sports/eventos/" not in partes.path:
        raise ValueError("URL de evento Bwin inválida")
    slug = match["slug"]
    equipos = slug.split("-vs-") if "-vs-" in slug else slug.split("-")
    equipos = [re.sub(r"[-_+]+", " ", x).strip().title() for x in equipos]
    eq1, eq2 = equipos if len(equipos) == 2 else ("", "")
    partido = f"{eq1} vs {eq2}" if eq1 and eq2 else slug.replace("-", " ").title()
    return dict(event_id=match["event_id"], slug=slug, equipo_1=eq1,
                equipo_2=eq2, partido=partido, evento=partido, url=url.strip())


def sanitizar_nombre_archivo(texto):
    texto = unicodedata.normalize("NFC", texto).lower()
    return re.sub(r"_+", "_", re.sub(r"[^\w]+", "_", texto)).strip("_") or "evento"


SCRAPER_PATCH_VERSION = '5.7.12-event-scoped-artifacts'

JS_COMMON = r"""
const root = document.querySelector('ms-event-details-main');
if (!root) throw new Error('No existe el contenedor del evento');
const clean = t => (t || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
const text = el => clean(el?.textContent);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const accordions = () => [...root.querySelectorAll('ds-accordion')];
const title = a => text(a.querySelector('[slot="title"]'));
const isMore = b => /^(mostrar|ver)\s+m[aá]s\b|^show\s+more\b/i.test(text(b));
const shown = el => !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
const inViewport = el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.top >= 0 && r.bottom <= (window.innerHeight || document.documentElement.clientHeight);
};
const normalizeTitle = s => clean(s).normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
const isRecommendationWidget = b => {
    const a = b.closest('ds-accordion');
    return !!a && normalizeTitle(title(a)) === 'crea tus apuestas recomendadas';
};
const rawMoreButtons = scope => [...scope.querySelectorAll('.show-more-less-button')]
    .filter(b => isMore(b) && shown(b));
const excludedRecommendationButtons = scope => rawMoreButtons(scope).filter(isRecommendationWidget);
const moreButtons = scope => rawMoreButtons(scope).filter(b => !isRecommendationWidget(b));
const closed = () => [...root.querySelectorAll('button[dsaccordiontoggle][aria-expanded="false"]')];

async function waitDomSettle(scope = root, minWait = 50, quietWindow = 50, maxWait = 600) {
    const start = performance.now();
    let lastChange = start;
    const sig = () => `${scope.querySelectorAll('ms-event-pick').length}|${moreButtons(scope).length}`;
    let prev = sig();
    while (performance.now() - start < maxWait) {
        await delay(20);
        const now = performance.now();
        const cur = sig();
        if (cur !== prev) {
            prev = cur;
            lastChange = now;
        } else if (now - start >= minWait && now - lastChange >= quietWindow) {
            break;
        }
    }
}

function scroller() {
    let el = root;
    while (el) {
        if (/(auto|scroll)/.test(getComputedStyle(el).overflowY) && el.clientHeight > 0 && el.scrollHeight > el.clientHeight + 2) return el;
        el = el.parentElement;
    }
    return document.scrollingElement;
}
function stats() {
    const s = scroller();
    return {total: accordions().length, cerrados: closed().length,
        mostrar_mas: moreButtons(root).length,
        recomendaciones_sin_expandir: excludedRecommendationButtons(root).length,
        picks: root.querySelectorAll('ms-event-pick').length,
        top: Math.round(s.scrollTop), height: s.scrollHeight, viewport: s.clientHeight,
        fondo: s.scrollTop + s.clientHeight >= s.scrollHeight - 5};
}
async function expandMore(scope) {
    let clicks = 0, stagnant = 0, previous = '';
    const describe = b => ({boton:text(b).slice(0,75),
        mercado:title(b.closest('ds-accordion') || scope),
        disabled:!!b.disabled});
    for (let round = 0; round < 18; round++) {
        const buttons = moreButtons(scope);
        if (!buttons.length) return {clicks, remaining:0, stalled:[]};
        const before = `${buttons.length}|${scope.querySelectorAll('ms-event-pick').length}`;
        const carefulMode = stagnant > 0;
        for (const b of buttons) {
            if (!b.isConnected || !isMore(b)) continue;
            if (carefulMode || !inViewport(b)) {
                try { b.scrollIntoView({block:'center', inline:'nearest'}); } catch (_) {}
                await delay(carefulMode ? 130 : 45);
            }
            if (!b.isConnected || !isMore(b)) continue;
            b.click(); clicks++;
            await delay(carefulMode ? 180 : 25);
        }
        await waitDomSettle(scope, carefulMode ? 180 : 60, 60, 600);
        const rest = moreButtons(scope);
        const after = `${rest.length}|${scope.querySelectorAll('ms-event-pick').length}`;
        if (!rest.length) return {clicks, remaining:0, stalled:[]};
        stagnant = (after === before || after === previous) ? stagnant+1 : 0;
        previous = after;
        if (stagnant >= 3) break;
    }
    const rest = moreButtons(scope);
    return {clicks, remaining:rest.length, stalled:rest.slice(0,8).map(describe)};
}
"""

JS_EXTRACT = r"""
function extract(scope) {
    const rows = [], errors = [], empty = [];
    let unavailable = 0;
    const processed = new Set();
    const odds = p => {
        const value = text(p.querySelector('.value.option-value')).replace(',', '.');
        return /^\d+(?:\.\d+)?$/.test(value) && Number(value) > 1 ? value : '';
    };
    const blocks = scope.matches?.('ds-accordion') ? [scope] : [...scope.querySelectorAll('ds-accordion')];
    for (const a of blocks) {
        const marketTitle = title(a);
        const active = [...a.querySelectorAll('button[role="tab"][aria-selected="true"]')].map(text);
        const market = [marketTitle, ...active].filter(Boolean).join(' | ');
        const start = rows.length;
        function add(p, selection, suffix = '') {
            processed.add(p);
            const value = odds(p);
            if (!value) { unavailable++; return; }
            if (!selection || !marketTitle) { errors.push('Selección sin contexto: ' + market); return; }
            rows.push({mercado: market + (suffix ? ' (' + suffix + ')' : ''),
                seleccion: selection, cuota: value,
                mkt_id: p.getAttribute('data-test-option-id') || ''});
        }
        for (const g of a.querySelectorAll('ms-player-stats-option-group')) {
            if (g.closest('ds-accordion') !== a) continue;
            const players = [...g.querySelectorAll('.player-stats-option-group__player-name')].map(text);
            const headers = [...g.querySelectorAll('.player-stats-option-group__header')].map(text);
            const matrixRows = [...g.querySelectorAll('.player-stats-option-group__options')];
            if (!headers.length || players.length !== matrixRows.length) {
                errors.push('Dimensiones de jugadores incoherentes: ' + market);
                g.querySelectorAll('ms-event-pick').forEach(p => processed.add(p));
                continue;
            }
            matrixRows.forEach((row, r) => {
                const cells = [...row.children].filter(c => c.matches('.option'));
                row.querySelectorAll('ms-event-pick').forEach(p => processed.add(p));
                if (cells.length !== headers.length) {
                    errors.push('Fila no rectangular: ' + market + ' / ' + players[r]);
                    return;
                }
                cells.forEach((cell, c) => {
                    const p = cell.querySelector('ms-event-pick');
                    if (!p) return;
                    const embedded = text(p.querySelector('.name'));
                    if (embedded && embedded !== players[r]) {
                        errors.push('Nombre desalineado: ' + embedded + ' / ' + players[r]);
                        return;
                    }
                    if (!headers[c]) { errors.push('Hito vacío: ' + market); return; }
                    add(p, players[r], headers[c]);
                });
            });
        }
        for (const g of a.querySelectorAll('ms-goal-scorer-option-group .option-group-container')) {
            const headers = [...g.querySelectorAll('.option-header-name')].map(text);
            let player = '', col = 0;
            for (const cell of g.children) {
                if (cell.matches('.attribute-key')) {
                    player = text(cell.querySelector('.goal-scorer-player-name')) || text(cell);
                    col = 0;
                } else if (cell.matches('.option')) {
                    const p = cell.querySelector('ms-event-pick');
                    if (p && headers[col]) add(p, player, headers[col]);
                    else if (p) { processed.add(p); errors.push('Columna de goleador desconocida: ' + market); }
                    col++;
                }
            }
        }
        for (const p of a.querySelectorAll('ms-event-pick.option-pick')) {
            if (processed.has(p) || p.closest('ds-accordion') !== a) continue;
            add(p, text(p.querySelector('.name')));
        }
        for (const card of a.querySelectorAll('ms-precreated-group-option')) {
            const selection = [...card.querySelectorAll('.leg')].map(text).join(' + ');
            const value = text(card.querySelector('ms-betbuilder-group-pick ms-font-resizer')).replace(',', '.');
            if (selection && /^\d+(?:\.\d+)?$/.test(value) && Number(value) > 1) {
                rows.push({mercado: market, seleccion: selection, cuota: value, mkt_id: ''});
            }
        }
        if (rows.length === start) empty.push(market);
    }
    return {rows, errors, empty, unavailable};
}
"""


def evaluate(page, code, arg=None, extraction=False):
    return page.evaluate("async (arg) => {" + JS_COMMON + (JS_EXTRACT if extraction else "") + code + "}", arg)


def scroll_completo(page, max_pasos=900):
    evaluate(page, "scroller().scrollTo({top:0, behavior:'instant'}); return stats();")
    previo, estables = None, 0
    for paso in range(max_pasos):
        estado = evaluate(page, """
            const s = scroller();
            s.scrollBy({top: Math.max(450, s.clientHeight * 0.88), behavior:'instant'});
            await delay(80);
            return stats();
        """)
        firma = (estado["total"], estado["height"], estado["picks"])
        estables = estables + 1 if estado["fondo"] and firma == previo else 0
        previo = firma
        if paso % 40 == 0 or estables == 5:
            print(f"  Scroll {paso + 1}: {estado['total']} acordeones; "
                  f"posición {estado['top']}/{estado['height']}; fondo={estado['fondo']}", flush=True)
        if estables >= 5:
            return estado
        if estado["fondo"]:
            page.wait_for_timeout(220)
    raise RuntimeError(f"Scroll incompleto tras {max_pasos} pasos: {estado}")


def _expandir_acordeon_con_playwright(page, info=None):
    """Fallback de clic real, solo en el toggle de un acordeón (nunca un pick).

    Es importante comprobar Event ID antes del clic si se conoce; una redirección
    de Bwin no puede acabar asociando cuotas a otro partido.
    """
    if info is not None:
        if obtener_info_url(page.url)['event_id'] != info['event_id']:
            raise RuntimeError(f"EVENT_ID_CAMBIADO durante expansión: {page.url}")
    button = page.locator('ms-event-details-main button[dsaccordiontoggle][aria-expanded="false"]').first
    if button.count() == 0:
        return False
    button.click(timeout=4500)
    page.wait_for_timeout(300)
    return True


def expandir_mercados_masivo(page, info=None):
    previous_closed = None
    stagnant = 0
    for lote in range(30):
        estado = evaluate(page, """
            let clicks = 0;
            const batch = closed().slice(0, 25);
            for (const b of batch) {
                if (!b.isConnected || b.getAttribute('aria-expanded') !== 'false') continue;
                b.click(); clicks++;
                if (clicks % 5 === 0) await delay(15);
            }
            if (clicks > 0) await waitDomSettle(root, 80, 50, 500);
            return {...stats(), clicks};
        """)
        print(f"  Expansión: {estado['total']} acordeones, {estado['cerrados']} cerrados", flush=True)
        if not estado['cerrados']:
            break
        stagnant = (stagnant + 1 if previous_closed == estado['cerrados'] else 0)
        previous_closed = estado['cerrados']
        if stagnant >= 2:
            print('  WARN: acordeones sin progreso; intentando clic Playwright sobre el toggle.', flush=True)
            before = estado['cerrados']
            if not _expandir_acordeon_con_playwright(page, info):
                raise RuntimeError('ACORDEONES_ATASCADOS: no hay toggle disponible')
            after = evaluate(page, 'return stats();')
            if after['cerrados'] >= before:
                raise RuntimeError(f'ACORDEONES_ATASCADOS: {before} pendientes tras clic real')
            previous_closed = after['cerrados']
            stagnant = 0
            if not after['cerrados']:
                break
    else:
        raise RuntimeError("Quedan acordeones cerrados tras 30 lotes")
    expansion = evaluate(page, 'return await expandMore(root);')
    remaining_widget = evaluate(page, "return excludedRecommendationButtons(root).map(b => ({mercado:title(b.closest('ds-accordion')),boton:text(b)}));")
    print(f"  Mostrar más (mercados ordinarios): {expansion['clicks']} clics; pendientes={expansion['remaining']}", flush=True)
    if remaining_widget:
        print(f"  NOTA: {len(remaining_widget)} botón(es) de recomendaciones excluidos del criterio de completitud.", flush=True)
    if expansion['remaining']:
        raise RuntimeError('MOSTRAR_MAS_ATASCADO: ' + json.dumps(expansion, ensure_ascii=False))
    page.wait_for_timeout(200)


def extraer_mercados_masivo(page, pestanas=True):
    result = evaluate(page, "return extract(root);", extraction=True)
    if pestanas:
        info_acordeones = evaluate(page, """
            const all = accordions();
            const indices = [];
            all.forEach((a, idx) => {
                if (a.querySelectorAll('ms-period-option-group button[role="tab"]').length > 1) {
                    indices.push(idx);
                }
            });
            return {total: all.length, indices};
        """)
        count = info_acordeones["total"]
        indices_con_pestanas = info_acordeones["indices"]
        print(f"  Extracción de pestañas: {len(indices_con_pestanas)}/{count} acordeones contienen subpestañas", flush=True)
        for pos, index in enumerate(indices_con_pestanas):
            extra = evaluate(page, """
                const a = accordions()[arg];
                const all = {rows:[], errors:[], empty:[], unavailable:0};
                const tabs = [...a.querySelectorAll('ms-period-option-group button[role="tab"]')];
                const original = tabs.find(b => b.getAttribute('aria-selected') === 'true');
                for (const b of tabs) {
                    if (!b.isConnected || b === original) continue;
                    b.click();
                    await waitDomSettle(a, 70, 50, 600);
                    const expanded = await expandMore(a);
                    if (expanded.remaining) throw new Error('PESTANA_MOSTRAR_MAS_ATASCADO: '+JSON.stringify(expanded));
                    await waitDomSettle(a, 40, 40, 400);
                    const e = extract(a);
                    all.rows.push(...e.rows); all.errors.push(...e.errors);
                    all.empty.push(...e.empty); all.unavailable += e.unavailable;
                }
                if (original?.isConnected) {
                    original.click();
                    await waitDomSettle(a, 50, 40, 400);
                    const restored = await expandMore(a);
                    if (restored.remaining) throw new Error('RESTAURACION_PESTANA_ATASCADA: '+JSON.stringify(restored));
                }
                return all;
            """, index, extraction=True)
            for key in ("rows", "errors", "empty"):
                result[key].extend(extra[key])
            result["unavailable"] += extra["unavailable"]
            if pos % 10 == 0 or pos == len(indices_con_pestanas) - 1:
                print(f"  Subpestañas procesadas: {pos + 1}/{len(indices_con_pestanas)} (acordeón {index + 1}/{count})", flush=True)
    return result


def deduplicar_resultados(resultados):
    finales = {}
    for item in resultados:
        clave = (item["mercado"], item["seleccion"], item.get("mkt_id", ""))
        finales[clave] = item
    return list(finales.values())


def guardar_txt(resultados, ruta, info, metricas):
    grupos = {}
    for item in resultados:
        grupos.setdefault(item["mercado"], []).append(item)

    lineas = [
        "=" * 60,
        "BWIN - CUOTAS",
        f"Partido: {info['partido']}",
        f"Event ID: {info['event_id']}",
        f"URL: {info['url']}",
        f"Captura: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"Acordeones: {metricas['total']} | Selecciones: {len(resultados)}",
        (f"Aviso de cobertura: {metricas.get('recomendaciones_sin_expandir', 0)} widget(s) "
         "de recomendaciones no expandidos; sus opciones adicionales NO se certifican."),
        ""
    ]
    for mercado, items in grupos.items():
        lineas.append(f"MERCADO: {mercado}")
        lineas.extend(f"{x['seleccion']} : {x['cuota']}" for x in items)
        lineas.append("")
    temporal = ruta.with_suffix(".txt.tmp")
    temporal.write_text("\n".join(lineas), encoding="utf-8")
    temporal.replace(ruta)


def normalizar_equipo(valor):
    # Pistas textuales: nunca eliminamos categorías B, U19, W, ni decidimos
    # IDs API a partir de este procedimiento.
    value = unicodedata.normalize('NFKD', valor or '')
    value = ''.join(x for x in value if unicodedata.category(x) != 'Mn').casefold()
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', value)).strip()


def alias_de_equipo(nombre):
    base = normalizar_equipo(nombre)
    variantes = {base}
    for canonical, aliases in TEAM_ALIASES.items():
        all_names = [canonical, *aliases]
        if any(normalizar_equipo(n) == base for n in all_names):
            variantes.update(normalizar_equipo(n) for n in all_names)
    # No inferir categorías ni reemplazar identificadores; estos son sinónimos
    # exclusivos para confirmar la cabecera Bwin.
    local = {'barca': ('barcelona','fc barcelona'),
             'alaves': ('deportivo alaves','dep alaves'),
             'e frankfurt': ('eintracht frankfurt',),
             'ss lazio': ('lazio',)}
    variantes.update(normalizar_equipo(n) for n in local.get(base, ()))
    return {v for v in variantes if v}


def verificar_encabezado(equipos, encabezado):
    head = ' ' + normalizar_equipo(encabezado) + ' '
    errors = []
    for equipo in equipos:
        if not equipo:
            continue
        if not any(re.search(r'(?<![a-z0-9])'+re.escape(v)+r'(?![a-z0-9])',head)
                   for v in alias_de_equipo(equipo)):
            errors.append(equipo)
    if errors:
        raise RuntimeError(f"ENCABEZADO_NO_COINCIDE: {errors}; cabecera Bwin={encabezado[:230]!r}")


def _encabezado_es_navegacion(heading):
    """Reconoce la barra de mercados que Bwin monta dentro de ms-header.

    No debe confundirse con la cabecera del encuentro. La comprobación se hace
    sobre el texto NORMALIZADO, sin usar esta heurística para acreditar equipos.
    """
    parts = [normalizar_equipo(p) for p in (heading or '').splitlines() if p.strip()]
    keys = {'todo', 'principales', 'cuotas especiales', 'goles', 'jugadores', 'corners', 'tarjetas'}
    return sum(p in keys for p in parts) >= 3


def _titulo_de_partido(titulo, info):
    """Comprueba que el título de la pestaña acredita AMBOS equipos.

    page.title() puede contener competición y sufijos. Se usan aliases solo para
    cotejo textual, nunca para resolver fixture o determinar un Event ID.
    """
    if not titulo or len(titulo) > 500:
        return False
    try:
        verificar_encabezado((info['equipo_1'], info['equipo_2']), titulo)
    except RuntimeError:
        return False
    # El título de una página de evento debe separar los equipos, no mencionar
    # por casualidad ambos en un catálogo o listado. Admite ' - ' y 'vs'.
    return bool(re.search(r'\s+(?:-|–|—|vs\.?|contra)\s+', titulo, flags=re.I))


def verificar_partido(page, info):
    """Verifica el Event ID y una fuente de identidad del evento.

    Bwin reemplaza a veces el primer ms-header por las pestañas de mercados
    durante el scraping. No interpretar esa navegación como un partido distinto:
    si el encabezado deja de mostrar equipos, contrastar document.title. Si
    tampoco hay identidad verificable, FALLAR en vez de etiquetar otro encuentro.
    """
    actual = obtener_info_url(page.url)
    if actual['event_id'] != info['event_id']:
        raise RuntimeError(f"EVENT_ID_CAMBIADO: {page.url}")
    cabecera = ''
    try:
        cabecera = page.locator('ms-event-details-main ms-header').first.inner_text(timeout=3000)
    except Exception as exc:
        # No convertir un fallo del DOM en una validación positiva.
        print(f"  WARN: cabecera dinámica no disponible ({type(exc).__name__}); comprobando título.", flush=True)
    if cabecera:
        try:
            verificar_encabezado((info['equipo_1'], info['equipo_2']), cabecera)
            return 'encabezado_evento'
        except RuntimeError:
            pass
    titulo = ''
    try:
        titulo = page.title()
    except Exception as exc:
        print(f"  WARN: document.title no disponible ({type(exc).__name__}).", flush=True)
    if _titulo_de_partido(titulo, info):
        print('  Identidad confirmada mediante título del evento; ms-header contiene navegación de mercados.', flush=True)
        return 'titulo_evento'
    clasificacion = 'navegacion_de_mercados' if _encabezado_es_navegacion(cabecera) else 'no_identificado'
    raise RuntimeError(
        f"IDENTIDAD_EVENTO_NO_VERIFICADA: ID={info['event_id']}; "
        f"ms_header_tipo={clasificacion}; ms_header={cabecera[:240]!r}; "
        f"titulo={titulo[:240]!r}"
    )


def rutas_salida_evento(info):
    """Todas las salidas Bwin quedan dentro de outputs/matches/<Event ID>/.

    En modo prematch_multi se utiliza exactamente la carpeta inputs declarada
    por el orquestador. En modo manual se deriva de la raíz del proyecto.
    Los nombres fijos por Event ID evitan bifurcaciones por slug/alias.
    """
    event = str(info['event_id'])
    expected = os.getenv('PREMATCH_EVENT_ID', '').strip()
    if expected and expected != event:
        raise ValueError(f'PREMATCH_EVENT_ID={expected} contradice la URL Bwin {event}')

    configured = os.getenv('PREMATCH_EVENT_DATA_DIR', '').strip()
    if configured:
        inputs = Path(configured).expanduser().resolve()
        if inputs.name != 'inputs' or inputs.parent.name != event:
            raise ValueError('PREMATCH_EVENT_DATA_DIR debe señalar outputs/matches/<Event ID>/inputs '
                             f'para el evento {event}: {inputs}')
        # La carpeta enviada desde prematch_multi pertenece al mismo proyecto
        # donde se ejecuta el scraper. No admitir rutas externas por accidente.
        base = OUTPUT_DIR_SCRAPER.parent.resolve() / 'matches' / event / 'inputs'
        staged=False
        if os.getenv('PREMATCH_IMMUTABLE_CAPTURE')=='1':
            staging=base.parent/'staging'
            if inputs.is_relative_to(staging):
                parts=inputs.relative_to(staging).parts
                staged=len(parts)==3 and parts[-2:]==(event,'inputs') and bool(re.fullmatch(r'\d{8}T\d{12}Z',parts[0]))
        if inputs != base and not staged:
            raise ValueError(f'La carpeta de cuotas del evento no coincide con el proyecto: '
                             f'{inputs} != {base}')
    else:
        inputs = OUTPUT_DIR_SCRAPER.parent.resolve() / 'matches' / event / 'inputs'

    # Una ejecución manual no actualiza los SHA256 del manifest existente.
    # Evitar que reemplace un snapshot fijado y rompa validate/finish.
    if os.getenv('PREMATCH_BATCH_MODE') != '1' and (inputs.parent / 'manifest.json').is_file():
        raise ValueError(f'El evento {event} ya tiene manifest.json. Para preservar hashes, '
                         'ejecuta prematch_multi.py prepare / retry-scraper según su estado; '
                         'no ejecutes scraper_bwin.py directamente sobre ese evento.')

    diagnostics = inputs.parent / 'scraper'
    failures = diagnostics / 'errores'
    inputs.mkdir(parents=True, exist_ok=True)
    diagnostics.mkdir(parents=True, exist_ok=True)
    return inputs, diagnostics, failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=BWIN_EVENT_URL)
    parser.add_argument('--intentos', type=int, default=MAX_INTENTOS_SCRAPER)
    parser.add_argument('--pausar', action='store_true')
    parser.add_argument('--sin-pestanas', action='store_true')
    args = parser.parse_args(argv)
    if args.intentos < 1:
        parser.error('--intentos debe ser positivo')
    info = obtener_info_url(args.url)
    inputs, diagnostics, failures = rutas_salida_evento(info)
    nombre = f"cuotas_bwin_{info['event_id']}"
    txt = inputs / f"{nombre}.txt"
    print(f"BWIN | {info['partido']} | Event ID {info['event_id']}\n{args.url}", flush=True)
    print(f"Scraper patch: {SCRAPER_PATCH_VERSION}", flush=True)
    print(f"Destino cuotas: {txt}", flush=True)
    print(f"Destino diagnósticos: {diagnostics}", flush=True)
    with sync_playwright() as p:
        for intento in range(1, args.intentos + 1):
            browser = context = page = None
            diagnostico = {"url_solicitada": args.url, "event_id": info['event_id'],
                           "destino_cuotas": str(txt), "intento": intento}
            try:
                print(f"\nINTENTO {intento}/{args.intentos}", flush=True)
                browser = p.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO)
                context = browser.new_context(locale='es-ES', timezone_id='Europe/Madrid',
                                              viewport={"width": 1440, "height": 2400})
                context.route(
                    "**/*",
                    lambda route: route.abort()
                    if route.request.resource_type in {"image", "media", "font"}
                    else route.continue_()
                )
                page = context.new_page()
                page.goto(args.url, wait_until='domcontentloaded', timeout=60000)
                page.locator('ms-event-details-main ds-accordion').first.wait_for(timeout=45000)
                consent = page.locator('#onetrust-reject-all-handler')
                if consent.is_visible():
                    consent.click()
                verificar_partido(page, info)
                diagnostico['inicial'] = evaluate(page, 'return stats();')
                print(f"Inicial: {diagnostico['inicial']}", flush=True)
                scroll_completo(page)
                for ciclo in range(4):
                    expandir_mercados_masivo(page, info)
                    estado = scroll_completo(page)
                    if not estado['cerrados'] and not estado['mostrar_mas']:
                        break
                else:
                    raise RuntimeError('Scroll/expansión no convergen')
                page.wait_for_timeout(WAIT_BEFORE_EXTRACTION_MS)
                extraidos = extraer_mercados_masivo(page, not args.sin_pestanas)
                resultados = deduplicar_resultados(extraidos['rows'])
                estado = evaluate(page, 'return stats();')
                # Volver a confirmar la cabecera al cerrar. Una SPA puede
                # reemplazar el contenedor o navegar mientras abre mercados.
                verificar_partido(page, info)
                if estado['cerrados'] or estado['mostrar_mas']:
                    raise RuntimeError('EXTRACCION_INCOMPLETA: ' + json.dumps({
                        'cerrados': estado['cerrados'],
                        'mostrar_mas_ordinarios': estado['mostrar_mas'],
                        'recomendaciones_no_expandidas': estado.get('recomendaciones_sin_expandir', 0),
                    }, ensure_ascii=False))
                diagnostico.update(final=estado, errores=extraidos['errors'],
                                   vacios=extraidos['empty'], no_disponibles=extraidos['unavailable'],
                                   selecciones=len(resultados), url_final=page.url,
                                   recomendados_no_expandidos=estado.get('recomendaciones_sin_expandir', 0),
                                   nota_cobertura='Los widgets de recomendaciones no se expanden; pueden contener propuestas adicionales.')
                if not resultados:
                    raise RuntimeError('Extracción fallida (0 cuotas): ' + json.dumps(diagnostico, ensure_ascii=False))

                guardar_txt(resultados, txt, info, estado)
                diagnostico['estado'] = 'correcto'
                (diagnostics / f'{nombre}.json').write_text(json.dumps(diagnostico, ensure_ascii=False, indent=2), encoding='utf-8')
                print(f"\nCORRECTO: {estado['total']} acordeones abiertos; {len(resultados)} selecciones; "
                      f"{len({x['mercado'] for x in resultados})} mercados/contextos.\nTXT: {txt}", flush=True)
                if args.pausar or PAUSAR_AL_FINAL:
                    input('Chromium permanece abierto. ENTER para cerrar: ')
                return 0
            except Exception as exc:
                diagnostico.update(estado='error', error=str(exc))
                if page:
                    diagnostico['url_final'] = page.url
                    try:
                        failures.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(failures / f'{nombre}_error_{intento}.png'))
                    except Exception:
                        pass
                failures.mkdir(parents=True, exist_ok=True)
                (failures / f'{nombre}_error_{intento}.json').write_text(
                    json.dumps(diagnostico, ensure_ascii=False, indent=2), encoding='utf-8')
                print(f"ERROR: {exc}", flush=True)
                if intento < args.intentos:
                    time.sleep(ESPERA_ENTRE_INTENTOS_SEGUNDOS)
            finally:
                if context:
                    context.close()
                if browser:
                    browser.close()
    print('No se generó un TXT nuevo: revisar los diagnósticos.', flush=True)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())