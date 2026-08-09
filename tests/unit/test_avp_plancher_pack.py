"""Rapport plancher : builder snapshot + intégration au pack AVP."""

from __future__ import annotations

import openpyxl

from audit_bim.extraction.model_data import ModelSnapshot
from audit_bim.reporting.avp_i3f import write_avp_i3f_report_pack
from audit_bim.reporting.avp_snapshot import build_plancher_from_snapshot, count_planchers


def _snap_with_slabs() -> ModelSnapshot:
    slab1 = {
        "uuid": "SL1",
        "type": "IfcSlab",
        "name": "Dalle RDC",
        "property_sets": [
            {
                "name": "BaseQuantities",
                "properties": [{"definition": {"name": "NetArea"}, "value": 80.0}],
            }
        ],
    }
    slab2 = {
        "uuid": "SL2",
        "type": "IfcSlab",
        "name": "Dalle R+1",
        # Pas de BaseQuantities : surface → « Superficie calculée » sinon NOT_AVAILABLE.
        "property_sets": [
            {
                "name": "Pset_SlabCommon",
                "properties": [{"definition": {"name": "Superficie calculée"}, "value": 60.0}],
            }
        ],
    }
    space = {
        "uuid": "S1",
        "type": "IfcSpace",
        "name": "CHAMBRE",
        "property_sets": [
            {
                "name": "BaseQuantities",
                "properties": [{"definition": {"name": "NetFloorArea"}, "value": 12.0}],
            }
        ],
    }
    return ModelSnapshot(
        project={"name": "Programme"},
        model={"name": "M.ifc"},
        spaces=[space],
        elements=[slab1, slab2],
    ).index()


def _all_text(path) -> str:
    wb = openpyxl.load_workbook(path, data_only=True)
    parts = [
        str(c)
        for ws in wb.worksheets
        for row in ws.iter_rows(values_only=True)
        for c in row
        if c is not None
    ]
    wb.close()
    return "\n".join(parts)


def test_build_plancher_from_snapshot_lists_slabs():
    # Multi-onglets comme le classeur MOA : détail « Dalles Ok » + synthèse « Planchers ».
    src = build_plancher_from_snapshot(_snap_with_slabs())
    assert src is not None
    # Plus une « Note de méthode » : le gabarit porte un total « Surface de
    # plancher » calculé sur une sélection métier des types de dalles, que la
    # maquette ne permet pas de reproduire. On dit pourquoi ce total manque.
    assert [g.title for g in src.grids] == [
        "TDB 2022 xx.2 - Dalles Ok",
        "Planchers",
        "Note de méthode",
    ]
    rows = src.grids[0].rows
    names = [r[1] for r in rows]
    assert "Dalle RDC" in names and "Dalle R+1" in names
    # Surface tracée par source (BaseQuantities vs Superficie calculée).
    surfaces = [r[3] for r in rows if len(r) > 3]
    assert 80.0 in surfaces and 60.0 in surfaces


def test_count_planchers():
    assert count_planchers(_snap_with_slabs()) == 2
    assert count_planchers(None) == 0


def test_pack_includes_non_empty_plancher(tmp_path):
    pack = write_avp_i3f_report_pack(
        None, tmp_path / "out", snapshot=_snap_with_slabs(), export_pdf=False
    )
    assert pack.plancher_xlsx.exists() and pack.plancher_xlsx.stat().st_size > 0
    assert pack.plancher_xlsx in pack.paths()
    text = _all_text(pack.plancher_xlsx)
    assert "Dalle RDC" in text


def test_pack_plancher_filename_convention(tmp_path):
    pack = write_avp_i3f_report_pack(
        None,
        tmp_path / "out",
        snapshot=_snap_with_slabs(),
        project_name="Tarare",
        project_code="0546L",
        phase="AVP",
        date="260203",
        export_pdf=False,
    )
    assert pack.plancher_xlsx.name == "260203 Tarare 0546L AVP - export plancher.xlsx"
