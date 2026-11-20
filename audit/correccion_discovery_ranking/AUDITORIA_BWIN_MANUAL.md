# Auditoría de Integración Bwin y Vinculación Manual (`bwin_manual_links.py`, `prematch_daily.py`, `prematch_runtime.py`)

## 1. Mecanismos existentes inspeccionados
- `prematch_daily.py`:
  - `--manual-select`: desempate en Shadow T-24h entre candidatos ya verificados; no vincula URLs.
  - `--bwin-identity-catalog`: reutiliza catálogos de reconciliación históricos (`reconciliation_73.json`) con `--direct-verified-only`.
  - `--sync-bwin`: descubrimiento ligero en Bwin sin cuotas profundas.
- `bwin_discovery.py`:
  - `extract_event_id(url)` con `EVENT_RE`, `verified_identity_candidates()`, `inspect_event()`, `summarize()`.
- `prematch_case.py`: gestión de expediente por `bwin_event_id` tras handoff.
- `bwin_market_preflight.py` + `scraper_bwin.py`: captura de cuotas TXT y validación SHA-256 / lead time / triplets.

## 2. Brecha cerrada en esta intervención
Se añadió `bwin_manual_links.py` y la interfaz CLI `--bind-bwin-url` en `prematch_daily.py` para permitir suministrar manualmente una o varias URLs de eventos Bwin cuando el descubridor automático no los encontró o quedaron diferidos por presupuesto:
1. **Asociación explícita:** `--manual-bwin-url <FIXTURE_ID>=<URL>` (repetible) y `--manual-bwin-file <JSON|CSV>`.
2. **Extracción determinista del Event ID:** vía `bwin_discovery.extract_event_id(url)` y validación de esquema `https` + host `bwin.es`; nunca se inventa un ID.
3. **Control de duplicados y conflictos:**
   - Mismo `fixture_id` con dos URLs/Event IDs distintos -> error `MANUAL_BWIN_DUPLICATE_FIXTURE_CONFLICT`.
   - Mismo `bwin_event_id` asignado a dos `fixture_id`s distintos -> error `MANUAL_BWIN_DUPLICATE_EVENT_ACROSS_FIXTURES` / `MANUAL_BWIN_EVENT_ALREADY_BOUND`.
   - Mismo `(fixture_id, bwin_event_id, bwin_url)` repetido -> idempotente, sin duplicar registros.
4. **Separación de tres estados en `prospective_trial/bwin/manual_bwin_links.json`:**
   - `url_provided`: URL suministrada, `extracted_event_id`, `provided_at_utc`, `source="MANUAL_CLI"`.
   - **Comprobación positiva obligatoria (`evaluate_observed_manual_identity`):**
     - Página abierta (`status == "OPENED"`).
     - Event ID observado explícitamente (`obs["event_id_from_url"]` presente e igual al extraído de la URL, nunca mediante fallback `or eid`).
     - Equipos y orden local/visitante verificados sobre `page_title` observado (permitiendo traducciones y alias válidos sin bloqueo arbitrario).
     - Kickoff absoluto compatible (`event_start_machine_readable` presente y `kickoff_difference_minutes <= 15`, nunca solo cuenta atrás relativa).
     - Competición compatible (`comp_ok` integrado en la decisión final, `competition_match_score >= 0.82` o compatibilidad explícita).
     - Categoría/género sin conflicto (`page_category_compatibility`).
     - Si falta evidencia observada (p. ej. `event_id_from_url` ausente, kickoff sin timestamp absoluto, competición no verificable o timeout), conserva `BWIN_MANUAL_LINK_PENDING_VALIDATION` en lugar de marcar conflicto falso o verificar positivamente.
   - **Reconciliación con descubrimiento automático (`prematch_runtime.discovery_for`):**
     - Cuando coexisten un descubrimiento automático almacenado y una vinculación manual para el mismo fixture, nunca ignora silenciosamente la discrepancia ni sustituye evidencia automática ya acreditada (`IDENTITY_VERIFIED_AUTOMATICALLY`). Revalida, registra la discrepancia en `failures` y persiste `auto_reconciliation` en `manual_bwin_links.json`.
   - **Nota sobre ejemplos documentados:** Las URLs con Event IDs `7990001` y `7990002` usadas en guías y tests sintéticos son **ficticias** y no deben usarse en ejecuciones reales.
   - `quote_capture_status`: `NOT_CAPTURED` | `CAPTURE_FAILED` | `CAPTURED_VERIFIED` (solo tras `bwin_market_preflight.py` + `verified_preflight`).
5. **Reintentos e invariancia:**
   - `--scrape-manual-bwin --confirm-external` y `--refresh-existing-odds` usan `--refresh-existing --immutable-capture` cuando existe `manifest.json`, escribiendo directorios `preflight` versionados sin alterar `fixtures.json`, `preselection.json`, `statistical_ranking` ni expedientes congelados.
   - Si el usuario intenta vincular un partido no `NS`, ya iniciado o excluido por juvenil/reserva, la operación falla cerrado (`ManualBwinLinkError`).
