"""Orchestrateur du pack de livrables AVP I3F.

Assemble les sources maquette-first et appelle chaque builder ; applique la QA
gate anti-livrable vide. Dépend de tous les builders (jamais l'inverse).
"""

from __future__ import annotations

import re
import zipfile
from datetime import datetime
from pathlib import Path

from bim_reporting.pdf import docx_to_pdf

from ...audit.engine import AuditResult
from ...extraction.model_data import ModelSnapshot
from ..avp_report_catalog import REPORT_SPECS_BY_KEY
from ..avp_snapshot import (
    count_candidate_envelope_walls,
    count_menuiseries,
    count_menuiseries_with_dimensions,
    count_planchers,
    count_planchers_with_area,
    count_spaces_with_area,
)
from ..avp_sources import (
    AvpSourcePaths,
    AvpSources,
    ControleMaquettesSource,
    load_sources,
)
from ..context import ReportProjectContext
from .docx_analyse import _build_analyse_bim_avp_docx
from .models import (
    _CONTROLE_STATS_SHEETS,
    AvpMeta,
    AvpQaError,
    AvpReportPack,
    AvpReportSelectionError,
    _deliverable_filename,
)
from .xlsx_common import _count_business_rows
from .xlsx_controle import (
    _audit_controle_table,
    _audit_stats,
    _build_controle_maquettes_xlsx,
    _count_controle_rows,
)
from .xlsx_enveloppe import _build_enveloppe_xlsx
from .xlsx_menuiseries import _build_menuiseries_xlsx
from .xlsx_plancher import _build_plancher_xlsx
from .xlsx_shab import build_shab_xlsx
from .xlsx_zones import build_zones_xlsx


def _controle_from_audit_or_metadata(
    result: AuditResult | None, src: ControleMaquettesSource | None
) -> ControleMaquettesSource | None:
    """Construit la source Contrôle utilisée par les livrables.

    Les données métier mesurables (grille + stats) viennent de l'``AuditResult``.
    La source MOA éventuelle fournit l'entête, la légende et surtout le classeur
    template à conserver pour rester conforme au livrable maître d'ouvrage.
    """
    header = dict(src.header) if src and src.header else {}
    legend = dict(src.legend) if src and src.legend else {}
    template_path = getattr(src, "template_path", None)
    stat_grids = dict(src.stat_grids) if src and src.stat_grids else {}
    grille = _audit_controle_table(result)
    stats: dict[str, dict] = {}
    if result is not None:
        for name in _CONTROLE_STATS_SHEETS:
            value = _audit_stats(name, result)
            if value:
                stats[name] = value
    if not header and not legend and grille is None and not stats and not template_path:
        return None
    return ControleMaquettesSource(
        template_path=template_path,
        header=header,
        legend=legend,
        grille=grille,
        stats=stats,
        stat_grids=stat_grids,
    )


def _ifc_first_sources(
    *,
    model_sources: AvpSources,
    source_inputs: AvpSources | None,
    result: AuditResult | None,
) -> AvpSources:
    """Assemble les sources de génération en donnant priorité à la maquette.

    Les cinq exports métriques sont ceux calculés depuis le snapshot IFC. Les
    fichiers MOA fournis ne sont conservés que pour les métadonnées non
    métriques qui ne se déduisent pas de la géométrie (ex. seuil 3F).
    """
    ctrl_src = source_inputs.controle if source_inputs else None
    input_env = source_inputs.enveloppe if source_inputs else None
    # Exception importante : l'enveloppe peut venir du contrat structuré
    # ``envelope.json`` produit par ifc-geometry (par_type / hors_filtre_type).
    # Ce n'est pas un XLS MOA externe : c'est la source IFC/OpenShell métier et
    # elle doit primer sur le repli snapshot élémentaire.
    env = input_env if _is_envelope_json_source(input_env) else model_sources.enveloppe
    if env is not None and input_env is not None and not _is_envelope_json_source(input_env):
        # Le seuil est une règle/paramètre documentaire, pas une mesure externe.
        if input_env.seuil_3f is not None:
            env.seuil_3f = input_env.seuil_3f
    return AvpSources(
        controle=_controle_from_audit_or_metadata(result, ctrl_src),
        shab=model_sources.shab,
        zones_espaces=model_sources.zones_espaces,
        enveloppe=env,
        menuiseries=model_sources.menuiseries,
        plancher=model_sources.plancher,
    )


