"""Consommation des contrats JSON versionnés (bim-core) côté audit.

Le trou fermé ici est côté **lecture** : avant toute fusion ou génération de
livrable, un document doit être reconnu. La politique n'est plus réimplémentée
dans audit-bim — elle vient de ``bim_core.contracts`` :

- document V1 (celui qu'émet le MCP géométrique) → accepté ;
- fichier historique **sans** ``schema`` → accepté via migration + avertissement ;
- ``schema`` présent mais inconnu ou invalide (``null``, ``""``, ``0``, ``False``)
  → **refusé**, avant toute fusion ;
- mode strict → la compat legacy disparaît, y compris pour les fichiers réels.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import openpyxl
import pytest
from bim_core.contracts import (
    SCHEMA_COMPUTED_BASE_QUANTITIES_V1,
    SCHEMA_ENVELOPE_QUANTITIES_V1,
    ContractError,
    LegacySchemaWarning,
    MissingSchemaError,
    UnknownSchemaError,
)

from audit_bim.extraction.computed_quantities import (
    load_computed_quantities,
    merge_into_snapshot,
)
from audit_bim.extraction.model_data import ModelSnapshot
from audit_bim.reporting.avp_i3f import AvpMeta, _build_enveloppe_xlsx
from audit_bim.reporting.avp_sources import AvpSources, read_envelope_json

# Fichier RÉEL du projet MN_BAT (3 juillet), antérieur aux contrats versionnés :
# pas de `schema`, clés `layer_pattern` / `seuil_i3f`, `etages` en liste.
MN_BAT_LEGACY = Path(__file__).parent.parent / "fixtures" / "250613_MN_BAT_envelope_legacy.json"


def _v1_envelope_document() -> dict:
    """Document tel que l'émet le MCP géométrique après passage en V1."""
    return {
        "schema": SCHEMA_ENVELOPE_QUANTITIES_V1,
        "source": {
            "producer": "ifc-geometry",
            "tool": "extract_envelope_surfaces",
            "version": "0.2.0",
            "ifc_file": "250613_MN_BAT.ifc",
        },
        "created_at": "2026-08-01T18:00:00+00:00",
        "summary": {
            "superficie_facades_m2": 2071.18,
            "superficie_facades_nette_m2": 1950.0,
            "superficie_calque_total_m2": 3053.49,
            "superficie_menuiseries_m2": 121.18,
            "superficie_menuiseries_fenetres_m2": 90.0,
            "superficie_menuiseries_portes_m2": 31.18,
            "shab_m2": 2164.68,
            "ratio_fac_shab": 0.9568,
            "seuil_i3f": 0.9,
            "methode_facade": "space_boundaries",
        },
        "par_type": [
            {
                "type": "ME_36",
                "etages": ["R+1", "R+2"],
                "net_side_area_m2": 1200.0,
                "n": 24,
                "menuiseries_m2": 57.6,
            },
            {
                "type": "ME_25",
                "etages": ["RDC"],
                "net_side_area_m2": 871.18,
                "n": 18,
                "menuiseries_m2": 63.58,
            },
        ],
        "hors_filtre_type": [
            {"type": "MUR INT", "etages": [], "net_side_area_m2": 982.31, "n": 120}
        ],
        "diagnostics": {"counts": {"n_murs_exterieurs": 42}, "menuiseries_detail": []},
    }


def _v1_quantities_document() -> dict:
    return {
        "schema": SCHEMA_COMPUTED_BASE_QUANTITIES_V1,
        "source": {
            "producer": "ifc-geometry",
            "tool": "export_computed_base_quantities",
            "version": "0.2.0",
            "ifc_file": "/in/250613_MN_BAT.ifc",
        },
        "created_at": "2026-08-01T18:00:00+00:00",
        "quantities": [
            {
                "global_id": "SPACE-1",
                "ifc_class": "IfcSpace",
                "qto": "Qto_SpaceBaseQuantities",
                "quantity": "NetFloorArea",
                "value": 12.98,
                "unit": "m2",
                "method": "ifcopenshell_geometry",
                "status": "computed",
                "source": "computed_ifcopenshell",
            }
        ],
        "coverage": {"n_elements": 1, "n_computed": 1, "n_failed": 0},
        "warnings": [],
    }


def _write(tmp_path, doc, name="envelope.json") -> str:
    p = tmp_path / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return str(p)


# ── V1 producteur accepté ──────────────────────────────────────────────


def test_v1_envelope_accepted_without_warning(tmp_path):
    path = _write(tmp_path, _v1_envelope_document())
    with warnings.catch_warnings():
        warnings.simplefilter("error", LegacySchemaWarning)  # aucune compat sollicitée
        src = read_envelope_json(path)
    assert len(src.table.rows) == 2
    assert src.shab == 2164.68
    assert src.ratio_fac_shab == pytest.approx(0.9568)
    assert src.seuil_3f == 0.9  # `seuil_i3f` du contrat, nom historique côté source
    assert len(src.hors_filtre_type) == 1  # diagnostic, hors total métier


def test_v1_quantities_accepted_and_mergeable(tmp_path):
    path = _write(tmp_path, _v1_quantities_document(), "computed.json")
    with warnings.catch_warnings():
        warnings.simplefilter("error", LegacySchemaWarning)
        doc = load_computed_quantities(path)
    snap = ModelSnapshot(elements=[{"uuid": "SPACE-1", "type": "IfcSpace"}]).index()
    coverage = merge_into_snapshot(snap, doc)
    assert coverage["n_merged"] == 1
    assert coverage["n_unknown_uuid"] == 0


