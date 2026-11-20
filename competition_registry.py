"""Season-scoped local competition evidence and admission policy. No network.

The registry identifies a competition.  Fixture-specific admission (notably
cup participants and senior international friendlies) remains a separate
decision so a known senior competition is never confused with an actionable
fixture.
"""
import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter
from functools import lru_cache
from pathlib import Path


_WOMEN_TEAM_RE = re.compile(
    r"(?i)(?:\bwomen(?:'s)?\b|\bwomens\b|\bfemenin[oa]\b|"
    r"\bfeminin[ea]\b|\bfrauen\b|\bfemminile\b|\bdamen\b|\(W\)|\s+W\s*$)"
)
_NON_SENIOR_TEAM_RE = re.compile(
    r"(?i)(?:\bu\s*[- ]?\s*\d{2}\b|\bunder\s*[- ]?\s*\d{2}\b|\bsub\s*[- ]?\s*\d{2}\b|"
    r"\b(?:youth|juvenil(?:es)?|juniores|primavera|reserves?|reservas?|reserve\s+team|"
    r"olympic|ol[ií]mpic[oa]?|amateur|universit\w*|military|army|select(?:ion)?|"
    r"local|home[- ]based|all[- ]stars?|xi|fc|cf|sc|cd|ca|club)\b|"
    r"\s+(?:B|C|II|III|2)\s*$|\s+\((?:B|C)\)\s*$|\s+A'\s*$)"
)
_NON_SENIOR_ROUND_RE = re.compile(
    r"(?i)(?:\bu\s*[- ]?\s*\d{2}\b|\bunder\s*[- ]?\s*\d{2}\b|\bsub\s*[- ]?\s*\d{2}\b|"
    r"\b(?:youth|juvenil|olympic|ol[ií]mpic|futsal|beach|esports|club|reserve|amateur)\b)"
)

# Catálogo determinista de selecciones nacionales absolutas reconocidas (miembros FIFA y variantes API-Football).
CURATED_SENIOR_NATIONAL_TEAMS: frozenset[str] = frozenset({
    "afghanistan", "albania", "algeria", "american samoa", "andorra", "angola", "anguilla",
    "antigua and barbuda", "argentina", "armenia", "aruba", "australia", "austria",
    "azerbaijan", "bahamas", "bahrain", "bangladesh", "barbados", "belarus", "belgium",
    "belize", "benin", "bermuda", "bhutan", "bolivia", "bonaire", "bosnia",
    "bosnia and herzegovina", "bosnia herzegovina", "botswana", "brazil",
    "british virgin islands", "brunei", "brunei darussalam", "bulgaria", "burkina faso",
    "burundi", "cabo verde", "cape verde", "cambodia", "cameroon", "canada",
    "cayman islands", "central african republic", "chad", "chile", "china", "china pr",
    "chinese taipei", "colombia", "comoros", "congo", "congo dr", "dr congo",
    "democratic republic of congo", "cook islands", "costa rica", "cote d ivoire",
    "ivory coast", "croatia", "cuba", "curacao", "cyprus", "czechia", "czech republic",
    "denmark", "djibouti", "dominica", "dominican republic", "ecuador", "egypt",
    "el salvador", "england", "equatorial guinea", "eritrea", "estonia", "eswatini",
    "swaziland", "ethiopia", "faroe islands", "fiji", "finland", "france",
    "french guiana", "gabon", "gambia", "georgia", "germany", "ghana", "gibraltar",
    "greece", "grenada", "guadeloupe", "guam", "guatemala", "guinea", "guinea bissau",
    "guyana", "haiti", "honduras", "hong kong", "hungary", "iceland", "india",
    "indonesia", "iran", "ir iran", "iraq", "ireland", "republic of ireland", "israel",
    "italy", "jamaica", "japan", "jordan", "kazakhstan", "kenya", "korea dpr",
    "north korea", "korea republic", "south korea", "kosovo", "kuwait", "kyrgyzstan",
    "kyrgyz republic", "laos", "latvia", "lebanon", "lesotho", "liberia", "libya",
    "liechtenstein", "lithuania", "luxembourg", "macau", "madagascar", "malawi",
    "malaysia", "maldives", "mali", "malta", "martinique", "mauritania", "mauritius",
    "mexico", "moldova", "mongolia", "montenegro", "montserrat", "morocco",
    "mozambique", "myanmar", "namibia", "nepal", "netherlands", "holland",
    "new caledonia", "new zealand", "nicaragua", "niger", "nigeria", "north macedonia",
    "macedonia", "northern ireland", "norway", "oman", "pakistan", "palestine",
    "panama", "papua new guinea", "paraguay", "peru", "philippines", "poland",
    "portugal", "puerto rico", "qatar", "romania", "russia", "rwanda", "saint kitts and nevis",
    "st kitts and nevis", "saint lucia", "st lucia", "saint vincent and the grenadines",
    "st vincent and the grenadines", "samoa", "san marino", "sao tome and principe",
    "saudi arabia", "scotland", "senegal", "serbia", "seychelles", "sierra leone",
    "singapore", "slovakia", "slovenia", "solomon islands", "somalia", "south africa",
    "south sudan", "spain", "sri lanka", "sudan", "suriname", "sweden", "switzerland",
    "syria", "tahiti", "taiwan", "tajikistan", "tanzania", "thailand", "timor leste",
    "east timor", "togo", "tonga", "trinidad and tobago", "tunisia", "turkey", "turkiye",
    "turkmenistan", "turks and caicos islands", "uae", "united arab emirates", "uganda",
    "ukraine", "uruguay", "usa", "united states", "us virgin islands", "uzbekistan",
    "vanuatu", "venezuela", "vietnam", "wales", "yemen", "zambia", "zimbabwe",
})


