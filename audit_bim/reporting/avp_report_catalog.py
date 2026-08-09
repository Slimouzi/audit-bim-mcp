"""Catalogue des rapports XLS AVP I3F et de leurs données requises.

Ce module est **déclaratif** : il décrit, pour chaque rapport que le MCP peut
proposer, la signature du classeur MOA de référence (onglets, en-têtes, formules
critiques) et la liste des **données requises** — chacune qualifiée par ce qui la
satisfait (entité IFC, BaseQuantity, relation zone/espace, calque d'enveloppe, ou
calcul IFC/OpenShell).

Il ne lit **aucune** donnée et ne génère **aucun** fichier : la vérification de
disponibilité vit dans :mod:`avp_availability`, la génération dans
:mod:`avp_i3f`. Le catalogue est la source de vérité partagée entre les deux et
le tool MCP ``list_avp_i3f_xls_reports``.

Distinction clé (position CTO) : les données métier générées doivent venir de
la maquette IFC ou de calculs IFC/OpenShell. Les classeurs MOA de référence
restent utiles pour une future reproduction « à l'identique » (formules /
pivots / styles), mais pas comme source autoritaire des surfaces.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: Variable d'environnement déclarant le dossier des classeurs MOA de référence.
MOA_TEMPLATES_ENV = "AVP_MOA_TEMPLATES_DIR"


def moa_templates_dir() -> Path | None:
    """Dossier des classeurs MOA de référence, ou ``None`` s'il n'est pas déclaré.

    **Absence assumée, pas de défaut « machine ».** Le code portait en dur le
    chemin d'un poste de développement. Sur tout autre poste il ne désignait
    rien, et le produit se comportait comme si la configuration existait — un
    défaut faux est plus difficile à diagnostiquer qu'une absence, parce qu'il
    ressemble à une configuration.

    Déclarer ``AVP_MOA_TEMPLATES_DIR`` pour activer la résolution des
    templates ; sans elle, ``resolved_template_path()`` renvoie ``None`` et le
    tool n'expose simplement pas de chemin de template.
    """
    brut = os.getenv(MOA_TEMPLATES_ENV)
    return Path(brut).expanduser() if brut else None


@dataclass(frozen=True)
class DataRequirement:
    """Une donnée nécessaire à un rapport, et ce qui la satisfait.

    ``kind`` pilote la sonde de disponibilité (cf. :mod:`avp_availability`) :

    - ``ifc_entity`` — au moins une entité d'une des ``ifc_classes`` ;
    - ``base_quantity`` — une BaseQuantity ``quantity`` portée par une de ces
      classes ;
    - ``relation_zone_space`` — au moins une IfcZone rattachée à des espaces ;
    - ``envelope_layer`` — au moins un mur sur le calque d'enveloppe ;
    - ``external_source`` — réservé à un futur besoin template/source ;
      les rapports métier courants ne l'utilisent pas.

    ``identical_only`` : la donnée n'est requise que pour la reproduction MOA
    stricte (pivots / formules / styles). Un rapport reste générable en version
    métier sans elle.
    """

    key: str
    label: str
    kind: str
    ifc_classes: tuple[str, ...] = ()
    quantity: str | None = None
    external: bool = False
    identical_only: bool = False

    def __post_init__(self) -> None:
        # Une donnée de source externe est par nature « identique seulement »
        # et jamais satisfaite par le snapshot.
        if self.kind == "external_source":
            object.__setattr__(self, "external", True)
            object.__setattr__(self, "identical_only", True)


@dataclass(frozen=True)
class ReportSpec:
    """Spécification d'un rapport XLS AVP I3F.

    ``key`` est la clé MOA-facing (ordre du CTO). ``deliverable_key`` fait le
    lien avec ``avp_i3f._DELIVERABLE_LABELS`` / le pack (le nom interne diffère :
    ``surface_enveloppe`` ↔ ``enveloppe``, ``controle_maquettes`` ↔ ``controle``).

    ``expected_sheets`` / ``headers`` / ``critical_formulas`` décrivent le
    **classeur que nous produisons**, et non le gabarit MOA : c'est ce que le
    catalogue promet à qui lit le contrat. Un décalage y est particulièrement
    trompeur — le module ne lisant ni ne générant rien, rien ne le contredit à
    l'exécution. ``tests/unit/test_avp_catalogue_vs_sortie.py`` confronte donc
    ces trois champs au classeur réellement écrit.

    Exception assumée et **nommée dans ce test** : trois rapports portent encore
    des ``critical_formulas`` épinglées à la géométrie du gabarit MOA
    (``SUM(D2:D10)``, ``COUNTA(D2:D16)``, ``E22/D22-1``…) plutôt qu'à notre
    sortie. Les figer serait un défaut à part entière — le compteur Fenêtres ne
    doit pas dépendre des 15 types du gabarit — mais les laisser décrire deux
    choses dans un même champ en est un autre. Dette explicite, non élargie :
    le test échoue si un quatrième rapport y tombe.
    """

    key: str
    label: str
    deliverable_key: str
    example_filename: str
    expected_sheets: tuple[str, ...]
    headers: tuple[str, ...]
    critical_formulas: tuple[str, ...]
    requirements: tuple[DataRequirement, ...]

    @property
    def template_path(self) -> Path | None:
        """Chemin attendu du template, ou ``None`` si le dossier n'est pas déclaré."""
        racine = moa_templates_dir()
        return racine / self.example_filename if racine else None

    def resolved_template_path(self) -> str | None:
        """Chemin du template MOA **s'il existe** sur ce poste, sinon ``None``."""
        p = self.template_path
        return str(p) if p is not None and p.is_file() else None

    @property
    def requires_external_for_identical(self) -> bool:
        # Une reproduction stricte d'un classeur MOA suppose un mode template
        # basé sur le classeur de référence. Cela ne signifie pas que les valeurs
        # métier viennent du XLS : elles restent IFC/OpenShell.
        return True

    def core_requirements(self) -> tuple[DataRequirement, ...]:
        """Données nécessaires pour un rapport **métier** (hors « à l'identique »)."""
        return tuple(r for r in self.requirements if not r.identical_only)


