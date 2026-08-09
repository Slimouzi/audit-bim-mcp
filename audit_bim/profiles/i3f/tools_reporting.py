"""Tools MCP — génération des livrables (Word / xlsx / pack AVP) (PR2 §2b)."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ...mcp.app import mcp
from ...mcp.phase import (
    _VALID_PHASES,
    _detect_snapshot_phase,
    _phase_question_dict,
    _snapshot_address_suggestion,
    _snapshot_description,
    _validate_audit_context,
)
from ...mcp.session import _State
from ...reporting.avp.pack import _qa_missing_quantities
from ...reporting.avp_snapshot import count_envelope_walls
from ...reporting.context import build_report_context, merge_user_context
from ...reporting.word_report import NOT_AVAILABLE, write_word_report
from ...reporting.xlsx_annex import write_xlsx_annex
from ...safe_paths import safe_export_dir, safe_export_path, safe_input_path

_server_logger = logging.getLogger("audit_bim.profiles.i3f.tools_reporting")

if TYPE_CHECKING:
    from ...reporting.avp.models import AvpReportPack


#: Libellés qui ne désignent **aucun chantier**. Trois familles, toutes vues en
#: production : l'espace de travail BIMData (``MCP_Audit``), le vocabulaire
#: générique du domaine (``Projet``, ``I3F``, ``Maquette``), et le projet de
#: référence dont les classeurs MOA servent de gabarit (``Tarare``). Aucun ne
#: doit pouvoir nommer un fichier remis au client.
_GENERIC_PROJECT_NAMES = frozenset(
    {
        # espaces de travail / bacs à sable
        "mcp_audit",
        "mcp audit",
        "audit",
        "sandbox",
        "test",
        "tests",
        "demo",
        "exemple",
        "example",
        "default",
        "untitled",
        "sans nom",
        "nouveau projet",
        # vocabulaire générique du domaine
        "projet",
        "project",
        "i3f",
        "3f",
        "maquette",
        "modele",
        "bim",
        "ifc",
        # projet de référence des gabarits MOA
        "tarare",
    }
)

#: Jetons du nom de maquette qui ne sont pas un nom de chantier : phases I3F et
#: indicatifs de discipline. Écartés des suggestions.
_MODEL_NAME_NOISE = frozenset(
    {
        # phases I3F
        "aps",
        "avp",
        "apd",
        "pro",
        "dce",
        "exe",
        "doe",
        "gestion",
        # indicatifs de discipline / lot
        "archi",
        "stru",
        "flu",
        "bata",
        # vocabulaire d'export, fréquent en tête de nom de fichier
        "export",
        "extract",
        "final",
        "copie",
        "copy",
    }
)

#: Code ESI I3F : 3 à 5 chiffres suivis d'une lettre (« 7427L », « 0546L »).
_ESI_CODE_RE = re.compile(r"^\d{3,5}[A-Za-z]$")


def _is_generic_identity(value: str | None) -> bool:
    """Ce libellé nomme-t-il un chantier, ou rien du tout ?"""
    if not value:
        return True
    return _norm_identity(value) in _GENERIC_PROJECT_NAMES


def _norm_identity(value: str) -> str:
    """Casse, accents et espaces multiples neutralisés — ``MCP_Audit``,
    ``mcp audit`` et ``MCP-AUDIT`` désignent le même non-projet."""
    sans_accent = "".join(
        c for c in unicodedata.normalize("NFD", value) if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"[\s_\-]+", " ", sans_accent).strip().lower()


def _model_identity_suggestion() -> tuple[str | None, str | None]:
    """Nom et code projet **suggérés** depuis le nom de la maquette.

    Une suggestion, jamais un défaut : elle alimente la question posée à
    l'auditeur, qui reste seul à trancher le nom porté par les livrables.
    ``DIEPPE-7427L-BATA-ARCHI-APD (3).ifc`` → ``("DIEPPE", "7427L")``.
    """
    snap = _State.snapshot
    brut = ((snap.model or {}).get("name") if snap else None) or ""
    stem = Path(str(brut)).stem
    if not stem:
        return None, None
    nom = code = None
    for jeton in (j for j in re.split(r"[-_\s.()]+", stem) if j):
        if code is None and _ESI_CODE_RE.match(jeton):
            code = jeton.upper()
            continue
        if nom is None and len(jeton) >= 3 and jeton[0].isalpha():
            norme = _norm_identity(jeton)
            if norme not in _MODEL_NAME_NOISE and norme not in _GENERIC_PROJECT_NAMES:
                nom = jeton
    return nom, code


def _contract_doc(path: str | Path) -> dict | None:
    """Relit un contrat JSON déjà validé par la sandbox, ou ``None``."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _contract_source_ifc_file(path: str | Path) -> str | None:
    """``source.ifc_file`` d'un contrat, pour la traçabilité du pack."""
    from ...reporting.avp_autocompute import contract_source_ifc

    return contract_source_ifc(_contract_doc(path))


def _guard_contract_provenance(path: str | Path, *, parametre: str) -> str | None:
    """Refuse un contrat **fourni** qui ne porte pas sur le modèle actif.

    Les contrats auto-résolus sont déjà corrélés à la cible (cf.
    ``avp_autocompute._contract_matches_model``) ; un chemin passé à la main ne
    l'était pas, et c'est par là qu'un contrat étranger entrait dans un livrable
    nommé d'après le projet courant. Renvoie la provenance déclarée pour la
    traçabilité du pack.
    """
    from ...reporting.avp_autocompute import assert_contract_matches_model, contract_source_ifc

    doc = _contract_doc(path)
    if doc is None:
        return None
    assert_contract_matches_model(
        doc,
        _State.snapshot,
        parametre=parametre,
        session_ifc_path=getattr(_State, "ifc_path", None),
        model_ids=(_State.cloud_id, _State.project_id, _State.model_id),
    )
    return contract_source_ifc(doc)


def _contract_mismatch_payload(exc) -> dict:
    """Refus explicite et actionnable — jamais un pack silencieusement faux."""
    return {
        "status": "error",
        "error": "contract_model_mismatch",
        "parametre": exc.parametre,
        "contract_source_ifc_file": exc.provenance,
        "active_model_id": _State.model_id,
        "message": str(exc),
    }


#: Origine d'un chemin de contrat. Elle est **donnée par l'appelant**, jamais
#: redevinée : « ce n'est pas nous qui l'avons calculé » ne veut pas dire
#: « l'appelant l'a fourni ». Un fichier détecté sur disque a déjà été corrélé
#: au modèle actif, un fichier fourni ne l'a pas été — et c'est cette seule
#: différence qui décide si la garde de provenance s'applique.
ContractOrigin = Literal["parametre", "detecte", "calcule"]


def _geometry_failure_response(exc, *, error: str) -> dict:
    """Refus commun quand le calcul géométrique ne peut pas aboutir.

    Les deux contrats — enveloppe et quantités calculées — échouent de la même
    façon sur ``GeometryInputMissing`` / ``GeometryBackendUnavailable`` : il
    manque une entrée, et la seule chose à dire à l'appelant est laquelle.
    Seule la clé ``error`` distingue les deux.

    ``missing`` est calculé **une fois** puis réemployé pour la liste et pour
    la question : les deux doivent désigner la même chose, et deux ``getattr``
    séparés laissaient la porte ouverte à ce qu'ils divergent.

    Ce helper est un raccourci d'écriture, **pas une promesse d'uniformité** :
    si l'un des deux contrats a besoin un jour d'un message d'aide propre, il
    reprend son payload plutôt que d'ajouter un paramètre ici.
    """
    missing = getattr(exc, "missing", "geometry_backend")
    return {
        "status": "needs_context",
        "missing": [missing],
        "error": error,
        "message": str(exc),
        "questions": [{"key": missing, "question": str(exc)}],
    }


