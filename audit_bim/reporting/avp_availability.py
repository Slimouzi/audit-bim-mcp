"""Vérification de disponibilité des rapports XLS AVP I3F.

Pour chaque rapport du catalogue (:mod:`avp_report_catalog`), on **sonde**
réellement le ``ModelSnapshot`` (entités IFC, BaseQuantities, relations
zone/espace, calque d'enveloppe) puis rend un verdict orienté utilisateur :

- ``can_generate`` : un rapport **métier** au format MOA reconstruit est produisible ;
- ``can_generate_identical`` : une reproduction MOA **stricte** (formules /
  pivots / styles préservés) est produisible — voir ci-dessous ;
- ``status`` : ``ready`` / ``partial`` / ``blocked`` ;
- ``available_data`` / ``missing_data`` : détail par donnée requise.

Position CTO respectée (P1) : ``can_generate_identical`` **ne dépend pas que**
de la disponibilité des données IFC. La génération courante reconstruit les
onglets/colonnes/formules principales au format MOA, mais ne préserve pas les
pivots et styles natifs Excel. Tant que le mode ``moa_template`` (copie du
workbook) n'est pas livré, ``can_generate_identical`` est **toujours** ``False``
(cf. ``_MOA_TEMPLATE_MODE_AVAILABLE``) et un rapport générable reste ``partial``.
"""

from __future__ import annotations

from ..extraction.model_data import ModelSnapshot
from .avp_report_catalog import (
    REPORT_SPECS,
    DataRequirement,
    ReportAvailability,
    ReportSpec,
)
from .avp_snapshot import (
    _base_quantity_ordered,
    _methode_comparable,
    _rich,
    _space_zone_uuid,
    _zone_member_uuids,
    _zone_members_from_tree,
    count_envelope_walls,
    quantite_affichable,
)

# Reproduction « à l'identique » d'un classeur MOA = préservation des **formules
# Excel natives, pivots, styles et formules critiques** du catalogue. Cela exige
# un mode « template » qui **copie** le workbook source et n'en remplace que les
# plages de données. Ce mode **n'existe pas encore** : la génération courante
# reconstruit des feuilles MOA plutôt que de copier le workbook source → pivots
# et styles natifs **ne sont pas préservés**. Tant que ce mode n'est pas livré,
# **aucun** rapport ne peut être annoncé « à l'identique », même avec toutes les
# données IFC disponibles. Voir docs/instruct-mcp-xls-moa-reports.md
# (mode ``moa_template``, priorité ultérieure).
_MOA_TEMPLATE_MODE_AVAILABLE = False


def _has_ifc_entity(snap: ModelSnapshot, classes: tuple[str, ...]) -> bool:
    return any(snap.of_class(c) for c in classes)


def _has_base_quantity(snap: ModelSnapshot, classes: tuple[str, ...], quantity: str) -> bool:
    """Vrai si au moins une entité porte la BaseQuantity nommée **et affichable**.

    « Présente dans un pset » ne suffit pas : la fusion y injecte la valeur
    calculée quand la native manque, y compris lorsque sa méthode n'est pas
    comparable — le writer l'écarte alors, et le rapport annoncerait disponible
    un livrable dont les colonnes de mesure sortiraient vides.
    """
    for cls in classes:
        for el in snap.of_class(cls):
            rich = _rich(snap, el)
            if quantite_affichable(rich, _base_quantity_ordered(rich, (quantity,)), (quantity,)):
                return True
    return False


def _uses_computed_quantity(snap: ModelSnapshot, classes: tuple[str, ...], quantity: str) -> bool:
    """Vrai si la BaseQuantity nommée provient d'une **fusion calculée** (Lot 3)
    sur au moins une entité des classes — signale un déblocage « partial_computed ».

    Une valeur dont la méthode n'est pas comparable ne débloque rien : elle
    n'atteint aucune cellule. L'annoncer « partial_computed » promettrait un
    livrable partiel là où il n'y a rien à écrire.
    """
    for cls in classes:
        for el in snap.of_class(cls):
            for c in _rich(snap, el).get("computed_base_quantities") or []:
                if c.get("quantity") == quantity and _methode_comparable(c.get("method")):
                    return True
    return False


def _has_zone_space_relation(snap: ModelSnapshot) -> bool:
    """Vrai si au moins une IfcZone est rattachée à des espaces (liste directe,
    référence inverse espace→zone, ou arborescence spatiale)."""
    zones = snap.zones or []
    if not zones:
        return False
    spaces = snap.spaces or []
    tree_members = _zone_members_from_tree(snap)
    for z in zones:
        if _zone_member_uuids(z):
            return True
        zuuid = z.get("uuid")
        if zuuid and any(_space_zone_uuid(sp) == zuuid for sp in spaces):
            return True
        if tree_members.get(zuuid):
            return True
    return False


def _source_present(sources, deliverable_key: str) -> bool:
    """Vrai si une source XLS MOA est chargée pour ce livrable.

    Indication utile pour le futur mode template uniquement : la présence de la
    source ne satisfait plus les données métier, qui doivent venir du snapshot
    IFC/OpenShell.
    """
    if sources is None:
        return False
    src = getattr(sources, deliverable_key, None)
    if src is None:
        return False
    # MultiSheetSource → au moins un onglet ; sinon présence de la dataclass.
    grids = getattr(src, "grids", None)
    if grids is not None:
        return any(getattr(g, "rows", None) for g in grids)
    table = getattr(src, "table", None)
    if table is not None:
        return bool(getattr(table, "rows", None))
    return True