def _is_envelope_json_source(src) -> bool:
    """Vrai pour une source issue du contrat ``envelope.json`` ifc-geometry."""
    if src is None:
        return False
    if getattr(src, "superficie_calque_total", None) is not None:
        return True
    if getattr(src, "hors_filtre_type", None):
        return True
    table = getattr(src, "table", None)
    headers = getattr(table, "headers", None) or []
    return "Surface IFC OpenShell" in headers and "IFC OpenShell Surface des Fenêtres" in headers


#: Correspondance clé catalogue → libellé employé par les gates QA, qui parlent
#: la langue du livrable (« SHAB ») et non celle du catalogue (« shab_maquette »).
_LIBELLE_QA_PAR_CLE = {
    "controle_maquettes": "Contrôle",
    "shab_maquette": "SHAB",
    "zones_espaces": "Zones/Espaces",
    "surface_enveloppe": "Enveloppe",
    "menuiseries": "Menuiseries",
    "plancher": "Plancher",
}


def _normalize_report_selection(reports) -> frozenset[str]:
    """Sélection **normalisée** : dédupliquée, validée, réduite au produisible.

    Un seul endroit décide de ce qui sera produit — et donc de ce qui doit être
    contrôlé. Deux copies de cette règle diborderaient : la façade MCP refusait
    les clés invalides pendant que le cœur les ignorait, et les gates QA
    préflightaient des rapports que la sélection n'avait pas demandés.

    ``None`` = tous les rapports non bloqués. Sinon la sélection est validée :
    clé inconnue ou bloquée ⇒ :class:`AvpReportSelectionError`, levée **avant
    toute écriture**.
    """
    produisibles = frozenset(
        cle for cle, spec in REPORT_SPECS_BY_KEY.items() if spec.blocked_reason is None
    )
    if reports is None:
        return produisibles
    demandes = list(dict.fromkeys(reports))
    inconnues = tuple(sorted(set(demandes) - set(REPORT_SPECS_BY_KEY)))
    bloques = {
        cle: REPORT_SPECS_BY_KEY[cle].blocked_reason
        for cle in sorted(set(demandes) & set(REPORT_SPECS_BY_KEY))
        if REPORT_SPECS_BY_KEY[cle].blocked_reason is not None
    }
    if inconnues or bloques:
        raise AvpReportSelectionError(
            unknown=inconnues, blocked=bloques, known=tuple(sorted(REPORT_SPECS_BY_KEY))
        )
    return frozenset(demandes)