@dataclass
class ReportAvailability:
    """Verdict de disponibilité d'un rapport pour la session courante."""

    key: str
    label: str
    can_generate: bool
    can_generate_identical: bool
    status: str  # "ready" | "partial" | "partial_computed" | "blocked"
    available_data: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    template_path: str | None = None
    source_xlsx_required_for_identical: bool = False
    next_action: str = ""
    # True si la génération s'appuie sur des BaseQuantities **calculées**
    # (IfcOpenShell fusionnées) et non natives BIMData → statut ``partial_computed``.
    computed_assisted: bool = False

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "can_generate": self.can_generate,
            "can_generate_identical": self.can_generate_identical,
            "status": self.status,
            "available_data": self.available_data,
            "missing_data": self.missing_data,
            "template_path": self.template_path,
            "source_xlsx_required_for_identical": self.source_xlsx_required_for_identical,
            "computed_assisted": self.computed_assisted,
            "next_action": self.next_action,
        }


# ── Données réutilisées ──────────────────────────────────────────────────────

_MENUISERIE_CLASSES = ("IfcWindow", "IfcWindowStandardCase", "IfcDoor", "IfcDoorStandardCase")

#: Périmètre RÉEL du livrable « Fenêtres » — il doit suivre
#: ``avp_snapshot._FENETRE_CLASSES``. Annoncer les portes ici ferait dire à
#: ``list_avp_i3f_xls_reports`` qu'un livrable est possible grâce à des portes
#: que le fichier généré exclut : une disponibilité vraie pour un document faux.
_FENETRE_CLASSES = ("IfcWindow", "IfcWindowStandardCase")
_ENVELOPE_WALL_CLASSES = ("IfcWall", "IfcWallStandardCase")
_SLAB_CLASSES = ("IfcSlab", "IfcCovering")