def _satisfied(
    req: DataRequirement,
    snap: ModelSnapshot | None,
    source_present: bool,  # noqa: ARG001
    has_audit: bool,
) -> bool:
    if req.kind == "external_source":
        return False
    if req.kind == "audit_or_control_source":
        return has_audit
    if snap is None:
        return False
    if req.kind == "ifc_entity":
        return _has_ifc_entity(snap, req.ifc_classes)
    if req.kind == "base_quantity":
        return _has_base_quantity(snap, req.ifc_classes, req.quantity or "")
    if req.kind == "relation_zone_space":
        return _has_zone_space_relation(snap)
    if req.kind == "envelope_layer":
        return count_envelope_walls(snap) > 0
    return False


def _identical_note(missing_external: list[str]) -> str:
    """Raison(s) pour lesquelles la repro « à l'identique » est indisponible."""
    note = (
        "reproduction à l'identique NON disponible : le mode template MOA "
        "(préservation formules/pivots/styles) n'est pas encore implémenté"
    )
    if missing_external:
        note += " ; données externes aussi manquantes : " + ", ".join(missing_external)
    return note


def _next_action(
    status: str,
    missing_core: list[str],
    missing_external: list[str],
    computed_assisted: bool = False,
) -> str:
    if status == "ready":
        return "Prêt : reproduction à l'identique possible."
    if status == "blocked":
        if missing_core:
            return (
                "Données maquette manquantes : "
                + ", ".join(missing_core)
                + ". Extraire un snapshot complet (status C) ou compléter la maquette."
            )
        # Bloqué par l'exigence stricte « à l'identique ».
        return "Listing strict : " + _identical_note(missing_external) + "."
    # partial / partial_computed : rapport métier générable, mais pas à l'identique.
    prefix = "Rapport métier au format MOA reconstruit générable depuis la maquette"
    if computed_assisted:
        prefix += (
            " grâce à des BaseQuantities **calculées** (IfcOpenShell, non "
            "contractuelles) — un ré-export maquette avec BaseQuantities natives "
            "reste recommandé"
        )
    return prefix + " ; " + _identical_note(missing_external) + "."


def _availability_for_spec(
    spec: ReportSpec,
    snap: ModelSnapshot | None,
    sources,
    require_identical: bool,
    has_audit: bool,
) -> ReportAvailability:
    source_present = _source_present(sources, spec.deliverable_key)

    available: list[str] = []
    missing: list[str] = []
    missing_core: list[str] = []
    missing_external: list[str] = []
    core_ok = True
    identical_ok = True

    computed_assisted = False
    for req in spec.requirements:
        ok = _satisfied(req, snap, source_present, has_audit)
        if ok:
            available.append(req.label)
            # Le déblocage vient-il d'une BaseQuantity **calculée** (fusion Lot 3) ?
            if (
                snap is not None
                and not source_present
                and req.kind == "base_quantity"
                and _uses_computed_quantity(snap, req.ifc_classes, req.quantity or "")
            ):
                computed_assisted = True
        else:
            missing.append(req.label)
            identical_ok = False
            if req.identical_only:
                missing_external.append(req.label)
            else:
                core_ok = False
                missing_core.append(req.label)

    can_generate = core_ok
    # La disponibilité des données IFC ne suffit PAS : sans mode template (copie
    # du workbook, préservation formules/pivots/styles), la génération courante
    # reconstruit un rapport MOA mais pas une reproduction stricte.
    can_generate_identical = identical_ok and _MOA_TEMPLATE_MODE_AVAILABLE

    if not can_generate:
        status = "blocked"
    elif can_generate_identical:
        status = "ready"
    elif computed_assisted:
        # Générable grâce aux quantités **calculées** (non natives) — signalé
        # distinctement, sans masquer les gaps externes restants.
        status = "partial_computed"
    else:
        status = "partial"
    # Listing strict : un rapport « partial » ne satisfait pas l'exigence
    # « à l'identique » → il n'est pas prêt.
    if require_identical and not can_generate_identical:
        status = "blocked"

    return ReportAvailability(
        key=spec.key,
        label=spec.label,
        can_generate=can_generate,
        can_generate_identical=can_generate_identical,
        status=status,
        available_data=available,
        missing_data=missing,
        template_path=spec.resolved_template_path(),
        source_xlsx_required_for_identical=spec.requires_external_for_identical,
        next_action=_next_action(status, missing_core, missing_external, computed_assisted),
        computed_assisted=computed_assisted,
    )


def inspect_avp_report_availability(
    snapshot: ModelSnapshot | None,
    sources=None,
    require_identical: bool = False,
    has_audit_result: bool = False,
) -> list[ReportAvailability]:
    """Verdict de disponibilité de **tous** les rapports du catalogue.

    Args:
        snapshot: snapshot BIMData courant (``None`` → tout rapport « blocked »).
        sources: ``AvpSources`` déjà chargées. Réservé au futur mode template ;
            ne satisfait pas les exigences métier de surface/dimension, qui
            viennent du snapshot IFC/OpenShell.
        require_identical: si ``True``, un rapport n'est ``ready`` que si la
            reproduction stricte est possible (toutes colonnes MOA).
        has_audit_result: un ``AuditResult`` est disponible dans la session
            (audit lancé). Nécessaire pour remplir la **grille de contrôle** du
            rapport ``controle_maquettes`` quand aucune source Contrôle n'est
            fournie — le seul snapshot ne suffit pas.

    Returns:
        Liste ordonnée (ordre catalogue CTO) de :class:`ReportAvailability`.
    """
    return [
        _availability_for_spec(spec, snapshot, sources, require_identical, has_audit_result)
        for spec in REPORT_SPECS
    ]
