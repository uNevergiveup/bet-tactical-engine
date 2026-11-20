# Codex Final PREMATCH · un evento

Lee primero `outputs/matches/<EVENT_ID>/CODEX_<EVENT_ID>.md`, después el dossier canónico y `AGENTS.md`. Analiza exclusivamente el Event ID y Fixture ID declarados. No reejecutes adquisición, scraper, validador ni motor; no uses otros encuentros como evidencia.

Comprueba antes de analizar:

1. identidad de evento, fixture, equipos, categoría, competición y kickoff;
2. hashes del manifiesto y del dossier;
3. corte PREMATCH de cada fuente;
4. estado y frescura del snapshot Bwin final;
5. estado real del XI: oficial, completo solo según API, probable o desconocido;
6. qué patrones fueron validados por Python y cuáles siguen exploratorios.

La salida debe empezar por `DECISIÓN` y terminar con una sola selección defendible o `NO BET` / `SOLO CANDIDATO EXPLORATORIO`. No inventes probabilidad, cuota justa, EV ni stake. Explica evidencia favorable, contradicciones, incertidumbre, escenario contrario y dependencia del XI. Escribe solo en `outputs/matches/<EVENT_ID>/analysis/analisis_<EVENT_ID>.md`.