def _norm_team_base(name: str) -> str:
    stripped = _WOMEN_TEAM_RE.sub(" ", str(name or ""))
    clean = unicodedata.normalize("NFKD", stripped).encode("ascii", "ignore").decode()
    clean = re.sub(r"[^a-zA-Z0-9]+", " ", clean).lower().strip()
    return " ".join(clean.split())


def resolve_national_friendly_evidence(row: dict) -> dict | None:
    """Verifica estrictamente si un encuentro de League ID 10 corresponde a selecciones absolutas."""
    explicit = row.get("national_team_evidence") or {}
    if explicit.get("source") and explicit.get("level") == "SENIOR" and explicit.get("gender") in ("M", "F"):
        return explicit

    entry = lookup(row)
    if not entry or entry.get("type") != "NATIONAL_FRIENDLY":
        return None

    round_val = str(row.get("round") or "")
    if round_val and _NON_SENIOR_ROUND_RE.search(round_val):
        return None

    home_raw = str(row.get("home") or "").strip()
    away_raw = str(row.get("away") or "").strip()
    if not home_raw or not away_raw:
        return None

    # Rechazar cualquier indicador juvenil, reserva, olímpico, amateur, B/C/local o club.
    for side in (home_raw, away_raw):
        if _NON_SENIOR_TEAM_RE.search(side):
            return None

    home_w = bool(_WOMEN_TEAM_RE.search(home_raw))
    away_w = bool(_WOMEN_TEAM_RE.search(away_raw))
    round_w = bool(_WOMEN_TEAM_RE.search(round_val))
    if home_w != away_w:
        return None
    if round_w and not (home_w and away_w):
        return None

    home_base = _norm_team_base(home_raw)
    away_base = _norm_team_base(away_raw)
    if not home_base or not away_base or home_base == away_base:
        return None
    if home_base not in CURATED_SENIOR_NATIONAL_TEAMS or away_base not in CURATED_SENIOR_NATIONAL_TEAMS:
        return None

    gender = "F" if (home_w and away_w) else "M"
    return {
        "source": "CURATED_SENIOR_NATIONAL_TEAM_CATALOG",
        "level": "SENIOR",
        "gender": gender,
        "home_canonical": home_base,
        "away_canonical": away_base,
        "round": round_val or None,
    }


