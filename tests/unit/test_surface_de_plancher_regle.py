"""La règle métier de la **Surface de plancher**, et sa vérification.

Le livrable Plancher était bloqué faute de cette règle : le gabarit
``260203 Tatare 0546L AVP - export plancher.xlsx`` totalise sa synthèse sur
**19 des 49 groupes** de dalles, et rien dans la maquette ne disait lesquels.

La recherche a d'abord écarté deux fausses pistes, mesurées :

- le **calque** ne discrimine pas — « Béton 300 » (retenu) et
  « Bois lamellé-collé 80 » (exclu) partagent ``241 - DALLES - Intérieures`` ;
- **aucune propriété catégorielle** ne sépare seule les deux groupes ; une
  recherche exhaustive sur les 92 propriétés observées ne rend que des
  identifiants et des quantités, uniques par dalle, qui « séparent »
  trivialement.

Le critère est une CONJONCTION, et il a un sens métier : une dalle compose un
plancher si elle est un **sol intérieur praticable** — sa face supérieure porte
un revêtement de sol, **et** elle est intérieure. La seconde condition n'est pas
redondante : elle écarte les dalles extérieures, dont le revêtement (dalette,
pierre) en ferait sinon des planchers.

Ce n'est pas une liste des 19 groupes : c'est un critère, et le gabarit sert à
le vérifier — pas à le produire.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from audit_bim.extraction.model_data import ModelSnapshot
from audit_bim.reporting.avp_report_catalog import REPORT_SPECS_BY_KEY
from audit_bim.reporting.avp_snapshot import (
    _contribue_a_la_surface_de_plancher,
    build_plancher_from_snapshot,
)

#: Mesuré sur le gabarit. La spécification vit ICI ; le classeur ne sert qu'à la
#: confronter quand il est présent (poste AMO, non versionné).
_GABARIT_GROUPES_DETAIL = 49
_GABARIT_GROUPES_SYNTHESE = 19
_GABARIT_SURFACE_DE_PLANCHER = 3355.76

_SNAPSHOT_REEL = Path("out/.audit_cache/snapshot_e47fc988e6862cfd.json.gz")


def _dalle(
    uuid: str,
    composite: str,
    revetement: str,
    aire: float,
    *,
    calque: str | None = None,
    materiau: str | None = None,
):
    """Dalle à la forme réelle : composite et revêtement en propriétés ArchiCAD.

    ``materiau`` sert quand il n'y a pas de composite : sans lui, deux dalles de
    natures différentes porteraient le même type et fusionneraient en un seul
    groupe — le test passerait alors sur un inventaire faux.
    """
    el = {
        "uuid": uuid,
        "type": "IfcSlab",
        "name": "Dalle",
        "object_type": None,
        "property_sets": [
            {
                "name": "BaseQuantities",
                "properties": [
                    {"definition": {"name": "NetArea"}, "value": aire},
                    {"definition": {"name": "Width"}, "value": 0.3},
                ],
            },
            {
                "name": "ArchiCADProperties",
                "properties": [
                    {
                        "definition": {
                            "name": "Matériau de construction / Composite / Profil / Hachure"
                        },
                        "value": composite or materiau or "Béton",
                    },
                    {"definition": {"name": "Structure Composite"}, "value": composite},
                    {"definition": {"name": "Surface supérieure"}, "value": revetement},
                ],
            },
        ],
    }
    if calque:
        el["layers"] = [{"name": calque}]
    return el


# ── Le critère, cas par cas ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("libelle", "composite", "revetement", "calque", "attendu"),
    [
        # Retenus — sol intérieur praticable.
        (
            "complexe intérieur carrelé",
            "DI 20+6+5+2 : … Carrelage",
            "Carrelage - Blanc mat",
            None,
            True,
        ),
        ("complexe intérieur parquet", "DI 2+1.5+5+2 OSB …", "Sol - Parquet 11", None, True),
        ("complexe intérieur sol souple", "DI 2+1.5 …", "Plastique - Laminé", None, True),
        (
            "dalle béton nue intérieure",
            None,
            "Béton - Lissé",
            "241 - DALLES - Intérieures.Exndo",
            True,
        ),
        # Écartés — pas de revêtement de sol.
        (
            "structure bois nue",
            None,
            "Bois - Lamellé-collé",
            "241 - DALLES - Intérieures.Exndo",
            False,
        ),
        (
            "faux plafond",
            "FP 2 : BA13",
            "Plâtre - Plaques de plâtre",
            "352 - FINITIONS - Faux plafonds.Exndo",
            False,
        ),
        ("toiture zinc", None, "Métal - Zinc", "262 - TOITURE - Pente.Exndo", False),
        (
            "complexe de plafond",
            "DI 1.5+1.5+325 BA13 + isolant",
            "Isolation - Laine de verre/roche",
            None,
            False,
        ),
        # Écarté — revêtement de sol MAIS extérieur : c'est la seconde condition
        # qui travaille, et elle n'est donc pas redondante.
        ("terrasse extérieure", "DE 10+23+2+9+2 : … Dalette", "Pierre - Calcaire fin", None, False),
        (
            "dalle béton extérieure",
            None,
            "Béton - Lissé",
            "242 - DALLES - Extérieures.Exndo",
            False,
        ),
    ],
)
def test_le_critere_tranche_cas_par_cas(libelle, composite, revetement, calque, attendu):
    el = _dalle("X", composite, revetement, 10.0, calque=calque)
    assert _contribue_a_la_surface_de_plancher(el) is attendu, libelle


def test_la_seconde_condition_nest_pas_redondante():
    """Non-vacuité : même revêtement de sol, seul l'intérieur/extérieur change."""
    interieure = _dalle("A", None, "Béton - Lissé", 10.0, calque="241 - DALLES - Intérieures.Exndo")
    exterieure = _dalle("B", None, "Béton - Lissé", 10.0, calque="242 - DALLES - Extérieures.Exndo")
    assert _contribue_a_la_surface_de_plancher(interieure)
    assert not _contribue_a_la_surface_de_plancher(exterieure)


