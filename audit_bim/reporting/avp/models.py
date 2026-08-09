"""Modèles, exceptions et constantes pures du pack AVP I3F.

Dataclasses (:class:`AvpMeta`, :class:`AvpReportPack`), exception QA
(:class:`AvpQaError`), constantes d'échafaudage et helpers de nommage de
fichier. Aucune dépendance vers les builders (feuille du DAG interne).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..word_report import NOT_AVAILABLE

# Mention normalisée (note méthodo) apposée sous les tables contenant des
# quantités calculées : dit le RÔLE de chaque colonne, et à quelle condition une
# valeur n'est pas contractuelle.
#
# Ce texte est LIVRÉ AU CLIENT : il énonçait « une quantité est native OU
# calculée », ce qui était vrai tant que la fusion jetait la valeur calculée dès
# qu'une native existait. Les deux coexistent désormais — c'est même le cas
# recherché, celui qui rend l'écart exploitable.
_COMPUTED_METHODO_NOTE = (
    "Provenance des quantités. Les colonnes « … (Qté de Base) » portent la "
    "valeur native issue de la maquette lorsqu'elle la fournit ; les colonnes "
    "« … IFC OpenShell » portent la valeur calculée par analyse géométrique, "
    "fournie pour COMPARAISON. Les deux peuvent coexister sur une même ligne : "
    "c'est le cas attendu, celui qui rend la colonne d'écart exploitable. "
    "Lorsqu'une colonne « … (Qté de Base) » est vide et que la colonne "
    "« … IFC OpenShell » est renseignée, la quantité provient du calcul : elle "
    "est alors NON contractuelle, en attente d'un ré-export maquette avec "
    "BaseQuantities natives."
)

# Convention de nommage documentaire I3F, **générée à partir de données
# projet confirmées** :
#
#     YYMMDD <NomProjet> <CodeProjet> <Phase> - <TypeLivrable>.<ext>
#
# ``YYMMDD`` = date de génération du livrable. Chaque livrable a un libellé
# de type et une extension fixes ; le nom du projet, le code (ESI) et la
# phase sont injectés depuis les valeurs confirmées par l'utilisateur.
_DELIVERABLE_LABELS: dict[str, tuple[str, str]] = {
    "controle": ("Contrôle Maquettes", "xlsx"),
    "shab": ("export SHAB maquette", "xlsx"),
    "zones_espaces": ("Export Zones et Espaces", "xlsx"),
    "enveloppe": ("Extraction surface enveloppe", "xlsx"),
    "menuiseries": ("export Menuiseries", "xlsx"),
    "plancher": ("export plancher", "xlsx"),
    "analyse": ("Rapport analyse BIM", "docx"),
}

# Caractères interdits / risqués dans un nom de fichier (séparateurs de
# chemin, caractères réservés Windows). Remplacés par un espace.
_FILENAME_BAD = '/\\:*?"<>|\r\n\t'


def _sanitize_filename_part(value: str | None) -> str:
    """Nettoie un fragment de nom de fichier (séparateurs, espaces)."""
    if not value:
        return ""
    out = "".join(" " if c in _FILENAME_BAD else c for c in str(value))
    return " ".join(out.split()).strip()


def _deliverable_filename(
    key: str, *, date: str, project_name: str, project_code: str, phase: str
) -> str:
    """Construit le nom d'un livrable selon la convention I3F.

    ``YYMMDD Nom Code Phase - TypeLivrable.ext`` — les fragments vides
    (code / phase absents) sont simplement omis (jamais inventés).
    """
    label, ext = _DELIVERABLE_LABELS[key]
    head_parts = [
        _sanitize_filename_part(date),
        _sanitize_filename_part(project_name),
        _sanitize_filename_part(project_code),
        _sanitize_filename_part(phase),
    ]
    head = " ".join(p for p in head_parts if p)
    label = _sanitize_filename_part(label)
    return f"{head} - {label}.{ext}"


_CONTROLE_STATS_SHEETS = (
    "Zones Nommage",
    "Pièces Nommage",
    "ARC bsence de matériau",
    "Zones ObjectType",
)


@dataclass
class AvpMeta:
    # Défauts **génériques** : aucune identité client codée en dur (le nom
    # et le code réels viennent des données confirmées / des sources I3F).
    project_name: str = "Projet"
    project_code: str = ""
    phase: str = "AVP"
    auditor: str = "AMO BIM"
    # Métadonnées opérationnelles du contrôle (issues du rapport I3F de
    # référence, fournies par l'appelant). Absentes → NOT_AVAILABLE, jamais
    # inventées.
    usages_bim: list[str] | None = None
    nombre_logements: str | None = None
    temoin_virtuel: str | None = None
    date_controle: str | None = None
    auteur_controle: str | None = None


@dataclass
class AvpReportPack:
    controle_xlsx: Path
    shab_xlsx: Path
    zones_espaces_xlsx: Path
    enveloppe_xlsx: Path
    menuiseries_xlsx: Path
    # ``None`` quand le rapport est bloqué par une règle métier absente : le
    # fichier n'est alors pas écrit du tout. Un chemin vers un classeur non
    # conforme serait pire qu'une absence — il se lirait comme un livrable.
    plancher_xlsx: Path | None
    analyse_docx: Path
    analyse_pdf: Path | None = None

    def paths(self) -> list[Path]:
        out = [
            self.controle_xlsx,
            self.shab_xlsx,
            self.zones_espaces_xlsx,
            self.enveloppe_xlsx,
            self.menuiseries_xlsx,
            *([self.plancher_xlsx] if self.plancher_xlsx is not None else []),
            self.analyse_docx,
        ]
        if self.analyse_pdf is not None:
            out.append(self.analyse_pdf)
        return out


class AvpQaError(RuntimeError):
    """Livrable(s) client inexploitable(s) alors que la maquette a des données.

    Deux cas, tous deux refusés avant livraison :

    - ``kind="empty"`` — un export sort sans aucune ligne métier alors que le
      snapshot expose des espaces / murs / zones exploitables ;
    - ``kind="missing_quantities"`` — l'export a des lignes mais **toutes ses
      colonnes de quantités sont vides**. C'est le cas le plus trompeur : le
      fichier paraît complet et se lit comme un résultat ;
    - ``kind="external_tool_mention"`` — un livrable cite un outil tiers hérité
      du classeur MOA de référence (``Solibri``, ``BimCollab*``). Le pack
      attribuerait alors le contrôle à un logiciel étranger à la chaîne BIMData,
      et trahirait le chantier dont le template a été recyclé ;
    - ``kind="envelope_filter_mode"`` — l'enveloppe a été calculée en mode
      ``geometric``, sans filtre de calque ni de type. Sur une maquette I3F le
      livrable compte alors des cloisons et des refends, et écarte des types
      d'enveloppe légitimes : les chiffres sont plausibles et faux. Refus le
      plus discret des quatre, donc le plus nécessaire.
    """

    def __init__(self, empty: list[str], *, kind: str = "empty"):
        self.empty = empty
        self.kind = kind
        if kind == "missing_quantities":
            message = (
                "Annexe(s) sans aucune quantité exploitable : "
                + ", ".join(empty)
                + ". Le livrable aurait des lignes mais des colonnes vides. "
                "Le snapshot ne porte pas de BaseQuantities : relancer "
                "``extract_model_snapshot(compute_missing_quantities=True, "
                "computed_quantities_json=…)``, ou passer "
                "``computed_quantities_json`` à ``generate_avp_i3f_pack`` — le "
                "JSON `computed_base_quantities/v1` est produit par "
                "``export_computed_base_quantities`` (MCP ifc-geometry)."
            )
        elif kind == "external_tool_mention":
            message = (
                "Livrable(s) citant un outil tiers hérité du classeur MOA de "
                "référence : "
                + ", ".join(empty)
                + ". Livraison refusée : le pack attribuerait le contrôle à un "
                "logiciel que la chaîne BIMData n'emploie pas."
            )
        elif kind == "envelope_filter_mode":
            message = (
                "Enveloppe calculée sans filtre de calque ni de type (mode "
                "``geometric``) : "
                + ", ".join(empty)
                + ". Livraison refusée — sur une maquette I3F ce mode compte des "
                "cloisons et des refends, et écarte des types d'enveloppe "
                "attendus. Les chiffres seraient plausibles et faux."
            )
        else:
            message = (
                "Annexe(s) vide(s) malgré des données exploitables dans la maquette : "
                + ", ".join(empty)
                + ". Livraison refusée (ni sources I3F ni extraction snapshot n'ont "
                "produit de lignes)."
            )
        super().__init__(message)


# Marqueurs d'échafaudage à ignorer lors du comptage des lignes métier.
_QA_SCAFFOLD = {
    _n
    for _n in (
        NOT_AVAILABLE.strip().lower(),
        "(onglet vide dans la source i3f)",
        "synthèse",
    )
}