# ── Catalogue ────────────────────────────────────────────────────────────────
#
# Ordre imposé par le CTO (cf. docs/instruct-mcp-xls-moa-reports.md).

REPORT_SPECS: tuple[ReportSpec, ...] = (
    ReportSpec(
        key="controle_maquettes",
        label="Contrôle Maquettes AVP",
        deliverable_key="controle",
        example_filename="260211 Tarare 0546L Contrôle Maquettes AVP.xlsx",
        expected_sheets=("Grille de contrôle", "Zones Nommage", "Pièces Nommage"),
        headers=("POINTS DE CONTROLE", "EVALUATION"),
        critical_formulas=(),
        requirements=(
            DataRequirement("IfcSpace", "Espaces (IfcSpace)", "ifc_entity", ("IfcSpace",)),
            DataRequirement("IfcZone", "Zones (IfcZone)", "ifc_entity", ("IfcZone",)),
            # La grille de contrôle est remplie par l'AuditResult. Le seul
            # snapshot (ex. après verify_active_model) ne suffit pas : sans
            # audit lancé, la grille sort vide / NOT_AVAILABLE.
            DataRequirement(
                "controle_grille",
                "Grille de contrôle (audit lancé)",
                "audit_or_control_source",
            ),
        ),
    ),
    ReportSpec(
        key="shab_maquette",
        label="export SHAB maquette",
        deliverable_key="shab",
        example_filename="260201 Tatare 0546L AVP - export SHAB maquette.xlsx",
        expected_sheets=("Feuil1", "TDB 2022 01.3 - Export Zones...", "Note de méthode"),
        headers=(
            "Composant",
            "Nom Zone",
            "Type de Zone",
            "Groupes",
            "Pièce",
            "Type Pièce",
            "Surface IFC OpenShell",
            "Surface Nette (Qté de Base)",
            "Étage",
            "Surface Brute (Qté de Base)",
            "Couleur",
            "écarts",
        ),
        critical_formulas=(
            'IF(OR(Gn="",Hn=""),"",IF(Hn/Gn-1=0,"",Hn/Gn-1))',
            "SUBTOTAL(9,Ln:Ln)",
        ),
        requirements=(
            DataRequirement("IfcSpace", "Espaces (IfcSpace)", "ifc_entity", ("IfcSpace",)),
            DataRequirement(
                "BaseQuantities.NetFloorArea",
                "Surface Nette (Qté de Base)",
                "base_quantity",
                ("IfcSpace",),
                quantity="NetFloorArea",
            ),
            DataRequirement(
                "zone_space", "Rattachement zone/espace", "relation_zone_space", ("IfcZone",)
            ),
        ),
    ),
    ReportSpec(
        key="zones_espaces",
        label="Export Zones et Espaces",
        deliverable_key="zones_espaces",
        example_filename="260130 Tarare Export Zones et Espaces.xlsx",
        expected_sheets=("Feuil2", "TDB 2022 01.3 - Export Zones...", "Feuil1"),
        headers=(
            "Composant",
            "Nom Zone",
            "Type de Zone",
            "Groupes",
            "Pièce (Nombre)",
            "Type Pièce",
            "Surface IFC OpenShell",
            "Surface Nette (Qté de Base)",
            "Étage",
            "Surface Brute (Qté de Base)",
            "Couleur",
            "écarts",
        ),
        critical_formulas=('IF(OR(Gn="",Hn=""),"",IF(Hn/Gn-1=0,"",Hn/Gn-1))',),
        requirements=(
            DataRequirement("IfcZone", "Zones (IfcZone)", "ifc_entity", ("IfcZone",)),
            DataRequirement("IfcSpace", "Espaces (IfcSpace)", "ifc_entity", ("IfcSpace",)),
            DataRequirement(
                "zone_space", "Rattachement zone/espace", "relation_zone_space", ("IfcZone",)
            ),
            DataRequirement(
                "BaseQuantities.NetFloorArea",
                "Surface Nette (Qté de Base)",
                "base_quantity",
                ("IfcSpace",),
                quantity="NetFloorArea",
            ),
        ),
    ),
    ReportSpec(
        key="surface_enveloppe",
        label="Extraction surface enveloppe",
        deliverable_key="enveloppe",
        example_filename="260130 Tarare Extraction surface enveloppe.xlsx",
        expected_sheets=("TDB 2022 04.2 - Extraction s...",),
        headers=(
            "Composant",
            "Type",
            "Étages",
            "Archicad BQ NetSideArea",
            "Surface IFC OpenShell",
            "ArchiCAD Superficie des ouvertures sur face extérieure",
            "IFC OpenShell Surface des Fenêtres",
            "IFC OpenShell Surface des Portes",
            "Nombre",
        ),
        critical_formulas=("SUM(D2:D10)", "E11/D11-1", 'GETPIVOTDATA("Surface Nette (Qté de Base)'),
        requirements=(
            DataRequirement(
                "envelope_walls",
                "Murs d'enveloppe (calque « Extérieurs périphériques »)",
                "envelope_layer",
                _ENVELOPE_WALL_CLASSES,
            ),
            DataRequirement(
                "BaseQuantities.NetSideArea",
                "Archicad BQ NetSideArea",
                "base_quantity",
                _ENVELOPE_WALL_CLASSES,
                quantity="NetSideArea",
            ),
        ),
    ),
    ReportSpec(
        key="menuiseries",
        label="export Fenêtres",
        deliverable_key="menuiseries",
        example_filename="260130 Tarare export Menuiseries.xlsx",
        expected_sheets=("TDB 2022 05.1 - Fenêtres Ok",),
        headers=(
            "Composant",
            "Type",
            "Matériau",
            "BaseQuantities.Width",
            "BaseQuantities.Height",
            "Surface Natif",
            "Nombre",
            "Largeur IFC OpenShell",
            "Hauteur IFC OpenShell",
            "Surface IFC OpenShell",
            "Ecart BaseQuantities / IFC OpenShell (largeur)",
            "Ecart BaseQuantities / IFC OpenShell (hauteur)",
        ),
        critical_formulas=('IF(Hn-Dn=0,"",Hn-Dn)', 'IF(In-En=0,"",In-En)', "COUNTA(D2:D16)"),
        requirements=(
            DataRequirement(
                "menuiseries",
                "Fenêtres (IfcWindow)",
                "ifc_entity",
                _FENETRE_CLASSES,
            ),
            DataRequirement(
                "BaseQuantities.Width",
                "BaseQuantities.Width",
                "base_quantity",
                _FENETRE_CLASSES,
                quantity="Width",
            ),
            DataRequirement(
                "BaseQuantities.Height",
                "BaseQuantities.Height",
                "base_quantity",
                _FENETRE_CLASSES,
                quantity="Height",
            ),
        ),
    ),
    ReportSpec(
        key="plancher",
        label="export plancher",
        deliverable_key="plancher",
        example_filename="260203 Tatare 0546L AVP - export plancher.xlsx",
        expected_sheets=("TDB 2022 xx.2 - Dalles Ok", "Planchers"),
        headers=(
            "Composant",
            "Type",
            "Étage",
            "BaseQuantities.NetArea",
            "Surface IFC OpenShell",
            "Nombre",
        ),
        critical_formulas=('IF(En-Dn=0,"",En/Dn-1)', "SUM(D2:D21)", "E22/D22-1"),
        requirements=(
            DataRequirement("IfcSlab", "Dalles / planchers (IfcSlab)", "ifc_entity", _SLAB_CLASSES),
            DataRequirement(
                "BaseQuantities.NetArea",
                "BaseQuantities.NetArea",
                "base_quantity",
                _SLAB_CLASSES,
                quantity="NetArea",
            ),
        ),
    ),
)

REPORT_SPECS_BY_KEY: dict[str, ReportSpec] = {spec.key: spec for spec in REPORT_SPECS}
