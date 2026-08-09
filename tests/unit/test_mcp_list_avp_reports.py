"""Tool MCP ``list_avp_i3f_xls_reports`` : listing de disponibilité sans effet
de bord, honnête sur la reproduction « à l'identique »."""

from __future__ import annotations

import pytest

from audit_bim.extraction.model_data import ModelSnapshot
from audit_bim.mcp.session import _Session, current_session
from audit_bim.profiles.i3f import tools_reporting
from audit_bim.requirements.models import BIMPhase


@pytest.fixture
def _isolated():
    sess = _Session()
    token = current_session.set(sess)
    try:
        yield sess
    finally:
        current_session.reset(token)


def _snap_with_slab() -> ModelSnapshot:
    slab = {
        "uuid": "SL1",
        "type": "IfcSlab",
        "name": "Dalle",
        "property_sets": [
            {
                "name": "BaseQuantities",
                "properties": [{"definition": {"name": "NetArea"}, "value": 50.0}],
            }
        ],
    }
    return ModelSnapshot(
        project={"name": "Programme"}, model={"name": "M.ifc"}, elements=[slab]
    ).index()


def test_lists_six_reports_in_order(_isolated):
    _isolated.snapshot = _snap_with_slab()
    out = tools_reporting.list_avp_i3f_xls_reports()
    assert out["status"] == "ok"
    keys = [r["key"] for r in out["reports"]]
    assert keys == [
        "controle_maquettes",
        "shab_maquette",
        "zones_espaces",
        "surface_enveloppe",
        "menuiseries",
        "plancher",
    ]


def test_plancher_est_annonce_bloque_avec_un_next_action_actionnable(_isolated):
    """Le tool MCP doit dire au client CE QU'IL DOIT FAIRE.

    « Bloqué » sans motif renverrait à compléter la maquette — une fausse
    piste : les dalles sont là, typées et étagées. Ce qui manque est un
    arbitrage métier.
    """
    _isolated.snapshot = _snap_with_slab()
    out = tools_reporting.list_avp_i3f_xls_reports()
    plancher = next(r for r in out["reports"] if r["key"] == "plancher")
    assert plancher["can_generate"] is False
    assert plancher["can_generate_identical"] is False
    assert plancher["status"] == "blocked"
    assert "règle métier" in plancher["next_action"]
    # Non-vacuité : les données sont bien reconnues comme présentes.
    assert plancher["available_data"], "un blocage métier ne doit pas masquer les données"


def test_no_snapshot_blocks_but_still_lists(_isolated):
    # Aucun snapshot chargé : le listing marche quand même (tout bloqué).
    out = tools_reporting.list_avp_i3f_xls_reports()
    assert out["status"] == "ok"
    assert out["project"]["name"] is None
    assert all(r["status"] == "blocked" for r in out["reports"])


def test_include_templates_false_drops_template_path(_isolated):
    _isolated.snapshot = _snap_with_slab()
    out = tools_reporting.list_avp_i3f_xls_reports(include_templates=False)
    assert all("template_path" not in r for r in out["reports"])


def test_phase_reflected_from_session(_isolated):
    _isolated.snapshot = _snap_with_slab()
    _isolated.phase = BIMPhase.AVP
    out = tools_reporting.list_avp_i3f_xls_reports()
    assert out["project"]["phase"] == "AVP"