def write_avp_i3f_report_pack(
    result: AuditResult | None,
    output_dir: str | Path,
    *,
    sources: AvpSourcePaths | AvpSources | None = None,
    snapshot: ModelSnapshot | None = None,
    project_name: str = "Projet",
    project_code: str = "",
    phase: str = "AVP",
    auditor: str = "AMO BIM",
    date: str | None = None,
    usages_bim: list[str] | None = None,
    nombre_logements: str | None = None,
    temoin_virtuel: str | None = None,
    date_controle: str | None = None,
    auteur_controle: str | None = None,
    reports: set[str] | frozenset[str] | None = None,
    export_pdf: bool = True,
    context: ReportProjectContext | None = None,  # noqa: ARG001 (compat future)
) -> AvpReportPack:
    """Génère le pack de livrables AVP I3F dans ``output_dir``.

    Les noms de livrables suivent la convention documentaire I3F,
    **générés à partir des données projet confirmées** :
    ``YYMMDD <NomProjet> <CodeProjet> <Phase> - <TypeLivrable>.<ext>``.

    Args:
        result: ``AuditResult`` BIMData (peut être ``None`` si ``snapshot`` est
            fourni explicitement).
        output_dir: dossier de sortie (créé si besoin).
        sources: chemins des .xlsx I3F (``AvpSourcePaths``) ou sources déjà
            chargées (``AvpSources``). Quand un snapshot existe, ces sources
            ne fournissent pas les surfaces : les exports métriques sont
            recalculés depuis la maquette IFC.
        project_name, project_code, phase: identité projet **confirmée**
            injectée dans les noms de livrables (et les entêtes).
        date: préfixe daté ``YYMMDD`` des noms de livrables. ``None`` →
            date de génération (aujourd'hui).
        usages_bim, nombre_logements, temoin_virtuel, date_controle,
            auteur_controle: métadonnées opérationnelles du contrôle (issues
            du rapport I3F de référence) pour les sections « Données
            d'entrée » et « Usages BIM 3F ». Absentes → ``NOT_AVAILABLE``.
            Exception : ``auteur_controle`` non fourni est aligné sur
            ``auditor`` (le rédacteur AMO est aussi l'auteur du contrôle
            par défaut) plutôt que ``NOT_AVAILABLE``.
        export_pdf: tente la conversion .docx → .pdf (best-effort).
    """
    # Le dossier n'est créé qu'APRÈS le préflight (plus bas) : un refus ne doit
    # rien laisser derrière lui, pas même un dossier vide.
    out = Path(output_dir)
    # Date de génération du livrable (YYMMDD) si non imposée par l'appelant.
    gen_date = (
        date.strip()
        if isinstance(date, str) and date.strip()
        else datetime.now().strftime("%y%m%d")
    )
    # « Auteur du contrôle » : champ opérationnel distinct du rédacteur
    # AMO (``auditor``). Sur le pack I3F il est facultatif ; plutôt que
    # d'écrire ``NOT_AVAILABLE`` quand il n'est pas précisé, on l'aligne
    # sur ``auditor`` (donnée fournie par l'utilisateur, jamais inventée).
    # L'appelant peut toujours le distinguer en passant ``auteur_controle``.
    eff_auteur_controle = (
        auteur_controle if auteur_controle and auteur_controle.strip() else auditor
    )
    meta = AvpMeta(
        project_name=project_name,
        project_code=project_code,
        phase=phase,
        auditor=auditor,
        usages_bim=usages_bim,
        nombre_logements=nombre_logements,
        temoin_virtuel=temoin_virtuel,
        date_controle=date_controle,
        auteur_controle=eff_auteur_controle,
    )

    # Noms de livrables générés depuis l'identité projet confirmée
    # (convention I3F uniforme). On n'hérite plus du basename des sources :
    # le livrable BIMData porte l'identité et la date de génération.
    def _name(key: str) -> str:
        return _deliverable_filename(
            key,
            date=gen_date,
            project_name=project_name,
            project_code=project_code,
            phase=phase,
        )

    fn_controle = _name("controle")
    fn_shab = _name("shab")
    fn_zones = _name("zones_espaces")
    fn_env = _name("enveloppe")
    fn_men = _name("menuiseries")
    fn_plancher = _name("plancher")
    fn_analyse = _name("analyse")

    if isinstance(sources, AvpSourcePaths):
        sources = load_sources(sources)
    source_inputs = sources

    # ── Maquette-first / IFC OpenShell ──────────────────────────────────
    # Le snapshot est pris explicitement (``snapshot=``, ex. après
    # ``verify_active_model`` sans audit), sinon depuis ``result.snapshot``.
    # Dès qu'il existe, il devient la source autoritaire des exports métriques.
    snap = snapshot if snapshot is not None else (result.snapshot if result is not None else None)

    # La sélection est normalisée AVANT tout préflight : elle décide de ce qui
    # sera produit, donc de ce qui doit être contrôlé. Préflighter un rapport
    # non demandé refuserait un pack pour un livrable qu'on n'écrit pas.
    selection = _normalize_report_selection(reports)

    # ── PRÉFLIGHT : refuser AVANT d'écrire quoi que ce soit ─────────────
    # Les quantités manquantes se voient sur le snapshot, sans rien produire.
    # Contrôler après génération laisserait un dossier de livrables non
    # conformes sur disque malgré le statut d'erreur — le piège même que cette
    # gate doit fermer. Les gates qui nécessitent de LIRE les fichiers produits
    # (annexes vides) restent nécessairement en aval.
    attendus = {_LIBELLE_QA_PAR_CLE[c] for c in selection}
    sans_quantites = [x for x in _qa_missing_quantities(snap) if x in attendus]
    if sans_quantites:
        raise AvpQaError(sans_quantites, kind="missing_quantities")

    if snap is not None:
        # ``build_sources_from_snapshot`` est ré-exporté par la façade ``avp_i3f``
        # et résolu via elle (point de patch historique des tests). Import
        # paresseux : la façade importe ``pack``, on évite ainsi le cycle à
        # l'import (au runtime la façade est entièrement chargée).
        from .. import avp_i3f as _avp_i3f

        sources = _ifc_first_sources(
            model_sources=_avp_i3f.build_sources_from_snapshot(snap),
            source_inputs=source_inputs,
            result=result,
        )
    elif sources is None:
        sources = AvpSources()

    # ── QA gate : anti-enveloppe calculée sans filtre I3F ────────────────
    # AVANT ``out.mkdir()`` et avant tout builder : une gate qui refuse après
    # génération laisse le livrable faux sur disque malgré le statut d'erreur.
    # C'est la même règle que pour ``missing_quantities`` ci-dessus.
    #
    # Le contrat d'enveloppe déclare le filtre RÉELLEMENT appliqué
    # (``diagnostics.filters.mode``). En mode ``geometric``, le backend retient
    # les murs sur un critère purement géométrique : sur une maquette ArchiCAD
    # I3F il compte des cloisons et des refends que le gabarit n'attend pas, et
    # rejette au passage des types d'enveloppe légitimes. Le livrable est alors
    # plausible ET faux — il ne se distingue qu'en le comparant au modèle.
    mode_env = (
        _qa_envelope_filter_mode(sources.enveloppe if sources else None)
        if "surface_enveloppe" in selection
        else None
    )
    if mode_env == "geometric":
        raise AvpQaError(["Extraction surface enveloppe"], kind="envelope_filter_mode")

    out.mkdir(parents=True, exist_ok=True)

    # ``selection`` porte déjà la règle : demandé ET non bloqué.
    def _produit(cle: str) -> bool:
        return cle in selection

    controle = (
        _build_controle_maquettes_xlsx(out / fn_controle, result, sources, meta, snap)
        if _produit("controle_maquettes")
        else None
    )
    shab = (
        build_shab_xlsx(out / fn_shab, (sources.shab if sources else None), meta)
        if _produit("shab_maquette")
        else None
    )
    zones = (
        build_zones_xlsx(out / fn_zones, (sources.zones_espaces if sources else None), meta)
        if _produit("zones_espaces")
        else None
    )
    enveloppe = (
        _build_enveloppe_xlsx(out / fn_env, sources, meta)
        if _produit("surface_enveloppe")
        else None
    )
    menuiseries = (
        _build_menuiseries_xlsx(out / fn_men, sources, meta) if _produit("menuiseries") else None
    )
    plancher = (
        _build_plancher_xlsx(out / fn_plancher, sources, meta) if _produit("plancher") else None
    )
    analyse = _build_analyse_bim_avp_docx(
        out / fn_analyse, result, sources, meta, snap, controle_xlsx=controle
    )

    pdf = docx_to_pdf(analyse) if export_pdf else None

    pack = AvpReportPack(
        controle_xlsx=controle,
        shab_xlsx=shab,
        zones_espaces_xlsx=zones,
        enveloppe_xlsx=enveloppe,
        menuiseries_xlsx=menuiseries,
        plancher_xlsx=plancher,
        analyse_docx=analyse,
        analyse_pdf=pdf,
    )

    # ── QA gate : anti-livrable vide ────────────────────────────────────
    # On rouvre chaque annexe et on compte les lignes métier. Échec si un
    # export sort sans ligne alors que la maquette contient des entités
    # exploitables (espaces / murs / zones). On lève : le tool renverra un
    # statut d'erreur explicite plutôt qu'un fichier vide.
    empty = _qa_empty_deliverables(pack, snap, result)
    if empty:
        raise AvpQaError(empty)

    # ── QA gate : anti-marque d'outil tiers ─────────────────────────────
    # Filet de sécurité indépendant des purges ciblées : on inspecte le XML
    # réel des fichiers produits. Une purge peut rater un emplacement (nouvel
    # onglet MOA, note de bas de page, en-tête) ; ce contrôle, lui, ne dépend
    # d'aucune hypothèse sur l'endroit où la marque se cache.
    contamines = _qa_external_tool_mentions(pack)
    if contamines:
        raise AvpQaError(contamines, kind="external_tool_mention")

    return pack


