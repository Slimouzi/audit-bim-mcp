"""Le livrable **Plancher** est bloqué par une règle métier absente.

Nous savons extraire les dalles, les typer et les étager correctement : #212 a
retrouvé les **49 groupes** du gabarit à l'identique — mêmes clés (Type, Étage),
0 surface divergente, somme identique au centième. Ce n'est donc pas un problème
de données.

Ce que nous ne savons pas produire, c'est la **Surface de plancher**. Le gabarit
la totalise sur **19 des 49 groupes** — une sélection métier des types qui
constituent le plancher, hors toitures zinc, faux plafonds BA13 et dalles
extérieures. Mesuré : le calque ne la discrimine pas (« Béton 300 », retenu, et
« Bois lamellé-collé 80 », exclu, partagent ``241 - DALLES - Intérieures``), et
la règle réglementaire la plus défendable donne 20 groupes, pas 19.

Un quasi-livrable serait pire qu'un refus clair : il se lirait comme un contrôle
alors que son total serait faux. Tant que la règle n'est pas définie, le rapport
est **blocked**, et rien n'est écrit.

Ce fichier fige les quatre conséquences : l'annonce, le refus avant écriture, la
génération des autres exports, et la survie du détail comme donnée d'audit.
"""

from __future__ import annotations

import pytest

from audit_bim.extraction.model_data import ModelSnapshot
from audit_bim.reporting.avp_i3f import write_avp_i3f_report_pack
from audit_bim.reporting.avp_report_catalog import REPORT_SPECS_BY_KEY
from audit_bim.reporting.avp_snapshot import build_plancher_from_snapshot

_CLE = "plancher"


def _dalle(uuid: str, composite: str, epaisseur: float, aire: float) -> dict:
    return {
        "uuid": uuid,
        "type": "IfcSlab",
        "name": "Dalle",
        "object_type": None,
        "property_sets": [
            {
                "name": "BaseQuantities",
                "properties": [
                    {"definition": {"name": "NetArea"}, "value": aire},
                    {"definition": {"name": "Width"}, "value": epaisseur},
                ],
            },
            {
                "name": "ArchiCADProperties",
                "properties": [
                    {
                        "definition": {
                            "name": "Matériau de construction / Composite / Profil / Hachure"
                        },
                        "value": composite,
                    }
                ],
            },
        ],
    }


def _snapshot() -> ModelSnapshot:
    """Une maquette dont les dalles sont COMPLÈTES : le blocage ne peut donc
    pas venir d'un manque de données."""
    espace = {
        "uuid": "SP1",
        "type": "IfcSpace",
        "name": "S1",
        "longname": "CHAMBRE",
        "property_sets": [
            {
                "name": "BaseQuantities",
                "properties": [{"definition": {"name": "NetFloorArea"}, "value": 12.0}],
            }
        ],
    }
    zone = {
        "uuid": "Z1",
        "type": "IfcZone",
        "name": "LGT-01",
        "attributes": {
            "properties": [{"definition": {"name": "ObjectType"}, "value": "Zone Logement T2"}]
        },
    }
    return ModelSnapshot(
        project={"name": "P"},
        model={"name": "M.ifc"},
        spaces=[{"uuid": "SP1", "name": "S1"}],
        zones=[{"uuid": "Z1", "name": "LGT-01", "spaces": [{"uuid": "SP1"}]}],
        elements=[
            _dalle("SL1", "Béton", 0.30, 224.03),
            _dalle("SL2", "Bois lamellé-collé", 0.08, 3.74),
            espace,
            zone,
        ],
    ).index()


# ── 1. L'annonce ───────────────────────────────────────────────────────────


def test_le_catalogue_declare_le_blocage_et_son_motif():
    spec = REPORT_SPECS_BY_KEY[_CLE]
    assert spec.blocked_reason, "le blocage doit être déclaré, pas implicite"
    assert "règle métier" in spec.blocked_reason
    assert "19" in spec.blocked_reason and "49" in spec.blocked_reason


def test_la_disponibilite_annonce_blocked_sans_masquer_les_donnees():
    """Non-vacuité : ``available_data`` est **non vide**. Si le blocage venait
    d'un manque de données, ce test passerait pour la mauvaise raison."""
    from audit_bim.reporting.avp_availability import inspect_avp_report_availability

    av = {a.key: a for a in inspect_avp_report_availability(_snapshot())}[_CLE]
    assert av.can_generate is False
    assert av.status == "blocked"
    assert av.available_data, "les dalles sont présentes : le motif n'est pas la donnée"
    assert "compléter la maquette" not in av.next_action


# ── 2. Le refus, AVANT écriture ────────────────────────────────────────────