# ── La synthèse ne totalise que le plancher ────────────────────────────────


def _snapshot_mixte() -> ModelSnapshot:
    return ModelSnapshot(
        project={"name": "P"},
        model={"name": "M.ifc"},
        elements=[
            _dalle("SL1", "DI 20+6+5+2 : … Carrelage", "Carrelage - Blanc mat", 100.0),
            _dalle(
                "SL2",
                None,
                "Béton - Lissé",
                200.0,
                calque="241 - DALLES - Intérieures.Exndo",
                materiau="Béton",
            ),
            _dalle(
                "SL3",
                None,
                "Métal - Zinc",
                50.0,
                calque="262 - TOITURE - Pente.Exndo",
                materiau="Métal - Zinc",
            ),
            _dalle("SL4", "FP 2 : BA13", "Plâtre - Plaques de plâtre", 30.0),
        ],
    ).index()


def _grilles(snap):
    return {g.title: g.rows for g in build_plancher_from_snapshot(snap).grids}


def test_le_detail_inventorie_tout_et_la_synthese_filtre():
    g = _grilles(_snapshot_mixte())
    detail = [r for r in g["TDB 2022 xx.2 - Dalles Ok"][1:] if r[1]]
    synthese = [r for r in g["Planchers"][1:] if r[1]]
    assert len(detail) == 4, "le détail reste l'inventaire complet — c'est une donnée d'audit"
    assert len(synthese) == 2, f"la synthèse doit filtrer : {[r[1] for r in synthese]}"
    assert sum(r[3] for r in synthese) == pytest.approx(300.0)


def test_le_bloc_de_total_porte_le_libelle_du_gabarit():
    lignes = _grilles(_snapshot_mixte())["Planchers"]
    plat = [str(c) for r in lignes for c in r if c]
    assert any("Surface de plancher" in c for c in plat)
    assert any(c.startswith("=SUM(D") for c in plat)
    assert any(c.startswith("=SUM(E") for c in plat)


def test_le_ratio_nest_pas_ecrit_sur_des_sommes_incomparables():
    """``SUM`` ignore les cellules vides : sans les deux valeurs sur CHAQUE
    ligne, le ratio comparerait une somme partielle à une somme complète."""
    lignes = _grilles(_snapshot_mixte())["Planchers"]
    ratios = [
        str(c) for r in lignes for c in r if isinstance(c, str) and c.startswith("=E") and "/D" in c
    ]
    assert not ratios, f"ratio écrit alors qu'aucune colonne calculée n'est remplie : {ratios}"


# ── Le catalogue est débloqué, et le refus d'approximation ─────────────────


def test_le_rapport_nest_plus_bloque():
    assert REPORT_SPECS_BY_KEY["plancher"].blocked_reason is None


# ── Confrontation au gabarit, quand la maquette réelle est là ──────────────


@pytest.mark.skipif(
    not _SNAPSHOT_REEL.exists(),
    reason="snapshot de la maquette réelle absent (poste AMO, non versionné)",
)
def test_la_regle_retrouve_exactement_les_19_groupes_du_gabarit():
    """Ni 20 ni 49 : exactement 19, et le total au centième.

    C'est la contre-épreuve des deux approximations refusées — la règle
    réglementaire « dalles intérieures » donnait 20 groupes, et l'inventaire
    complet 49.
    """
    payload = json.loads(gzip.open(_SNAPSHOT_REEL).read())
    snap = ModelSnapshot(**{k: v for k, v in payload.items() if not k.startswith("_")}).index()
    g = _grilles(snap)

    detail = [r for r in g["TDB 2022 xx.2 - Dalles Ok"][1:] if r[1]]
    synthese = [r for r in g["Planchers"][1:] if r[1]]
    assert len(detail) == _GABARIT_GROUPES_DETAIL
    assert len(synthese) == _GABARIT_GROUPES_SYNTHESE, (
        f"{len(synthese)} groupes : ni 20 (règle réglementaire) ni 49 (inventaire) "
        "ne sont acceptables"
    )
    total = sum(r[3] or 0 for r in synthese)
    assert total == pytest.approx(_GABARIT_SURFACE_DE_PLANCHER, abs=0.02), (
        f"surface de plancher {total:.2f} contre {_GABARIT_SURFACE_DE_PLANCHER} au gabarit"
    )