def _qa_envelope_filter_mode(env) -> str | None:
    """Mode de filtrage déclaré par le contrat d'enveloppe, ou ``None``.

    ``None`` couvre deux cas qu'on ne doit PAS refuser : pas d'enveloppe du
    tout, et une enveloppe qui ne vient pas d'un contrat structuré (repli
    snapshot ou .xlsx source). Le refus ne vise que le cas mesurable — un
    contrat qui dit lui-même avoir été calculé sans filtre.
    """
    return getattr(env, "filter_mode", None)


def _multisheet_is_empty(multi) -> bool:
    """Un ``MultiSheetSource`` est vide si aucun onglet ne porte de ligne."""
    if multi is None:
        return True
    grids = getattr(multi, "grids", None) or []
    return not any(getattr(g, "rows", None) for g in grids)


def _tabular_is_empty(src) -> bool:
    """Une source tabulaire (enveloppe/menuiseries) est vide sans lignes."""
    if src is None:
        return True
    table = getattr(src, "table", None)
    return table is None or not getattr(table, "rows", None)


def _qa_empty_deliverables(
    pack: AvpReportPack, snap, result: AuditResult | None = None
) -> list[str]:
    """Liste des annexes vides alors que la maquette a des données."""
    if snap is None:
        return []
    problems: list[str] = []
    # 5ᵉ annexe : quand un audit est disponible, la « Grille de contrôle » doit
    # porter des points de contrôle réels (comptés SOUS son titre, hors entête/
    # légende/NOT_AVAILABLE). Vide malgré un audit = livrable non exploitable.
    # Une annexe NON PRODUITE — non demandée, ou bloquée — ne peut pas être
    # « vide » : ne la contrôler qu'à condition qu'elle existe.
    if result is not None and pack.controle_xlsx is not None:
        expected_controle = _audit_controle_table(result) is not None or not result.findings
        if expected_controle and _count_controle_rows(pack.controle_xlsx) == 0:
            problems.append("Contrôle")
    has_spaces_or_zones = bool(getattr(snap, "spaces", None)) or bool(getattr(snap, "zones", None))
    if has_spaces_or_zones:
        if pack.shab_xlsx is not None and _count_business_rows(pack.shab_xlsx) == 0:
            problems.append("SHAB")
        if (
            pack.zones_espaces_xlsx is not None
            and _count_business_rows(pack.zones_espaces_xlsx) == 0
        ):
            problems.append("Zones/Espaces")
    # Murs CANDIDATS, pas murs à calque reconnu : ``count_envelope_walls`` est
    # layer-first (ArchiCAD) et tombe à zéro sur un export Revit, ce qui faisait
    # taire cette gate précisément sur les maquettes où l'annexe sortait vide.
    if (
        pack.enveloppe_xlsx is not None
        and count_candidate_envelope_walls(snap) > 0
        and _count_business_rows(pack.enveloppe_xlsx) == 0
    ):
        problems.append("Enveloppe")
    if (
        pack.menuiseries_xlsx is not None
        and count_menuiseries(snap) > 0
        and _count_business_rows(pack.menuiseries_xlsx) == 0
    ):
        problems.append("Menuiseries")
    if (
        pack.plancher_xlsx is not None
        and count_planchers(snap) > 0
        and _count_business_rows(pack.plancher_xlsx) == 0
    ):
        problems.append("Plancher")
    return problems