def test_demander_plancher_nommement_refuse_avant_toute_ecriture(tmp_path, monkeypatch):
    """Nommer un rapport bloqué mérite un refus explicite, pas une omission
    silencieuse — et le refus doit précéder la création du moindre fichier."""
    from audit_bim.mcp.session import _Session, current_session
    from audit_bim.profiles.i3f import tools_reporting

    monkeypatch.setenv("AUDIT_OUTPUT_DIR", str(tmp_path))
    sess = _Session()
    sess.snapshot = _snapshot()
    jeton = current_session.set(sess)
    try:
        sortie = tmp_path / "pack"
        res = tools_reporting.generate_avp_i3f_pack(
            output_dir=str(sortie),
            reports=[_CLE],
            project_name="P",
            project_code="C",
            phase="AVP",
            auditor_name="S",
            export_pdf=False,
        )
    finally:
        current_session.reset(jeton)

    assert res["status"] == "error"
    assert res["error"] == "report_blocked"
    assert _CLE in res["blocked_reports"]
    assert "règle métier" in res["blocked_reports"][_CLE]
    assert not sortie.exists(), "un dossier a été créé malgré le refus"


# ── 3. Les autres exports restent générables ───────────────────────────────


def test_un_pack_sans_plancher_reste_generable(tmp_path):
    """Le blocage retire UN rapport, il ne met pas le pack à l'arrêt."""
    pack = write_avp_i3f_report_pack(None, tmp_path / "out", snapshot=_snapshot(), export_pdf=False)
    assert pack.plancher_xlsx is None
    assert not list((tmp_path / "out").glob("*plancher*")), "un fichier plancher a été écrit"
    for chemin in pack.paths():
        assert chemin.exists() and chemin.stat().st_size > 0
    # Non-vacuité : les annexes qui dépendent des mêmes espaces sortent bien.
    assert pack.shab_xlsx.exists() and pack.zones_espaces_xlsx.exists()


def test_le_pack_trace_ce_quil_ecarte(tmp_path, monkeypatch):
    """« Filtrer, c'est tracer » : une annexe absente sans explication se lit
    comme un oubli."""
    from audit_bim.mcp.session import _Session, current_session
    from audit_bim.profiles.i3f import tools_reporting

    monkeypatch.setenv("AUDIT_OUTPUT_DIR", str(tmp_path))
    sess = _Session()
    sess.snapshot = _snapshot()
    jeton = current_session.set(sess)
    try:
        res = tools_reporting.generate_avp_i3f_pack(
            output_dir=str(tmp_path / "pack"),
            project_name="P",
            project_code="C",
            phase="AVP",
            auditor_name="S",
            export_pdf=False,
        )
    finally:
        current_session.reset(jeton)

    assert res.get("status") != "error", res
    assert _CLE in res["blocked_reports"]
    assert not any("plancher" in p.lower() for p in res["paths"])


# ── 4. Le détail reste une donnée d'audit ──────────────────────────────────


def test_les_groupes_de_dalles_restent_calculables():
    """Ce qui est bloqué est la LIVRAISON, pas l'extraction. Le travail de #212
    — composite, étage, provenance — reste disponible pour un audit."""
    src = build_plancher_from_snapshot(_snapshot())
    assert src is not None
    detail = next(g for g in src.grids if g.title.startswith("TDB"))
    types = {r[1] for r in detail.rows[1:]}
    assert types == {"Béton 300", "Bois lamellé-collé 80"}


def test_le_blocage_nest_pas_un_effacement_du_catalogue():
    """La forme du livrable reste décrite : le jour où la règle existe, la
    cible ne se réinvente pas."""
    spec = REPORT_SPECS_BY_KEY[_CLE]
    assert spec.expected_sheets and spec.headers and spec.critical_formulas
    assert "Surface IFC OpenShell" in spec.headers


# ── 5. Le blocage ne s'étend pas aux autres rapports ───────────────────────


@pytest.mark.parametrize(
    "cle",
    ["controle_maquettes", "shab_maquette", "zones_espaces", "surface_enveloppe", "menuiseries"],
)
def test_aucun_autre_rapport_nest_bloque(cle):
    """Contre-épreuve du champ : s'il bloquait tout, les tests ci-dessus
    passeraient sans rien prouver."""
    assert REPORT_SPECS_BY_KEY[cle].blocked_reason is None


# ── 6. ``reports`` est une VRAIE sélection ─────────────────────────────────