# ── legacy MN_BAT accepté (fichier réel) ───────────────────────────────


def test_mn_bat_legacy_file_accepted_with_warning():
    with pytest.warns(LegacySchemaWarning, match="legacy_schema_missing"):
        src = read_envelope_json(str(MN_BAT_LEGACY))
    # Valeurs pré-validées sur le projet réel.
    assert len(src.table.rows) == 8
    assert src.superficie_facades == pytest.approx(2071.18)
    assert src.shab == pytest.approx(2164.68)
    assert src.ratio_fac_shab == pytest.approx(0.9568)
    assert src.seuil_3f == 0.9
    assert round(sum(r[3] for r in src.table.rows), 2) == pytest.approx(2071.19)


def test_legacy_key_aliases_still_understood(tmp_path):
    """Les alias historiques restent lus — mais la normalisation vit dans bim-core."""
    legacy = {
        "par_type": [{"type": "ME_30", "etages": "RDC", "netsidearea_m2": 300.0, "nombre": 38}],
        "superficie_facades_m2": 300.0,
        "shab_m2": 400.0,
        "seuil_3f": 0.9,  # ancien nom
    }
    with pytest.warns(LegacySchemaWarning):
        src = read_envelope_json(_write(tmp_path, legacy))
    row = src.table.rows[0]
    assert row[3] == 300.0  # `netsidearea_m2` normalisé
    assert row[8] == 38  # `nombre` normalisé
    assert row[2] == "RDC"  # `etages` scalaire normalisé en liste
    assert src.seuil_3f == 0.9  # `seuil_3f` normalisé en `seuil_i3f`


# ── schéma inconnu ou invalide refusé AVANT toute fusion ───────────────


@pytest.mark.parametrize("declared", [None, "", 0, False, "envelope_quantities/v2", 1])
def test_invalid_schema_is_refused(tmp_path, declared):
    doc = _v1_envelope_document()
    doc["schema"] = declared
    with pytest.raises(UnknownSchemaError):
        read_envelope_json(_write(tmp_path, doc))


@pytest.mark.parametrize("declared", [None, "", 0, False, "computed_base_quantities/v2"])
def test_invalid_quantities_schema_is_refused_before_merge(tmp_path, declared):
    doc = _v1_quantities_document()
    doc["schema"] = declared
    with pytest.raises(UnknownSchemaError):
        load_computed_quantities(_write(tmp_path, doc, "computed.json"))


def test_unrecognized_shape_without_schema_is_refused(tmp_path):
    with pytest.raises(ContractError):
        read_envelope_json(_write(tmp_path, {"peu importe": 1}))


def test_contract_errors_remain_value_errors(tmp_path):
    """Les appelants historiques attrapent ``ValueError`` — contrat préservé."""
    doc = _v1_quantities_document()
    doc["schema"] = "autre/v9"
    with pytest.raises(ValueError):
        load_computed_quantities(_write(tmp_path, doc, "computed.json"))


# ── mode strict : la compat legacy disparaît ───────────────────────────


def test_strict_mode_refuses_legacy_envelope(monkeypatch):
    monkeypatch.setenv("BIM_CORE_JSON_STRICT_SCHEMA", "true")
    with pytest.raises(MissingSchemaError, match="BIM_CORE_JSON_STRICT_SCHEMA"):
        read_envelope_json(str(MN_BAT_LEGACY))


def test_strict_mode_still_accepts_v1(monkeypatch, tmp_path):
    path = _write(tmp_path, _v1_envelope_document())
    monkeypatch.setenv("BIM_CORE_JSON_STRICT_SCHEMA", "true")
    assert read_envelope_json(path).shab == 2164.68


def test_strict_mode_refuses_legacy_quantities(monkeypatch, tmp_path):
    legacy = {"quantities": [{"global_id": "A", "quantity": "NetArea", "status": "computed"}]}
    path = _write(tmp_path, legacy, "computed.json")
    monkeypatch.setenv("BIM_CORE_JSON_STRICT_SCHEMA", "true")
    with pytest.raises(MissingSchemaError):
        load_computed_quantities(path)


# ── le livrable reste généré, et non vide ──────────────────────────────


def _envelope_sheet(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    grid = [list(row) for row in ws.iter_rows(values_only=True)]
    wb.close()
    return grid


def test_envelope_deliverable_is_not_empty_with_v1(tmp_path):
    src = read_envelope_json(_write(tmp_path, _v1_envelope_document()))
    out = tmp_path / "enveloppe.xlsx"
    _build_enveloppe_xlsx(out, AvpSources(enveloppe=src), AvpMeta(project_name="MN_BAT"))
    grid = _envelope_sheet(out)
    assert sum(1 for row in grid for c in row if c == "Mur") == 2  # 1 ligne / type
    text = "\n".join(str(c) for row in grid for c in row if c is not None)
    assert "Archicad BQ NetSideArea" in text
    assert "Surface IFC OpenShell" in text
    assert "1200" in text.replace(",", "").replace(".0", "")  # la valeur métier y est


def test_envelope_deliverable_is_not_empty_with_real_legacy_file(tmp_path):
    with pytest.warns(LegacySchemaWarning):
        src = read_envelope_json(str(MN_BAT_LEGACY))
    out = tmp_path / "enveloppe_legacy.xlsx"
    _build_enveloppe_xlsx(out, AvpSources(enveloppe=src), AvpMeta(project_name="MN_BAT"))
    grid = _envelope_sheet(out)
    assert sum(1 for row in grid for c in row if c == "Mur") == 8
