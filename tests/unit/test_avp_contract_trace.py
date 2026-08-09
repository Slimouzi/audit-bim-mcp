"""La trace de contrat dit ce qui a été utilisé — sans se mêler de l'autoriser.

``AvpContractTrace`` porte ce qu'il faut **dire** d'un contrat dans la réponse
du pack. ``_resolve_contract_source`` décide s'il est **acceptable**. Les deux
ne s'exécutent pas au même moment : ``coverage`` n'existe qu'après la fusion des
quantités dans le snapshot, bien après la résolution du chemin. Les mélanger
ferait porter au garde de provenance des champs qui n'existent pas encore
quand il agit.

Ces tests fixent deux choses : les **clés publiques** de la réponse, qui sont un
contrat d'API vis-à-vis du harnais, et le fait que les cinq origines réelles
(enveloppe explicite / détectée / calculée, quantités explicites / calculées)
produisent bien une trace.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from audit_bim.profiles.i3f.tools_reporting import AvpContractTrace

MODULE = Path(__file__).resolve().parents[2] / "audit_bim/profiles/i3f/tools_reporting.py"

#: Clés publiques de la réponse de succès. Elles sont lues par le harnais MCP :
#: en renommer une est un changement d'API, pas un détail de refactor.
CLES_PUBLIQUES = {
    "output_dir",
    "paths",
    "analyse_docx",
    "analyse_pdf",
    "pdf_available",
    "project_name",
    "project_code",
    "phase",
    "controle_xlsx_used",
    "envelope_json_used",
    "computed_quantities_json_used",
    "computed_quantities_coverage",
    # Ajoutée délibérément : un pack qui écarte un rapport doit dire lequel et
    # pourquoi. Une annexe absente sans explication se lit comme un oubli.
    "blocked_reports",
    "active_cloud_id",
    "active_project_id",
    "active_model_id",
    "downloaded_ifc_path",
    "computed_source_ifc_file",
    "envelope_source_ifc_file",
    "auto_computed",
}


def _cles_du_formatter() -> set[str]:
    fn = next(
        n
        for n in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8")))
        if isinstance(n, ast.FunctionDef) and n.name == "_format_avp_pack_response"
    )
    ret = next(x for x in ast.walk(fn) if isinstance(x, ast.Return))
    return {k.value for k in ret.value.keys if isinstance(k, ast.Constant)}


def test_the_public_response_keys_are_unchanged():
    """Le refactor déplace des paramètres, jamais des clés de réponse."""
    assert _cles_du_formatter() == CLES_PUBLIQUES


def test_the_formatter_takes_traces_not_flat_fields():
    """Non-vacuité : les sept champs plats ne doivent plus être des paramètres."""
    fn = next(
        n
        for n in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8")))
        if isinstance(n, ast.FunctionDef) and n.name == "_format_avp_pack_response"
    )
    params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}

    assert {"envelope_trace", "computed_trace"} <= params
    disparus = {
        "envelope_json_used",
        "envelope_source_ifc_file",
        "auto_envelope",
        "computed_json_used",
        "computed_source_ifc_file",
        "computed_coverage",
        "auto_quantities",
    }
    assert not (disparus & params), sorted(disparus & params)


@pytest.mark.parametrize(
    ("origine", "trace"),
    [
        # Enveloppe — trois origines, jamais de coverage.
        ("enveloppe explicite", AvpContractTrace(json_used="/in/e.json", source_ifc_file="M.ifc")),
        (
            "enveloppe détectée",
            AvpContractTrace(json_used="/in/auto_e.json", source_ifc_file="M.ifc"),
        ),
        (
            "enveloppe calculée",
            AvpContractTrace(
                json_used="/out/e.json",
                source_ifc_file="M.ifc",
                auto_result={"json_path": "/out/e.json"},
            ),
        ),
        # Quantités — deux origines, coverage seulement après fusion.
        (
            "quantités explicites",
            AvpContractTrace(
                json_used="/in/q.json", source_ifc_file="M.ifc", coverage={"slabs": 12}
            ),
        ),
        (
            "quantités calculées",
            AvpContractTrace(
                json_used="/out/q.json",
                source_ifc_file="M.ifc",
                auto_result={"json_path": "/out/q.json"},
                coverage={"slabs": 12},
            ),
        ),
    ],
)
def test_every_real_origin_produces_a_usable_trace(origine, trace):
    """Les cinq origines réelles se décrivent avec un seul type."""
    assert trace.json_used
    assert trace.source_ifc_file == "M.ifc"


def test_the_envelope_trace_carries_no_coverage_by_default():
    """L'asymétrie est portée par un défaut, pas par deux types.

    L'enveloppe ne produit pas de couverture ; les quantités si. Un second type
    pour cette seule différence aurait dupliqué trois champs sur quatre.
    """
    assert AvpContractTrace().coverage is None
    assert AvpContractTrace(coverage={"slabs": 1}).coverage == {"slabs": 1}


def test_an_absent_contract_is_an_empty_trace_not_a_missing_one():
    """Aucun contrat ⇒ une trace vide, pas un `None` à tester partout.

    Sans ça, le formatter devrait porter des gardes `if trace is not None` sur
    chacun des cinq champs qu'il expose.
    """
    vide = AvpContractTrace()
    assert (vide.json_used, vide.source_ifc_file, vide.auto_result, vide.coverage) == (
        None,
        None,
        None,
        None,
    )


# ---------------------------------------------------------------------------
# Comportement : le tool assemble-t-il vraiment la trace ?
# ---------------------------------------------------------------------------


def test_the_tool_reports_the_envelope_provenance_it_actually_used(tmp_path, monkeypatch):
    """``envelope_source_ifc_file`` doit sortir du contrat RÉELLEMENT lu.

    Les tests ci-dessus construisent une ``AvpContractTrace`` ; ils ne prouvent
    pas que le tool l'assemble. Celui-ci appelle ``generate_avp_i3f_pack`` avec
    une enveloppe explicite portant ``source.ifc_file``, et vérifie la clé
    publique dans la réponse — la seule des cinq clés déplacées vers la trace
    qui n'était couverte par aucun test de comportement.
    """
    import json as _json

    from audit_bim.extraction.model_data import ModelSnapshot
    from audit_bim.mcp.session import _Session, current_session
    from audit_bim.profiles.i3f.tools_reporting import generate_avp_i3f_pack

    monkeypatch.setenv("AUDIT_INPUT_DIR", str(tmp_path))
    monkeypatch.setenv("AUDIT_OUTPUT_DIR", str(tmp_path / "out"))
    (tmp_path / "out").mkdir()

    ifc = "250613_MN_BAT.ifc"
    contrat = tmp_path / "mn_bat_envelope.json"
    contrat.write_text(
        _json.dumps(
            {
                "schema": "envelope_quantities/v1",
                "source": {
                    "producer": "ifc-geometry",
                    "tool": "extract_envelope_surfaces",
                    "version": "0.6.1",
                    "ifc_file": ifc,
                },
                # ``summary`` est requis par le contrat — lu sur le schéma réel,
                # pas deviné : une première version de ce test l'omettait et
                # échouait à la validation avant d'atteindre la clé visée.
                "summary": {"superficie_facades_m2": 2071.18, "shab_m2": 2164.98},
                "par_type": [{"type": "Mur", "net_side_area_m2": 120.0, "n": 4}],
            }
        ),
        encoding="utf-8",
    )

    sess = _Session()
    sess.snapshot = ModelSnapshot(
        project={"name": "Dieppe"},
        model={"name": ifc},
        # Quantités natives présentes : sans elles la QA gate refuse des
        # livrables vides, et le test n'atteindrait jamais la clé visée.
        spaces=[
            {
                "uuid": "SP1",
                "type": "IfcSpace",
                "name": "SEJOUR",
                "longname": "SEJOUR",
                "property_sets": [
                    {
                        "name": "Qto_SpaceBaseQuantities",
                        "properties": [
                            {"definition": {"name": "NetFloorArea"}, "value": 24.5},
                            {"definition": {"name": "Height"}, "value": 2.5},
                        ],
                    }
                ],
            }
        ],
        elements=[
            {
                "uuid": "W1",
                "type": "IfcWall",
                "name": "Mur",
                "layers": [{"name": "221 - MURS - Extérieurs périphériques.Exndo"}],
                "property_sets": [
                    {
                        "name": "Qto_WallBaseQuantities",
                        "properties": [{"definition": {"name": "NetSideArea"}, "value": 120.0}],
                    }
                ],
            }
        ],
    ).index()
    token = current_session.set(sess)
    try:
        res = generate_avp_i3f_pack(
            project_name="Dieppe Chantier",
            project_code="0546L",
            phase="AVP",
            auditor_name="S. Limouzi",
            envelope_json=str(contrat),
            # Le snapshot n'a pas de BaseQuantities : sans ça, le tool tente de
            # les calculer et refuse faute d'``ifc_path``. Ce test porte sur la
            # trace d'ENVELOPPE, pas sur l'auto-calcul des quantités.
            auto_compute_quantities=False,
            export_pdf=False,
        )
    finally:
        current_session.reset(token)

    assert res.get("status") != "needs_context", res
    # La clé visée : elle vient de `source.ifc_file` du contrat lu, pas d'un
    # paramètre ni du nom du fichier.
    assert res["envelope_source_ifc_file"] == ifc, sorted(res)
    assert res["envelope_json_used"] == str(contrat)
