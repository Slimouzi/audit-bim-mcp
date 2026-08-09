"""Catalogue + vérification de disponibilité des rapports XLS AVP I3F.

Couvre l'exigence CTO : sonder réellement le snapshot (entités IFC,
BaseQuantities, relations) et **ne pas promettre « à l'identique »** sans mode
template MOA, même quand les données IFC sont disponibles.
"""

from __future__ import annotations

from audit_bim.extraction.model_data import ModelSnapshot
from audit_bim.reporting.avp_availability import inspect_avp_report_availability
from audit_bim.reporting.avp_report_catalog import (
    REPORT_SPECS,
    REPORT_SPECS_BY_KEY,
)
from audit_bim.reporting.avp_sources import AvpSources


def _bq(name: str, value: float) -> dict:
    return {
        "name": "BaseQuantities",
        "properties": [{"definition": {"name": name}, "value": value}],
    }


def _full_snapshot() -> ModelSnapshot:
    """Maquette riche : slab+NetArea, fenêtre+W/H, espace+NetFloorArea, zone
    rattachée, mur d'enveloppe+NetSideArea."""
    slab = {
        "uuid": "SL1",
        "type": "IfcSlab",
        "name": "Dalle RDC",
        "property_sets": [_bq("NetArea", 80.0)],
    }
    window = {
        "uuid": "WIN1",
        "type": "IfcWindow",
        "name": "F1",
        "property_sets": [
            {
                "name": "BaseQuantities",
                "properties": [
                    {"definition": {"name": "Width"}, "value": 1.2},
                    {"definition": {"name": "Height"}, "value": 1.0},
                ],
            }
        ],
    }
    wall = {
        "uuid": "W1",
        "type": "IfcWall",
        "name": "Mur ext",
        "layers": [{"name": "MURS - Extérieurs périphériques.Exnd"}],
        "property_sets": [_bq("NetSideArea", 25.0)],
    }
    space = {
        "uuid": "S1",
        "type": "IfcSpace",
        "name": "CHAMBRE",
        "property_sets": [_bq("NetFloorArea", 12.0)],
    }
    zone = {"uuid": "Z1", "type": "IfcZone", "name": "0546L-LOGT1", "spaces": ["S1"]}
    return ModelSnapshot(
        project={"name": "Programme"},
        model={"name": "M.ifc"},
        spaces=[space],
        zones=[zone],
        elements=[slab, window, wall],
    ).index()


def _by_key(avails):
    return {a.key: a for a in avails}


# ── Catalogue ────────────────────────────────────────────────────────────


def test_catalog_has_six_reports_in_cto_order():
    keys = [s.key for s in REPORT_SPECS]
    assert keys == [
        "controle_maquettes",
        "shab_maquette",
        "zones_espaces",
        "surface_enveloppe",
        "menuiseries",
        "plancher",
    ]


def test_plancher_spec_targets_ifcslab_and_ifc_openshell_surface():
    spec = REPORT_SPECS_BY_KEY["plancher"]
    assert spec.deliverable_key == "plancher"
    classes = {c for r in spec.requirements for c in r.ifc_classes}
    assert "IfcSlab" in classes
    assert "Surface IFC OpenShell" in spec.headers
    # La reproduction stricte nécessite un template MOA, pas une donnée Solibri.
    assert spec.requires_external_for_identical is True


def test_every_report_maps_to_a_deliverable_key():
    valid = {"controle", "shab", "zones_espaces", "enveloppe", "menuiseries", "plancher"}
    assert all(s.deliverable_key in valid for s in REPORT_SPECS)


# ── Disponibilité snapshot-only ──────────────────────────────────────────


def test_snapshot_only_generates_business_but_never_identical_without_template_mode():
    avails = _by_key(inspect_avp_report_availability(_full_snapshot()))
    for key in ("shab_maquette", "zones_espaces", "surface_enveloppe", "menuiseries", "plancher"):
        av = avails[key]
        assert av.can_generate is True, key
        assert av.can_generate_identical is False, key
        assert av.status == "partial", key
        assert av.source_xlsx_required_for_identical is True, key
        assert not any("Solibri" in item for item in av.missing_data), key


def test_controle_blocked_on_snapshot_without_audit_or_source():
    # P1 : la grille de contrôle a besoin d'un AuditResult ou d'une source
    # Contrôle. Le seul snapshot (ex. après verify_active_model) ne suffit pas.
    av = _by_key(inspect_avp_report_availability(_full_snapshot()))["controle_maquettes"]
    assert av.can_generate is False
    assert av.status == "blocked"


