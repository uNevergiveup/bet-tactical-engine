#!/usr/bin/env python3
"""Paquete local de archivos y prompts PREMATCH para varias IA.

Prepara un único Event ID de Bwin. No consulta API-Football ni Bwin, no
ejecuta el motor y no elige apuestas. No copia los archivos grandes del
expediente y no modifica el ``manifest.json`` operativo.

Uso::

    python3 prematch_ai_packager.py --event-id 7909230 \\
        --expediente outputs/matches/7909230

    python3 prematch_ai_packager.py --event-id 7909230 \\
        --expediente outputs/matches/7909230 \\
        --unavailable perplexity,deepseek
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PACKAGE_VERSION = '1.0.0'
PROVIDERS = ('grok', 'gemini', 'perplexity', 'argon', 'deepseek', 'codex')
HEAD_BYTES = 16 * 1024
JSON_PARSE_LIMIT = 32 * 1024 * 1024
SHA_RE = re.compile(r'[0-9a-fA-F]{64}')
BACKUP_NAME_RE = re.compile(r'\.\d{8}T\d{6,}Z\.[0-9a-f]{12}\.', re.I)
HEADER_EVENT_RE = re.compile(r'(?m)^Event ID:\s*(\d+)\s*$')
HISTORICAL_PARTS = frozenset({
    'archives', 'context_updates', 'previous_outputs', 'before_prepare', 'selection',
})
SKIP_DIRS = frozenset({'ai_packages', 'logs', '__pycache__', '.git'})
SENSITIVE_NAMES = frozenset({'credentials.json', 'secrets.json'})
URL_EVENT_RE = re.compile(r'-2:(\d+)\s*$')

# Prefijos largos primero: analisis_integral_ no debe clasificarse como analisis_.
NAME_PREFIXES = (
    'research_profiles_',
    'cuotas_bwin_', 'match_engine_', 'reporte_cualitativo_', 'validacion_',
    'matriz_evidencia_', 'ranking_probabilistico_', 'resumen_codex_',
    'mapa_tactico_', 'hipotesis_cruzadas_', 'patrones_jugadores_',
    'contexto_competitivo_', 'evidence_cache_', 'rendimiento_profundo_',
    'rendimiento_historico_', 'contexto_ampliado_', 'tendencias_profundas_',
    'tendencias_equipos_', 'jugadores_historicos_', 'profundidad_historica_',
    'analisis_integral_', 'analisis_', 'auditoria_calidad_', 'auditoria_adquisicion_',
    'auditoria_', 'cobertura_', 'codex_', 'copiar_', 'prompt_', 'fusion_',
    'perplexity_', 'deepseek_', 'gemini_', 'argon_', 'grok_',
)
FAMILY_BY_PREFIX = (
    ('research_profiles_', 'perfil_tactico_evento'),
    ('cuotas_bwin_', 'cuotas'),
    ('match_engine_', 'contexto_api'),
    ('reporte_cualitativo_', 'reporte_cualitativo'),
    ('validacion_', 'validacion'),
    ('matriz_evidencia_', 'motor'),
    ('ranking_probabilistico_', 'motor'),
    ('resumen_codex_', 'motor'),
    ('mapa_tactico_', 'mapa_tactico'),
    ('hipotesis_cruzadas_', 'motor'),
    ('patrones_jugadores_', 'motor'),
    ('contexto_competitivo_', 'motor'),
    ('evidence_cache_', 'estadisticas'),
    ('rendimiento_profundo_', 'estadisticas'),
    ('rendimiento_historico_', 'estadisticas'),
    ('contexto_ampliado_', 'estadisticas'),
    ('tendencias_profundas_', 'estadisticas'),
    ('tendencias_equipos_', 'estadisticas'),
    ('jugadores_historicos_', 'estadisticas'),
    ('profundidad_historica_', 'estadisticas'),
    ('analisis_integral_', 'analisis'),
    ('analisis_', 'analisis'),
    ('auditoria_', 'auditoria'),
    ('cobertura_', 'auditoria'),
    ('codex_', 'orden_codex'),
    ('copiar_', 'prompt_existente'),
    ('prompt_', 'prompt_existente'),
    ('fusion_', 'resultado_ia'),
    ('perplexity_', 'resultado_ia'),
    ('deepseek_', 'resultado_ia'),
    ('gemini_', 'resultado_ia'),
    ('argon_', 'resultado_ia'),
    ('grok_', 'resultado_ia'),
)
PROVIDER_FAMILIES = {
    'grok': frozenset({'identidad'}),
    'perplexity': frozenset({'identidad'}),
    'gemini': frozenset({'identidad', 'contexto_api', 'mapa_tactico', 'perfil_tactico', 'perfil_tactico_evento'}),
    'deepseek': frozenset({'identidad', 'estadisticas', 'mapa_tactico', 'perfil_tactico', 'perfil_tactico_evento'}),
    'argon': frozenset({'identidad', 'resultado_ia'}),
    'codex': frozenset({
        'cuotas', 'contexto_api', 'identidad', 'reporte_cualitativo', 'validacion',
        'motor', 'mapa_tactico', 'analisis', 'orden_codex', 'estadisticas', 'perfil_tactico_evento',
    }),
}
ARGON_INPUTS = frozenset({'grok', 'gemini', 'perplexity'})
EXCLUSION_TEXT = {
    'grok': 'No adjuntes cuotas, el contexto API ni snapshots de archives/ o selection/.',
    'perplexity': 'No adjuntes cuotas, el contexto API ni snapshots históricos.',
    'gemini': 'No adjuntes cuotas ni snapshots históricos. El contexto se adjunta por ruta, sin pegarlo.',
    'deepseek': 'No adjuntes cuotas. Si falta una estadística, declara el archivo ausente y no inventes cifras.',
    'argon': 'No adjuntes cuotas. No rellenes informes de otras IA que aún no existan.',
    'codex': 'No adjuntes snapshots históricos como si fueran la captura vigente.',
}
TASK_TEXT = {
    'grok': (
        'Tarea Grok: actualidad PREMATCH verificable de ambos clubes. Revisa canales oficiales, '
        'prensa fechada, convocatoria, bajas, XI oficial o probable, declaraciones, árbitro y '
        'meteorología horaria en el estadio. Separa hecho, inferencia y dato no verificado. '
        'Excluye lo publicado después del kickoff.'
    ),
    'gemini': (
        'Tarea Gemini: scouting táctico PREMATCH con las fuentes locales adjuntas y la '
        'investigación que tus instrucciones permanentes ya definen. No conviertas una formación '
        'nominal en una conducta demostrada. Formula mecanismos contrapuestos y cita la ruta del '
        'archivo del que sale cada cifra.'
    ),
    'perplexity': (
        'Tarea Perplexity: recolección de prensa PREMATCH, comunicados y crónicas fechadas de '
        'ambos equipos. No interpretes precios. Entrega fuentes con fecha de publicación y '
        'separa lo que no pudiste abrir.'
    ),
    'argon': (
        'Tarea Argon: audita los informes independientes ya escritos y no completes sus huecos. '
        'Conserva Event ID, Fixture ID y kickoff declarado. Clasifica cada afirmación material '
        'como CONFIRMADO, PROBABLE, INFERIDO o NO_VERIFICADO solo cuando la fuente del informe '
        'lo permita; si no, déjala en NO_VERIFICADO.'
    ),
    'deepseek': (
        'Tarea DeepSeek: lectura local de la estadística y los perfiles tácticos ya presentes. '
        'No navegues y no sustituyas a Codex. Resume muestras, denominadores y vacíos. Una cifra '
        'que no esté en un archivo adjunto no existe para este encargo.'
    ),
    'codex': (
        'Tarea Codex: prepara la lectura del expediente de este Event ID. Si existe la orden '
        'CODEX del evento, léela en su ruta y síguela; este mensaje no copia su contenido. '
        'Aplica las instrucciones permanentes en sus rutas. No reejecutes scraper, generador '
        'de contexto, validador ni motor.'
    ),
}
OUTPUT_NAMES = {
    'grok': 'grok_{event_id}.md',
    'gemini': 'gemini_{event_id}.md',
    'perplexity': 'perplexity_{event_id}.md',
    'argon': 'fusion_{event_id}.md',
    'deepseek': 'deepseek_{event_id}.md',
    'codex': 'analisis_{event_id}.md',
}
NOTE_VALIDATION = (
    'La existencia de un archivo no lo valida. Coincidir con el manifiesto operativo '
    'solo indica que esa ruta y ese SHA-256 están referenciados. No acredita vigencia, '
    'corte prepartido ni calidad del contenido.'
)


class PackagerError(ValueError):
    """El expediente o la salida no permiten empaquetar este Event ID."""


@dataclass
class SourceFile:
    relative_path: str | None
    absolute_path: str
    family: str
    temporal_class: str
    event_binding: str
    sha256: str | None
    size_bytes: int
    filesystem_mtime_utc: str | None
    declared_timestamp: str | None = None
    declared_timestamp_kind: str | None = None
    declared_version: str | None = None
    manifest_relation: str = 'SIN_MANIFIESTO'
    price: str | None = None
    notes: list[str] = field(default_factory=list)
    outside_expediente: bool = False

    @property
    def name(self) -> str:
        return Path(self.relative_path or self.absolute_path).name

    def to_dict(self) -> dict:
        return {
            'ruta_relativa': self.relative_path,
            'ruta_absoluta': self.absolute_path,
            'familia': self.family,
            'clase_temporal': self.temporal_class,
            'vinculo_evento': self.event_binding,
            'sha256': self.sha256,
            'bytes': self.size_bytes,
            'mtime_filesystem_utc': self.filesystem_mtime_utc,
            'mtime_no_acredita_captura': True,
            'marca_temporal_declarada': self.declared_timestamp,
            'marca_temporal_declarada_tipo': self.declared_timestamp_kind,
            'marca_temporal_fiabilidad': (
                'AUTODECLARADA_NO_VERIFICADA' if self.declared_timestamp else None
            ),
            'version_declarada': self.declared_version,
            'validado': False,
            'precio': self.price,
            'relacion_manifiesto': self.manifest_relation,
            'fuera_del_expediente': self.outside_expediente,
            'copiado': False,
            'notas': list(self.notes),
        }


@dataclass
class ProviderPlan:
    provider: str
    available: bool
    attach: list[SourceFile]
    missing: list[str]
    prompt_path: Path | None = None
    kept_foreign_prompt: Path | None = None


@dataclass
class PackageResult:
    event_id: str
    expediente: Path
    output_dir: Path
    inventory_path: Path
    adjuntos_path: Path
    prompts: dict[str, Path | None]
    missing: list[str]
    unavailable: list[str]
    coverage: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def marker_line(provider: str, event_id: str) -> str:
    return f'<!-- prematch_ai_packager: {provider} {event_id} -->'


def is_our_prompt(path: Path, provider: str, event_id: str) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    with path.open(encoding='utf-8') as handle:
        return handle.readline().strip() == marker_line(provider, event_id)


def expected_paths(event_id: str) -> list[tuple[str, str]]:
    event = event_id
    return [
        ('cuotas', f'inputs/cuotas_bwin_{event}.txt'),
        ('contexto_txt', f'inputs/match_engine_{event}.txt'),
        ('contexto_json', f'inputs/match_engine_{event}.json'),
        ('identidad', 'inputs/match_identity.json'),
        ('reporte_cualitativo', f'inputs/reporte_cualitativo_{event}.md'),
        ('validacion_json', f'validation/validacion_{event}.json'),
        ('validacion_md', f'validation/validacion_{event}.md'),
        ('matriz', f'probabilistic/matriz_evidencia_{event}.json'),
        ('ranking', f'probabilistic/ranking_probabilistico_{event}.json'),
        ('resumen_codex', f'probabilistic/resumen_codex_{event}.md'),
        ('mapa_tactico', f'probabilistic/mapa_tactico_{event}.md'),
        ('hipotesis', f'probabilistic/hipotesis_cruzadas_{event}.md'),
        ('patrones_jugadores', f'probabilistic/patrones_jugadores_{event}.md'),
        ('contexto_competitivo_md', f'probabilistic/contexto_competitivo_{event}.md'),
        ('contexto_competitivo_json', f'probabilistic/contexto_competitivo_{event}.json'),
        ('analisis', f'analysis/analisis_{event}.md'),
        ('orden_codex', f'CODEX_{event}.md'),
        ('gemini', f'research/external/gemini_{event}.md'),
        ('grok', f'research/external/grok_{event}.md'),
        ('fusion', f'research/external/fusion_{event}.md'),
        ('perplexity', f'research/external/perplexity_{event}.md'),
        ('rendimiento_profundo', f'analysis/deep_v1042/rendimiento_profundo_{event}.json'),
        ('contexto_ampliado', f'analysis/deep_v1042/contexto_ampliado_{event}.json'),
    ]


def provider_hints(event_id: str) -> dict[str, list[str]]:
    event = event_id
    codex = [
        rel for _key, rel in expected_paths(event)
        if not rel.startswith('research/external/')
    ]
    return {
        'grok': ['inputs/match_identity.json'],
        'perplexity': ['inputs/match_identity.json'],
        'gemini': [
            'inputs/match_identity.json',
            f'inputs/match_engine_{event}.txt',
            f'inputs/match_engine_{event}.json',
            'config/tactical_profiles/profiles.json',
        ],
        'deepseek': [
            'inputs/match_identity.json',
            f'analysis/deep_v1042/rendimiento_profundo_{event}.json',
            f'analysis/deep_v1042/contexto_ampliado_{event}.json',
            'config/tactical_profiles/profiles.json',
        ],
        'argon': [
            'inputs/match_identity.json',
            f'research/external/grok_{event}.md',
            f'research/external/gemini_{event}.md',
            f'research/external/perplexity_{event}.md',
        ],
        'codex': codex,
    }


def parse_unavailable(raw: str | None) -> list[str]:
    if not raw:
        return []
    found: list[str] = []
    for piece in raw.split(','):
        name = piece.strip().casefold()
        if not name:
            continue
        if name not in PROVIDERS:
            known = ', '.join(PROVIDERS)
            raise PackagerError(f'PROVEEDOR_DESCONOCIDO: {name}. Proveedores: {known}')
        if name not in found:
            found.append(name)
    return found


def validate_event_id(event_id: str) -> str:
    event_id = str(event_id).strip()
    if not re.fullmatch(r'\d{6,}', event_id):
        raise PackagerError('EVENT_ID_INVALIDO: se espera un Event ID Bwin numérico de al menos 6 dígitos')
    return event_id


def resolve_expediente(path: Path, event_id: str) -> Path:
    expediente = Path(path).expanduser()
    if not expediente.is_dir():
        raise PackagerError(f'EXPEDIENTE_AUSENTE: {expediente}')
    expediente = expediente.resolve()
    if expediente.name in {'matches', 'outputs', 'archives'}:
        raise PackagerError('EXPEDIENTE_NO_ES_UN_EVENTO: indica la carpeta del Event ID, no el directorio padre')
    if expediente.name.isdigit() and expediente.name != event_id:
        raise PackagerError(f'EXPEDIENTE_DE_OTRO_EVENTO: la carpeta es {expediente.name} y el Event ID es {event_id}')
    parts = expediente.parts
    if 'matches' in parts:
        index = parts.index('matches')
        if index + 1 < len(parts) and parts[index + 1].isdigit() and parts[index + 1] != event_id:
            raise PackagerError('EXPEDIENTE_DE_OTRO_EVENTO: la ruta está dentro de otro Event ID')
    return expediente


def resolve_output(expediente: Path, output_dir: Path | None) -> Path:
    target = expediente / 'ai_packages' if output_dir is None else Path(output_dir).expanduser()
    if not target.is_absolute():
        target = expediente / target
    target = target.resolve()
    if target == expediente:
        raise PackagerError('OUTPUT_ES_EL_EXPEDIENTE: el paquete debe escribirse en ai_packages')
    if not target.is_relative_to(expediente):
        raise PackagerError('OUTPUT_FUERA_DEL_EXPEDIENTE')
    relative = target.relative_to(expediente).as_posix()
    if relative != 'ai_packages' and not relative.startswith('ai_packages/'):
        raise PackagerError('OUTPUT_SOLO_EN_AI_PACKAGES')
    return target


def resolve_project_root(expediente: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        root = Path(explicit).expanduser().resolve()
        if not root.is_dir():
            raise PackagerError(f'PROJECT_ROOT_AUSENTE: {root}')
        return root
    if expediente.parent.name == 'matches' and expediente.parent.parent.name == 'outputs':
        candidate = expediente.parent.parent.parent
        if (candidate / 'AGENTS.md').is_file():
            return candidate
    return Path(__file__).resolve().parent


def read_head(path: Path) -> str:
    with path.open('rb') as handle:
        raw = handle.read(HEAD_BYTES)
    return raw.decode('utf-8-sig', errors='replace')


def load_json_document(path: Path, size: int) -> tuple[dict | None, str | None]:
    if path.suffix.lower() != '.json' or size > JSON_PARSE_LIMIT:
        return None, 'JSON_NO_ANALIZADO_POR_TAMANO' if path.suffix.lower() == '.json' and size > JSON_PARSE_LIMIT else None
    try:
        payload = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, 'JSON_NO_LEIBLE'
    if not isinstance(payload, dict):
        return None, 'JSON_RAIZ_NO_OBJETO'
    return payload, None


def header_value(head: str, label: str) -> str | None:
    match = re.search(rf'(?m)^{re.escape(label)}:\s*(.+?)\s*$', head)
    return match.group(1).strip() if match else None


def filename_event_ids(name: str) -> set[str]:
    lowered = name.casefold()
    found: set[str] = set()
    for prefix in sorted(NAME_PREFIXES, key=len, reverse=True):
        if lowered.startswith(prefix):
            tail = lowered[len(prefix):]
            match = re.match(r'(\d{6,})', tail)
            if match:
                found.add(match.group(1))
            break
    return found


def json_event_ids(payload: dict | None) -> set[str]:
    found: set[str] = set()
    if not isinstance(payload, dict):
        return found

    def take(value: object) -> None:
        if isinstance(value, (str, int)) and re.fullmatch(r'\d{6,}', str(value)):
            found.add(str(value))

    for key in ('event_id', 'bwin_event_id', 'event_id_bwin'):
        take(payload.get(key))
    match = payload.get('match')
    if isinstance(match, dict):
        take(match.get('event_id'))
    return found


def classify_family(path: Path, relative: str | None) -> str:
    name = path.name.casefold()
    if name == 'match_identity.json':
        return 'identidad'
    if name == 'selected_match.json':
        return 'seleccion'
    if name == 'profiles.json':
        return 'perfil_tactico'
    if name == 'manifest.json' and relative == 'manifest.json':
        return 'manifiesto_operativo'
    if name == 'manifest.json':
        return 'manifiesto_archivado'
    for prefix, family in FAMILY_BY_PREFIX:
        if name.startswith(prefix):
            return family
    return 'otro'


def historical_location(relative: str | None, name: str) -> bool:
    if BACKUP_NAME_RE.search(name):
        return True
    if not relative:
        return False
    return any(part in HISTORICAL_PARTS for part in Path(relative).parts)


def manifest_sha_index(manifest: dict | None, expediente: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    if not isinstance(manifest, dict):
        return found

    def visit(value: object) -> None:
        if isinstance(value, dict):
            raw_path = value.get('path')
            digest = value.get('sha256')
            if isinstance(raw_path, str) and isinstance(digest, str) and SHA_RE.fullmatch(digest):
                candidate = Path(raw_path).expanduser()
                if not candidate.is_absolute():
                    candidate = expediente / candidate
                try:
                    key = str(candidate.resolve())
                except OSError:
                    key = str(candidate.absolute())
                found[key] = digest.lower()
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(manifest.get('inputs'))
    return found


def load_operational_manifest(expediente: Path, event_id: str) -> tuple[dict | None, str | None]:
    path = expediente / 'manifest.json'
    if not path.is_file() or path.is_symlink():
        return None, None
    try:
        payload = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackagerError(f'MANIFIESTO_ILEGIBLE: {path}') from exc
    if not isinstance(payload, dict):
        raise PackagerError('MANIFIESTO_ILEGIBLE: la raíz no es un objeto')
    declared = payload.get('event_id')
    if declared is not None and str(declared) != event_id:
        raise PackagerError(
            f'MANIFIESTO_DE_OTRO_EVENTO: declara {declared} y se pidió {event_id}'
        )
    url = payload.get('url')
    if isinstance(url, str):
        match = URL_EVENT_RE.search(url.strip())
        if match and match.group(1) != event_id:
            raise PackagerError('MANIFIESTO_DE_OTRO_EVENTO: la URL pertenece a otro Event ID')
    return payload, sha256_file(path)


def bind_event(name: str, head: str, payload: dict | None, event_id: str, family: str) -> str:
    if family == 'perfil_tactico':
        return 'GLOBAL'
    ids = set(filename_event_ids(name))
    ids.update(HEADER_EVENT_RE.findall(head))
    ids.update(json_event_ids(payload))
    foreign = {item for item in ids if item != event_id}
    if foreign:
        return 'EVENTO_AJENO'
    if event_id in ids:
        return 'ESTE_EVENTO'
    if family == 'manifiesto_operativo':
        return 'ESTE_EVENTO'
    return 'NO_DETERMINADO'


def declared_version(payload: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    values: list[str] = []
    for key in ('schema_version', 'version'):
        value = payload.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            text = str(value).strip()
            if text not in values:
                values.append(text)
    return ' / '.join(values) if values else None


def declared_clock(head: str, payload: dict | None) -> tuple[str | None, str | None]:
    for label in ('Captura', 'Generado'):
        value = header_value(head, label)
        if value:
            return value, label
    if isinstance(payload, dict):
        for key in ('generated_at', 'captured_at', 'captured_at_utc'):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), key
    return None, None


def temporal_class(relative: str | None, name: str, relation: str) -> str:
    """El manifiesto manda sobre la carpeta: una ruta archivada pero referenciada sigue siendo el input fijado."""
    if relation == 'COINCIDE':
        return 'ACTUAL_REFERENCIADO'
    if historical_location(relative, name):
        return 'SNAPSHOT_HISTORICO'
    return 'VIGENCIA_NO_VERIFICADA'


def iter_source_paths(expediente: Path) -> tuple[list[Path], list[str]]:
    files: list[Path] = []
    omitted: list[str] = []
    for directory, dirnames, filenames in os.walk(expediente, followlinks=False):
        current = Path(directory)
        kept: list[str] = []
        for dirname in sorted(dirnames):
            if dirname in SKIP_DIRS or dirname.startswith('.'):
                # ai_packages es la salida de este módulo: no forma parte del inventario
                # y no debe cambiar el resultado entre la primera ejecución y la siguiente.
                if dirname != 'ai_packages':
                    omitted.append(current.joinpath(dirname).relative_to(expediente).as_posix())
                continue
            kept.append(dirname)
        dirnames[:] = kept
        for name in sorted(filenames):
            if name.startswith('.') or name.endswith('.tmp') or name.casefold() in SENSITIVE_NAMES:
                continue
            path = current / name
            if path.is_symlink() or not path.is_file():
                continue
            files.append(path)
    return files, omitted


def describe_file(
    path: Path,
    expediente: Path,
    event_id: str,
    sha_index: dict[str, str],
    has_manifest: bool,
) -> SourceFile:
    relative = path.relative_to(expediente).as_posix()
    size = path.stat().st_size
    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    digest = sha256_file(path)
    head = read_head(path) if path.suffix.lower() in {'.txt', '.md', '.json', '.csv'} or size <= HEAD_BYTES else ''
    payload, json_note = load_json_document(path, size)
    family = classify_family(path, relative)
    binding = bind_event(path.name, head, payload, event_id, family)
    resolved = str(path.resolve())
    if not has_manifest:
        relation = 'SIN_MANIFIESTO'
    elif resolved in sha_index:
        relation = 'COINCIDE' if sha_index[resolved] == digest else 'HASH_DISTINTO'
    else:
        relation = 'RUTA_NO_REFERENCIADA'
    if family in {'manifiesto_operativo', 'manifiesto_archivado', 'perfil_tactico'}:
        relation = 'NO_APLICA' if family != 'manifiesto_operativo' else relation
    notes: list[str] = []
    if json_note:
        notes.append(json_note)
    if relation == 'COINCIDE' and historical_location(relative, path.name):
        notes.append('RUTA_EN_ARCHIVO_PERO_REFERENCIADA')
    if relation == 'HASH_DISTINTO':
        notes.append('HASH_DISTINTO_DEL_MANIFIESTO')
    clock, clock_kind = declared_clock(head, payload)
    price = 'PRECIO_PENDIENTE_DE_VERIFICACION' if family == 'cuotas' else None
    return SourceFile(
        relative_path=relative,
        absolute_path=resolved,
        family=family,
        temporal_class=temporal_class(relative, path.name, relation if family != 'perfil_tactico' else 'RUTA_NO_REFERENCIADA'),
        event_binding=binding,
        sha256=digest,
        size_bytes=size,
        filesystem_mtime_utc=modified,
        declared_timestamp=clock,
        declared_timestamp_kind=clock_kind,
        declared_version=declared_version(payload),
        manifest_relation=relation if family != 'perfil_tactico' else 'NO_APLICA',
        price=price,
        notes=notes,
    )


def external_profile(project_root: Path) -> SourceFile | None:
    path = project_root / 'config' / 'tactical_profiles' / 'profiles.json'
    if not path.is_file() or path.is_symlink():
        return None
    size = path.stat().st_size
    payload, json_note = load_json_document(path, size)
    notes = [json_note] if json_note else []
    notes.append('CATALOGO_GLOBAL_NO_ESPECIFICO_DEL_EVENTO')
    return SourceFile(
        relative_path='config/tactical_profiles/profiles.json',
        absolute_path=str(path.resolve()),
        family='perfil_tactico',
        temporal_class='VIGENCIA_NO_VERIFICADA',
        event_binding='GLOBAL',
        sha256=sha256_file(path),
        size_bytes=size,
        filesystem_mtime_utc=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        declared_version=declared_version(payload),
        manifest_relation='NO_APLICA',
        notes=notes,
        outside_expediente=True,
    )


def instruction_records(project_root: Path) -> list[dict]:
    records = []
    for label, relative in (
        ('AGENTS.md', 'AGENTS.md'),
        ('analysis_instructions', 'instructions/analysis_instructions.txt'),
    ):
        path = project_root / relative
        exists = path.is_file() and not path.is_symlink()
        records.append({
            'id': label,
            'ruta_relativa': relative,
            'ruta_absoluta': str(path.resolve()),
            'existe': exists,
            'sha256': sha256_file(path) if exists else None,
            'se_copia_en_el_prompt': False,
            'validado': False,
        })
    return records


def role_of(source: SourceFile) -> str:
    if source.family == 'perfil_tactico':
        return 'perfil_tactico'
    return f'{source.family}:{source.name.casefold()}'


def is_canonical_path(source: SourceFile, event_id: str) -> bool:
    if source.family == 'perfil_tactico':
        return True
    expected = {relative for _key, relative in expected_paths(event_id)}
    expected.add('inputs/match_identity.json')
    return source.relative_path in expected


def select_attachments(
    files: list[SourceFile],
    provider: str,
    event_id: str,
) -> list[SourceFile]:
    groups: dict[str, list[SourceFile]] = {}
    allowed = PROVIDER_FAMILIES[provider]
    for source in files:
        if source.family not in allowed or source.event_binding not in {'ESTE_EVENTO', 'GLOBAL'}:
            continue
        if source.temporal_class == 'SNAPSHOT_HISTORICO':
            continue
        if provider == 'argon' and source.family == 'resultado_ia':
            stem = source.name.casefold()
            if not any(stem == f'{name}_{event_id}.md' for name in ARGON_INPUTS):
                continue
        groups.setdefault(role_of(source), []).append(source)
    chosen: list[SourceFile] = []
    for group in groups.values():
        referenced = [item for item in group if item.temporal_class == 'ACTUAL_REFERENCIADO']
        pool = referenced or list(group)
        if len(pool) > 1:
            canonical = [item for item in pool if is_canonical_path(item, event_id)]
            if len(canonical) == 1:
                pool = canonical
        if len(pool) == 1:
            chosen.append(pool[0])
    chosen.sort(key=lambda item: (item.relative_path or item.absolute_path))
    return chosen


def missing_for_provider(provider: str, event_id: str, attached: list[SourceFile], files: list[SourceFile]) -> list[str]:
    attached_keys = {(item.relative_path or '') for item in attached}
    notes = []
    for relative in provider_hints(event_id)[provider]:
        if relative in attached_keys:
            continue
        name = Path(relative).name.casefold()
        historical = any(
            item.name.casefold() == name and item.temporal_class == 'SNAPSHOT_HISTORICO'
            and item.event_binding == 'ESTE_EVENTO'
            for item in files
        )
        if historical:
            notes.append(f'{relative} (ausente en la ruta canónica; hay snapshot histórico, no adjuntar como vigente)')
        else:
            notes.append(relative)
    return notes


def same_value(left: str, right: str) -> bool:
    if left.casefold() == right.casefold():
        return True
    try:
        first = datetime.fromisoformat(left.replace('Z', '+00:00'))
        second = datetime.fromisoformat(right.replace('Z', '+00:00'))
    except ValueError:
        return False
    if first.tzinfo is None or second.tzinfo is None:
        return False
    return first.astimezone(timezone.utc) == second.astimezone(timezone.utc)


def add_declaration(bucket: dict[str, list[dict]], field_name: str, value: object, source: str, historical: bool) -> None:
    if value is None:
        return
    text = str(value).strip()
    if not text:
        return
    rows = bucket.setdefault(field_name, [])
    if any(row['valor'] == text and row['ruta'] == source and row['historico'] is historical for row in rows):
        return
    rows.append({'valor': text, 'ruta': source, 'historico': historical})


def declarations_from_source(source: SourceFile, expediente: Path) -> dict[str, list[dict]]:
    bucket: dict[str, list[dict]] = {}
    if source.event_binding != 'ESTE_EVENTO':
        return bucket
    path = Path(source.absolute_path)
    historical = source.temporal_class == 'SNAPSHOT_HISTORICO'
    origin = source.relative_path or source.absolute_path
    payload, _note = load_json_document(path, source.size_bytes)
    head = read_head(path) if path.suffix.lower() in {'.txt', '.md', '.json', '.csv'} else ''
    if isinstance(payload, dict):
        match = payload.get('match') if isinstance(payload.get('match'), dict) else {}
        fixture = payload.get('fixture') if isinstance(payload.get('fixture'), dict) else {}
        add_declaration(bucket, 'fixture_id', payload.get('api_fixture_id') or payload.get('fixture_id') or fixture.get('fixture_id'), origin, historical)
        home = payload.get('home')
        away = payload.get('away')
        if isinstance(home, dict):
            add_declaration(bucket, 'home', home.get('bwin_name') or home.get('api_name'), origin, historical)
        elif isinstance(home, str):
            add_declaration(bucket, 'home', home, origin, historical)
        if isinstance(away, dict):
            add_declaration(bucket, 'away', away.get('bwin_name') or away.get('api_name'), origin, historical)
        elif isinstance(away, str):
            add_declaration(bucket, 'away', away, origin, historical)
        add_declaration(bucket, 'home', match.get('home_name_raw') or fixture.get('home_name'), origin, historical)
        add_declaration(bucket, 'away', match.get('away_name_raw') or fixture.get('away_name'), origin, historical)
        add_declaration(bucket, 'kickoff', payload.get('kickoff_utc') or payload.get('kickoff_madrid') or fixture.get('date'), origin, historical)
        add_declaration(bucket, 'competicion', payload.get('league_name') or payload.get('league') or fixture.get('league_name') or match.get('competition_display'), origin, historical)
    add_declaration(bucket, 'fixture_id', header_value(head, 'Fixture ID'), origin, historical)
    add_declaration(bucket, 'home', header_value(head, 'Home Bwin'), origin, historical)
    add_declaration(bucket, 'away', header_value(head, 'Away Bwin'), origin, historical)
    add_declaration(bucket, 'kickoff', header_value(head, 'Date UTC'), origin, historical)
    add_declaration(bucket, 'competicion', header_value(head, 'League'), origin, historical)
    partido = header_value(head, 'Partido')
    if partido and ' vs ' in partido:
        home_name, away_name = partido.split(' vs ', 1)
        add_declaration(bucket, 'home', home_name, origin, historical)
        add_declaration(bucket, 'away', away_name, origin, historical)
    return bucket


def reconcile_field(rows: list[dict]) -> dict:
    current = [row for row in rows if not row['historico']]
    pool = current or rows
    if not pool:
        return {'valor': None, 'estado': 'AUSENTE', 'verificacion': 'NO_VERIFICADO', 'validado': False, 'fuentes': [], 'snapshots_que_difieren': []}
    unique: list[dict] = []
    for row in pool:
        if not any(same_value(row['valor'], item['valor']) for item in unique):
            unique.append(row)
    sources = [{'valor': row['valor'], 'ruta': row['ruta'], 'historico': row['historico']} for row in pool]
    if len(unique) > 1:
        return {
            'valor': None, 'estado': 'NO_CONCILIADO', 'verificacion': 'NO_VERIFICADO',
            'validado': False, 'fuentes': sources, 'snapshots_que_difieren': [],
        }
    value = unique[0]['valor']
    if not current:
        estado = 'SOLO_SNAPSHOT_HISTORICO'
    elif len(pool) == 1:
        estado = 'FUENTE_UNICA_NO_VERIFICADA'
    else:
        estado = 'CONCORDANTE_NO_VERIFICADO'
    differ = [
        {'valor': row['valor'], 'ruta': row['ruta']}
        for row in rows if row['historico'] and not same_value(row['valor'], value)
    ]
    return {
        'valor': value, 'estado': estado, 'verificacion': 'NO_VERIFICADO', 'validado': False,
        'fuentes': sources, 'snapshots_que_difieren': differ,
    }


def build_identity(event_id: str, files: list[SourceFile], expediente: Path) -> dict:
    merged: dict[str, list[dict]] = {}
    for source in files:
        for key, rows in declarations_from_source(source, expediente).items():
            merged.setdefault(key, []).extend(rows)
    fields = {key: reconcile_field(merged.get(key, [])) for key in ('fixture_id', 'home', 'away', 'kickoff', 'competicion')}
    return {
        'event_id': event_id,
        'verificacion': 'NO_VERIFICADO',
        'validado': False,
        'campos': fields,
    }


def format_source_line(source: SourceFile) -> str:
    version = source.declared_version or 'N/D'
    clock = source.declared_timestamp or 'N/D'
    pieces = [
        source.absolute_path,
        source.temporal_class,
        f'manifiesto={source.manifest_relation}',
        f'sha256={source.sha256}',
        'validado=false',
        f'version={version}',
        f'marca_declarada={clock}',
    ]
    if source.price:
        pieces.append(f'precio={source.price}')
    return ' | '.join(pieces)


def identity_lines(card: dict) -> list[str]:
    labels = (
        ('fixture_id', 'Fixture API-Football'),
        ('home', 'Local'),
        ('away', 'Visitante'),
        ('kickoff', 'Kickoff declarado'),
        ('competicion', 'Competición declarada'),
    )
    lines = [f"Event ID Bwin: {card['event_id']}"]
    for key, label in labels:
        field_info = card['campos'][key]
        if field_info['estado'] == 'AUSENTE':
            lines.append(f'{label}: AUSENTE')
        elif field_info['estado'] == 'NO_CONCILIADO':
            lines.append(f'{label}: NO_CONCILIADO')
            for source in field_info['fuentes']:
                lines.append(f"- {source['valor']} ({source['ruta']})")
        else:
            lines.append(f"{label}: {field_info['valor']} [{field_info['estado']}]")
    lines.append('Identidad: NO_VERIFICADO. Este paquete no certifica equipos, horario ni XI.')
    lines.append('Corte prepartido: NO_ACREDITADO.')
    return lines


def render_prompt(
    provider: str,
    event_id: str,
    card: dict,
    plan: ProviderPlan,
    instructions: list[dict],
    inventory_path: Path,
) -> str:
    lines = [
        marker_line(provider, event_id),
        '',
        f'Copia este encargo en {provider}. Aplica las instrucciones permanentes ya existentes; no están reproducidas aquí.',
        '',
    ]
    for record in instructions:
        if record['existe']:
            lines.append(f"- {record['ruta_relativa']}: {record['ruta_absoluta']}")
        else:
            lines.append(f"- {record['ruta_relativa']}: AUSENTE en {record['ruta_absoluta']}. No inventes su contenido.")
    lines.extend(['', *identity_lines(card), '', TASK_TEXT[provider], ''])
    if provider == 'codex':
        lines.append(
            'Este paquete no contiene una selección ni una cuota justa. '
            'La decisión, si procede, queda definida solo por las instrucciones permanentes.'
        )
    else:
        lines.append('No generes picks, cuotas, probabilidades, valor esperado ni stake.')
    lines.extend([
        'No trates un archivo como validado porque exista o porque el manifiesto lo referencie.',
        'Una cuota capturada queda como PRECIO_PENDIENTE_DE_VERIFICACION.',
        '',
        'Archivos a adjuntar manualmente, sin que este mensaje los incluya:',
    ])
    if plan.attach:
        lines.extend(f'- {format_source_line(item)}' for item in plan.attach)
    else:
        lines.append('- NINGUNO. No inventes datos locales que no estén listados.')
    if plan.missing:
        lines.append('')
        lines.append('Ausentes para este encargo; continúa sin ellos y decláralos:')
        lines.extend(f'- {item}' for item in plan.missing)
    output_name = OUTPUT_NAMES[provider].format(event_id=event_id)
    lines.extend([
        '',
        EXCLUSION_TEXT[provider],
        f'Salida prevista: {output_name}. Déjala en la carpeta de resultados de {provider}; no sustituyas inputs/ ni manifest.json.',
        f'Inventario con hashes y clases temporales: {inventory_path}',
        '',
    ])
    return '\n'.join(lines)


def render_adjuntos(
    event_id: str,
    plans: list[ProviderPlan],
    command: str,
    coverage: str,
    output_dir: Path,
) -> str:
    lines = [
        f'# Adjuntos manuales — Event ID {event_id}',
        '',
        'Este paquete no copia los archivos del expediente y no genera una apuesta.',
        'Existir no significa validado. Las cuotas son snapshots: PRECIO_PENDIENTE_DE_VERIFICACION.',
        f'Cobertura de rutas canónicas: {coverage}.',
        '',
        f'Reejecución: `{command}`',
        '',
    ]
    for plan in plans:
        state = 'DISPONIBLE' if plan.available else 'NO_DISPONIBLE'
        lines.extend([
            f'## {plan.provider.capitalize()}',
            '',
            f'Disponibilidad: {state}',
            f'Carpeta de resultados: `{output_dir / "resultados" / plan.provider}`',
            '',
        ])
        if not plan.available:
            lines.extend(['No se genera prompt. El resto del paquete continúa.', ''])
        lines.extend(['### Adjuntar', ''])
        if plan.available and plan.attach:
            lines.extend(f'- {format_source_line(item)}' for item in plan.attach)
        elif plan.available:
            lines.append('- Ningún archivo local cumple las condiciones de este encargo.')
        else:
            lines.append('- No aplica mientras la IA esté marcada como no disponible.')
        lines.extend(['', '### Ausentes', ''])
        if plan.missing:
            lines.extend(f'- {item}' for item in plan.missing)
        else:
            lines.append('- Ninguno de la lista prevista.')
        lines.extend(['', '### Límite', '', EXCLUSION_TEXT[plan.provider], ''])
        if plan.prompt_path:
            lines.append(f'Prompt: `{plan.prompt_path}`')
        elif plan.kept_foreign_prompt:
            lines.append(f'Prompt ajeno conservado, sin sobrescribir: `{plan.kept_foreign_prompt}`')
        else:
            lines.append('Prompt: no generado.')
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def render_inventory_md(summary: dict, files: list[SourceFile]) -> str:
    lines = [
        f"# Inventario PREMATCH IA {summary['event_id']}",
        '',
        summary['nota_validacion'],
        '',
        f"- Generado UTC: {summary['generado_utc']}",
        f"- Versión del empaquetador: {summary['packager_version']}",
        f"- Cobertura: {summary['cobertura']}",
        f"- Presentes: {summary['presentes']}",
        f"- Actuales referenciados: {summary['actuales_referenciados']}",
        f"- Snapshots históricos: {summary['snapshots_historicos']}",
        f"- Vigencia no verificada: {summary['vigencia_no_verificada']}",
        f"- Evento ajeno: {summary['evento_ajeno']}",
        f"- Validados: {summary['validados']}",
        '',
        '## Ausentes canónicos',
        '',
    ]
    if summary['ausentes']:
        lines.extend(f'- {item}' for item in summary['ausentes'])
    else:
        lines.append('- Ninguno.')
    lines.extend(['', '## Archivos', ''])
    for source in files:
        relative = source.relative_path or source.absolute_path
        lines.append(
            f"- {relative} | {source.family} | {source.temporal_class} | {source.event_binding} | "
            f"sha256={source.sha256} | version={source.declared_version or 'N/D'} | "
            f"mtime_fs={source.filesystem_mtime_utc} | validado=false"
        )
    lines.append('')
    return '\n'.join(lines)


def write_bytes(path: Path, content: bytes, output_dir: Path, expediente: Path) -> bool:
    if path.name == 'manifest.json' or path.absolute() == (expediente / 'manifest.json').absolute():
        raise PackagerError('MANIFIESTO_OPERATIVO_PROTEGIDO')
    parent = path.parent.resolve()
    root = output_dir.resolve()
    if parent != root and not parent.is_relative_to(root):
        raise PackagerError(f'ESCRITURA_FUERA_DEL_PAQUETE: {path}')
    if path.is_file() and not path.is_symlink() and path.read_bytes() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def write_text(path: Path, content: str, output_dir: Path, expediente: Path) -> bool:
    return write_bytes(path, content.encode('utf-8'), output_dir, expediente)


def manifest_fingerprint(expediente: Path) -> tuple[bytes | None, int | None]:
    path = expediente / 'manifest.json'
    if not path.exists():
        return None, None
    stat = path.stat()
    return path.read_bytes(), stat.st_mtime_ns


def ensure_manifest_untouched(expediente: Path, before: tuple[bytes | None, int | None]) -> None:
    after = manifest_fingerprint(expediente)
    if after != before:
        raise PackagerError('MANIFIESTO_OPERATIVO_ALTERADO')


def place_prompt(directory: Path, provider: str, event_id: str, content: str, output_dir: Path, expediente: Path) -> tuple[Path | None, Path | None]:
    primary = directory / f'COPIAR_{provider.upper()}_{event_id}.md'
    target = primary
    kept = None
    if primary.exists() and not is_our_prompt(primary, provider, event_id):
        kept = primary
        target = primary.with_name(f'{primary.stem}.prematch_ai_packager.md')
        if target.exists() and not is_our_prompt(target, provider, event_id):
            raise PackagerError(f'PROMPT_AJENO_BLOQUEA: {target}')
    write_text(target, content, output_dir, expediente)
    return target, kept


def retire_our_prompts(directory: Path, provider: str, event_id: str) -> None:
    names = (
        f'COPIAR_{provider.upper()}_{event_id}.md',
        f'COPIAR_{provider.upper()}_{event_id}.prematch_ai_packager.md',
    )
    for name in names:
        path = directory / name
        if path.is_file() and not path.is_symlink() and is_our_prompt(path, provider, event_id):
            path.unlink()


def result_readme(provider: str, event_id: str) -> str:
    output_name = OUTPUT_NAMES[provider].format(event_id=event_id)
    return (
        f'# Resultados de {provider}\n\n'
        f'Coloca aquí la respuesta manual del evento {event_id}.\n'
        f'Nombre previsto: {output_name}.\n\n'
        'Esta carpeta no sustituye `research/external/` ni `inputs/`.\n'
        'Depositar un archivo aquí no lo valida y no actualiza el manifiesto operativo.\n'
    )


def package_event(
    event_id: str,
    expediente: Path,
    *,
    project_root: Path | None = None,
    output_dir: Path | None = None,
    unavailable: list[str] | None = None,
    now: datetime | None = None,
) -> PackageResult:
    event_id = validate_event_id(event_id)
    expediente = resolve_expediente(expediente, event_id)
    output = resolve_output(expediente, output_dir)
    project = resolve_project_root(expediente, project_root)
    skipped = parse_unavailable(','.join(unavailable or []))
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise PackagerError('RELOJ_SIN_ZONA_HORARIA')
    generated = moment.astimezone(timezone.utc).isoformat()
    fingerprint = manifest_fingerprint(expediente)
    manifest, manifest_sha = load_operational_manifest(expediente, event_id)
    sha_index = manifest_sha_index(manifest, expediente)
    paths, omitted_dirs = iter_source_paths(expediente)
    files = [describe_file(path, expediente, event_id, sha_index, manifest is not None) for path in paths]
    profile = external_profile(project)
    if profile is not None:
        files.append(profile)
    files.sort(key=lambda item: (item.outside_expediente, item.relative_path or item.absolute_path))
    instructions = instruction_records(project)
    card = build_identity(event_id, files, expediente)
    present = {item.relative_path for item in files if item.relative_path and not item.outside_expediente}
    absent = [relative for _key, relative in expected_paths(event_id) if relative not in present]
    coverage = 'CANONICA_COMPLETA' if not absent else 'INCOMPLETA'
    inventory_path = output / 'inventory.json'
    plans: list[ProviderPlan] = []
    prompts_dir = output / 'prompts'
    for provider in PROVIDERS:
        attached = select_attachments(files, provider, event_id)
        missing = missing_for_provider(provider, event_id, attached, files)
        plan = ProviderPlan(provider=provider, available=provider not in skipped, attach=attached, missing=missing)
        result_dir = output / 'resultados' / provider
        write_text(result_dir / 'LEEME.md', result_readme(provider, event_id), output, expediente)
        if plan.available:
            content = render_prompt(provider, event_id, card, plan, instructions, inventory_path)
            prompt_path, kept = place_prompt(prompts_dir, provider, event_id, content, output, expediente)
            plan.prompt_path = prompt_path
            plan.kept_foreign_prompt = kept
        else:
            retire_our_prompts(prompts_dir, provider, event_id)
            primary = prompts_dir / f'COPIAR_{provider.upper()}_{event_id}.md'
            if primary.exists() and not is_our_prompt(primary, provider, event_id):
                plan.kept_foreign_prompt = primary
        plans.append(plan)
    unavailable_flag = f' --unavailable {",".join(skipped)}' if skipped else ''
    command = f'python3 prematch_ai_packager.py --event-id {event_id} --expediente {expediente}{unavailable_flag}'
    adjuntos = render_adjuntos(event_id, plans, command, coverage, output)
    write_text(output / 'identidad_declarada.json', json.dumps(card, ensure_ascii=False, indent=2, sort_keys=True) + '\n', output, expediente)
    prompt_hashes = {
        plan.provider: sha256_file(plan.prompt_path) if plan.prompt_path and plan.prompt_path.is_file() else None
        for plan in plans
    }
    summary = {
        'event_id': event_id,
        'packager_version': PACKAGE_VERSION,
        'generado_utc': generated,
        'expediente': str(expediente),
        'nota_validacion': NOTE_VALIDATION,
        'cobertura': coverage,
        'presentes': len(files),
        'actuales_referenciados': sum(item.temporal_class == 'ACTUAL_REFERENCIADO' for item in files),
        'snapshots_historicos': sum(item.temporal_class == 'SNAPSHOT_HISTORICO' for item in files),
        'vigencia_no_verificada': sum(item.temporal_class == 'VIGENCIA_NO_VERIFICADA' for item in files),
        'evento_ajeno': sum(item.event_binding == 'EVENTO_AJENO' for item in files),
        'validados': 0,
        'ausentes': absent,
        'directorios_omitidos': omitted_dirs,
        'copias_de_fuentes': 0,
    }
    inventory = {
        'packager_version': PACKAGE_VERSION,
        'event_id': event_id,
        'generado_utc': generated,
        'expediente': str(expediente),
        'nota_validacion': NOTE_VALIDATION,
        'cobertura': coverage,
        'manifiesto_operativo': {
            'path': str((expediente / 'manifest.json').resolve()),
            'existe': manifest is not None,
            'sha256': manifest_sha,
            'modificado_por_el_empaquetador': False,
            'event_id_declarado': None if manifest is None else manifest.get('event_id'),
            'validado': False,
        },
        'instrucciones_permanentes': instructions,
        'resumen': summary,
        'archivos': [item.to_dict() for item in files],
        'ausentes': absent,
        'proveedores': {
            plan.provider: {
                'disponibilidad': 'DISPONIBLE' if plan.available else 'NO_DISPONIBLE',
                'prompt': None if plan.prompt_path is None else str(plan.prompt_path),
                'prompt_sha256': prompt_hashes[plan.provider],
                'prompt_ajeno_conservado': None if plan.kept_foreign_prompt is None else str(plan.kept_foreign_prompt),
                'adjuntar': [item.to_dict() for item in plan.attach] if plan.available else [],
                'ausentes': plan.missing,
                'carpeta_resultado': str((output / 'resultados' / plan.provider).resolve()),
                'salida_prevista': OUTPUT_NAMES[plan.provider].format(event_id=event_id),
            }
            for plan in plans
        },
    }
    inventory_bytes = (json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')
    write_bytes(inventory_path, inventory_bytes, output, expediente)
    write_text(output / 'inventory.md', render_inventory_md(summary, files), output, expediente)
    adjuntos_path = output / 'adjuntos.md'
    write_text(adjuntos_path, adjuntos, output, expediente)
    record = {
        'packager_version': PACKAGE_VERSION,
        'event_id': event_id,
        'generado_utc': generated,
        'inventory_sha256': sha256_bytes(inventory_bytes),
        'adjuntos_sha256': sha256_file(adjuntos_path),
        'identidad_sha256': sha256_file(output / 'identidad_declarada.json'),
        'prompts_sha256': prompt_hashes,
        'manifiesto_operativo_sha256': manifest_sha,
        'manifiesto_operativo_modificado': False,
        'copias_de_fuentes': 0,
        'validados': 0,
    }
    write_text(output / 'package_record.json', json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + '\n', output, expediente)
    ensure_manifest_untouched(expediente, fingerprint)
    return PackageResult(
        event_id=event_id,
        expediente=expediente,
        output_dir=output,
        inventory_path=inventory_path,
        adjuntos_path=adjuntos_path,
        prompts={plan.provider: plan.prompt_path for plan in plans},
        missing=absent,
        unavailable=skipped,
        coverage=coverage,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--event-id', required=True, help='Event ID Bwin, por ejemplo 7909230')
    parser.add_argument('--expediente', type=Path, required=True, help='Carpeta del evento, normalmente outputs/matches/<EVENT_ID>')
    parser.add_argument('--project-root', type=Path, help='Raíz con AGENTS.md e instructions/. Por defecto se infiere.')
    parser.add_argument('--output-dir', type=Path, help='Solo dentro de <expediente>/ai_packages. Por defecto esa carpeta.')
    parser.add_argument('--unavailable', default='', help='IA separadas por comas que no se preparan: grok,gemini,perplexity,argon,deepseek,codex')
    args = parser.parse_args(argv)
    try:
        result = package_event(
            args.event_id,
            args.expediente,
            project_root=args.project_root,
            output_dir=args.output_dir,
            unavailable=parse_unavailable(args.unavailable),
        )
    except PackagerError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
    print(f'EMPAQUETADO {result.event_id}')
    print(f'Cobertura: {result.coverage}')
    print(f'Adjuntos: {result.adjuntos_path}')
    print(f'Inventario: {result.inventory_path}')
    print(f'Ausentes canónicos: {len(result.missing)}')
    if result.unavailable:
        print('No disponibles: ' + ', '.join(result.unavailable))
    for provider, path in result.prompts.items():
        if path is not None:
            print(f'Prompt {provider}: {path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
