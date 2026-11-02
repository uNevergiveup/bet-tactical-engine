"""Build rc1 documentation from verified local artifacts; no API or scraping."""
import json
import zipfile
from pathlib import Path
import prematch_prospective_trial as t
import prematch_single as single
import prematch_release as release
from types import SimpleNamespace

def main():
    root=Path(__file__).resolve().parent;out=root/'outputs/releases/v0.1.0-rc1'
    active=t.read(root/'outputs/latest/ACTIVE_EVENT.json');eid=active['bwin_event_id']
    selected=Path(active['selected']);s=t.read(selected)
    ctx=single.load_context(SimpleNamespace(root=root,selected=selected,decision=None))
    manifest=single.match_manifest(ctx);identity=t.read(Path(manifest['inputs']['identity']['path']))
    release.verify_refs(s['source_refs'])
    assert identity['api_fixture_id']==s['fixture_id']
    assert identity['home']['api_team_id']==s['home_api_id'] and identity['away']['api_team_id']==s['away_api_id']
    assert identity['league_id']==s['league_id'] and identity['season']==s['season']
    assert manifest['inputs']['odds']['sha256']==s['odds_sha256']
    base=root/'outputs/discovery/2026-11-02_1500_0300/prospective_trial'
    pre=t.read(base/'enriched/preselection.json')
    refs={k:release.reference(v['path']) for k,v in manifest['inputs'].items()}
    refs.update(selection=release.reference(selected),decision=release.reference(selected.parent/'selection_decision.json'))
    historical_sources={k:release.reference(v['path']) for k,v in t.read(root/'outputs/matches/7899092/manifest.json')['inputs'].items()}
    unchanged={}
    with zipfile.ZipFile(root/'outputs/engineering/prematch_comparison_v2_1.zip') as archive:
        import hashlib
        for name in ('prematch_tactical_signal_engine.py','prematch_pattern_engine.py','preselect_matches.py','motor_probabilistico.py'):
            old=archive.read('prematch_comparison_v2_1/code/'+name)
            unchanged[name]=hashlib.sha256(old).hexdigest()==t.sha(root/name)
            assert unchanged[name],name
    timing=[]
    for line in (base/'logs/pipeline_stage_timings.jsonl').read_text().splitlines():
        row=json.loads(line)
        if row['finished_madrid']>='2026-11-02T15:49:38':timing.append(row)
    cost={'discovery_requests_new':2,'preselection_requests_new':pre['api_usage']['requests_sent'],
          'full_context_requests_new':29,'total_api_football_requests_new':2+pre['api_usage']['requests_sent']+29,
          'preselection_cache_hits':pre['api_usage']['cache_hits'],'full_context_cache_hits':12,
          'bwin_discovery_processes':sum(x['script']=='bwin_discovery.py' for x in timing),
          'bwin_new_quote_captures':sum(x['script']=='bwin_market_preflight.py' for x in timing),
          'prepared_event_odds_reused':manifest.get('odds_reused',False),
          'browser_http_request_total':'NOT_INSTRUMENTED; process counts are not HTTP request counts'}
    t.dump(out/'verification_results.json',{'version':release.VERSION,'prospective_status':'INCOMPLETO',
        'prepared_manifest_status':manifest['status'],'fixture_id':s['fixture_id'],'bwin_event_id':eid,
        'identity_checks':'ALL_MATCHED','prospective_sources':refs,'historical_sources':historical_sources,
        'historical_validation':'APTO_VALIDACION_BASICA; BLOCK=0 REVIEW=0',
        'historical_motor':'RETROSPECTIVO_NO_EJECUTABLE; 661 selections; 66 direct evidence',
        'tests_passed':185,'tests_failed_final':0,'statistical_files_unchanged':unchanged,'cost':cost,
        'timings':timing,'external_steps_executed':False,'final_analyst_executed':False})
    audit=f'''# Estado e identidad rc1

## Resultado de la ejecución prospectiva

Estado general: INCOMPLETO, en espera de investigación externa manual. Manifest técnico: {manifest['status']}.

| Campo | Identidad verificada |
| --- | --- |
| Partido | {s['home']}–{s['away']} |
| Fixture API | {s['fixture_id']} |
| Event ID Bwin | {eid} |
| Equipos API | {s['home_api_id']} / {s['away_api_id']} |
| Liga y temporada | {s['league']} ({s['league_id']}), {s['season']} |
| Kickoff Madrid | {s['kickoff_madrid']} |
| URL Bwin | {s['bwin_url']} |
| Captura | {s['capture_madrid']} |
| SHA cuotas | {s['odds_sha256']} |

Se comprobó la misma identidad en handoff, preflight, selected_match, selection_decision, contexto TXT/JSON y match_identity. Las rutas y hashes completos constan en verification_results.json.

El primer intento capturó Sport Huancayo–Melgar (1549543 / 7905351, 157 cuotas) y terminó INSUFFICIENT_EVIDENCE por falta de mercado vinculado a señal sustentada. Se amplió explícitamente max-scrapes de 1 a 3. El segundo intento reutilizó esa captura y la preparación y obtuvo Lorient–Brest (757 cuotas) y Defensa y Justicia–Independiente (565 cuotas). Los tres preflights y el scanner terminaron VERIFIED; se seleccionó Lorient con las fórmulas existentes. No se forzó el primer candidato ni se cambiaron pesos/umbrales.

La preparación inicial se renovó porque el alcance elegible cambió con el reloj y el presupuesto de 15 dejó de estar satisfecho por el subconjunto anterior. Se archivó la carpeta previa en outputs/discovery/archives. El descubrimiento actual conserva 163 fixtures de la ventana en CSV (158 todavía NS seleccionables por discovery y cinco excluidos por estado/tiempo); la muestra congelada V2.1 conserva sus 162 filas. No son la misma foto temporal.

El contexto completo se generó una sola vez para {eid}: fixture NS, liga {s['league_id']}, equipos {s['home_api_id']}/{s['away_api_id']}, fecha UTC {identity['kickoff_utc']}. Las cuotas se reutilizaron por SHA. Investigación externa recibe una proyección sin cuotas/predicciones; ninguna plantilla se contabilizó como informe completado.

## Ensayo offline

Bridge histórico sobre el handoff V2 al corte 14:52:45 CET, antes de renovar discovery; salida HISTORICAL_AUDIT_ONLY, rechazada por el contrato operativo de single.
Validador y motor ejecutados realmente sobre los originales congelados Barcelona–Alavés: fixture 1570434 / Bwin 7899092. Validación APTO_VALIDACION_BASICA, sin BLOCK/REVIEW; motor con 661 selecciones y 66 evidencias directas, etiqueta RETROSPECTIVO_NO_EJECUTABLE. Salidas aisladas en historical/; no se ejecutó un nuevo analista final ni se simularon informes externos.

## Coste registrado de esta release

{json.dumps(cost,ensure_ascii=False,indent=2)}

Los tiempos de subprocesos por etapa están en verification_results.json; la preparación completa del evento aparece en prospective_prepare.log y outputs/matches/{eid}/logs/contexto.log. El número de procesos Bwin no equivale a solicitudes HTTP internas.

## Fases pendientes

Grok Expert, Gemini Deep Research y fusión Argon: PENDIENTE_MANUAL. Reporte canónico: plantilla pendiente. Validación y motor prospectivos: pendientes del reporte y la captura final. Codex analista final: NO EJECUTADO. Revisión final: PENDIENTE. No se ha completado un end-to-end prospectivo ni se ha creado v0.1.0 estable.

Para este kickoff, la ventana de captura final comienza a las 19:45 CET. No considerar la captura de las 15:53 lista para la decisión final.
'''
    (out/'STATE_AND_IDENTITY_AUDIT.md').write_text(audit,encoding='utf-8')
    tests='''# Pruebas rc1

Comando: `.venv/bin/python -m unittest discover -v`.

- Línea base: 168 correctas (baseline_tests.log).
- Suite final: 185 correctas, 0 fallidas (final_tests.log), incluidas 17 nuevas puertas de integración.
- Ensayo histórico real: bridge no accionable; validate_prematch exit 0 y motor_probabilistico exit 0. Logs offline_bridge.log, historical_validation.log y historical_motor.log.
- Ensayo prospectivo real: primer daily exit 2 por evidencia insuficiente; segundo daily exit 0; bridge, single check, prepare con cuotas reutilizadas y research-kit exit 0.
- Puerta prematura analysis-input exit 2 esperado: manifest aún PREPARADO. No se emitió entrada del analista final.
- Las pruebas mockean solo los límites de red; no se presentan como verificación real Bwin. La ejecución prospectiva sí hizo discovery, scraper, preflight y contexto reales.

Los casos nuevos verifican IDs/competición cruzados, hashes, selección caducada, rechazo de reloj simulado, historical-only no operativo, fallo de recaptura con bytes anteriores intactos, respaldo SHA de recaptura, protección de manifest preparado, revisión NO_BET, invalidación por cambio de precio/línea, cutoff externo postkickoff, plantillas pendientes, bloqueo postkickoff y edad final >15 minutos.

## Fallos encontrados y corregidos

La primera versión del test de bridge omitía source_filter_sha256 en su fixture sintético: se completó la procedencia del fixture, sin relajar el validador. Tras discovery real aparecieron tres dependencias de tests antiguos sobre salidas mutables: cobertura de presupuesto leída de preparación del día, fixture LAFC que ya había pasado a FT en caché y CSV que exigía 162 filas en una nueva foto de 163. Se corrigieron los tests para usar un caso sintético de presupuesto/frontera y el CSV congelado V2.1. Se conservan los logs de fallo y las aserciones; no se desactivaron validaciones ni se eliminaron pruebas.

Las fórmulas estadísticas de cuatro módulos se compararon por SHA con el código del ZIP V2.1 y permanecen idénticas. El ensayo prospectivo se detuvo en el paso externo indicado por el encargo; no se certifica una ejecución de herramientas ausentes.
'''
    (out/'TEST_RESULTS.md').write_text(tests,encoding='utf-8')
    continuation=f'''# Continuar el evento activo

{s['home']}–{s['away']}; fixture {s['fixture_id']}; Bwin {eid}; kickoff {s['kickoff_madrid']}.

1. Abrir ENTRADA_GROK_{eid}.md en Grok Expert y ENTRADA_GEMINI_{eid}.md en Gemini Deep Research, por separado. Adjuntar contexto_sin_cuotas_{eid}.json. Guardar respuestas grok_{eid}.md y gemini_{eid}.md aquí. Declarar cutoff_utc y fuentes fechadas; retirar la marca de pendiente solo tras completar.
2. Aplicar ENTRADA_ARGON_{eid}.md en la sesión Argon del usuario. Guardar fusion_{eid}.md y reporte_cualitativo_{eid}.md, conservando las fuentes originales.
3. Desde /Users/antonio/Projects/scraper, a partir de las 19:45 CET y antes de perder margen, ejecutar:

```bash
.venv/bin/python prematch_single.py --selected outputs/latest/selected_match.json refresh-odds
.venv/bin/python prematch_single.py --selected outputs/latest/selected_match.json finish --report outputs/latest/research/reporte_cualitativo_{eid}.md
.venv/bin/python prematch_release.py analysis-input
```

4. Entregar el CODEX impreso al analista final una sola vez. Después:

```bash
.venv/bin/python prematch_release.py final-review --analysis outputs/latest/event/analysis/analisis_{eid}.md
```

Para candidato, añadir --market, --selection y --odd literales. Si hay BLOCK, archivos pendientes o kickoff pasado, no continuar como PREMATCH. Estado actual INCOMPLETO; no se ejecutaron las herramientas manuales.
'''
    (ctx['directory']/'research/external/CONTINUAR.md').write_text(continuation,encoding='utf-8')
    (out/'CONTINUAR_EVENTO_ACTIVO.md').write_text(continuation,encoding='utf-8')
    print('Identity, provenance, statistical hashes and release documentation verified:',eid)

if __name__=='__main__':main()