def _generer(tmp_path, monkeypatch, **kw):
    from audit_bim.mcp.session import _Session, current_session
    from audit_bim.profiles.i3f import tools_reporting

    monkeypatch.setenv("AUDIT_OUTPUT_DIR", str(tmp_path))
    sess = _Session()
    sess.snapshot = _snapshot()
    jeton = current_session.set(sess)
    try:
        return tools_reporting.generate_avp_i3f_pack(
            output_dir=str(tmp_path / "pack"),
            project_name="P",
            project_code="C",
            phase="AVP",
            auditor_name="S",
            export_pdf=False,
            **kw,
        )
    finally:
        current_session.reset(jeton)


def test_reports_ne_produit_que_les_rapports_demandes(tmp_path, monkeypatch):
    """Un paramètre qui promet une sélection doit sélectionner.

    Il ne faisait qu'une intersection avec les rapports bloqués : demander
    ``shab_maquette`` produisait quand même tout le pack — une API publique qui
    écrit plus que demandé.
    """
    res = _generer(tmp_path, monkeypatch, reports=["shab_maquette"])
    assert res.get("status") != "error", res
    noms = [p.rsplit("/", 1)[-1] for p in res["paths"]]
    assert any("export SHAB maquette" in n for n in noms), noms
    # Le .docx consolidé reste produit : c'est lui qui porte l'analyse.
    assert any(n.endswith(".docx") for n in noms), noms
    # …et AUCUNE autre annexe.
    for absent in ("Contrôle Maquettes", "Export Zones et Espaces", "surface enveloppe"):
        assert not any(absent in n for n in noms), f"{absent} écrit sans être demandé : {noms}"


def test_reports_refuse_une_cle_inconnue_avant_ecriture(tmp_path, monkeypatch):
    """Une faute de frappe qui produirait silencieusement autre chose que ce
    qui est demandé est pire qu'une erreur."""
    res = _generer(tmp_path, monkeypatch, reports=["typo"])
    assert res["status"] == "error"
    assert res["error"] == "unknown_report"
    assert res["unknown_reports"] == ["typo"]
    assert "plancher" in res["known_reports"]
    assert not (tmp_path / "pack").exists(), "un dossier a été créé malgré le refus"


def test_reports_refuse_une_cle_bloquee_meme_accompagnee(tmp_path, monkeypatch):
    """Un rapport bloqué contamine toute la demande : on ne produit pas
    « la partie faisable » en silence."""
    res = _generer(tmp_path, monkeypatch, reports=["shab_maquette", _CLE])
    assert res["status"] == "error"
    assert res["error"] == "report_blocked"
    assert list(res["blocked_reports"]) == [_CLE]
    assert not (tmp_path / "pack").exists()


def test_reports_none_produit_tout_ce_qui_est_produisible(tmp_path, monkeypatch):
    """Contre-épreuve : sans sélection, la garde ne doit rien retirer d'autre
    que les rapports bloqués."""
    res = _generer(tmp_path, monkeypatch)
    assert res.get("status") != "error", res
    noms = [p.rsplit("/", 1)[-1] for p in res["paths"]]
    for attendu in (
        "Contrôle Maquettes",
        "export SHAB maquette",
        "Export Zones et Espaces",
        "surface enveloppe",
        "export Menuiseries",
    ):
        assert any(attendu in n for n in noms), f"{attendu} manquant : {noms}"
    assert not any("plancher" in n.lower() for n in noms)


def test_deux_motifs_a_la_fois_sont_tous_deux_rendus_au_client(tmp_path, monkeypatch):
    """Le cœur cumulait déjà les deux motifs ; la traduction MCP sortait sur le
    premier et perdait le second.

    L'utilisateur corrigeait donc sa typo pour découvrir ensuite qu'un rapport
    est bloqué — l'aller-retour que l'exception interne évitait déjà. Le code
    devient générique quand il y a deux causes : nommer l'une masquerait l'autre.
    """
    res = _generer(tmp_path, monkeypatch, reports=["typo", _CLE])

    assert res["status"] == "error"
    assert res["error"] == "invalid_report_selection"
    assert res["unknown_reports"] == ["typo"]
    assert _CLE in res["known_reports"]
    assert _CLE in res["blocked_reports"]
    assert "règle métier" in res["blocked_reports"][_CLE]
    # Le message porte les deux, pas seulement le premier rencontré.
    assert "typo" in res["message"] and _CLE in res["message"]
    assert not (tmp_path / "pack").exists()


def test_un_seul_motif_garde_son_code_specifique(tmp_path, monkeypatch):
    """Contre-épreuve : le code générique ne doit pas avaler les cas simples,
    qui portent l'information la plus utile."""
    inconnue = _generer(tmp_path, monkeypatch, reports=["typo"])
    assert inconnue["error"] == "unknown_report"
    assert "blocked_reports" not in inconnue

    bloque = _generer(tmp_path, monkeypatch, reports=[_CLE])
    assert bloque["error"] == "report_blocked"
    assert "unknown_reports" not in bloque
