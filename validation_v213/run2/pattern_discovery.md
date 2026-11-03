# Descubrimiento híbrido de patrones V2

Corte: `2026-11-02T21:32:35.469161+01:00` · preselection SHA: `20218d6ef05c505428709f74b43749f0375de4e4c67035f14dbafbd3821c237e`

Patrones descriptivos: **357** · estados: `{"DESCRIPTIVE_SUPPORTED": 32, "EXPLORATORY_UNVALIDATED": 151, "INSUFFICIENT_DATA": 174}`.

| Fixture | Lado | Métrica/segmento | Modo | Estado | n | Efecto descriptivo |
|---:|---|---|---|---|---:|---|
| 1492442 | home | goals / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | DESCRIPTIVE_SUPPORTED | 7/7 | `{"mean_difference": 0.5714, "direction": "OWN_HIGHER_THAN_RIVAL_CONCESSION"}` |
| 1492442 | home | corners / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | INSUFFICIENT_DATA | 3/4 | `{"mean_difference": -0.4167, "direction": "OWN_LOWER_THAN_RIVAL_CONCESSION"}` |
| 1492442 | home | shots / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | INSUFFICIENT_DATA | 3/4 | `{"mean_difference": 9.5833, "direction": "OWN_HIGHER_THAN_RIVAL_CONCESSION"}` |
| 1492442 | home | on_target / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | INSUFFICIENT_DATA | 3/4 | `{"mean_difference": 5.5833, "direction": "OWN_HIGHER_THAN_RIVAL_CONCESSION"}` |
| 1492442 | home | fouls / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | INSUFFICIENT_DATA | 3/4 | `{"mean_difference": -1.8333, "direction": "OWN_LOWER_THAN_RIVAL_CONCESSION"}` |
| 1492442 | home | yellow / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | INSUFFICIENT_DATA | 1/4 | `{"mean_difference": -0.25, "direction": "OWN_LOWER_THAN_RIVAL_CONCESSION"}` |
| 1492442 | home | offsides / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | INSUFFICIENT_DATA | 3/2 | `{"mean_difference": 0.5, "direction": "OWN_HIGHER_THAN_RIVAL_CONCESSION"}` |
| 1492442 | home | xg / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | INSUFFICIENT_DATA | 3/4 | `{"mean_difference": 1.365, "direction": "OWN_HIGHER_THAN_RIVAL_CONCESSION"}` |
| 1492442 | away | goals / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | DESCRIPTIVE_SUPPORTED | 7/7 | `{"mean_difference": 0.8571, "direction": "OWN_HIGHER_THAN_RIVAL_CONCESSION"}` |
| 1492442 | away | corners / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | INSUFFICIENT_DATA | 4/3 | `{"mean_difference": -1.0833, "direction": "OWN_LOWER_THAN_RIVAL_CONCESSION"}` |
| 1492442 | away | shots / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | INSUFFICIENT_DATA | 4/3 | `{"mean_difference": -2.25, "direction": "OWN_LOWER_THAN_RIVAL_CONCESSION"}` |
| 1492442 | away | on_target / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | INSUFFICIENT_DATA | 4/3 | `{"mean_difference": -2.0, "direction": "OWN_LOWER_THAN_RIVAL_CONCESSION"}` |
| 1492442 | away | fouls / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | INSUFFICIENT_DATA | 4/3 | `{"mean_difference": 5.5, "direction": "OWN_HIGHER_THAN_RIVAL_CONCESSION"}` |
| 1492442 | away | yellow / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | INSUFFICIENT_DATA | 4/1 | `{"mean_difference": 1.25, "direction": "OWN_HIGHER_THAN_RIVAL_CONCESSION"}` |
| 1492442 | away | offsides / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | INSUFFICIENT_DATA | 2/3 | `{"mean_difference": 0.1667, "direction": "OWN_HIGHER_THAN_RIVAL_CONCESSION"}` |
| 1492442 | away | xg / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | INSUFFICIENT_DATA | 4/3 | `{"mean_difference": -0.3517, "direction": "OWN_LOWER_THAN_RIVAL_CONCESSION"}` |
| 1492442 | home | goals / HOME_VS_AWAY | EXPLORATORY | EXPLORATORY_UNVALIDATED | 7/8 | `{"home_minus_away_mean": 0.8214, "direction": "HOME_HIGHER"}` |
| 1492442 | home | goals / RECENT_VS_PREVIOUS | EXPLORATORY | EXPLORATORY_UNVALIDATED | 5/5 | `{"recent_minus_previous_mean": 0.8, "direction": "RECENT_HIGHER"}` |
| 1492442 | home | corners / RECENT_VS_PREVIOUS | EXPLORATORY | EXPLORATORY_UNVALIDATED | 5/3 | `{"recent_minus_previous_mean": 1.4, "direction": "RECENT_HIGHER"}` |
| 1492442 | home | shots / RECENT_VS_PREVIOUS | EXPLORATORY | EXPLORATORY_UNVALIDATED | 5/3 | `{"recent_minus_previous_mean": 3.2667, "direction": "RECENT_HIGHER"}` |
| 1492442 | home | on_target / RECENT_VS_PREVIOUS | EXPLORATORY | EXPLORATORY_UNVALIDATED | 5/3 | `{"recent_minus_previous_mean": 0.9333, "direction": "RECENT_HIGHER"}` |
| 1492442 | home | fouls / RECENT_VS_PREVIOUS | EXPLORATORY | EXPLORATORY_UNVALIDATED | 5/3 | `{"recent_minus_previous_mean": 0.4, "direction": "RECENT_HIGHER"}` |
| 1492442 | home | yellow / RECENT_VS_PREVIOUS | EXPLORATORY | EXPLORATORY_UNVALIDATED | 3/3 | `{"recent_minus_previous_mean": 0.3333, "direction": "RECENT_HIGHER"}` |
| 1492442 | home | offsides / RECENT_VS_PREVIOUS | EXPLORATORY | EXPLORATORY_UNVALIDATED | 5/3 | `{"recent_minus_previous_mean": 0.6667, "direction": "RECENT_HIGHER"}` |
| 1492442 | home | xg / RECENT_VS_PREVIOUS | EXPLORATORY | EXPLORATORY_UNVALIDATED | 5/3 | `{"recent_minus_previous_mean": 1.0627, "direction": "RECENT_HIGHER"}` |
| 1492442 | home | shots__corners / PAIRED_FIXTURE_ASSOCIATION | EXPLORATORY | EXPLORATORY_UNVALIDATED | 8/8 | `{"pearson": 0.6203, "paired_n": 8}` |
| 1492442 | home | blocked_shots__corners / PAIRED_FIXTURE_ASSOCIATION | EXPLORATORY | EXPLORATORY_UNVALIDATED | 8/8 | `{"pearson": 0.4505, "paired_n": 8}` |
| 1492442 | home | fouls__yellow / PAIRED_FIXTURE_ASSOCIATION | EXPLORATORY | EXPLORATORY_UNVALIDATED | 6/6 | `{"pearson": -0.0407, "paired_n": 6}` |
| 1492442 | away | goals / HOME_VS_AWAY | EXPLORATORY | EXPLORATORY_UNVALIDATED | 6/7 | `{"home_minus_away_mean": -1.0, "direction": "AWAY_HIGHER"}` |
| 1492442 | away | goals / RECENT_VS_PREVIOUS | EXPLORATORY | EXPLORATORY_UNVALIDATED | 5/5 | `{"recent_minus_previous_mean": -0.8, "direction": "RECENT_LOWER"}` |
| 1492442 | away | shots__corners / PAIRED_FIXTURE_ASSOCIATION | EXPLORATORY | EXPLORATORY_UNVALIDATED | 7/7 | `{"pearson": 0.8038, "paired_n": 7}` |
| 1492442 | away | blocked_shots__corners / PAIRED_FIXTURE_ASSOCIATION | EXPLORATORY | EXPLORATORY_UNVALIDATED | 7/7 | `{"pearson": 0.1543, "paired_n": 7}` |
| 1492442 | away | fouls__yellow / PAIRED_FIXTURE_ASSOCIATION | EXPLORATORY | EXPLORATORY_UNVALIDATED | 7/7 | `{"pearson": -0.0568, "paired_n": 7}` |
| 1493215 | home | goals / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | DESCRIPTIVE_SUPPORTED | 9/10 | `{"mean_difference": 0.3333, "direction": "OWN_HIGHER_THAN_RIVAL_CONCESSION"}` |
| 1493215 | home | corners / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | DESCRIPTIVE_SUPPORTED | 4/4 | `{"mean_difference": -2.0, "direction": "OWN_LOWER_THAN_RIVAL_CONCESSION"}` |
| 1493215 | home | shots / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | DESCRIPTIVE_SUPPORTED | 4/4 | `{"mean_difference": -2.5, "direction": "OWN_LOWER_THAN_RIVAL_CONCESSION"}` |
| 1493215 | home | on_target / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | DESCRIPTIVE_SUPPORTED | 4/4 | `{"mean_difference": 0.25, "direction": "OWN_HIGHER_THAN_RIVAL_CONCESSION"}` |
| 1493215 | home | fouls / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | DESCRIPTIVE_SUPPORTED | 4/4 | `{"mean_difference": 1.0, "direction": "OWN_HIGHER_THAN_RIVAL_CONCESSION"}` |
| 1493215 | home | yellow / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | DESCRIPTIVE_SUPPORTED | 4/4 | `{"mean_difference": 0.25, "direction": "OWN_HIGHER_THAN_RIVAL_CONCESSION"}` |
| 1493215 | home | offsides / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | DESCRIPTIVE_SUPPORTED | 4/4 | `{"mean_difference": 1.25, "direction": "OWN_HIGHER_THAN_RIVAL_CONCESSION"}` |
| 1493215 | home | xg / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | DESCRIPTIVE_SUPPORTED | 4/4 | `{"mean_difference": 0.62, "direction": "OWN_HIGHER_THAN_RIVAL_CONCESSION"}` |
| 1493215 | away | goals / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | DESCRIPTIVE_SUPPORTED | 10/9 | `{"mean_difference": 0.0889, "direction": "OWN_HIGHER_THAN_RIVAL_CONCESSION"}` |
| 1493215 | away | corners / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | DESCRIPTIVE_SUPPORTED | 4/4 | `{"mean_difference": -4.0, "direction": "OWN_LOWER_THAN_RIVAL_CONCESSION"}` |
| 1493215 | away | shots / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | DESCRIPTIVE_SUPPORTED | 4/4 | `{"mean_difference": -2.0, "direction": "OWN_LOWER_THAN_RIVAL_CONCESSION"}` |
| 1493215 | away | on_target / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | DESCRIPTIVE_SUPPORTED | 4/4 | `{"mean_difference": 0.5, "direction": "OWN_HIGHER_THAN_RIVAL_CONCESSION"}` |
| 1493215 | away | fouls / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | DESCRIPTIVE_SUPPORTED | 4/4 | `{"mean_difference": -1.5, "direction": "OWN_LOWER_THAN_RIVAL_CONCESSION"}` |
| 1493215 | away | yellow / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | DESCRIPTIVE_SUPPORTED | 4/4 | `{"mean_difference": -0.25, "direction": "OWN_LOWER_THAN_RIVAL_CONCESSION"}` |
| 1493215 | away | offsides / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | EXPLORATORY_UNVALIDATED | 4/4 | `{"mean_difference": 0.0, "direction": "ALIGNED"}` |
| 1493215 | away | xg / CURRENT_VENUE_VS_RIVAL_MATCHING_VENUE | EXPLICIT | DESCRIPTIVE_SUPPORTED | 4/4 | `{"mean_difference": 0.38, "direction": "OWN_HIGHER_THAN_RIVAL_CONCESSION"}` |
| 1493215 | home | goals / HOME_VS_AWAY | EXPLORATORY | EXPLORATORY_UNVALIDATED | 9/10 | `{"home_minus_away_mean": 0.2333, "direction": "HOME_HIGHER"}` |

La exploración de asociaciones múltiples queda `EXPLORATORY_UNVALIDATED`; correlación no implica causalidad. Jugadores, periodos, entrenadores, árbitros y PPDA no se fabrican.
