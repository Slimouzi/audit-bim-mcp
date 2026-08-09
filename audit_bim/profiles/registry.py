"""Registre des profils MCP clients/AMO.

I3F reste le profil par défaut et le seul profil pleinement opérationnel dans
ce dépôt. BIM in Motion est déclaré comme prochain profil cible : il compose les
mêmes briques génériques, mais aucune spécialisation I3F ne lui est appliquée.
"""

from __future__ import annotations

from .models import (
    ClassificationNarrativeSpec,
    ClientSpecialization,
    GenericModule,
    McpProfile,
    ReferenceFrameworkSpec,
    ReportNarrativeSpec,
    ReportStructureSpec,
)

DEFAULT_PROFILE_ID = "i3f"

_GENERIC_MODULES: tuple[GenericModule, ...] = (
    GenericModule(
        key="extraction",
        label="Extraction BIMData / snapshot",
        current_location="audit_bim/extraction + bimdata-read",
        target_package="bimdata-read",
        status="externalized",
        responsibility="Lire BIMData, normaliser un ModelSnapshot et gérer le cache snapshot.",
        next_step="Conserver audit_bim/extraction comme façade ; ne pas y ajouter de règles client.",
    ),
    GenericModule(
        key="geometry",
        label="Calculs IFC OpenShell",
        current_location="ifc-geometry-mcp",
        target_package="ifc-geometry-mcp",
        status="externalized",
        responsibility="Calculer les BaseQuantities et les surfaces d'enveloppe depuis l'IFC.",
        next_step="Faire consommer les contrats JSON par les MCP enfants, jamais des XLS intermédiaires.",
    ),
    GenericModule(
        key="audit_engine",
        label="Moteur d'audit",
        current_location="audit_bim/audit/engine + bim-audit-engine",
        target_package="bim-audit-engine",
        status="externalized",
        responsibility="Orchestrer des règles injectées et agréger des findings déterministes.",
        next_step="Garder les règles client dans les profils enfants.",
    ),
    GenericModule(
        key="query",
        label="Requêtes et sélections",
        current_location="audit_bim/query + bim-query",
        target_package="bim-query",
        status="externalized",
        responsibility="Filtrer snapshots, findings et suggestions sans réseau ni écriture.",
        next_step="Les futurs MCP doivent réutiliser les presets génériques avant d'en ajouter.",
    ),
    GenericModule(
        key="bcf",
        label="BCF",
        current_location="audit_bim/bcf + bim-publication",
        target_package="bim-publication",
        status="externalized",
        responsibility="Transformer des findings en payloads BCF et plans d'écriture.",
        next_step="La poussée BIMData reste dans la façade MCP avec confirmation et journal.",
    ),
    GenericModule(
        key="smartview",
        label="Smart Views BIMData",
        current_location="audit_bim/smartview + bim-publication",
        target_package="bim-publication",
        status="externalized",
        responsibility="Transformer des sélections en vues partageables BIMData.",
        next_step="Mutualiser les styles de sélection, pas les libellés de campagne client.",
    ),
    GenericModule(
        key="classifier",
        label="Classification",
        current_location="audit_bim/classifier",
        target_package="bim-classifier",
        status="in_repo",
        responsibility="Lire catalogues de classification, suggérer et préparer les corrections.",
        next_step="Extraire après avoir séparé tables génériques et tables propres aux clients.",
    ),
    GenericModule(
        key="doe",
        label="DOE",
        current_location="audit_bim/doe",
        target_package="bim-doe",
        status="in_repo",
        responsibility="Extraire, rapprocher et préparer l'enrichissement DOE vers IFC/BIMData.",
        next_step="Séparer extracteurs de formats et règles de rapprochement client.",
    ),
    GenericModule(
        key="enrichment",
        label="Enrichissement données publiques",
        current_location="audit_bim/enrichment",
        target_package="bim-enrichment",
        status="in_repo",
        responsibility="Enrichir un projet avec BAN, PLU, DPE et Géorisques.",
        next_step="Isoler les connecteurs publics des attentes de reporting client.",
    ),
    GenericModule(
        key="reporting",
        label="Reporting Word / Excel / PDF / PPT",
        current_location="audit_bim/reporting",
        target_package="bim-reporting",
        status="in_repo",
        responsibility="Produire des livrables à partir de snapshots, findings et contrats JSON.",
        next_step="Extraire le socle de rendu ; garder les packs MOA dans les profils enfants.",
    ),
)

_ALL_GENERIC_KEYS = tuple(m.key for m in _GENERIC_MODULES)

# ── Narratif I3F ─────────────────────────────────────────────────────────────
# Textes déplacés VERBATIM depuis ``audit_bim/reporting/word_report.py``.
# Aucune reformulation : le livrable I3F doit rester octet pour octet le même.