#: Marques d'outils tiers interdites dans un livrable client. Elles proviennent
#: des classeurs MOA de référence, recyclés comme gabarit de mise en forme.
_QA_EXTERNAL_TOOL_RE = re.compile(r"BimCollab\s*Zoom|BimCollab|Solibri", re.IGNORECASE)


def _qa_external_tool_mentions(pack: AvpReportPack) -> list[str]:
    """Livrables citant encore un outil tiers, inspectés **dans leur XML**.

    ``.xlsx`` et ``.docx`` sont des archives ZIP : on lit les parties XML plutôt
    que de rouvrir les documents par leur API. Une marque peut vivre hors des
    cellules et des paragraphes — chaîne partagée, en-tête, note, propriété de
    document — et n'importe laquelle atteindrait le client.
    """
    contamines: list[str] = []
    for chemin in pack.paths():
        try:
            with zipfile.ZipFile(chemin) as archive:
                trouve = any(
                    _QA_EXTERNAL_TOOL_RE.search(archive.read(nom).decode("utf-8", errors="ignore"))
                    for nom in archive.namelist()
                    if nom.endswith((".xml", ".rels"))
                )
        except (OSError, zipfile.BadZipFile):
            continue  # le PDF best-effort et un fichier illisible ne sont pas des marques
        if trouve:
            contamines.append(Path(chemin).name)
    return contamines


def _qa_missing_quantities(snap) -> list[str]:
    """Annexes dont les colonnes de **quantités** seraient intégralement vides.

    Un livrable avec des lignes mais aucune valeur numérique est pire qu'un
    livrable vide : il paraît complet et se lit comme un résultat. Le cas se
    produit quand le snapshot BIMData ne porte pas de ``BaseQuantities`` et que
    les quantités calculées (contrat ``computed_base_quantities/v1``) n'ont pas
    été fusionnées — ``compute_missing_quantities`` non demandé, ou
    ``computed_quantities_json`` non transmis.

    On refuse alors la génération plutôt que de produire un pack faux.
    """
    if snap is None:
        return []
    problems: list[str] = []
    if snap.spaces and count_spaces_with_area(snap) == 0:
        # SHAB et Zones/Espaces reposent toutes deux sur la surface d'espace.
        problems += ["SHAB", "Zones/Espaces"]
    if count_menuiseries(snap) > 0 and count_menuiseries_with_dimensions(snap) == 0:
        problems.append("Menuiseries")
    if (
        REPORT_SPECS_BY_KEY["plancher"].blocked_reason is None
        and count_planchers(snap) > 0
        and count_planchers_with_area(snap) == 0
    ):
        problems.append("Plancher")
    return problems
