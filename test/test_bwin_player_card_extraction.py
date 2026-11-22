import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from scraper_bwin import JS_COMMON, JS_EXTRACT, asegurar_vista_todo, completar_ambos_reciben_tarjetas, url_todos_mercados


def player(name):
    return f'<span class="player-stats-option-group__player-name">{name}</span>'


def pick(name="", price="2.50"):
    return (
        f'<ms-event-pick class="option-pick"><span class="name">{name}</span>'
        f'<span class="value option-value">{price}</span></ms-event-pick>'
    )


class PlayerCardExtractionTests(unittest.TestCase):
    def test_all_markets_url_overwrites_category_and_keeps_other_parameters(self):
        base = "https://www.bwin.es/es/sports/eventos/fc-barcelona-villarreal-2:7911859"
        self.assertEqual(url_todos_mercados(base), base + "?market=-1")
        self.assertEqual(url_todos_mercados(base + "?market=8&src=calendar&market=3#event"),
                         base + "?src=calendar&market=-1#event")
        self.assertEqual(url_todos_mercados(base + "?market=-1"), base + "?market=-1")

    def test_both_teams_cards_reads_yes_no_by_row_and_handles_reversed_columns(self):
        with sync_playwright() as playwright:
            options = {}
            if not Path(playwright.chromium.executable_path).is_file():
                cached = sorted((Path.home() / "Library/Caches/ms-playwright").glob(
                    "chromium-*/chrome-mac-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
                ))
                if not cached:
                    self.skipTest("Chromium local no disponible")
                options["executable_path"] = str(cached[-1])
            browser = playwright.chromium.launch(**options)
            try:
                page = browser.new_page(offline=True)
                page.set_content(
                    '<ms-event-details-main><button role="tab" aria-selected="false" '
                    'onclick="this.setAttribute(\'aria-selected\', \'true\')">Todo</button>'
                    '<ds-accordion>Mercados del evento</ds-accordion></ms-event-details-main>'
                )
                with patch("scraper_bwin.WAIT_BEFORE_EXTRACTION_MS", 0):
                    market_view = asegurar_vista_todo(page)
                page.set_content(
                    '<ms-event-details-main><ds-accordion><span slot="title">'
                    'Ambos equipos recibirán X o más tarjetas</span><table>'
                    '<tr><th></th><th>No</th><th>Sí</th></tr>'
                    f'<tr><th scope="row">1</th><td>{pick(price="3.10")}</td><td>{pick(price="1.32")}</td></tr>'
                    f'<tr><th scope="row">2</th><td>{pick(price="1.39")}</td><td>{pick(price="2.75")}</td></tr>'
                    f'<tr><th scope="row">3</th><td>{pick(price="1.06")}</td><td>{pick(price="8.50")}</td></tr>'
                    f'<tr><th scope="row">?</th><td>{pick("Sí", "2.21")}</td></tr>'
                    '</table></ds-accordion></ms-event-details-main>'
                )
                result = page.evaluate("() => {" + JS_COMMON + JS_EXTRACT + "return extract(root);}")
                page.set_content(
                    '<ms-event-details-main><button role="tab">Tarjetas</button><section></section></ms-event-details-main>'
                )
                flat_market = (
                    '<ds-accordion><span slot="title">Ambos equipos recibirán X o más tarjetas</span>'
                    '<div class="option-group-container score-market-container triple"><div class="option-group-header">'
                    '<span></span><span class="option-group-custom-header"><div>Sí</div></span>'
                    '<span class="option-group-custom-header"><div>No</div></span></div><div class="attribute-key">2</div>'
                    f'<div class="option">{pick(price="2.75")}</div><div class="option">{pick(price="1.39")}</div>'
                    '</div></ds-accordion>'
                )
                page.evaluate("(markup) => { document.querySelector('button').onclick = () => "
                              "{ document.querySelector('section').innerHTML = markup; }; }", flat_market)
                base = {"rows": [], "errors": []}
                with patch("scraper_bwin.verificar_partido"), patch("scraper_bwin.scroll_completo"), \
                        patch("scraper_bwin.expandir_mercados_masivo"), patch("scraper_bwin.WAIT_BEFORE_EXTRACTION_MS", 0):
                    coverage = completar_ambos_reciben_tarjetas(page, {}, base)
            finally:
                browser.close()
        self.assertEqual([(row["seleccion"], row["cuota"]) for row in result["rows"]], [
            ("1+ · No", "3.10"), ("1+ · Sí", "1.32"), ("2+ · No", "1.39"),
            ("2+ · Sí", "2.75"), ("3+ · No", "1.06"), ("3+ · Sí", "8.50"),
        ])
        self.assertIn("Umbral o columna", result["errors"][0])
        self.assertEqual(market_view["status"], "TODO_ACTIVO_EN_DOM")
        self.assertTrue(market_view["clicked"])
        self.assertEqual(coverage, {"status": "EXTRAIDO_EN_TARJETAS", "selections": 2})
        self.assertEqual([(row["seleccion"], row["cuota"]) for row in base["rows"]],
                         [("2+ · Sí", "2.75"), ("2+ · No", "1.39")])

    def test_all_markets_recovers_query_removed_by_canonical_redirect(self):
        with sync_playwright() as playwright:
            options = {}
            if not Path(playwright.chromium.executable_path).is_file():
                cached = sorted((Path.home() / "Library/Caches/ms-playwright").glob(
                    "chromium-*/chrome-mac-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
                ))
                if not cached:
                    self.skipTest("Chromium local no disponible")
                options["executable_path"] = str(cached[-1])
            browser = playwright.chromium.launch(**options)
            try:
                page = browser.new_page(offline=True)
                url = "https://www.bwin.es/es/sports/eventos/ajax-de-amsterdam-ado-den-haag-2:7911880"

                def event_page(route):
                    selected = "true" if "market=-1" in route.request.url else "false"
                    route.fulfill(status=200, content_type="text/html", body=(
                        '<ms-event-details-main>'
                        f'<span role="tab" aria-selected="{selected}">Todo</span>'
                        '<span role="tab" aria-selected="false">Principales</span>'
                        '<ds-accordion>Mercados</ds-accordion>'
                        '</ms-event-details-main>'
                    ))

                page.route("https://www.bwin.es/**", event_page)
                page.goto(url)
                with patch("scraper_bwin.WAIT_BEFORE_EXTRACTION_MS", 0):
                    result = asegurar_vista_todo(page)
            finally:
                browser.close()
        self.assertEqual(result["status"], "TODO_ACTIVO_EN_DOM")
        self.assertTrue(result["redirect_recovered"])
        self.assertEqual(result["market_param"], "-1")
        self.assertIn("market=-1", result["url"])

    def test_headerless_cards_require_explicit_player_binding(self):
        with sync_playwright() as playwright:
            options = {}
            if not Path(playwright.chromium.executable_path).is_file():
                cached = sorted((Path.home() / "Library/Caches/ms-playwright").glob(
                    "chromium-*/chrome-mac-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
                ))
                if not cached:
                    self.skipTest("Chromium local no disponible")
                options["executable_path"] = str(cached[-1])
            browser = playwright.chromium.launch(**options)
            try:
                page = browser.new_page(offline=True)
                card_rows = (
                    player("Ana") + pick("Ana") + player("Eva") + pick("Nombre desconocido")
                    + '<div class="player-stats-option-group__options">'
                    + player("Luz") + pick(price="3.10") + '</div>'
                    + '<div class="player-stats-option-group__options">'
                    + player("Inés") + pick("Eva") + '</div>'
                    + player("Sara") + pick("Sara", "") + pick()
                )
                page.set_content(
                    '<ms-event-details-main><ds-accordion><span slot="title">Recibe una tarjeta</span>'
                    f'<ms-player-stats-option-group>{card_rows}</ms-player-stats-option-group></ds-accordion>'
                    '<ds-accordion><span slot="title">Jugador - Total de tiros</span>'
                    f'<ms-player-stats-option-group>{player("Ana")}{pick("Ana")}</ms-player-stats-option-group>'
                    '</ds-accordion></ms-event-details-main>'
                )
                result = page.evaluate("() => {" + JS_COMMON + JS_EXTRACT + "return extract(root);}")
            finally:
                browser.close()
        self.assertEqual([(r["mercado"], r["seleccion"], r["cuota"]) for r in result["rows"]],
                         [("Recibe una tarjeta", "Ana", "2.50"), ("Recibe una tarjeta", "Luz", "3.10")])
        self.assertEqual(result["unavailable"], 1)
        self.assertEqual(len(result["errors"]), 2)
        self.assertIn("inequívoca", result["errors"][0])
        self.assertEqual(result["empty"], ["Jugador - Total de tiros"])