def _resolve_contract_source(
    *,
    path: str | Path,
    origin: ContractOrigin,
    param_name: str,
) -> tuple[Path, str | None]:
    """Sécurise un chemin de contrat et applique la politique de provenance.

    Le **choix** du chemin reste chez l'appelant : ce helper reçoit un chemin
    déjà décidé, le passe par la sandbox si son origine l'exige, puis applique
    la règle de provenance correspondante.

    - ``parametre`` — sandbox en lecture, puis **garde de provenance** qui peut
      lever :class:`ContractModelMismatch`. C'est le seul cas où un contrat
      étranger doit être refusé : rien n'a corrélé ce chemin au modèle actif ;
    - ``detecte`` — sandbox en lecture, provenance seulement **informative** :
      la corrélation a eu lieu en amont (``_envelope_json_matches_model``) ;
    - ``calcule`` — produit dans cette exécution, donc sous ``AUDIT_OUTPUT_DIR``
      que ``safe_input_path`` ne couvre pas nécessairement ; provenance
      informative, le modèle est juste par construction.

    Ne protège **pas** de l'absence de fichier : ``_guard_contract_provenance``
    rend ``None`` sur un document illisible. Le refus d'un chemin absent vient
    de ``safe_input_path`` et du lecteur de contrat, en amont — les confondre
    retirerait le contrôle qui agit réellement.

    Renvoie ``(chemin_sûr, provenance_déclarée_ou_None)``.
    """
    if origin == "calcule":
        safe = Path(path)
    else:
        safe = safe_input_path(path, allowed_extensions={".json"})

    if origin == "parametre":
        # Peut lever ContractModelMismatch — l'appelant décide quoi en faire.
        return safe, _guard_contract_provenance(safe, parametre=param_name)
    if origin in {"detecte", "calcule"}:
        return safe, _contract_source_ifc_file(safe)
    # Branchement exhaustif, et refus explicite. ``Literal`` ne contraint que
    # les vérificateurs statiques : à l'exécution, une faute de frappe
    # (« paramètre », « detected ») tomberait sinon dans la branche
    # informative et **contournerait la garde de provenance** — un helper de
    # politique de sécurité doit échouer fermé, jamais ouvert.
    raise ValueError(f"origine de contrat inconnue : {origin!r}")


def _auto_envelope_roots() -> list[Path]:
    roots: list[Path] = []
    # Déploiement local Codex/Claude : audit-bim-mcp est lancé depuis le repo,
    # tandis que les JSON produits par ifc-geometry vivent dans ../audit_in.
    repo_root = Path(__file__).resolve().parents[2]
    workspace_root = repo_root.parent
    roots.extend(
        [
            Path.cwd() / "audit_in",
            Path.cwd().parent / "audit_in",
            repo_root / "audit_in",
            workspace_root / "audit_in",
        ]
    )

    seen: set[Path] = set()
    unique: list[Path] = []
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def _unique_envelope_json(roots: list[Path]) -> str | None:
    candidates: list[Path] = []
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved.is_dir():
            candidates.extend(sorted(resolved.glob("*_envelope.json")))
    if len(candidates) != 1:
        return None
    return str(candidates[0])


def _envelope_json_matches_model(path: str | Path) -> bool:
    """L'``envelope.json`` détecté porte-t-il bien sur le **modèle actif** ?

    Un fichier « seul dans le dossier » n'est pas pour autant le bon : reprendre
    l'enveloppe d'une autre maquette livrerait les façades d'un autre bâtiment,
    sans aucun signal. On exige donc une corrélation :

    - contrat V1 → ``source.ifc_file`` doit correspondre au modèle actif ;
    - fichier legacy (sans provenance) → seul le **nom du fichier** peut
      corréler ; à défaut, on l'ignore et on recalcule.
    """
    snap = _State.snapshot
    modele = ((snap.model or {}).get("name") if snap else None) or ""
    stem_modele = Path(str(modele)).stem
    if not stem_modele:
        return False
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    source = (doc.get("source") or {}).get("ifc_file") if isinstance(doc, dict) else None
    if source:
        return Path(str(source)).stem == stem_modele
    # Legacy sans provenance : le nom du fichier est le seul indice disponible.
    return Path(path).stem.startswith(stem_modele)


def _auto_envelope_json() -> str | None:
    """Détecte un ``*_envelope.json`` **du modèle actif** dans les dossiers connus.

    La détection ne suffit pas : le fichier doit être corrélé au modèle actif
    (cf. :func:`_envelope_json_matches_model`). Sinon on préfère recalculer.
    """
    root_raw = os.getenv("AUDIT_INPUT_DIR")
    trouve = (
        _unique_envelope_json([Path(root_raw)])
        if root_raw
        else _unique_envelope_json(_auto_envelope_roots())
    )
    if trouve and not _envelope_json_matches_model(trouve):
        _server_logger.info("envelope.json ignoré (ne correspond pas au modèle actif) : %s", trouve)
        return None
    return trouve