_I3F_THEME_HINTS: dict[str, str] = {
    "Hiérarchie spatiale": "compléter / corriger la hiérarchie spatiale Site → Bâtiment → Étage → Espace (CCH chap. 6.1)",
    "Nommage Site / Bâtiment / Étage": "aligner le nommage des sites, bâtiments et étages sur les listes fermées du CCH chap. 6.3",
    "Nommage Zone": "reprendre le nommage des zones (codification I3F, CCH chap. 6.3)",
    "Nommage Pièce": "reprendre le nommage des pièces (listes fermées, CCH chap. 6.3)",
    "Classification IFC": "compléter la classification IFC (UniFormat / Omniclass / table 3F)",
    "Propriété manquante": "renseigner les propriétés / Psets manquants pour la phase",
    "Propriété invalide": "corriger les valeurs de propriétés invalides ou hors domaine",
    "Quantités (surfaces, volumes)": "compléter les quantités (NetFloorArea / BaseQuantities)",
    "Document attendu": "fournir les documents attendus manquants",
}

_I3F_REPORT_NARRATIVE = ReportNarrativeSpec(
    theme_hints=_I3F_THEME_HINTS,
    classification_intro=(
        "Présence et cohérence de la classification IFC (UniFormat II par "
        "défaut ; Omniclass / CCI / table interne 3F selon le référentiel)."
    ),
    naming_intro=(
        "Contrôle du nommage des objets, niveaux, zones et espaces selon "
        "les listes fermées et la codification I3F (CCH chap. 6.3)."
    ),
    reference_documents_line=(
        "• Référentiel CCH I3F : documents transmis par la maîtrise "
        "d'ouvrage (Cahier des annexes, annexe Spécifications, annexe Nommage)."
    ),
    cover_reference_label="Référence du CCBIM utilisé",
    applied_reference_label="CCBIM appliqué",
    low_conformity_recommendation=(
        "Ré-itérer un audit après reprise : l'écart au CCH est important — "
        "prévoir une revue conjointe MOA / MOE avant la phase suivante."
    ),
)

# Valeurs HISTORIQUES, reprises à l'octet près. Ce sont des clés de gabarit :
# le classeur I3F doit rester ouvrable par les mêmes outils MOA qu'avant.
_I3F_REPORT_STRUCTURE = ReportStructureSpec(
    finding_reference_column_label="Référence CCH",
    referential_sheet_name="Référentiel I3F",
)

_I3F_CLASSIFICATION_NARRATIVE = ClassificationNarrativeSpec(
    default_system="UniFormat II",
    known_systems=("UniFormat", "Omniclass", "CCI"),
    proprietary_systems=("table 3F",),
    proprietary_label="table interne 3F",
)


_I3F_PROFILE = McpProfile(
    id="i3f",
    label="AMO BIM I3F",
    owner_name="I3F",
    audience="AMO BIM contrôlant des livrables CCH BIM I3F.",
    prompt_key="amo_bim_i3f",
    default_catalog_label="CCH BIM I3F V3.x",
    default_classification_system="UniFormat II",
    reference_framework=ReferenceFrameworkSpec(
        name="CCH BIM I3F",
        short_name="CCH",
        long_name="Cahier des Charges BIM I3F",
    ),
    report_narrative=_I3F_REPORT_NARRATIVE,
    classification_narrative=_I3F_CLASSIFICATION_NARRATIVE,
    report_structure=_I3F_REPORT_STRUCTURE,
    enabled_generic_modules=_ALL_GENERIC_KEYS,
    report_packs=("avp_i3f",),
    # Ordre historique de l'enregistrement : domaine (session/audit/reporting)
    # puis lecture/écriture. Il est repris tel quel — la surface MCP doit rester
    # identique au tri près, et le golden le vérifie.
    tool_modules=(
        # Socle partagé : cible, identité, lecture. Extrait du profil en E7,
        # une fois BIM in Motion là pour prouver que ces outils servent à un
        # autre AMO — et pas seulement à celui qui les a écrits.
        "audit_bim.tools_shared.session",
        "audit_bim.profiles.i3f.tools_session",
        "audit_bim.profiles.i3f.tools_audit",
        "audit_bim.profiles.i3f.tools_reporting",
        "audit_bim.profiles.i3f.tools_actions",
        "audit_bim.profiles.i3f.tools_query",
    ),
    prompt_module="audit_bim.profiles.i3f.prompts",
    legacy_alias_module="audit_bim.profiles.i3f.aliases",
    specializations=(
        ClientSpecialization(
            key="requirements_i3f",
            label="Catalogue CCH BIM I3F",
            current_location="audit_bim/requirements",
            status="ready",
            responsibility="Parser les annexes I3F et exposer RequirementsCatalog/BIMPhase.",
        ),
        ClientSpecialization(
            key="audit_rules_i3f",
            label="Règles d'audit I3F",
            current_location="audit_bim/audit/rules",
            status="ready",
            responsibility="Injecter les règles CCH I3F dans bim-audit-engine.",
        ),
        ClientSpecialization(
            key="report_pack_avp_i3f",
            label="Pack AVP I3F",
            current_location="audit_bim/reporting/avp",
            status="ready",
            responsibility=(
                "Produire les annexes XLSX produisibles et le rapport Word selon le "
                "modèle I3F. Plancher est bloqué faute de règle métier de sélection."
            ),
        ),
        ClientSpecialization(
            key="tools_i3f",
            label="Surface d'outils MCP I3F",
            current_location="audit_bim/profiles/i3f",
            status="ready",
            responsibility=(
                "45 outils métier I3F — session, audit, reporting, actions, "
                "requêtes — plus list_mcp_profiles transverse, soit 46 outils "
                "visibles. Les aliases LEGACY restent opt-in."
            ),
        ),
        ClientSpecialization(
            key="prompt_i3f",
            label="Prompt AMO BIM I3F",
            current_location="audit_bim/profiles/i3f/prompts.py",
            status="ready",
            responsibility="Cadrer Claude sur le référentiel et le vocabulaire I3F.",
        ),
    ),
    notes=(
        "Profil par défaut : aucun comportement historique I3F ne change.",
        "Les templates MOA I3F servent de gabarits, jamais de source d'identité projet.",
    ),
    is_default=True,
)

