# RIESGOS RESIDUALES Y JUSTIFICACIÓN DE PRUEBAS OMITIDAS

**Riesgos críticos pendientes:** `0`

---

## 1. Riesgos Operativos Residuales (No Bloqueantes, Controlados)

1. **Publicación tardía de 22 titulares en API-Football (`< T-15m`):**
   - **Impacto:** En algunas competiciones (p. ej. Brasileirão Serie A o copas nacionales), el proveedor estructurado API-Football puede publicar las alineaciones con retraso próximo a `T-15m`.
   - **Mitigación implementada:** `acquire-lineups` habilita automáticamente la bandeja `outputs/matches/<EVENT_ID>/lineups/manual/`, `prematch_case.py sync-manual-lineups` funciona sin `case.json` y `prematch_single.py close` auto-reconcilia cualquier JSON manual depositado en la bandeja antes de ejecutar `refresh-odds`.

2. **Latencia del scraper de Bwin cerca de `T-5m`:**
   - **Impacto:** Si el operador inicia `close` exactamente en `T-5.1m` y el scraping en vivo tarda más de 10 segundos, el margen restante tras `refresh-odds` podría caer por debajo de `min_finish_lead_minutes=5`.
   - **Mitigación implementada:** `close` vuelve a comprobar el margen temporal inmediatamente después de `refresh-odds` y después de `finish`. Si se cruza el límite o el kickoff, aborta cerrado o revoca `CODEX_<EVENT_ID>.md` (`status=EXPIRADO_POST_KICKOFF`). Se recomienda iniciar `close` entre `T-15m` y `T-8m`.

---

## 2. Justificación Explícita de los 10 Tests Omitidos (`skipped=10` de `1075`)

Los 10 tests omitidos corresponden exclusivamente a guardas ambientales o snapshots históricos opcionales de principios de noviembre que tienen su equivalente sintético activo en la misma clase de prueba:

1. `test_prematch_depth_v1042.py` (8 métodos en `PrematchDepthTests`): omitidos cuando el fixture estático opcional `fixtures/match_engine_7896590.json` no está instalado en `test/fixtures/`.
2. `test_prematch_sos_reference_collector_v1045.py::test_missing_credential_fails_closed`: se omite explícitamente (`credential configured in this environment`) cuando el entorno local sí tiene `FOOTBALL_API_KEY` configurada.
3. `test_prematch_pilot_aliases.py` (1 test DOM Chromium): se omite si no se solicita ejecución de navegador headless en test unitario offline.