def label(value):
    return ' '.join(unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode().lower().split())


@lru_cache(maxsize=1)
def entries():
    return json.loads((Path(__file__).parent/'config/competition_registry.json').read_text())['competitions']


def lookup(row):
    matches=[e for e in entries() if e['league_id']==row.get('league_id')
             and e['country']==row.get('country') and e['season']==row.get('season')
             and label(row.get('league')) in {label(n) for n in e['names']}]
    if len(matches)!=1:return None
    entry=matches[0]
    api_type=row.get('league_type')
    if api_type and ((entry['type']=='LEAGUE' and api_type!='League') or
                     (entry['type']=='NATIONAL_CUP' and api_type!='Cup')):return None
    return entry


def admission(row):
    entry=lookup(row)
    if not entry:return 'REVIEW','TIER_UNKNOWN'
    typ=entry['type']
    if typ=='CLUB_FRIENDLY':return 'EXCLUDED','CLUB_FRIENDLY'
    if typ=='LEAGUE':
        tier=entry['national_tier']
        return ('ELIGIBLE','NATIONAL_TIER_1_OR_2') if tier in (1,2) else ('EXCLUDED','NATIONAL_TIER_NOT_ADMITTED') if isinstance(tier,int) else ('REVIEW','TIER_UNKNOWN')
    if typ=='NATIONAL_CUP':
        # The registry already supplies positive evidence that this is an
        # adult national cup. Participant league tiers are metadata, not event
        # identity or admission gates in V3.1.
        return 'ELIGIBLE','ADULT_NATIONAL_CUP'
    if typ in ('NATIONAL_OFFICIAL','NATIONAL_FRIENDLY'):
        if entry['level']=='SENIOR':return 'ELIGIBLE',typ
        evidence=resolve_national_friendly_evidence(row) if typ=='NATIONAL_FRIENDLY' else (row.get('national_team_evidence') or {})
        if evidence and evidence.get('source') and evidence.get('level')=='SENIOR' and evidence.get('gender') in ('M','F'):
            return 'ELIGIBLE',typ
        return 'REVIEW','SENIOR_NATIONAL_TEAMS_PENDING'
    return 'REVIEW','TIER_UNKNOWN'


def eligibility_details(row):
    """Return independent category, competition and participant decisions."""
    entry=lookup(row)
    if not entry:
        return {'competition_identity_status':'UNKNOWN',
                'competition_eligibility':'PENDING_CLASSIFICATION',
                'competition_eligibility_reason':'TIER_UNKNOWN',
                'participant_eligibility':'NOT_APPLICABLE'}
    status,reason=admission(row)
    competition_status={'ELIGIBLE':'ELIGIBLE','EXCLUDED':'EXCLUDED'}.get(status,'PENDING_CLASSIFICATION')
    participant_status='NOT_APPLICABLE'
    participant_reason=None
    if entry['type']=='NATIONAL_CUP':
        participant_status='NOT_REQUIRED'
        participant_reason='ADULT_NATIONAL_CUP_NO_TIER_GATE'
    elif entry['type']=='NATIONAL_FRIENDLY':
        participant_status=competition_status
        participant_reason=reason
    extra = {'coverage_exception_reason': 'NATIONAL_FRIENDLY_NO_STANDINGS_FORMAT'} if (
        entry['type'] == 'NATIONAL_FRIENDLY' and competition_status == 'ELIGIBLE'
    ) else {}
    return {'competition_identity_status':'CORROBORATED',
            'competition_eligibility':competition_status,
            'competition_eligibility_reason':reason,
            'participant_eligibility':participant_status,
            'participant_eligibility_reason':participant_reason,
            **extra}


