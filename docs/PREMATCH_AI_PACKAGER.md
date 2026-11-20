# Empaquetador de IA PREMATCH

`prematch_ai_packager.py` prepara, para un solo Event ID de Bwin, el inventario y los prompts que hay que copiar en Grok, Gemini, Perplexity, Argon, DeepSeek y Codex. No llama a API-Football ni a Bwin, no ejecuta el motor y no elige apuestas.

El expediente es la carpeta del evento, normalmente `outputs/matches/<EVENT_ID>/`. La salida queda solo en `<expediente>/ai_packages/`. El `manifest.json` operativo se lee y no se modifica.

## Comando

```sh
python3 prematch_ai_packager.py \
  --event-id 7909230 \
  --expediente outputs/matches/7909230
```

Si una IA no está disponible, el resto se prepara igual:

```sh
python3 prematch_ai_packager.py \
  --event-id 7909230 \
  --expediente outputs/matches/7909230 \
  --unavailable perplexity,deepseek
```

`--project-root` indica la raíz que contiene `AGENTS.md` e `instructions/analysis_instructions.txt`. Si no se pasa y el expediente está en `outputs/matches/<EVENT_ID>`, se usa esa raíz.

## Qué escribe

- `adjuntos.md`: qué archivo hay que adjuntar a mano en cada IA, con ruta, SHA-256, clase temporal y versión declarada.
- `inventory.json` y `inventory.md`: presentes, ausentes y directorios omitidos (`logs`).
- `identidad_declarada.json`: nombres, fixture y kickoff solo si las fuentes locales concuerdan. Si no, `NO_CONCILIADO`.
- `prompts/COPIAR_<IA>_<EVENT_ID>.md`: texto breve para copiar.
- `resultados/<ia>/`: carpeta donde dejar la respuesta. No sustituye a `research/external/` ni a `inputs/`.
- `package_record.json`: hashes de lo generado en esa ejecución.

Los prompts citan `AGENTS.md` e `instructions/analysis_instructions.txt` por ruta. No copian su contenido. Tampoco copian cuotas, contexto ni informes.

## Clases temporales

- `ACTUAL_REFERENCIADO`: la ruta y el SHA-256 coinciden con una entrada del manifiesto operativo.
- `SNAPSHOT_HISTORICO`: está en `archives/`, `selection/`, `context_updates/` o lleva el nombre de una copia de respaldo. No se adjunta como vigente.
- `VIGENCIA_NO_VERIFICADA`: el archivo está, pero este módulo no acredita que siga vigente.

`validado` es siempre `false`. Que el archivo exista, o que el manifiesto lo referencie, no valida su contenido. Toda cuota queda como `PRECIO_PENDIENTE_DE_VERIFICACION`. Una marca `Captura:` o `Generado:` se guarda como autodeclarada.

Un archivo de otro Event ID se inventaría como `EVENTO_AJENO` y no entra en ningún prompt.

## Qué adjunta cada IA

| IA | Uso del prompt | Archivos que pide adjuntar |
|---|---|---|
| Grok | Actualidad, convocatoria, meteorología | Identidad |
| Gemini | Scouting táctico | Identidad, contexto API, mapa táctico, perfiles |
| Perplexity | Prensa fechada | Identidad |
| Argon | Auditoría de informes ya escritos | Identidad y los informes Grok, Gemini y Perplexity que existan |
| DeepSeek | Lectura local de estadística | Identidad, rendimiento, contexto ampliado, mapa y perfiles |
| Codex | Lectura del expediente | Cuotas, contexto, validación, motor, informe cualitativo y orden `CODEX` si existe |

Si falta un adjunto, el prompt lo declara y la ejecución sigue. Una reejecución con las mismas fuentes no crea otra copia: actualiza los mismos nombres. Un prompt ya editado a mano dentro de `ai_packages/prompts/` no se sobrescribe.

## Qué no integra

No importa la respuesta de una IA a `inputs/reporte_cualitativo_<EVENT_ID>.md`, no lanza `finish`, `research_kit` ni `generar_prompt_grok.py`, y no comprueba el precio vivo. Esas piezas siguen en el pipeline principal.

## Pruebas

```sh
python3 -m unittest test_prematch_ai_packager.py
```
