# Reproducción offline V2.1.3

Desde la raíz del paquete, con Python 3.12 y las dependencias locales instaladas:

```sh
python3 verify_v213_offline.py
```

El script ejecuta exactamente:

```sh
python3 -m unittest -v test_prematch_pattern_discovery_v2 test_prematch_selection_audit test_prematch_tactical_signal_engine test_prematch_daily test_preflight_timing_v0_3_1
python3 -m compileall -q prematch_pattern_discovery.py test_prematch_pattern_discovery_v2.py
```

Después genera dos veces los cinco JSON en run1 y run2 y compara sus SHA256.
Usa sources/enriched/preselection.json, sources/budget_assignment.json,
sources/opportunity_scan.json y sources/bwin_discovery.json. El orden original
se coteja directamente con budget_assignment.json. verification.json contiene
los hashes de las fuentes y de los cinco resultados de cada ejecución.

Las fuentes se copiaron de la corrida 2026-11-02_1500_0300; el scanner corresponde
a daily_runs/20261102T223114514553. El directorio congelado patterns_v2 no se
utiliza como evidencia ni se sobrescribe.

Código de producto modificado en V2.1.3: prematch_pattern_discovery.py,
exclusivamente commercial_link(). Regresiones: test_prematch_pattern_discovery_v2.py.
verify_v213_offline.py es el reproductor del paquete.
