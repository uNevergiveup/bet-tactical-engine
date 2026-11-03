# Cierre formal V2.1.3 estable offline

## Alcance congelado

Esta versión conserva el selector deportivo, familias, ranking, presupuestos y
controles PREMATCH. El enlace comercial exige identidad de fixture y evento
Bwin, SHA de las fuentes, kickoffs completos y equivalentes, capturas anteriores
al kickoff y al corte del scanner, y aislamiento por partido. Ninguna salida
acredita probabilidad calibrada, EV, stake ni apuesta ejecutable.

El perfil `normal` usa `16:00–02:00 Europe/Madrid`; el perfil `ampliada` conserva
`15:00–03:00`. La aritmética se realiza con datetimes timezone-aware, por lo que
Europe/Madrid aplica CET o CEST según la fecha.

## Verificación

Comandos de cierre:

```sh
.venv/bin/python -m compileall -q prematch_*.py preselect_matches.py test_*.py
.venv/bin/python -m unittest -q test_prematch_pattern_discovery_v2 test_prematch_selection_audit test_prematch_tactical_signal_engine test_prematch_daily test_preflight_timing_v0_3_1 test_prematch_comparison_v21
.venv/bin/python -m unittest discover -p 'test_*.py' -q
.venv/bin/python prematch_daily.py --window normal --date 2026-11-03 --dry-run
shasum -a 256 outputs/discovery/2026-11-01_1600_0200/bwin_v04_mls/bwin_discovery.json
```

Resultados del cierre, sujetos a repetición en el entorno receptor:

- Suite específica: 96/96.
- Suite integral: 215/221; cinco errores y un fallo por artefactos históricos ausentes.
- Catálogo Bwin recuperado: `cf5ad504ffd563f06052ee6eb24fb10788c44fcde3932fb372838b24c728e1b0`.

## Limitaciones históricas conocidas

Las seis pruebas siguientes no pueden completarse sin sus fuentes originales.
No se sustituyen por snapshots parecidos ni se marcan como omitidas:

1. `test_historical_daily_csv_retains_162_unique_rows`: falta
   `outputs/engineering/prematch_comparison_v2_1/partidos_diarios.csv` de 162 fixtures.
2. `test_lafc_historical_not_lost`: falta
   `outputs/discovery/2026-11-01_1600_0300/fixtures.json`.
3. `test_seven_candidates_all_enriched_and_discovered_with_batch_size_two`: falta
   `outputs/discovery/2026-11-01_1600_0300/fixtures_major.json`.
4. `test_at_0129_near_scope_keeps_lafc`: faltan los `fixtures_major.json` de
   `2026-11-01_1600_0300` y `2026-11-02_1600_0300`.
5. `test_lafc_was_sixth_before_old_top_five_cut`: faltan `fixtures_major.json` y
   `prospective_trial/enriched/preselection.json` de `2026-11-01_1600_0300`.
6. `test_incomplete_legacy_preparation_not_reused`: falta
   `prospective_trial/prospective_test_summary.json` de esa misma corrida; la
   ausencia se detecta antes de poder comprobar `SCOPE_INCOMPLETE`.

La corrida `2026-11-01_1600_0200` recuperada de `scraper.zip` no contiene el
fixture 1490590 y no sirve como sustituto de `1600_0300`. Los CSV disponibles
tienen 163 fixtures únicos y no sustituyen el CSV esperado de 162.

## Recorrido operativo esperado

`prematch_daily.py` resuelve la ventana y aplica el bloqueo de ejecución. La
preparación encadena descubrimiento, filtro de ligas, preselección, descubrimiento
de patrones, motor descriptivo y descubrimiento Bwin. El scan revalida snapshots
y preflights, ejecuta el scanner, genera el enlace comercial sidecar y finalmente
escribe la selección para investigación manual. Los límites de identidad, SHA,
kickoff, frescura y corte temporal se vuelven a comprobar antes del handoff.

Prueba operativa real propuesta, a ejecutar manualmente cuando se autoricen red,
API-Football y capturas Bwin:

```sh
.venv/bin/python prematch_daily.py --window normal --date hoy --horizon-hours 3 --evaluation-budget 15 --discovery-budget 15 --max-matches 5 --bwin-top 5 --max-scrapes 3 --history 20 --details 8 --discovery-retries 2 --min-lead 60 --max-odds-age 120
```

Los presupuestos limitan evaluación, descubrimiento y capturas; los candidatos
no procesados quedan registrados como diferidos. El comando no incluye opciones
que omitan preflight, identidad, temporalidad o comprobaciones SHA.
