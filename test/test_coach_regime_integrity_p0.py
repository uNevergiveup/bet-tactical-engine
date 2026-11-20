"""Pruebas deterministas Fase 2A.1: integridad, procedencia y verificación de régimen de entrenadores."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bet_engine.analytics import TacticalInvestigator, build_stage3_registry
from bet_engine.context import ObservationStore
from bet_engine.contracts import VerificationStatus
from bet_engine.modeling import CoherentMatchModeler
from bet_engine.orchestration import ShadowPipelineRunner

ROOT = Path(__file__).resolve().parent.parent


def _base_match_engine_payload(
    *,
    home_hist_coach: tuple[int, str] | None = (11, "Hist Home Coach"),
    away_hist_coach: tuple[int, str] | None = (22, "Hist Away Coach"),
    coachs_home_resp: list[dict] | None = None,
    coachs_away_resp: list[dict] | None = None,
    target_lineups_resp: list[dict] | None = None,
    target_lineups_primary_verified: bool = False,
) -> dict:
    ko = datetime(2026, 12, 10, 20, 0, tzinfo=timezone.utc)
    cut = ko - timedelta(minutes=60)

    def _hist_bundles(team_id: int, opp_base: int, base_fid: int, coach: tuple[int, str] | None) -> list[dict]:
        out = []
        for idx in range(10):
            dt = ko - timedelta(days=7 * (idx + 1))
            is_home = idx % 2 == 0
            h_id = team_id if is_home else opp_base + idx
            a_id = opp_base + idx if is_home else team_id
            lu = (
                [{"team": {"id": team_id}, "coach": {"id": coach[0], "name": coach[1]}}]
                if coach is not None
                else []
            )
            out.append(
                {
                    "fixture": {"id": base_fid + idx, "date": dt.isoformat(), "status": {"short": "FT"}},
                    "league": {"id": 71, "name": "Serie A", "type": "League", "country": "Brazil", "season": 2026},
                    "teams": {
                        "home": {"id": h_id, "name": f"Team {h_id}"},
                        "away": {"id": a_id, "name": f"Team {a_id}"},
                    },
                    "goals": {"home": 1, "away": 1},
                    "score": {"halftime": {"home": 0, "away": 0}},
                    "statistics": [],
                    "lineups": lu,
                    "events": [],
                }
            )
        return out

    lu_block: dict = {"response": target_lineups_resp or []}
    if target_lineups_primary_verified:
        lu_block["provenance"] = {"primary_official_verified": True}

    return {
        "generated_at": cut.isoformat(),
        "match": {"event_id": "9900111", "home_name": "Club Alfa", "away_name": "Club Beta"},
        "fixture": {
            "fixture_id": 8800111,
            "league_id": 71,
            "season": 2026,
            "date": ko.isoformat(),
            "home_id": 1001,
            "away_id": 2002,
            "home_name": "Club Alfa",
            "away_name": "Club Beta",
            "status": "NS",
        },
        "context": {
            "home_history_deep": {"detailed_fixtures": _hist_bundles(1001, 3000, 71000, home_hist_coach)},
            "away_history_deep": {"detailed_fixtures": _hist_bundles(2002, 4000, 72000, away_hist_coach)},
            "coachs": {
                "home": {"response": coachs_home_resp or []},
                "away": {"response": coachs_away_resp or []},
            },
            "lineups": lu_block,
            "home_next_5": {"response": []},
            "away_next_5": {"response": []},
        },
    }


class TestCoachRegimeIntegrityP0(unittest.TestCase):
    def test_1_historical_coach_with_unknown_target_never_promoted(self) -> None:
        """Histórico + /coachs coincidente pero coach=null en objetivo -> HISTORICAL_ONLY_UNVERIFIED (nunca CONFIRMADO_OFICIAL)."""
        payload = _base_match_engine_payload(
            home_hist_coach=(11, "Hist Home Coach"),
            away_hist_coach=(22, "Hist Away Coach"),
            coachs_away_resp=[
                {"id": 22, "name": "Hist Away Coach", "career": [{"team": {"id": 2002}, "start": "2026-01-01", "end": None}]}
            ],
            target_lineups_resp=[
                {"team": {"id": 1001}, "coach": {"id": 0, "name": None}, "startXI": []},
                {"team": {"id": 2002}, "coach": {"id": 0, "name": None}, "startXI": []},
            ],
        )
        bundle = ObservationStore.from_match_engine_json(payload)
        coaches = {c.facts["side"]: c for c in bundle.contextual_evidence if c.category == "COACH_REGIME"}

        for side, expected_id, expected_name in (("home", 11, "Hist Home Coach"), ("away", 22, "Hist Away Coach")):
            ev = coaches[side]
            self.assertNotEqual(ev.verification_status, VerificationStatus.CONFIRMADO_OFICIAL)
            self.assertEqual(ev.verification_status, VerificationStatus.NO_VERIFICADO)
            self.assertEqual(ev.facts["coach_status"], "HISTORICAL_ONLY_UNVERIFIED")
            self.assertIsNone(ev.facts["current_coach_id"])
            self.assertIsNone(ev.facts["current_coach_name"])
            self.assertEqual(ev.facts["last_historical_coach_id"], expected_id)
            self.assertEqual(ev.facts["last_historical_coach_name"], expected_name)
            self.assertFalse(ev.facts["continuity_verified"])
            self.assertNotIn("current_coach_regime", bundle.windows[side])

        an = build_stage3_registry().run_all(bundle)
        flags = {f for p in an.prematch_patterns for f in p.contradiction_flags}
        self.assertIn("COACH_CONTINUITY_UNVERIFIED", flags)

    def test_2_confirmed_target_coach_without_change(self) -> None:
        """Entrenador confirmado en alineación objetivo coincidente con histórico -> CURRENT_VERIFIED."""
        payload = _base_match_engine_payload(
            home_hist_coach=(11, "Hist Home Coach"),
            away_hist_coach=(22, "Hist Away Coach"),
            target_lineups_resp=[
                {"team": {"id": 1001}, "coach": {"id": 11, "name": "Hist Home Coach"}, "startXI": []},
                {"team": {"id": 2002}, "coach": {"id": 22, "name": "Hist Away Coach"}, "startXI": []},
            ],
            target_lineups_primary_verified=False,
        )
        bundle = ObservationStore.from_match_engine_json(payload)
        coaches = {c.facts["side"]: c for c in bundle.contextual_evidence if c.category == "COACH_REGIME"}

        self.assertEqual(coaches["home"].facts["coach_status"], "CURRENT_VERIFIED")
        self.assertEqual(coaches["home"].verification_status, VerificationStatus.PUBLICADO_PREPARTIDO)
        self.assertTrue(coaches["home"].facts["continuity_verified"])
        self.assertEqual(coaches["home"].facts["current_coach_id"], 11)
        self.assertEqual(coaches["home"].facts["historical_matches_under_current_coach_n"], 10)
        self.assertFalse(coaches["home"].facts["historical_sample_belongs_to_prior_regime"])

        # Solo si existe verificación primaria oficial explícita asciende a CONFIRMADO_OFICIAL
        payload["context"]["lineups"]["provenance"] = {"primary_official_verified": True}
        bundle_prim = ObservationStore.from_match_engine_json(payload)
        coaches_prim = {c.facts["side"]: c for c in bundle_prim.contextual_evidence if c.category == "COACH_REGIME"}
        self.assertEqual(coaches_prim["home"].verification_status, VerificationStatus.CONFIRMADO_OFICIAL)

    def test_3_debut_coach_backed_by_verifiable_source_marks_prior_regime_and_keeps_history(self) -> None:
        """Técnico debutante confirmado externamente -> NEW_COACH_VERIFIED, n=0 propios, historia conservada."""
        payload = _base_match_engine_payload(
            home_hist_coach=(11, "Hist Home Coach"),
            away_hist_coach=(22, "Hist Away Coach"),
            target_lineups_resp=[
                {"team": {"id": 1001}, "coach": {"id": 0, "name": None}, "startXI": []},
                {"team": {"id": 2002}, "coach": {"id": 0, "name": None}, "startXI": []},
            ],
        )
        qual_md = (
            "## 3. Entrenadores\n"
            "### 3.2. Club Beta — Nuevo DT Debutante (debut oficial)\n"
            "- **Identidad y vigencia:** [CONFIRMADO] (CONFIRMADO_OFICIAL). Oficializado el 08/12/2026 en https://clubbeta.com/oficial-nuevo-dt; debuta hoy.\n"
        )
        bundle = ObservationStore.from_match_engine_json(payload, qualitative_markdown=qual_md)
        away_ev = next(c for c in bundle.contextual_evidence if c.category == "COACH_REGIME" and c.facts["side"] == "away")

        # Un informe cualitativo con URL + fecha documentada es fuente secundaria/citada (PUBLICADO_PREPARTIDO), nunca CONFIRMADO_OFICIAL primario
        self.assertEqual(away_ev.verification_status, VerificationStatus.PUBLICADO_PREPARTIDO)
        self.assertEqual(away_ev.facts["coach_status"], "NEW_COACH_SECONDARY_DOCUMENTED")
        self.assertEqual(away_ev.facts["source_tier"], "SECONDARY_DOCUMENTED")
        self.assertFalse(away_ev.facts["primary_official_verified"])
        self.assertEqual(away_ev.facts["current_coach_name"], "Nuevo DT Debutante")
        self.assertEqual(away_ev.facts["last_historical_coach_name"], "Hist Away Coach")
        self.assertTrue(away_ev.facts["regime_change_detected"])
        self.assertEqual(away_ev.facts["historical_matches_under_current_coach_n"], 0)
        self.assertTrue(away_ev.facts["historical_sample_belongs_to_prior_regime"])
        # No elimina partidos históricos del bundle ni altera pesos del modelo
        self.assertEqual(len(bundle.away_matches), 10)

        an = build_stage3_registry().run_all(bundle)
        away_pat = next(p for p in an.prematch_patterns if p.pattern_id == f"pat_coach_shift_{bundle.fixture_id}_away")
        self.assertIn("COACH_REGIME_SHIFT_LIMITS_HISTORY_COMPARABILITY", away_pat.contradiction_flags)
        self.assertIn("DEBUT_COACH_PRIOR_REGIME_HISTORY_NON_REPRESENTATIVE", away_pat.contradiction_flags)
        self.assertIn("COACH_EVIDENCE_SECONDARY_NOT_PRIMARY_OFFICIAL", away_pat.contradiction_flags)

    def test_4_unconfirmed_rumor_classified_suspected_not_verified(self) -> None:
        """Rumor no confirmado -> REGIME_CHANGE_SUSPECTED + NO_VERIFICADO, nunca cambia current_coach_name verificado."""
        payload = _base_match_engine_payload(away_hist_coach=(22, "Hist Away Coach"))
        qual_rumor = (
            "## 3. Entrenadores\n"
            "### 3.2. Club Beta — Técnico Rumor\n"
            "- **Identidad y vigencia:** [NO VERIFICADO] Rumor de prensa sin confirmar sobre posible interinato.\n"
        )
        bundle = ObservationStore.from_match_engine_json(payload, qualitative_markdown=qual_rumor)
        away_ev = next(c for c in bundle.contextual_evidence if c.category == "COACH_REGIME" and c.facts["side"] == "away")

        self.assertEqual(away_ev.verification_status, VerificationStatus.NO_VERIFICADO)
        self.assertEqual(away_ev.facts["coach_status"], "REGIME_CHANGE_SUSPECTED")
        self.assertIsNone(away_ev.facts["current_coach_name"])
        self.assertEqual(away_ev.facts["suspected_coach_name"], "Técnico Rumor")
        self.assertEqual(away_ev.facts["last_historical_coach_name"], "Hist Away Coach")
        self.assertTrue(away_ev.facts["regime_change_suspected"])
        self.assertFalse(away_ev.facts["regime_change_detected"])

        an = build_stage3_registry().run_all(bundle)
        flags = {f for p in an.prematch_patterns for f in p.contradiction_flags}
        self.assertIn("COACH_REGIME_CHANGE_SUSPECTED_UNVERIFIED", flags)

    def test_5_complete_absence_of_coach_information(self) -> None:
        """Ausencia completa de entrenador histórico, /coachs y objetivo -> UNKNOWN + CURRENT_COACH_UNKNOWN."""
        payload = _base_match_engine_payload(
            home_hist_coach=None,
            away_hist_coach=None,
            coachs_home_resp=[],
            coachs_away_resp=[],
            target_lineups_resp=[],
        )
        bundle = ObservationStore.from_match_engine_json(payload)
        coaches = {c.facts["side"]: c for c in bundle.contextual_evidence if c.category == "COACH_REGIME"}

        for side in ("home", "away"):
            ev = coaches[side]
            self.assertEqual(ev.verification_status, VerificationStatus.NO_VERIFICADO)
            self.assertEqual(ev.facts["coach_status"], "UNKNOWN")
            self.assertIsNone(ev.facts["current_coach_id"])
            self.assertIsNone(ev.facts["current_coach_name"])
            self.assertIsNone(ev.facts["last_historical_coach_id"])
            self.assertIsNone(ev.facts["last_historical_coach_name"])

        an = build_stage3_registry().run_all(bundle)
        flags = {f for p in an.prematch_patterns for f in p.contradiction_flags}
        self.assertIn("CURRENT_COACH_UNKNOWN", flags)

    def test_6_shadow_serialization_and_warning_propagation(self) -> None:
        """Shadow serializa dossier + prompt MD y propaga advertencias de entrenador hasta mercados."""
        payload = _base_match_engine_payload(
            home_hist_coach=(11, "Hist Home Coach"),
            away_hist_coach=(22, "Hist Away Coach"),
            target_lineups_resp=[],
        )
        bundle = ObservationStore.from_match_engine_json(payload)
        qual_md = (
            "## 3. Entrenadores\n"
            "### 3.2. Club Beta — Vinícius Bergantin (debut oficial)\n"
            "- **Identidad y vigencia:** [CONFIRMADO] (CONFIRMADO_OFICIAL). Oficializado el 08/12/2026 en https://clubbeta.com/oficial; debuta hoy.\n"
        )
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            rep = ShadowPipelineRunner(min_lead_minutes=45, lineup_cutoff_minutes=15).run_shadow_on_bundle(
                bundle,
                odds_map={"draw": 4.2, "home_win": 2.0, "away_win": 3.5},
                output_dir=out_dir,
                qualitative_markdown=qual_md,
            )
            dossier = json.loads((out_dir / "tactical_dossier_9900111.json").read_text(encoding="utf-8"))
            prompt_md = (out_dir / "CODEX_INVESTIGATOR_9900111.md").read_text(encoding="utf-8")

            self.assertIn("Régimen de Entrenadores y Procedencia", prompt_md)
            self.assertIn("NEW_COACH_SECONDARY_DOCUMENTED", prompt_md)
            self.assertIn("HISTORICAL_ONLY_UNVERIFIED", prompt_md)

            away_ev = next(
                c for c in dossier["contextual_evidence"]
                if c["category"] == "COACH_REGIME" and c["facts"]["side"] == "away"
            )
            self.assertEqual(away_ev["verification_status"], "PUBLICADO_PREPARTIDO")
            self.assertFalse(away_ev["facts"]["primary_official_verified"])
            self.assertEqual(away_ev["facts"]["coach_status"], "NEW_COACH_SECONDARY_DOCUMENTED")
            self.assertEqual(away_ev["facts"]["last_historical_coach_name"], "Hist Away Coach")
            self.assertEqual(away_ev["facts"]["historical_matches_under_current_coach_n"], 0)

            all_mc_warnings = {w for m in rep.market_comparisons for w in m.get("tactical_warnings", ())}
            self.assertIn("COACH_REGIME_SHIFT_LIMITS_HISTORY_COMPARABILITY", all_mc_warnings)
            self.assertIn("DEBUT_COACH_PRIOR_REGIME_HISTORY_NON_REPRESENTATIVE", all_mc_warnings)
            self.assertIn("COACH_CONTINUITY_UNVERIFIED", all_mc_warnings)

    def test_7_regression_audit_7901472_p_model_invariant_and_closed_artifacts_untouched(self) -> None:
        """Regresión 7901472: p_model exactamente idéntico, sin BET ni alteración de artefactos cerrados."""
        ctx_path = ROOT / "outputs/matches/7901472/context_updates/db56988fc1b71627b65cfe07/match_engine_7901472.json"
        qual_path = ROOT / "outputs/matches/7901472/inputs/reporte_cualitativo_7901472.md"
        cmp_path = ROOT / "outputs/matches/7901472/shadow/shadow_comparison_7901472.json"
        if not (ctx_path.is_file() and qual_path.is_file() and cmp_path.is_file()):
            self.skipTest("Snapshot 7901472 no presente")

        payload = json.loads(ctx_path.read_text(encoding="utf-8"))
        qual_md = qual_path.read_text(encoding="utf-8")
        closed_cmp = json.loads(cmp_path.read_text(encoding="utf-8"))

        # Expediente cerrado intacto
        self.assertEqual(closed_cmp["decision"], "NO_BET")
        self.assertEqual(closed_cmp["decision_taxonomy"], "CANDIDATO_CONDICIONAL")

        bundle_api = ObservationStore.from_match_engine_json(payload)
        bundle_rec = ObservationStore.from_match_engine_json(payload, qualitative_markdown=qual_md)
        m_api = CoherentMatchModeler.build_model(bundle_api)
        m_rec = CoherentMatchModeler.build_model(bundle_rec)

        # Invarianza exacta de p_model y lambdas
        self.assertEqual(m_api.families["GOALS_FT"].lambda_home, 1.5303)
        self.assertEqual(m_api.families["GOALS_FT"].lambda_away, 1.1636)
        self.assertEqual(m_rec.families["GOALS_FT"].lambda_home, 1.5303)
        self.assertEqual(m_rec.families["GOALS_FT"].lambda_away, 1.1636)
        self.assertEqual(m_rec.outcomes_1x2, {"home_win": 0.4525, "draw": 0.2631, "away_win": 0.2843})

        # Shadow en memoria (sin escribir sobre outputs/matches/7901472/) con mismas cuotas del cierre
        closed_odds = {
            m["selection_key"]: m["odd"]
            for m in closed_cmp["market_comparisons"]
            if m.get("odd") and m["odd"] > 1.0
        }
        rep = ShadowPipelineRunner(min_lead_minutes=10, lineup_cutoff_minutes=15).run_shadow_on_bundle(
            bundle_api,
            odds_map=closed_odds,
            qualitative_markdown=qual_md,
        )
        self.assertEqual(rep.decision, "NO_BET")
        self.assertEqual(rep.decision_classification, "NO_BET_POR_INCERTIDUMBRE")
        self.assertEqual(rep.decision_breakdown_counts["PICK_ACCIONABLE"], 0)
        self.assertEqual(rep.formal_decision_breakdown_counts["BET_AUTORIZADO"], 0)

        # Advertencia del debutante Vinícius Bergantin llega a Shadow y candidatos sin elevar a CONFIRMADO_OFICIAL
        away_ev_7901472 = next(
            c for c in bundle_rec.contextual_evidence
            if c.category == "COACH_REGIME" and c.facts["side"] == "away"
        )
        self.assertEqual(away_ev_7901472.verification_status, VerificationStatus.PUBLICADO_PREPARTIDO)
        self.assertEqual(away_ev_7901472.facts["coach_status"], "NEW_COACH_SECONDARY_DOCUMENTED")
        self.assertEqual(away_ev_7901472.facts["source_tier"], "SECONDARY_DOCUMENTED")
        self.assertFalse(away_ev_7901472.facts["primary_official_verified"])
        self.assertTrue(
            any("DEBUT_COACH_PRIOR_REGIME_HISTORY_NON_REPRESENTATIVE" in c["limitations"] for c in rep.top_investigable_candidates)
        )


class TestCoachProvenanceVerification2A1B(unittest.TestCase):
    """Batería Fase 2A.1B: verifica que etiquetas textuales de un LLM nunca acrediten CONFIRMADO_OFICIAL."""

    def test_1_qualitative_confirmado_claim_without_source_never_promoted(self) -> None:
        """Informe cualitativo con [CONFIRMADO] / CONFIRMADO_OFICIAL sin URL ni fecha -> LLM_CLAIM_UNVERIFIED / NO_VERIFICADO."""
        payload = _base_match_engine_payload(away_hist_coach=(22, "Hist Away Coach"))
        qual_no_source = (
            "## 3. Entrenadores\n"
            "### 3.2. Club Beta — Técnico Inventado\n"
            "- **Identidad y vigencia:** [CONFIRMADO] (CONFIRMADO_OFICIAL). Oficializado como nuevo entrenador.\n"
        )
        bundle = ObservationStore.from_match_engine_json(payload, qualitative_markdown=qual_no_source)
        away_ev = next(c for c in bundle.contextual_evidence if c.category == "COACH_REGIME" and c.facts["side"] == "away")

        self.assertEqual(away_ev.verification_status, VerificationStatus.NO_VERIFICADO)
        self.assertEqual(away_ev.facts["source_tier"], "LLM_CLAIM_UNVERIFIED")
        self.assertFalse(away_ev.facts["primary_official_verified"])
        self.assertIsNone(away_ev.facts["current_coach_name"])
        self.assertEqual(away_ev.facts["coach_status"], "REGIME_CHANGE_SUSPECTED")
        self.assertEqual(away_ev.facts["suspected_coach_name"], "Técnico Inventado")

    def test_2_documented_secondary_source_stays_below_official(self) -> None:
        """Fuente secundaria documentada con URL y fecha -> PUBLICADO_PREPARTIDO + SECONDARY_DOCUMENTED, nunca CONFIRMADO_OFICIAL."""
        payload = _base_match_engine_payload(away_hist_coach=(22, "Hist Away Coach"))
        qual_sec = (
            "## 3. Entrenadores\n"
            "### 3.2. Club Beta — DT Prensa Documentado (debut)\n"
            "- **Identidad y vigencia:** [CONFIRMADO] (PUBLICADO_PREPARTIDO). Publicado el 08/12/2026 en https://ge.globo.com/club-beta/dt.\n"
        )
        bundle = ObservationStore.from_match_engine_json(payload, qualitative_markdown=qual_sec)
        away_ev = next(c for c in bundle.contextual_evidence if c.category == "COACH_REGIME" and c.facts["side"] == "away")

        self.assertEqual(away_ev.verification_status, VerificationStatus.PUBLICADO_PREPARTIDO)
        self.assertEqual(away_ev.facts["coach_status"], "NEW_COACH_SECONDARY_DOCUMENTED")
        self.assertEqual(away_ev.facts["source_tier"], "SECONDARY_DOCUMENTED")
        self.assertFalse(away_ev.facts["primary_official_verified"])
        self.assertEqual(away_ev.facts["verifiable_reference"], "https://ge.globo.com/club-beta/dt")
        self.assertEqual(away_ev.facts["publication_date"], "08/12/2026")

    def test_3_verifiable_primary_official_source_accredits_official(self) -> None:
        """Solo una fuente primaria verificable explícita (no texto LLM) acredita CONFIRMADO_OFICIAL."""
        payload = _base_match_engine_payload(away_hist_coach=(22, "Hist Away Coach"))
        bundle = ObservationStore.from_match_engine_json(
            payload,
            coach_overrides={
                "away": {
                    "coach_name": "DT Oficial Club",
                    "verification_tier": "VERIFIED_SOURCE",
                    "primary_verified": True,
                    "source": "OFFICIAL_CLUB_STATEMENT",
                    "verifiable_reference": "https://clubbeta.com/comunicado-oficial-dt",
                    "publication_date": "2026-12-09T14:00:00+00:00",
                }
            },
        )
        away_ev = next(c for c in bundle.contextual_evidence if c.category == "COACH_REGIME" and c.facts["side"] == "away")

        self.assertEqual(away_ev.verification_status, VerificationStatus.CONFIRMADO_OFICIAL)
        self.assertEqual(away_ev.facts["coach_status"], "NEW_COACH_VERIFIED")
        self.assertEqual(away_ev.facts["source_tier"], "PRIMARY_OFFICIAL")
        self.assertTrue(away_ev.facts["primary_official_verified"])
        self.assertEqual(away_ev.facts["current_coach_name"], "DT Oficial Club")
        self.assertEqual(away_ev.facts["historical_matches_under_current_coach_n"], 0)

    def test_4_coach_change_rumor_never_verified(self) -> None:
        """Rumor de cambio de entrenador -> RUMOR + NO_VERIFICADO + REGIME_CHANGE_SUSPECTED."""
        payload = _base_match_engine_payload(away_hist_coach=(22, "Hist Away Coach"))
        qual_rumor = (
            "## 3. Entrenadores\n"
            "### 3.2. Club Beta — Rumor Suplente\n"
            "- **Identidad y vigencia:** Rumor en redes sobre posible cambio de DT el 09/12/2026 en https://ejemplo.com/rumor.\n"
        )
        bundle = ObservationStore.from_match_engine_json(payload, qualitative_markdown=qual_rumor)
        away_ev = next(c for c in bundle.contextual_evidence if c.category == "COACH_REGIME" and c.facts["side"] == "away")

        self.assertEqual(away_ev.verification_status, VerificationStatus.NO_VERIFICADO)
        self.assertEqual(away_ev.facts["source_tier"], "RUMOR")
        self.assertEqual(away_ev.facts["coach_status"], "REGIME_CHANGE_SUSPECTED")
        self.assertFalse(away_ev.facts["primary_official_verified"])
        self.assertIsNone(away_ev.facts["current_coach_name"])

    def test_5_contradiction_between_api_and_external_source_flagged_as_conflict(self) -> None:
        """Contradicción entre XI API y fuente externa secundaria -> COACH_IDENTITY_CONFLICT + NO_VERIFICADO."""
        payload = _base_match_engine_payload(
            away_hist_coach=(22, "Hist Away Coach"),
            target_lineups_resp=[
                {"team": {"id": 1001}, "coach": {"id": 11, "name": "Hist Home Coach"}, "startXI": []},
                {"team": {"id": 2002}, "coach": {"id": 22, "name": "Hist Away Coach"}, "startXI": []},
            ],
        )
        qual_conflict = (
            "## 3. Entrenadores\n"
            "### 3.2. Club Beta — Otro Técnico Distinto\n"
            "- **Identidad y vigencia:** [CONFIRMADO] (PUBLICADO_PREPARTIDO). Oficializado el 09/12/2026 en https://prensa.com/otro-dt.\n"
        )
        bundle = ObservationStore.from_match_engine_json(payload, qualitative_markdown=qual_conflict)
        away_ev = next(c for c in bundle.contextual_evidence if c.category == "COACH_REGIME" and c.facts["side"] == "away")

        self.assertEqual(away_ev.verification_status, VerificationStatus.NO_VERIFICADO)
        self.assertEqual(away_ev.facts["coach_status"], "COACH_IDENTITY_CONFLICT")
        self.assertFalse(away_ev.facts["primary_official_verified"])
        self.assertIsNone(away_ev.facts["current_coach_name"])
        self.assertIsNotNone(away_ev.facts["coach_conflict_detail"])

        an = build_stage3_registry().run_all(bundle)
        flags = {f for p in an.prematch_patterns for f in p.contradiction_flags}
        self.assertIn("COACH_SOURCE_CONTRADICTION_UNVERIFIED", flags)

    def test_6_insufficient_information_never_elevated_by_downstream_consumers(self) -> None:
        """Información insuficiente en informe y API -> NO_VERIFICADO y p_model invariante."""
        payload = _base_match_engine_payload(home_hist_coach=None, away_hist_coach=None)
        qual_insuf = (
            "## 3. Entrenadores\n"
            "### 3.2. Club Beta — Técnico Sin Fuente\n"
            "- **Identidad y vigencia:** Sin parte ni fecha acreditada.\n"
        )
        bundle_plain = ObservationStore.from_match_engine_json(payload)
        bundle_insuf = ObservationStore.from_match_engine_json(payload, qualitative_markdown=qual_insuf)
        away_ev = next(c for c in bundle_insuf.contextual_evidence if c.category == "COACH_REGIME" and c.facts["side"] == "away")

        self.assertEqual(away_ev.verification_status, VerificationStatus.NO_VERIFICADO)
        self.assertFalse(away_ev.facts["primary_official_verified"])
        self.assertEqual(away_ev.facts["coach_status"], "UNKNOWN")

        # p_model permanece exactamente idéntico
        m1 = CoherentMatchModeler.build_model(bundle_plain)
        m2 = CoherentMatchModeler.build_model(bundle_insuf)
        self.assertEqual(m1.outcomes_1x2, m2.outcomes_1x2)
        self.assertEqual(m1.families["GOALS_FT"].lambda_home, m2.families["GOALS_FT"].lambda_home)
        self.assertEqual(m1.families["GOALS_FT"].lambda_away, m2.families["GOALS_FT"].lambda_away)


if __name__ == "__main__":
    unittest.main()