_BIM_IN_MOTION_PROFILE = McpProfile(
    id="bim_in_motion",
    label="AMO BIM in Motion",
    owner_name="BIM in Motion",
    audience="AMO BIM préparant des audits et livrables sur mesure pour ses clients finaux.",
    prompt_key="amo_bim_in_motion",
    default_catalog_label=None,
    default_classification_system=None,
    # Volontairement absent : BIM in Motion devra déclarer SON référentiel.
    # Un défaut hérité ici imprimerait « CCH BIM I3F » dans le rapport d'un
    # autre AMO — exactement l'accident que ce registre existe pour empêcher.
    reference_framework=None,
    # Idem : aucun narratif hérité. Un profil tiers doit écrire ses phrases,
    # pas récupérer celles d'I3F par défaut.
    report_narrative=None,
    classification_narrative=None,
    report_structure=None,
    enabled_generic_modules=_ALL_GENERIC_KEYS,
    report_packs=(),
    # Trois outils écrits pour ce profil, jamais copiés d'I3F : les équivalents
    # I3F portent phase BIM, système de classification et catalogue d'exigences,
    # qui appartiennent au référentiel d'I3F et non à un socle.
    tool_modules=(
        "audit_bim.tools_shared.session",
        "audit_bim.profiles.bim_in_motion.tools_session",
        "audit_bim.profiles.bim_in_motion.tools_mrn",
    ),
    prompt_module="audit_bim.profiles.bim_in_motion.prompts",
    # Les aliases LEGACY sont une dette d'I3F : un profil neuf n'en hérite pas.
    legacy_alias_module=None,
    target_tool_name="set_active_target",
    specializations=(
        ClientSpecialization(
            key="requirements_bim_in_motion",
            label="Référentiel client BIM in Motion",
            current_location=None,
            status="planned",
            responsibility="Brancher un référentiel par mission sans importer le CCH I3F.",
        ),
        ClientSpecialization(
            key="report_pack_bim_in_motion",
            label="Packs de rapports BIM in Motion",
            current_location=None,
            status="planned",
            responsibility="Composer Word, Excel, PDF et PPT depuis le socle reporting générique.",
        ),
        ClientSpecialization(
            key="prompt_bim_in_motion",
            label="Prompt AMO BIM in Motion",
            current_location="audit_bim/profiles/bim_in_motion/prompts.py",
            status="ready",
            responsibility="Décrire la posture AMO BIM in Motion et les questions de cadrage client.",
        ),
        ClientSpecialization(
            key="tools_bim_in_motion",
            label="Surface d'outils MCP BIM in Motion",
            current_location="audit_bim/profiles/bim_in_motion",
            status="ready",
            responsibility=(
                "1 outil de cible + 1 de couverture MRN, plus 5 outils du socle "
                "partagé et list_mcp_profiles transverse, soit 8 outils visibles. "
                "Aucun import du profil I3F."
            ),
        ),
    ),
    notes=(
        "Profil minimal : il ne doit pas activer le pack AVP I3F.",
        "Second consommateur réel des briques neutres — c'est ce qui rend "
        "mesurable ce qui est vraiment générique.",
        "Le nouveau MCP doit dépendre des briques génériques, pas des modules du profil I3F.",
    ),
)