def category(row):
    entry=lookup(row)
    if not entry:return {}
    evidence=(resolve_national_friendly_evidence(row) or {}) if entry['type']=='NATIONAL_FRIENDLY' else (row.get('national_team_evidence') or {})
    gender,level=entry['gender'],entry['level']
    if entry['type']=='NATIONAL_FRIENDLY' and evidence.get('source'):
        gender,level=evidence.get('gender'),evidence.get('level')
    if gender not in ('M','F') or level!='SENIOR':return {}
    current=str(row.get('gender') or 'UNKNOWN').upper()
    if current not in ('UNKNOWN',gender):return {}
    status,reason=admission(row)
    details=eligibility_details(row)
    return {'gender':gender,'category_level':level,'category_evidence_status':'CORROBORATED',
            # Category describes the known senior M/F identity.  Admission is
            # carried separately and may remain pending for a cup fixture.
            'category_status':'ELIGIBLE','tier_status':reason,
            **details,
            'category_provenance':{'method':'SEASON_COMPETITION_REGISTRY','entry':entry,
                                   'fixture_evidence':evidence if entry['type']=='NATIONAL_FRIENDLY' else None}}


def unknown_inventory(fixtures):
    """Build a stable, count-ordered queue for later evidence curation."""
    grouped=Counter()
    examples={}
    from filter_major_leagues import classify
    for row in fixtures:
        status,reason=classify({**row,'status':'NS'},policy='adult-open')
        if status!='REVIEW':continue
        key=(row.get('league_id'),row.get('country'),row.get('season'),row.get('league'),reason)
        grouped[key]+=1
        examples.setdefault(key,[])
        if len(examples[key])<3:
            examples[key].append({'fixture_id':row.get('fixture_id'),'home':row.get('home'),'away':row.get('away')})
    required={
        'TIER_UNKNOWN':['accredited national tier','gender','senior/youth/reserve scope'],
        'CUP_STAGE_AND_TEAM_TIERS_PENDING':['knockout-stage source','home national tier','away national tier'],
        'CUP_KNOCKOUT_NOT_CORROBORATED':['knockout-stage source'],
        'CUP_TEAM_TIERS_PENDING':['home national tier','away national tier'],
        'SENIOR_NATIONAL_TEAMS_PENDING':['senior national-team source','gender'],
    }
    rows=[]
    for key,count in grouped.items():
        lid,country,season,name,reason=key
        rows.append({'league_id':lid,'country':country,'season':season,'league':name,
                     'fixtures':count,'reason':reason,
                     'required_evidence':required.get(reason,['manual classification evidence']),
                     'examples':examples[key]})
    return sorted(rows,key=lambda item:(-item['fixtures'],str(item['country']),str(item['league'])))


def main(argv=None):
    parser=argparse.ArgumentParser(description='Inventario offline de competiciones pendientes')
    parser.add_argument('--inventory-fixtures',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    source_bytes=args.inventory_fixtures.read_bytes()
    document=json.loads(source_bytes.decode('utf-8-sig'))
    fixtures=document.get('fixtures') if isinstance(document,dict) else None
    if not isinstance(fixtures,list):parser.error('El archivo no contiene fixtures[]')
    rows=unknown_inventory(fixtures)
    registry_path=Path(__file__).parent/'config/competition_registry.json'
    payload={'schema_version':'1.0-competition-classification-queue',
             'source':str(args.inventory_fixtures.resolve()),
             'source_sha256':hashlib.sha256(source_bytes).hexdigest(),
             'registry':str(registry_path.resolve()),
             'registry_sha256':hashlib.sha256(registry_path.read_bytes()).hexdigest(),
             'unknown_competitions':rows,
             'counts':{'competitions':len(rows),'fixtures':sum(row['fixtures'] for row in rows)}}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f"Pendientes: {payload['counts']['competitions']} competiciones / {payload['counts']['fixtures']} fixtures")
    return 0


if __name__=='__main__':raise SystemExit(main())