def _normalized_filename(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()


def _documents_maitre_ouvrage_roots() -> list[Path]:
    repo_root = Path(__file__).resolve().parents[2]
    workspace_root = repo_root.parent
    roots: list[Path] = []
    if workspace_root.is_dir():
        for child in workspace_root.iterdir():
            if not child.is_dir():
                continue
            name = _normalized_filename(child.name)
            if "documents" in name and "maitre" in name and "ouvrage" in name:
                roots.append(child.resolve())
    return roots


def _auto_controle_roots() -> list[Path]:
    roots: list[Path] = []
    root_raw = os.getenv("AUDIT_INPUT_DIR")
    if root_raw:
        roots = [Path(root_raw)]
    else:
        roots.extend(_auto_envelope_roots())
        roots.extend(_documents_maitre_ouvrage_roots())
    seen: set[Path] = set()
    unique: list[Path] = []
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def _is_controle_maquettes_xlsx(path: Path) -> bool:
    name = _normalized_filename(path.name)
    return path.suffix.lower() in {".xlsx", ".xlsm"} and "controle maquettes" in name


def _unique_controle_xlsx(roots: list[Path]) -> str | None:
    candidates: list[Path] = []
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved.is_dir():
            candidates.extend(
                p for p in sorted(resolved.glob("*.xls*")) if _is_controle_maquettes_xlsx(p)
            )
    if len(candidates) != 1:
        return None
    return str(candidates[0])


def _auto_controle_xlsx() -> str | None:
    """Détecte un template Contrôle Maquettes MOA unique.

    Sans template, le livrable Contrôle retombe sur une grille synthétique. On
    préfère donc consommer le classeur MOA disponible localement quand il est
    unique, sans choisir arbitrairement entre plusieurs références.
    """
    return _unique_controle_xlsx(_auto_controle_roots())


def _default_output_paths() -> tuple[Path, Path]:
    """Renvoie deux chemins relatifs (docx, xlsx) — passés ensuite à
    :func:`safe_export_path` qui les résoudra sous ``AUDIT_OUTPUT_DIR``.
    """
    project_name = (_State.snapshot.project or {}).get("name") if _State.snapshot else None
    project_name = project_name or _State.project_id or "projet"
    safe = "".join(c for c in str(project_name) if c not in r'\/:*?"<>|').strip()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    phase = _State.phase.value if _State.phase else "PRO"
    base = f"audit_{safe}_{phase}_{ts}"
    return Path(f"{base}.docx"), Path(f"{base}_annexes.xlsx")


@mcp.tool()
def generate_xlsx_annex(output_path: str | None = None, overwrite: bool = False) -> dict:
    """Génère l'annexe Excel détaillée de l'audit courant.

    Le chemin de sortie est filtré par la sandbox d'export
    (:func:`audit_bim.safe_paths.safe_export_path`) : doit rester sous
    ``AUDIT_OUTPUT_DIR`` (défaut ``./out``), sans ``..``, pas
    d'écrasement silencieux sauf ``overwrite=True``.
    """
    _State.ensure_result()
    raw = Path(output_path) if output_path else _default_output_paths()[1]
    target = safe_export_path(raw, overwrite=overwrite)
    written = write_xlsx_annex(_State.result, target)
    return {"path": str(written), "size_bytes": written.stat().st_size}


@mcp.tool()
def list_avp_i3f_xls_reports(
    include_templates: bool = True,
    require_identical: bool = False,
) -> dict:
    """Liste les rapports XLS AVP I3F **préparables** et leur disponibilité.

    Tool **sans effet de bord** : il sonde le snapshot BIMData de la session
    courante (entités IFC, BaseQuantities, relations zone/espace, calque
    d'enveloppe) et rend, pour chacun des 6 rapports MOA, un verdict :

    - ``can_generate`` : un rapport **métier** (charte BIMData) est produisible ;
    - ``can_generate_identical`` : une reproduction MOA **stricte** (formules /
      pivots / styles préservés) est produisible. **Actuellement toujours
      ``False``** : la génération réécrit des tables brandées (valeurs figées),
      le mode template MOA (copie du workbook) n'est pas encore livré — on ne
      promet donc **jamais** « à l'identique », même avec les classeurs MOA ;
    - ``status`` : ``ready`` (jamais atteint sans mode template) / ``partial``
      (générable en brandé) / ``blocked`` + ``next_action``.

    C'est l'étape à appeler **avant** ``generate_avp_i3f_pack`` : elle explique
    pourquoi un rapport est générable (partiel) ou bloqué.

    Args:
        include_templates: inclure le chemin du classeur MOA de référence
            (``template_path``) quand il existe sur le poste.
        require_identical: si ``True``, un rapport n'est ``ready`` que si la
            reproduction stricte est possible — donc **aucun** tant que le mode
            template MOA n'existe pas (tout passe ``blocked``).

    Returns:
        ``{status, project, reports: [...]}`` — ``reports`` dans l'ordre CTO.
    """
    from ...reporting.avp_availability import inspect_avp_report_availability

    snap = _State.snapshot
    availabilities = inspect_avp_report_availability(
        snap,
        sources=None,
        require_identical=require_identical,
        has_audit_result=_State.result is not None,
    )
    reports: list[dict] = []
    for av in availabilities:
        d = av.to_dict()
        if not include_templates:
            d.pop("template_path", None)
        reports.append(d)

    phase = _State.phase.value if _State.phase else None
    # Signal d'amont : les rapports auraient des lignes mais des colonnes de
    # quantités vides. Remonté ICI pour information ; la génération, elle,
    # résout le manque automatiquement (``auto_compute_quantities``).
    sans_quantites = _qa_missing_quantities(snap)
    out = {
        "status": "ok",
        "project": {
            "name": (snap.project or {}).get("name") if snap else None,
            "model": (snap.model or {}).get("name") if snap else None,
            "phase": phase,
        },
        "require_identical": require_identical,
        "reports": reports,
    }
    if sans_quantites:
        out["needs_computed_quantities_json"] = True
        out["reports_without_quantities"] = sans_quantites
        out["next_action"] = (
            "Le snapshot ne porte pas de BaseQuantities pour : "
            + ", ".join(sans_quantites)
            + ". Produire le JSON via ``export_computed_base_quantities`` (MCP "
            "ifc-geometry) puis le passer en ``computed_quantities_json`` à "
            "``generate_avp_i3f_pack`` (ou à ``extract_model_snapshot`` avec "
            "``compute_missing_quantities=True``)."
        )
    return out


@dataclass(frozen=True)
class AvpIdentityContext:
    """Identité projet **résolue**, telle qu'elle nommera les livrables.

    Ces valeurs sortent de la validation ; ``missing``, ``questions`` et
    ``identity_missing`` n'en sortent pas — ils n'existent que pour construire
    le refus, et les exposer inviterait à les reconstruire ailleurs.

    ``auteur_controle`` porte une seule valeur : en amont, ``eff_auditor`` et
    ``eff_auteur`` étaient deux noms de la même chose. Deux champs toujours
    égaux seraient un piège pour qui les ferait diverger sans le vouloir.
    """

    project_name: str
    project_code: str
    phase: str
    auteur_controle: str | None
    controle_src: str | None
    sources: object


@dataclass(frozen=True)
class AvpContextCheck:
    """Résultat de la validation de contexte : soit une identité, soit un refus.

    Pas de booléen ``ok`` : ``response is not None`` **est** la condition, et un
    drapeau redondant peut diverger de ce qu'il résume. L'appelant écrit donc :

    .. code-block:: python

        context = _validate_avp_context(...)
        if context.response is not None:
            return context.response

    C'est ce qui fait passer le refus de contexte du statut de ``return`` perdu
    au milieu de la génération à celui de décision nommée.
    """

    identity: AvpIdentityContext | None
    response: dict | None

    def __post_init__(self) -> None:
        # Invariant : exactement l'un des deux. Un check qui porterait les deux
        # (ou aucun) laisserait l'appelant décider au hasard.
        if (self.identity is None) == (self.response is None):
            raise ValueError("AvpContextCheck : exactement une identité OU un refus")


@dataclass(frozen=True)
class AvpPackBuildResult:
    """Résultat de génération : soit un pack produit, soit une réponse QA.

    Même forme que :class:`AvpContextCheck`, appliquée au dernier bloc du tool :
    la QA gate reste un refus explicite, mais elle n'est plus mélangée au
    formatage du succès.
    """

    out_dir: Path
    pack: AvpReportPack | None
    response: dict | None

    def __post_init__(self) -> None:
        if (self.pack is None) == (self.response is None):
            raise ValueError("AvpPackBuildResult : exactement un pack OU un refus")


def _validate_avp_context(
    *,
    controle_xlsx: str | None,
    shab_xlsx: str | None,
    zones_espaces_xlsx: str | None,
    enveloppe_xlsx: str | None,
    menuiseries_xlsx: str | None,
    plancher_xlsx: str | None,
    project_name: str | None,
    project_code: str | None,
    phase: str | None,
    auditor_name: str | None,
    auteur_controle: str | None,
    auditor: str | None,
    confirm_context: bool,
    AvpSourcePaths,
    load_sources,
) -> AvpContextCheck:
    """Résout l'identité projet et les sources, ou refuse en demandant le contexte.

    Extrait tel quel du corps de ``generate_avp_i3f_pack`` : mêmes règles, mêmes
    messages, même ordre de priorité. Le seul changement est que le refus
    devient une **valeur nommée** au lieu d'un ``return`` au milieu de la
    génération.

    ``AvpSourcePaths`` et ``load_sources`` sont passés en paramètres parce que
    l'appelant les importe paresseusement — les réimporter ici dupliquerait le
    point d'entrée du module de sources.
    """
    # Repliée ici depuis le corps du tool : « je n'ai pas de maquette » est un
    # refus de CONTEXTE au même titre qu'une identité manquante. La garder
    # dehors obligeait l'appelant à porter deux sorties pour une seule question.
    if _State.snapshot is None and _State.result is None:
        return AvpContextCheck(
            identity=None,
            response={
                "status": "needs_context",
                "missing": ["snapshot"],
                "questions": [
                    {
                        "key": "snapshot",
                        "question": (
                            "Extraire la maquette active avant de générer le pack "
                            "AVP I3F (set_active_model puis extract_model_snapshot, "
                            "ou full_audit)."
                        ),
                    }
                ],
                "next_step": (
                    "Appeler set_active_model(...), puis extract_model_snapshot "
                    "ou full_audit. Relancer ensuite generate_avp_i3f_pack : les "
                    "Excel utiliseront les données IFC/OpenShell plutôt que les "
                    "colonnes d'outils externes des sources."
                ),
            },
        )

    def _src(p: str | None) -> str | None:
        return str(safe_input_path(p, allowed_extensions={".xlsx", ".xlsm"})) if p else None

    controle_xlsx_used = controle_xlsx or _auto_controle_xlsx()
    controle_src = _src(controle_xlsx_used)
    source_paths = AvpSourcePaths(
        controle=controle_src,
        shab=_src(shab_xlsx),
        zones_espaces=_src(zones_espaces_xlsx),
        enveloppe=_src(enveloppe_xlsx),
        menuiseries=_src(menuiseries_xlsx),
        plancher=_src(plancher_xlsx),
    )
    # Chargement unique des sources (lues aussi pour résoudre le code ESI).
    sources = load_sources(source_paths)

    # ── Résolution de l'identité projet (nom / code) ────────────────────
    # Note : l'entête du classeur MOA n'est plus lue ici, et le helper qui la
    # lisait a été supprimé plutôt que laissé inutilisé — un lecteur d'entête
    # encore présent à portée de main est une invitation à le rebrancher.
    # Ordre STRICT : **paramètre explicite → on demande**. Il n'y a pas de
    # troisième source.
    #
    # Le repli sur le nom du projet BIMData a été SUPPRIMÉ. Il a livré un pack
    # « 260803 MCP_Audit 7427L AVP - … » : ``MCP_Audit`` est un espace de
    # travail, pas un chantier. Un nom de projet BIMData n'est pas une identité
    # client — il est choisi par celui qui crée l'espace, souvent pour lui-même.
    # Même traitement pour les libellés génériques (« Projet », « I3F ») et pour
    # ``Tarare``, le chantier dont les classeurs MOA servent de gabarit.
    #
    # L'entête du classeur de contrôle ne fournit rien non plus — ni valeur, ni
    # **suggestion**. Refuser une mauvaise valeur ne suffit pas s'il on continue
    # de la proposer : une suggestion « Tarare / 0546L » finit recopiée. Et la
    # filtrer par liste de libellés génériques ne fermerait pas le trou, puisque
    # le gabarit du jour peut venir de n'importe quel autre chantier réel, dont
    # le nom ne figurera dans aucune liste. La seule source de suggestion est le
    # **nom de la maquette**, posé par l'équipe projet.
    eff_name = (project_name or "").strip() or None
    eff_code = (project_code or "").strip() or None
    nom_rejete = eff_name if _is_generic_identity(eff_name) and eff_name else None
    code_rejete = eff_code if _is_generic_identity(eff_code) and eff_code else None
    if nom_rejete:
        eff_name = None
    if code_rejete:
        eff_code = None
    sug_name, sug_code = _model_identity_suggestion()

    # Phase : paramètre explicite > phase d'audit **confirmée** > on demande.
    #
    # L'entête du classeur MOA est écartée ici pour la même raison que le nom et
    # le code : la phase entre dans le nom du fichier remis au client. Un gabarit
    # Tarare en AVP nommait « … Dieppe 7427L AVP - … » un pack en APD — le défaut
    # est le même, simplement plus discret parce qu'une phase reste toujours
    # plausible. ``_State.phase`` est conservée : elle a été confirmée par
    # l'auditeur au moment de ``set_active_model``, ce n'est pas un repli.
    eff_phase = (phase or "").strip() or None
    if not eff_phase and _State.phase is not None:
        eff_phase = _State.phase.value

    # Auteur du contrôle : I3F attend un auteur nommé (CdP BIM / auditeur
    # AMO). On **demande** explicitement plutôt que de retomber sur un
    # « AMO BIM » générique.
    #
    # Trois noms coexistent, par ordre de priorité :
    #   ``auditor_name``     — nom proposé/validé depuis la session (à employer) ;
    #   ``auteur_controle``  — vocabulaire métier I3F, conservé en compat ;
    #   ``auditor``          — paramètre historique, conservé en compat.
    eff_auteur = (
        (auditor_name or "").strip()
        or (auteur_controle or "").strip()
        or (auditor or "").strip()
        or None
    )

    # Nom / code / phase obligatoires pour un livrable I3F fiable → sinon on
    # demande (jamais de valeur inventée ni de défaut silencieux).
    missing: list[str] = []
    questions: list[dict] = []
    if not eff_name:
        missing.append("project_name")
        q = {
            "key": "project_name",
            "question": "Quel nom de projet doit apparaître dans les livrables ?",
        }
        if nom_rejete:
            q["rejected"] = nom_rejete
            q["question"] = (
                f"« {nom_rejete} » ne nomme pas un chantier (espace de travail, "
                "libellé générique ou projet de référence des gabarits MOA) et ne "
                "peut pas nommer un livrable client. Quel nom de projet doit "
                "apparaître dans les livrables ?"
            )
        # Seule source de suggestion : le nom de la maquette, posé par l'équipe
        # projet. Surtout pas l'entête du classeur MOA — voir plus haut.
        if sug_name:
            q["suggestion"] = sug_name
            q["question"] += f" (le nom de la maquette suggère « {sug_name} »)"
        questions.append(q)
    if not eff_code:
        missing.append("project_code")
        q = {
            "key": "project_code",
            "question": (
                "Quel code projet / ESI doit apparaître dans les livrables ? "
                "(ex. « 7427L », visible sur le contrôle maquettes I3F)"
            ),
        }
        if code_rejete:
            q["rejected"] = code_rejete
        if sug_code:
            q["suggestion"] = sug_code
            q["question"] += f" (le nom de la maquette suggère « {sug_code} »)"
        questions.append(q)
    if not eff_phase:
        # Phase unique : proposée si détectée **dans la maquette**, sinon
        # demandée — jamais défautée silencieusement sur « AVP », et jamais
        # reprise de l'entête d'un gabarit MOA (elle nomme le fichier client).
        missing.append("project_phase")
        questions.append(_phase_question_dict(*_detect_snapshot_phase()))
    if not eff_auteur:
        # Clé alignée sur le PARAMÈTRE à employer : une question dont la clé ne
        # correspond à aucun paramètre du tool guide vers un appel invalide.
        missing.append("auditor_name")
        questions.append(
            {
                "key": "auditor_name",
                "question": (
                    "Quel nom afficher comme « Auteur du contrôle » sur le pack "
                    "AVP I3F ? (ex. le CdP BIM 3F, ou l'auditeur AMO)"
                ),
                "accepted_aliases": ["auteur_controle", "auditor"],
            }
        )
    # Tout ce qui NOMME un fichier remis au client est incontournable : le nom,
    # le code **et la phase**. ``confirm_context`` ne couvre que ce qui reste
    # interne au document — ici l'auteur du contrôle.
    identity_missing = [
        m for m in missing if m in ("project_name", "project_code", "project_phase")
    ]
    if identity_missing or (missing and not confirm_context):
        refus = {
            "status": "needs_context",
            "missing": missing,
            "questions": questions,
            "next_step": (
                "Renseigner ``project_name`` / ``project_code`` / "
                "``project_phase`` (=``phase``) / ``auditor_name`` puis "
                "re-appeler ``generate_avp_i3f_pack``."
                + (
                    " ``project_name``, ``project_code`` et ``project_phase`` "
                    "sont OBLIGATOIRES : ils nomment les livrables client et ne "
                    "peuvent pas être contournés par ``confirm_context``."
                    if identity_missing
                    else " Pour générer malgré tout, passer ``confirm_context=True``."
                )
            ),
        }
        return AvpContextCheck(identity=None, response=refus)
    return AvpContextCheck(
        identity=AvpIdentityContext(
            project_name=eff_name,
            project_code=eff_code,
            phase=eff_phase,
            auteur_controle=eff_auteur,
            controle_src=controle_src,
            sources=sources,
        ),
        response=None,
    )


def _avp_qa_error_response(exc, *, out_dir: Path) -> dict:
    """Transforme la QA gate AVP en refus MCP actionnable."""
    # QA gate : annexe vide, ou annexe dont TOUTES les colonnes de quantités
    # sont vides. Statut d'erreur explicite — surtout pas un livrable client
    # faux, qui se lirait comme un résultat.
    manque_quantites = exc.kind == "missing_quantities"
    # Un refus ne doit rien laisser derrière lui. Le dossier a été créé en
    # amont par la sandbox d'export ; on le retire s'il est resté vide (jamais
    # s'il contient quoi que ce soit — on ne supprime pas de fichiers de
    # l'utilisateur).
    try:
        if out_dir.is_dir() and not any(out_dir.iterdir()):
            out_dir.rmdir()
    except OSError:  # nettoyage best-effort, jamais bloquant
        pass
    _codes = {
        "missing_quantities": "missing_quantities",
        "external_tool_mention": "external_tool_mention",
        "envelope_filter_mode": "envelope_filter_mode",
    }
    payload = {
        "status": "error",
        "error": _codes.get(exc.kind, "empty_deliverable"),
        "empty_deliverables": exc.empty,
        "message": str(exc),
    }
    if exc.kind == "envelope_filter_mode":
        payload["envelope_filter_mode"] = "geometric"
        payload["expected_envelope_filter_mode"] = "layer_type_filter"
        # Dire QUOI relancer, avec les paramètres exacts : un refus qui laisse
        # deviner la commande fait recommencer à l'identique.
        payload["next_step"] = (
            "Le contrat d'enveloppe a été calculé sans filtre. Régénérer avec "
            '``envelope_filter_mode="layer_type_filter"``, '
            "``envelope_layer_pattern`` (ex. « Extérieurs périphériques »), "
            "``envelope_type_pattern`` (ex. « ^ME(?:[\\s_]|$) ») et "
            "``force_recompute_envelope=True`` — sans ce dernier, le contrat en "
            "cache serait réutilisé tel quel."
        )
    if exc.kind == "external_tool_mention":
        payload["contaminated_deliverables"] = exc.empty
        payload["next_step"] = (
            "Un livrable cite encore un outil tiers (Solibri / BimCollab*) "
            "hérité du classeur MOA de référence. Retirer la mention du "
            "template source, ou générer sans ``controle_xlsx``."
        )
    if exc.kind == "empty" and "Enveloppe" in exc.empty:
        # La maquette porte des murs d'enveloppe mais aucune source exploitable
        # n'a produit de ligne. Dire quoi faire : sur une maquette sans calque
        # ArchiCAD (export Revit), la sélection par défaut ne retient rien et il
        # faut des motifs adaptés au nommage réel des types.
        payload["needs_envelope_source"] = True
        payload["next_step"] = (
            "L'annexe Enveloppe est vide alors que la maquette porte des "
            "murs d'enveloppe. Fournir ``envelope_json`` (contrat "
            "``envelope_quantities/v1`` du MCP ifc-geometry), ou relancer "
            "avec ``auto_compute_envelope=True`` et des motifs adaptés au "
            "nommage de CETTE maquette (``envelope_layer_pattern`` / "
            "``envelope_type_pattern``) — les motifs ArchiCAD I3F "
            "(« 221|extérieurs périphériques », « ^ME[ _] ») ne retiennent "
            "rien sur un export Revit, qui n'expose pas de calque."
        )
    if manque_quantites:
        payload["needs_computed_quantities_json"] = True
        payload["next_step"] = (
            "Produire le JSON via ``export_computed_base_quantities`` (MCP "
            "ifc-geometry), puis rappeler ce tool avec "
            "``computed_quantities_json=<chemin>`` — ou relancer "
            "``extract_model_snapshot(compute_missing_quantities=True, "
            "computed_quantities_json=<chemin>)``."
        )
    return payload


def _build_avp_pack(
    *,
    output_dir: str | None,
    identity: AvpIdentityContext,
    sources,
    working_snapshot,
    usages_bim: list[str] | None,
    nombre_logements: str | None,
    temoin_virtuel: str | None,
    date_controle: str | None,
    reports: list[str] | None,
    export_pdf: bool,
) -> AvpPackBuildResult:
    """Produit le pack AVP, ou rend le refus QA correspondant."""
    from ...reporting.avp_i3f import AvpQaError, write_avp_i3f_report_pack

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = safe_export_dir(output_dir or f"avp_pack_{ts}")
    try:
        pack = write_avp_i3f_report_pack(
            _State.result,  # peut être None : le pack se limite alors aux sources
            out_dir,
            sources=sources,
            # Snapshot explicite : le repli maquette s'active même sans audit
            # (ex. après verify_active_model seul, _State.result est None).
            snapshot=working_snapshot,
            # Garantis non vides par la gate d'identité ci-dessus : aucun nom
            # générique ni d'exemple ne peut atteindre un livrable.
            project_name=identity.project_name,
            project_code=identity.project_code,
            phase=identity.phase or "AVP",
            reports=None if reports is None else frozenset(reports),
            # Auteur validé/fourni (ou repli « AMO BIM » uniquement sous
            # confirm_context — voluntary confirmation).
            auditor=identity.auteur_controle or NOT_AVAILABLE,
            usages_bim=usages_bim,
            nombre_logements=nombre_logements,
            temoin_virtuel=temoin_virtuel,
            date_controle=date_controle,
            auteur_controle=identity.auteur_controle,
            export_pdf=export_pdf,
        )
    except AvpQaError as exc:
        return AvpPackBuildResult(
            out_dir=out_dir, pack=None, response=_avp_qa_error_response(exc, out_dir=out_dir)
        )
    return AvpPackBuildResult(out_dir=out_dir, pack=pack, response=None)


@dataclass(frozen=True)
class AvpContractTrace:
    """Ce qu'il faut **dire** d'un contrat géométrique dans la réponse du pack.

    À ne pas confondre avec :func:`_resolve_contract_source`, qui décide si un
    contrat est *acceptable* : celui-ci relève de la sécurité et de la
    provenance, celle-ci du reporting. Les mélanger ferait porter au garde de
    provenance des champs qui n'existent pas encore au moment où il agit —
    ``coverage`` n'est connu qu'**après** la fusion des quantités dans le
    snapshot, bien après la résolution du chemin.

    ``coverage`` reste donc optionnel : l'enveloppe n'en produit pas, les
    quantités calculées si. C'est la seule asymétrie entre les deux traces, et
    elle est portée par un défaut plutôt que par deux types.
    """

    json_used: str | None = None
    source_ifc_file: str | None = None
    auto_result: dict | None = None
    coverage: dict | None = None


def _format_avp_pack_response(
    *,
    out_dir: Path,
    pack: AvpReportPack,
    identity: AvpIdentityContext,
    controle_src: str | None,
    envelope_trace: AvpContractTrace,
    computed_trace: AvpContractTrace,
) -> dict:
    """Payload de succès du tool AVP, séparé de la QA gate."""
    from ...reporting.avp_report_catalog import REPORT_SPECS_BY_KEY

    return {
        "output_dir": str(out_dir),
        "paths": [str(p) for p in pack.paths()],
        # Un pack peut écarter un rapport, mais il doit dire lequel et pourquoi :
        # une annexe absente sans explication se lit comme un oubli.
        "blocked_reports": {
            cle: spec.blocked_reason
            for cle, spec in REPORT_SPECS_BY_KEY.items()
            if spec.blocked_reason is not None
        },
        "analyse_docx": str(pack.analyse_docx),
        "analyse_pdf": str(pack.analyse_pdf) if pack.analyse_pdf else None,
        "pdf_available": pack.analyse_pdf is not None,
        "project_name": identity.project_name,
        "project_code": identity.project_code,
        "phase": identity.phase,
        "controle_xlsx_used": controle_src,
        "envelope_json_used": envelope_trace.json_used,
        "computed_quantities_json_used": computed_trace.json_used,
        "computed_quantities_coverage": computed_trace.coverage,
        # Traçabilité de cible : de quel modèle et de quel .ifc sortent réellement
        # les chiffres du pack. Sans ces champs, seul le nom de fichier des
        # contrats trahissait la cible — un contrôle de recette impossible à
        # faire de tête.
        "active_cloud_id": _State.cloud_id,
        "active_project_id": _State.project_id,
        "active_model_id": _State.model_id,
        "downloaded_ifc_path": (
            str(getattr(_State, "ifc_path", None)) if getattr(_State, "ifc_path", None) else None
        ),
        "computed_source_ifc_file": computed_trace.source_ifc_file,
        "envelope_source_ifc_file": envelope_trace.source_ifc_file,
        "auto_computed": {
            "quantities": computed_trace.auto_result,
            "envelope": envelope_trace.auto_result,
        },
    }


@mcp.tool()
def generate_avp_i3f_pack(
    output_dir: str | None = None,
    controle_xlsx: str | None = None,
    shab_xlsx: str | None = None,
    zones_espaces_xlsx: str | None = None,
    enveloppe_xlsx: str | None = None,
    envelope_json: str | None = None,
    computed_quantities_json: str | None = None,
    auto_compute_quantities: bool = True,
    auto_compute_envelope: bool = True,
    force_recompute_quantities: bool = False,
    ifc_path: str | None = None,
    envelope_layer_pattern: str | None = None,
    envelope_type_pattern: str | None = None,
    envelope_filter_mode: str | None = None,
    force_recompute_envelope: bool = False,
    menuiseries_xlsx: str | None = None,
    plancher_xlsx: str | None = None,
    project_name: str | None = None,
    project_code: str | None = None,
    phase: str | None = None,
    auditor_name: str | None = None,
    auditor: str | None = None,
    usages_bim: list[str] | None = None,
    nombre_logements: str | None = None,
    temoin_virtuel: str | None = None,
    date_controle: str | None = None,
    auteur_controle: str | None = None,
    reports: list[str] | None = None,
    export_pdf: bool = True,
    confirm_context: bool = False,
) -> dict:
    """Génère le pack de livrables AVP I3F (charte BIMData).

    Produit le pack des rapports **produisibles** — par défaut les Excel
    Contrôle Maquettes, SHAB, Zones/Espaces, Enveloppe et Menuiseries — plus le
    rapport consolidé « Analyse BIM AVP » (.docx, + .pdf best-effort), toujours
    généré. **Plancher est bloqué** tant que la règle métier de sélection des 19
    groupes contribuant à la Surface de plancher n'est pas définie : il n'est pas
    écrit, et son motif est rendu dans ``blocked_reports``. ``list_avp_i3f_xls_reports``
    donne l'état à jour de chaque rapport. Les données métier sont
    **maquette-first** : elles
    viennent du snapshot/audit courant et des quantités IFC extraites ou
    calculées via la chaîne IFC OpenShell. Les .xlsx MOA éventuellement fournis
    servent de **template de mise en forme** et de contexte documentaire
    (seuils), pas à remplir des colonnes issues d'outils externes — et **jamais**
    à résoudre l'identité projet : ces classeurs sont le plus souvent ceux d'un
    projet de référence, et leur entête nommerait les livrables d'après un autre
    chantier.

    Nommage des livrables — convention documentaire I3F **générée à partir
    de données projet confirmées** :
    ``YYMMDD <NomProjet> <CodeProjet> <Phase> - <TypeLivrable>.<ext>``
    (``YYMMDD`` = date de génération). L'identité vient **exclusivement des
    paramètres** : il n'existe aucun repli. Ni le nom du projet BIMData (c'est un
    espace de travail — ``MCP_Audit`` a déjà nommé un pack), ni l'entête d'un
    classeur MOA (c'est le gabarit d'un autre chantier) ne peuvent nommer un
    livrable. Un libellé générique (``Projet``, ``I3F``, ``Tarare``…) est refusé
    au même titre qu'un paramètre absent. Le tool renvoie alors
    ``{status: needs_context}`` avec une **suggestion extraite du nom de la
    maquette** — une proposition à valider, jamais un défaut appliqué.
    La **phase** obéit à la même règle — elle nomme le fichier : paramètre, sinon
    phase confirmée de l'audit (``_State.phase``), sinon ``needs_context`` ;
    jamais l'entête d'un gabarit MOA. ``project_name``, ``project_code`` et
    ``phase`` sont **obligatoires** et ``confirm_context`` ne les contourne
    **jamais** — il ne couvre que l'auteur du contrôle.

    Args:
        output_dir: sous-dossier d'export (sandbox ``AUDIT_OUTPUT_DIR``).
        controle_xlsx … plancher_xlsx: chemins des .xlsx MOA/I3F de référence
            (optionnels, sandbox lecture ``safe_input_path``). Ils servent de
            **template de mise en forme** ; leur entête n'est jamais appliquée
            comme identité projet — au plus proposée en ``suggestion``, et
            seulement si l'appelant a désigné le classeur. Les surfaces et
            dimensions exportées viennent de la maquette IFC.
        auto_compute_quantities: **défaut ``True``** — si le snapshot ne porte
            pas les ``BaseQuantities`` nécessaires, le contrat
            ``computed_base_quantities/v1`` est retrouvé ou **calculé
            localement** depuis le ``.ifc`` actif, puis fusionné. Aucune
            consigne manuelle n'est requise. ``False`` → l'absence de quantités
            devient un refus.
        auto_compute_envelope: **défaut ``True``** — idem pour
            ``envelope_quantities/v1`` quand ``envelope_json`` n'est ni fourni
            ni détecté.
        force_recompute_quantities: recalcule le contrat même si un fichier
            existe déjà (rejeu de recette, maquette modifiée).
        ifc_path: ``.ifc`` à utiliser pour le calcul. ``None`` → résolu depuis
            le cache ``download_model_ifc`` puis le dossier d'entrée.
        envelope_layer_pattern, envelope_type_pattern: motifs de sélection des
            murs d'enveloppe (calque ArchiCAD, nom de type). Nécessaires sur les
            maquettes où la détection géométrique ne suffit pas.
            ``envelope_type_pattern`` fonctionne **aussi sans calque** : c'est le
            chemin des exports **Revit**, qui n'en exposent aucun et modélisent
            chaque façade en murs superposés (structure, isolant, peau). Sans
            motif de type, ces couches s'additionnent et la même façade est
            comptée trois ou quatre fois.
        envelope_filter_mode: impose le mode de sélection au lieu de le déduire —
            ``layer_type_filter`` (ArchiCAD), ``geometric_type_filter`` (Revit
            sans calque) ou ``geometric``. ``None`` (défaut) → déduit des motifs.
            Un mode dont le motif manque, ou à qui l'on passe un motif qu'il
            n'emploie pas, est **refusé** : se rabattre en silence changerait la
            nature du total sans que rien ne le signale.
        force_recompute_envelope: recalcule le contrat d'enveloppe même si un
            fichier réutilisable existe. Le cache est déjà invalidé
            automatiquement quand les motifs, le mode ou la version du backend
            diffèrent ; ce paramètre sert au rejeu explicite (maquette modifiée
            en place, recette à refaire de zéro).
        computed_quantities_json: JSON ``computed_base_quantities/v1`` produit
            par ``export_computed_base_quantities`` (MCP ifc-geometry). Fusionné
            **gap-only** dans le snapshot avant génération : comble les
            ``BaseQuantities`` absentes de BIMData (surfaces d'espaces,
            largeurs/hauteurs de menuiseries, aires de dalles) sans jamais
            écraser une valeur native. Sans lui — et sans
            ``extract_model_snapshot(compute_missing_quantities=True, …)``
            préalable — les colonnes de quantités des annexes sortiraient
            vides et la QA gate refuse la génération.
        project_name, project_code: identité projet, **obligatoire et
            explicite**. Il n'existe aucun repli : ni le nom du projet BIMData
            (c'est un espace de travail), ni l'entête d'un classeur MOA (c'est
            le gabarit d'un autre chantier) ne peuvent nommer un livrable. Un
            libellé générique (``Projet``, ``I3F``, ``Tarare``…) est refusé au
            même titre qu'une valeur absente. ``None`` ou générique →
            ``needs_context``, avec une suggestion lue sur le **nom de la
            maquette**. Non contournable par ``confirm_context``.
        phase: ``None`` → phase d'audit **confirmée** (``set_active_model``),
            sinon ``needs_context``. Jamais reprise de l'entête d'un classeur
            MOA : elle nomme le fichier. Non contournable par
            ``confirm_context``.
        auditor_name: nom de l'auteur du contrôle affiché sur le pack —
            **paramètre à employer**. ``auteur_controle`` (vocabulaire I3F) et
            ``auditor`` (historique) restent acceptés, dans cet ordre de
            priorité. Aucun n'est fourni → ``needs_context`` : pas de
            « AMO BIM » générique.
        usages_bim, nombre_logements, temoin_virtuel, date_controle:
            métadonnées opérationnelles du contrôle (issues du rapport I3F de
            référence) pour « Données d'entrée » / « Usages BIM 3F ». Absentes
            → « Information non disponible… ».
        reports: clés catalogue des rapports XLS à produire — celles de
            ``list_avp_i3f_xls_reports``. ``None`` (défaut) = tous ceux qui sont
            produisibles. Sinon **seuls les rapports nommés sont écrits** ; le
            rapport consolidé .docx est toujours produit, c'est lui qui porte
            l'analyse. Refus **avant toute écriture**, sans créer le dossier de
            sortie, si la sélection contient une clé inconnue (``unknown_report``)
            ou un rapport bloqué par une règle métier (``report_blocked``) : une
            faute de frappe qui produirait silencieusement autre chose que ce qui
            est demandé serait pire qu'une erreur.
        export_pdf: tente la conversion .docx → .pdf (LibreOffice si présent).
        confirm_context: ``True`` pour générer malgré un **auteur du contrôle**
            manquant. Ne contourne **jamais** ce qui nomme les livrables —
            ``project_name``, ``project_code`` et ``phase``.

    Returns:
        ``{output_dir, paths, analyse_docx, analyse_pdf, pdf_available}`` ou
        ``{status: needs_context, missing, questions}``.
    """
    # La règle de sélection vit dans le CŒUR : ce tool ne fait que traduire son
    # refus en payload MCP. Deux copies auraient divergé — c'est exactement ce
    # qui s'est produit quand le tool validait pendant que le cœur ignorait.
    from ...reporting.avp.models import AvpReportSelectionError
    from ...reporting.avp.pack import _normalize_report_selection
    from ...reporting.avp_sources import AvpSourcePaths, load_sources, read_envelope_json

    try:
        _normalize_report_selection(reports)
    except AvpReportSelectionError as refus:
        # Les deux motifs sont RENDUS ENSEMBLE quand ils coexistent. Sortir sur
        # le premier ferait corriger une typo pour découvrir ensuite qu'un
        # rapport est bloqué : un aller-retour que l'exception, elle, avait déjà
        # évité côté cœur.
        motifs: list[str] = []
        etapes: list[str] = []
        payload: dict = {"status": "error"}
        if refus.unknown:
            payload["unknown_reports"] = list(refus.unknown)
            payload["known_reports"] = list(refus.known)
            motifs.append("clé(s) de rapport inconnue(s) : " + ", ".join(refus.unknown))
            etapes.append("utiliser les clés de ``list_avp_i3f_xls_reports``")
        if refus.blocked:
            payload["blocked_reports"] = dict(refus.blocked)
            motifs.append(
                "rapport(s) non produisibles : "
                + ", ".join(f"{c} — {m}" for c, m in sorted(refus.blocked.items()))
            )
            etapes.append(
                "retirer ce(s) rapport(s) de ``reports``, ou définir la règle métier manquante"
            )
        # Un code par cause tant qu'il n'y en a qu'une ; le code générique dès
        # qu'il y en a deux — nommer l'une des deux masquerait l'autre.
        if refus.unknown and refus.blocked:
            payload["error"] = "invalid_report_selection"
        elif refus.unknown:
            payload["error"] = "unknown_report"
        else:
            payload["error"] = "report_blocked"
        # Les motifs peuvent déjà se terminer par un point (le motif de blocage
        # est une phrase) : ne pas en ajouter un second.
        payload["message"] = " ; ".join(motifs).rstrip(". ") + ". Aucun fichier n'a été écrit."
        payload["next_step"] = " ; ".join(etapes).capitalize() + "."
        return payload
    selection = None if reports is None else list(dict.fromkeys(reports))

    context = _validate_avp_context(
        controle_xlsx=controle_xlsx,
        shab_xlsx=shab_xlsx,
        zones_espaces_xlsx=zones_espaces_xlsx,
        enveloppe_xlsx=enveloppe_xlsx,
        menuiseries_xlsx=menuiseries_xlsx,
        plancher_xlsx=plancher_xlsx,
        project_name=project_name,
        project_code=project_code,
        phase=phase,
        auditor_name=auditor_name,
        auteur_controle=auteur_controle,
        auditor=auditor,
        confirm_context=confirm_context,
        AvpSourcePaths=AvpSourcePaths,
        load_sources=load_sources,
    )
    if context.response is not None:
        return context.response

    identite = context.identity
    controle_src = identite.controle_src
    sources = identite.sources
    # Enveloppe « logique MOA » : source structurée envelope.json (MCP ifc-geometry)
    # → onglet par_type (8 lignes métier), prioritaire sur le repli snapshot (484
    # murs) et sur le .xlsx source.
    # Origine du contrat, à distinguer : un chemin **passé en paramètre** n'a subi
    # aucun contrôle de cible, alors qu'un fichier **détecté** a déjà été corrélé
    # au modèle actif par ``_envelope_json_matches_model``. Seul le premier doit
    # repasser la garde de provenance.
    envelope_json_explicite = bool(envelope_json)
    envelope_json_used = envelope_json or _auto_envelope_json()
    auto_envelope = None
    envelope_source_ifc_file = None
    # On ne calcule que ce qui est attendu : sans mur d'enveloppe dans la
    # maquette, l'annexe n'a pas lieu d'être et lancer un calcul serait du bruit
    # (voire un refus sur une maquette qui n'en a pas besoin).
    #
    # EXCEPTION IMPORTANTE : des motifs explicites valent demande explicite. Le
    # cas réel est précisément celui où BIMData ne remonte pas le calque alors
    # qu'IfcOpenShell sait le lire dans l'IFC — s'en tenir au snapshot ferait
    # rater l'enveloppe sur les maquettes qui en ont le plus besoin.
    motifs_explicites = bool(
        envelope_layer_pattern or envelope_type_pattern or envelope_filter_mode
    )
    envelope_attendue = _State.snapshot is not None and (
        motifs_explicites or count_envelope_walls(_State.snapshot) > 0
    )
    if envelope_json_used is None and auto_compute_envelope and envelope_attendue:
        from ...extraction.geometry_backend import GeometryBackendUnavailable
        from ...reporting.avp_autocompute import GeometryInputMissing, ensure_envelope_json

        try:
            auto_envelope = ensure_envelope_json(
                _State.snapshot,
                ifc_path=ifc_path,
                layer_pattern=envelope_layer_pattern,
                type_pattern=envelope_type_pattern,
                filter_mode=envelope_filter_mode,
                force=force_recompute_envelope,
                session_ifc_path=getattr(_State, "ifc_path", None),
                model_ids=(_State.cloud_id, _State.project_id, _State.model_id),
            )
            envelope_json_used = auto_envelope["json_path"]
        except (GeometryInputMissing, GeometryBackendUnavailable) as exc:
            return _geometry_failure_response(exc, error="cannot_compute_envelope")
        except ValueError as exc:
            # Mode de filtrage incohérent avec les motifs (le backend refuse
            # plutôt que de se rabattre en silence). C'est une erreur d'appel,
            # pas un contexte manquant : la question à poser porte sur les
            # paramètres, et le message du backend la formule déjà.
            return {
                "status": "error",
                "error": "invalid_envelope_filter_mode",
                "envelope_filter_mode": envelope_filter_mode,
                "message": str(exc),
            }
    if envelope_json_used:
        from ...reporting.avp_autocompute import ContractModelMismatch

        # L'origine est écrite noir sur blanc : trois cas, trois politiques.
        # `detecte` échappe à la garde parce que `_auto_envelope_json` a déjà
        # corrélé le fichier au modèle actif.
        if auto_envelope is not None:
            origine_env: ContractOrigin = "calcule"
        elif envelope_json_explicite:
            origine_env = "parametre"
        else:
            origine_env = "detecte"

        # Schéma d'abord (diagnostic le plus précis), provenance ensuite : un
        # fichier illisible doit se dire illisible, pas « d'un autre modèle ».
        # La lecture précède donc la résolution de provenance.
        safe_env = (
            Path(envelope_json_used)
            if origine_env == "calcule"
            else safe_input_path(envelope_json_used, allowed_extensions={".json"})
        )
        sources.enveloppe = read_envelope_json(safe_env)
        try:
            safe_env, envelope_source_ifc_file = _resolve_contract_source(
                path=safe_env, origin=origine_env, param_name="envelope_json"
            )
        except ContractModelMismatch as exc:
            return _contract_mismatch_payload(exc)
        envelope_json_used = str(safe_env)

    # Quantités calculées : fusion **gap-only** dans le snapshot AVANT
    # génération. Sans elles, un snapshot BIMData dépourvu de BaseQuantities
    # produit des annexes aux colonnes vides (la QA gate les refuse plus bas).
    computed_coverage = None
    computed_json_used = None
    computed_source_ifc_file = None
    working_snapshot = _State.snapshot
    # Auto-résolution : si le snapshot ne porte pas les quantités et qu'aucun
    # JSON n'est fourni, on le retrouve ou on le calcule — plutôt que d'exiger
    # de l'appelant qu'il pense à enchaîner les outils lui-même.
    auto_quantities = None
    if (
        computed_quantities_json is None
        and auto_compute_quantities
        and _State.snapshot is not None
        and _qa_missing_quantities(_State.snapshot)
    ):
        from ...extraction.geometry_backend import GeometryBackendUnavailable
        from ...reporting.avp_autocompute import (
            GeometryInputMissing,
            ensure_computed_quantities_json,
        )

        try:
            auto_quantities = ensure_computed_quantities_json(
                _State.snapshot,
                ifc_path=ifc_path,
                force=force_recompute_quantities,
                session_ifc_path=getattr(_State, "ifc_path", None),
                model_ids=(_State.cloud_id, _State.project_id, _State.model_id),
            )
        except (GeometryInputMissing, GeometryBackendUnavailable) as exc:
            return _geometry_failure_response(exc, error="cannot_compute_quantities")
        computed_quantities_json = auto_quantities["json_path"]

    if computed_quantities_json:
        from ...extraction.computed_quantities import (
            load_computed_quantities,
            merge_into_snapshot,
        )

        if _State.snapshot is None:
            return {
                "status": "needs_context",
                "missing": ["snapshot"],
                "next_step": (
                    "``computed_quantities_json`` se fusionne dans le snapshot : "
                    "appeler ``extract_model_snapshot`` avant ``generate_avp_i3f_pack``."
                ),
            }
        from ...reporting.avp_autocompute import ContractModelMismatch

        # Deux origines seulement ici : ce contrat ne se détecte pas sur disque.
        origine_cq: ContractOrigin = "calcule" if auto_quantities is not None else "parametre"

        # Sandbox : un fichier FOURNI par l'utilisateur est validé en lecture ;
        # un fichier que NOUS venons de produire vit sous ``AUDIT_OUTPUT_DIR``,
        # que ``safe_input_path`` ne couvre pas nécessairement.
        safe_cq = (
            Path(computed_quantities_json)
            if origine_cq == "calcule"
            else safe_input_path(computed_quantities_json, allowed_extensions={".json"})
        )
        doc = load_computed_quantities(safe_cq)  # valide le contrat (sinon ValueError)
        # Provenance après le schéma, même raison que pour l'enveloppe.
        try:
            safe_cq, computed_source_ifc_file = _resolve_contract_source(
                path=safe_cq, origin=origine_cq, param_name="computed_quantities_json"
            )
        except ContractModelMismatch as exc:
            return _contract_mismatch_payload(exc)
        # Copie de travail : la fusion est gap-only, donc muter le snapshot de
        # SESSION la rendrait non rejouable — un second appel avec un JSON
        # recalculé verrait les anciennes valeurs comme « déjà présentes » et
        # les conserverait. On part donc systématiquement du snapshot d'origine.
        # La copie est profonde parce que la fusion mute les dicts d'éléments
        # (``property_sets``) ; le coût est assumé pour garder la génération
        # rejouable et sans effet de bord sur la session.
        working_snapshot = copy.deepcopy(_State.snapshot).index()
        computed_coverage = merge_into_snapshot(working_snapshot, doc)
        working_snapshot.computed_coverage = dict(computed_coverage)
        computed_json_used = str(safe_cq)

    build = _build_avp_pack(
        output_dir=output_dir,
        identity=identite,
        sources=sources,
        working_snapshot=working_snapshot,
        usages_bim=usages_bim,
        nombre_logements=nombre_logements,
        temoin_virtuel=temoin_virtuel,
        date_controle=date_controle,
        reports=selection,
        export_pdf=export_pdf,
    )
    if build.response is not None:
        return build.response
    # Les deux traces sont assemblées ici, au seul endroit où toutes leurs
    # composantes existent : le chemin retenu, la provenance déclarée, le
    # résultat d'auto-calcul, et — pour les quantités seulement — la couverture
    # issue de la fusion.
    envelope_trace = AvpContractTrace(
        json_used=envelope_json_used,
        source_ifc_file=envelope_source_ifc_file,
        auto_result=auto_envelope,
    )
    computed_trace = AvpContractTrace(
        json_used=computed_json_used,
        source_ifc_file=computed_source_ifc_file,
        auto_result=auto_quantities,
        coverage=computed_coverage,
    )
    return _format_avp_pack_response(
        out_dir=build.out_dir,
        pack=build.pack,
        identity=identite,
        controle_src=controle_src,
        envelope_trace=envelope_trace,
        computed_trace=computed_trace,
    )


@mcp.tool()
def generate_word_report(
    output_path: str | None = None,
    xlsx_annex_path: str | None = None,
    auditor: str | None = None,
    overwrite: bool = False,
    project_address: str | None = None,
    project_phase: str | None = None,
    auditor_name: str | None = None,
    project_description: str | None = None,
    confirm_context: bool = False,
) -> dict:
    """Génère le rapport Word d'audit (enrichi avec contexte projet).

    Le rapport Word produit inclut désormais les sections :
    *Contexte de la mission*, *Description du projet*, *Référentiels*,
    *Attendus du projet*, *Objectifs BIM*, *Liste des contrôles
    réalisés*, *Informations non disponibles*. Voir
    :mod:`audit_bim.reporting.context`.

    Trois informations contextuelles sont **recommandées** pour un
    livrable AMO BIM professionnel :

    - ``project_address`` : adresse du projet (affichée dans
      *Description du projet*).
    - ``project_phase`` : APS / APD / PRO / DCE / EXE / DOE / GESTION.
      Si fourni, écrase la phase déduite du ``AuditResult`` pour
      l'affichage. **Ne change PAS** la phase utilisée pour exécuter
      l'audit (qui a déjà tourné).
    - ``auditor_name`` : nom de l'auditeur (page de garde + section
      *Contexte de la mission*).

    Si l'une de ces 3 infos est manquante **et** ``confirm_context``
    est ``False``, le tool retourne ``{"status": "needs_context", ...}``
    avec la liste des questions à poser à l'utilisateur, sans
    régénérer le rapport.

    Args:
        output_path: Chemin de sortie (sandbox ``AUDIT_OUTPUT_DIR``).
        xlsx_annex_path: Référence à l'annexe XLSX (mise en annexe).
        auditor: Nom de l'auditeur (legacy param ; déprécié au profit
            de ``auditor_name`` qui propage dans le contexte enrichi).
        overwrite: Écraser le fichier existant.
        project_address: Adresse projet (data fiable utilisateur).
        project_phase: Phase BIM à afficher.
        auditor_name: Nom de l'auditeur enrichi.
        confirm_context: ``True`` pour passer outre la validation des
            3 champs obligatoires (rapport généré avec
            ``Information non disponible`` pour les manquants).

    Returns:
        - ``{"path": "...", "size_bytes": N}`` en cas de succès.
        - ``{"status": "needs_context", "missing": [...], "questions":
          [...]}`` si validation refusée.
    """
    _State.ensure_result()

    # Suggestions issues de la maquette pour le dialogue de contexte
    # (adresse IfcPostalAddress, description projet). Le snapshot est
    # présent (``ensure_result`` implique un audit sur snapshot).
    sugg_address = _snapshot_address_suggestion()
    sugg_description = _snapshot_description()

    # Phase — unique source de vérité. L'audit a déjà tourné : ``_State.phase``
    # est la phase confirmée. On ne re-demande une confirmation que si aucune
    # phase n'est établie (ni fournie, ni posée en session).
    explicit_phase = (
        project_phase.strip() if isinstance(project_phase, str) and project_phase.strip() else None
    )
    detected_raw, detected_mapped = _detect_snapshot_phase()
    if explicit_phase:
        eff_phase = explicit_phase
    elif _State.phase is not None:
        eff_phase = _State.phase.value
    else:
        eff_phase = detected_mapped
    require_phase_confirmation = explicit_phase is None and _State.phase is None
    suggested_phase = (
        eff_phase if eff_phase and eff_phase.upper() in _VALID_PHASES else detected_mapped
    )

    # Validation contexte
    refusal = _validate_audit_context(
        project_address=project_address,
        project_phase=eff_phase,
        # ``auditor`` (legacy) doit être vu par la validation : sinon un appel
        # historique ``auditor="Stan"`` repartait en ``needs_context`` avant
        # d'atteindre le repli prévu plus bas.
        auditor_name=(auditor_name or "").strip() or (auditor or "").strip() or None,
        # On passe la valeur **utilisateur brute** (pas la description du
        # snapshot) : la description est demandée puis validée/corrigée par
        # l'utilisateur, avec la description maquette proposée en suggestion.
        project_description=project_description,
        require_description=True,
        suggested_address=sugg_address,
        suggested_description=sugg_description,
        suggested_phase=suggested_phase,
        detected_phase_raw=detected_raw,
        require_phase_confirmation=require_phase_confirmation,
        confirm_context=confirm_context,
    )
    if refusal is not None:
        return refusal

    raw = Path(output_path) if output_path else _default_output_paths()[0]
    target = safe_export_path(raw, overwrite=overwrite)

    # Construire le contexte enrichi avec les inputs utilisateur. La
    # description utilisateur (si fournie) écrase la description déduite ;
    # sinon ``base_ctx`` conserve la description extraite du snapshot.
    base_ctx = build_report_context(_State.result)
    ctx = merge_user_context(
        base_ctx,
        project_address=project_address,
        project_phase=eff_phase,
        auditor_name=auditor_name,
        project_description=project_description,
    )

    # ``auditor_name`` est le paramètre à employer ; ``auditor`` reste accepté
    # en compat. Aucun nom n'est codé en dur : sans valeur, le rapport porte
    # ``NOT_AVAILABLE`` plutôt qu'un auteur inventé — l'appelant doit demander
    # le nom avant de générer.
    display_auditor = (auditor_name or "").strip() or (auditor or "").strip() or None

    written = write_word_report(
        _State.result,
        target,
        auditor=display_auditor,
        xlsx_annex_path=xlsx_annex_path,
        context=ctx,
    )
    return {"path": str(written), "size_bytes": written.stat().st_size}