_DOMOFRANCE_PROFILE = McpProfile(
    id="domofrance",
    label="AMO Domofrance",
    owner_name="Domofrance",
    audience=("Équipe confrontant une maquette au référentiel de contrôle d'un bailleur social."),
    prompt_key="amo_bim_domofrance",
    default_catalog_label=None,
    default_classification_system=None,
    # Volontairement absents, comme pour tout profil tiers : un référentiel ou
    # un narratif hérité imprimerait dans le rapport d'un maître d'ouvrage le
    # vocabulaire d'un autre.
    reference_framework=None,
    report_narrative=None,
    classification_narrative=None,
    report_structure=None,
    enabled_generic_modules=_ALL_GENERIC_KEYS,
    report_packs=(),
    # Deux modules propres, aucun copié d'un profil frère. Le socle partagé
    # fournit les cinq outils de session ; `tools_profiles` ajoute l'outil
    # transverse. Total exposé : 8 outils.
    tool_modules=(
        "audit_bim.tools_shared.session",
        "audit_bim.profiles.domofrance.tools_session",
        "audit_bim.profiles.domofrance.tools_coverage",
    ),
    prompt_module="audit_bim.profiles.domofrance.prompts",
    # Les aliases LEGACY sont une dette d'I3F : un profil neuf n'en hérite pas.
    legacy_alias_module=None,
    target_tool_name="set_active_target",
    specializations=(
        ClientSpecialization(
            key="controls_domofrance",
            label="Référentiel de contrôle Domofrance",
            current_location="audit_bim/profiles/domofrance/controls.py",
            status="ready",
            responsibility=(
                "Décrire le classeur du maître d'ouvrage : lignes, doublons, "
                "signaux lexicaux et tables de surfaces. Aucune maquette lue."
            ),
        ),
        ClientSpecialization(
            key="coverage_domofrance",
            label="Évaluabilité mesurée Domofrance",
            current_location="audit_bim/profiles/domofrance/coverage.py",
            status="ready",
            responsibility=(
                "Croiser le référentiel avec un document de preuves géométriques "
                "et dire ce qui pourra être tranché — jamais ce qui est conforme."
            ),
        ),
        ClientSpecialization(
            key="prompt_domofrance",
            label="Prompt AMO Domofrance",
            current_location="audit_bim/profiles/domofrance/prompts.py",
            status="ready",
            responsibility=(
                "Décrire la posture de diagnostic d'évaluabilité et interdire "
                "tout verdict de conformité dans les réponses."
            ),
        ),
        ClientSpecialization(
            key="report_pack_domofrance",
            label="Packs de rapports Domofrance",
            current_location=None,
            status="planned",
            responsibility=(
                "Composer un livrable depuis le socle reporting générique, le "
                "jour où un besoin réel existera."
            ),
        ),
    ),
    notes=(
        "Profil de diagnostic : il mesure l'évaluabilité, il ne juge pas.",
        "Aucun statut de conformité, aucune écriture dans le classeur du client.",
        "Le document de preuves géométriques est fourni par l'appelant ; ce "
        "profil ne le fabrique pas.",
    ),
)

_PROFILES: tuple[McpProfile, ...] = (_I3F_PROFILE, _BIM_IN_MOTION_PROFILE, _DOMOFRANCE_PROFILE)


def list_generic_modules() -> tuple[GenericModule, ...]:
    """Renvoie le catalogue des briques réutilisables."""
    return _GENERIC_MODULES


def list_profiles() -> tuple[McpProfile, ...]:
    """Renvoie les profils client connus, I3F en premier."""
    return _PROFILES


def get_profile(profile_id: str) -> McpProfile:
    """Retourne un profil par identifiant, ou lève ``KeyError``."""
    normalized = (profile_id or "").strip().lower().replace("-", "_")
    for profile in _PROFILES:
        if profile.id == normalized:
            return profile
    raise KeyError(profile_id)


def profiles_payload(profile_id: str | None = None) -> dict:
    """Payload JSON-friendly exposé par le tool MCP."""
    selected = None
    profiles = list_profiles()
    if profile_id:
        selected = get_profile(profile_id)
        profiles = (selected,)
    return {
        "status": "ok",
        "default_profile_id": DEFAULT_PROFILE_ID,
        "profile_id": selected.id if selected else None,
        "generic_modules": [m.to_dict() for m in list_generic_modules()],
        "profiles": [p.to_dict() for p in profiles],
        "next_mcp_rule": (
            "Un MCP client compose les modules génériques et ajoute uniquement "
            "ses prompts, référentiels, règles et packs de rapports spécifiques."
        ),
    }