def test_controle_generatable_but_not_identical_with_audit():
    # Contrôle : générable dès qu'un audit a tourné, mais JAMAIS « à l'identique »
    # (grille brandée BIMData dérivée de l'audit, pas une copie du classeur MOA).
    av = _by_key(inspect_avp_report_availability(_full_snapshot(), has_audit_result=True))[
        "controle_maquettes"
    ]
    assert av.can_generate is True
    assert av.can_generate_identical is False
    assert av.status == "partial"


def test_plancher_available_data_lists_slab_and_netarea():
    av = _by_key(inspect_avp_report_availability(_full_snapshot()))["plancher"]
    joined = " ".join(av.available_data)
    assert "IfcSlab" in joined and "NetArea" in joined
    assert av.missing_data == []


def test_plancher_blocked_without_slab():
    snap = ModelSnapshot(spaces=[{"uuid": "S1", "type": "IfcSpace"}]).index()
    av = _by_key(inspect_avp_report_availability(snap))["plancher"]
    assert av.can_generate is False
    assert av.status == "blocked"


def test_no_snapshot_blocks_entity_reports():
    avails = _by_key(inspect_avp_report_availability(None))
    assert avails["menuiseries"].status == "blocked"
    assert avails["menuiseries"].can_generate is False


# ── Sources externes / hybride ───────────────────────────────────────────


def test_source_present_generates_branded_not_identical():
    from audit_bim.reporting.avp_sources import MenuiseriesSource, SheetTable

    # Une source XLS chargée ne remplace pas les données IFC : le snapshot
    # reste la source métier, et la génération brandée n'est pas
    # « à l'identique ».
    sources = AvpSources(
        menuiseries=MenuiseriesSource(
            table=SheetTable(title="Menuiseries", headers=["Composant"], rows=[["IfcWindow"]])
        )
    )
    av = _by_key(inspect_avp_report_availability(_full_snapshot(), sources=sources))["menuiseries"]
    assert av.can_generate is True
    assert av.can_generate_identical is False
    assert av.status == "partial"
    assert "template" in av.next_action.lower()


def test_plancher_est_de_nouveau_produisible():
    """La règle métier de la Surface de plancher est établie et vérifiée
    (cf. ``test_surface_de_plancher_regle``) : plus rien ne le bloque.

    Le MÉCANISME de blocage, lui, reste couvert dans ``test_blocage_metier`` —
    sur une spécification patchée, pour ne pas dépendre d'un rapport
    réellement bloqué.
    """
    av = _by_key(inspect_avp_report_availability(_full_snapshot()))["plancher"]
    assert av.can_generate is True
    assert av.status == "partial"


def test_menuiseries_source_only_generates_without_snapshot():
    # Source XLS seule (pas de snapshot) → pas de donnée métier IFC/OpenShell.
    sources = AvpSources(
        menuiseries=None,
    )
    # simulate a loaded menuiseries source via the tabular attribute
    from audit_bim.reporting.avp_sources import MenuiseriesSource, SheetTable

    sources.menuiseries = MenuiseriesSource(
        table=SheetTable(title="Menuiseries", headers=["Composant"], rows=[["F1"]])
    )
    av = _by_key(inspect_avp_report_availability(None, sources=sources))["menuiseries"]
    assert av.can_generate is False
    assert av.can_generate_identical is False
    assert av.status == "blocked"


# ── require_identical ────────────────────────────────────────────────────


def test_require_identical_blocks_everything_without_template_mode():
    avails = _by_key(
        inspect_avp_report_availability(
            _full_snapshot(), require_identical=True, has_audit_result=True
        )
    )
    # Aucun mode template → « à l'identique » impossible → tout bloqué en strict,
    # y compris le contrôle (grille brandée, jamais une copie du classeur MOA).
    assert avails["menuiseries"].status == "blocked"
    assert avails["controle_maquettes"].status == "blocked"


def test_no_report_ever_promises_identical():
    # Verrou P1 : tant que le mode template MOA n'existe pas, AUCUN rapport
    # n'annonce « à l'identique » — même audit lancé et sources présentes.
    for av in inspect_avp_report_availability(_full_snapshot(), has_audit_result=True):
        assert av.can_generate_identical is False, av.key
