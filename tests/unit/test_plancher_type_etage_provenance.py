"""Livrable **Plancher** : type de composite, étage, et provenance des mesures.

Trois défauts mesurés face à ``260203 Tatare 0546L AVP - export plancher.xlsx``
(onglets ``TDB 2022 xx.2 - Dalles Ok`` et ``Planchers``) :

1. **FAUSSE COMPARAISON** — ``area`` était écrit LITTÉRALEMENT dans
   ``BaseQuantities.NetArea`` (D) *et* dans ``Surface IFC OpenShell`` (E), donc
   l'écart ``E/D-1`` était vide par construction et se lisait comme une
   concordance vérifiée alors que rien ne l'avait été. Même défaut que
   Menuiseries avant #210 et Zones/Espaces avant #211.
2. **TYPE ÉCRASÉ** — la colonne ``Type`` sortait « Dalle » pour tout le monde.
   L'``ObjectType`` IFC est nul sur les 116 dalles de la maquette de recette et
   le ``Name`` vaut « Dalle » pour 97 d'entre elles ; le composite ArchiCAD
   (« Béton 300 », « DI 10+23+6+5+2 : … 460 ») vit dans les propriétés.
3. **ÉTAGE VIDE** — ``_storey(el)`` ne lit que des attributs plats, absents des
   charges utiles réelles. Les 116 dalles portent pourtant toutes leur étage
   dans ``structure_tree``.

Les deux derniers effondraient **49 groupes du gabarit en 5 lignes** : un
livrable qui ne dit plus rien, sans qu'aucune erreur ne soit levée.

Ce que le livrable **ne dit pas**, et le dit : le gabarit porte un total
« Surface de plancher » calculé sur 19 des 49 groupes — une sélection métier des
types qui constituent le plancher, qu'aucune donnée de la maquette ne porte. On
ne la fabrique pas ; la note de méthode l'explique plutôt que de laisser croire
à un oubli.
"""

from __future__ import annotations

import pytest

from audit_bim.extraction.model_data import ModelSnapshot
from audit_bim.reporting.avp_snapshot import build_plancher_from_snapshot

_DETAIL = "TDB 2022 xx.2 - Dalles Ok"
_SYNTHESE = "Planchers"


def _dalle(
    uuid: str,
    composite: str,
    epaisseur: float,
    aire: float,
    *,
    calculee: bool = False,
) -> dict:
    """Dalle à la forme réelle : composite en propriété ArchiCAD, épaisseur en
    BaseQuantities, **aucun** ``ObjectType`` et un ``Name`` constant."""
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
                    {"definition": {"name": "Width"}, "value": epaisseur},
                ],
            },
            {
                "name": "ArchiCADProperties",
                "properties": [
                    {"definition": {"name": "ID de l'élément"}, "value": "Dalle"},
                    {
                        "definition": {
                            "name": "Matériau de construction / Composite / Profil / Hachure"
                        },
                        "value": composite,
                    },
                ],
            },
        ],
    }
    if calculee:
        el["computed_base_quantities"] = [{"quantity": "NetArea", "value": aire}]
    return el


def _snapshot() -> ModelSnapshot:
    """Quatre dalles, deux composites, deux étages — et une seule calculée.

    Les étages ne sont portés QUE par ``structure_tree``, à son orthographe
    réelle (``storey`` / pas de type IFC long) : c'est ce que la production
    reçoit, et c'est ce qui n'était pas lu.
    """
    dalles = [
        _dalle("SL1", "Béton", 0.30, 224.03),
        _dalle("SL2", "Béton", 0.30, 297.18),
        _dalle("SL3", "Bois lamellé-collé", 0.08, 3.74),
        _dalle("SL4", "Béton", 0.30, 100.0, calculee=True),
    ]
    tree = [
        {
            "type": "project",
            "uuid": "P1",
            "name": "P",
            "children": [
                {
                    "type": "storey",
                    "uuid": "ST1",
                    "name": "RDC_A/B",
                    "children": [
                        {"type": "slab", "uuid": "SL1", "name": "Dalle", "children": []},
                        {"type": "slab", "uuid": "SL3", "name": "Dalle", "children": []},
                    ],
                },
                {
                    "type": "storey",
                    "uuid": "ST2",
                    "name": "R+1_C/D",
                    "children": [
                        {"type": "slab", "uuid": "SL2", "name": "Dalle", "children": []},
                        {"type": "slab", "uuid": "SL4", "name": "Dalle", "children": []},
                    ],
                },
            ],
        }
    ]
    return ModelSnapshot(
        project={"name": "P"},
        model={"name": "M.ifc"},
        storeys=[{"uuid": "ST1", "name": "RDC_A/B"}, {"uuid": "ST2", "name": "R+1_C/D"}],
        elements=dalles,
        structure_tree=tree,
    ).index()


def _grilles() -> dict[str, list[list]]:
    return {g.title: g.rows for g in build_plancher_from_snapshot(_snapshot()).grids}


def _col(rows: list[list], nom: str) -> list:
    idx = rows[0].index(nom)
    return [r[idx] for r in rows[1:]]


# ── 1. Une seule colonne de mesure remplie par ligne ────────────────────────


def test_une_seule_colonne_de_mesure_est_remplie_par_ligne():
    rows = _grilles()[_DETAIL]
    natif = _col(rows, "BaseQuantities.NetArea")
    calcule = _col(rows, "Surface IFC OpenShell")
    for d, e in zip(natif, calcule, strict=True):
        assert (d is None) != (e is None), (
            f"D={d} E={e} : les deux colonnes portent une valeur, l'écart ne compare rien"
        )


def test_la_provenance_decide_de_la_colonne():
    rows = _grilles()[_DETAIL]
    mesures = list(
        zip(
            _col(rows, "Type"),
            _col(rows, "Étage"),
            _col(rows, "BaseQuantities.NetArea"),
            _col(rows, "Surface IFC OpenShell"),
            strict=True,
        )
    )
    # SL4 est la seule dalle dont la quantité vient du calcul géométrique :
    # elle est donc la seule surface écrite en colonne « IFC OpenShell ».
    calculees = [(t, e, o) for t, e, d, o in mesures if o is not None]
    assert calculees == [("Béton 300", "R+1_C/D", 100.0)]
    natives = {(t, e): d for t, e, d, o in mesures if d is not None}
    assert natives[("Béton 300", "RDC_A/B")] == 224.03
    assert natives[("Bois lamellé-collé 80", "RDC_A/B")] == 3.74


def test_la_colonne_source_quantite_a_disparu():
    """La provenance se lit à l'emplacement de la valeur : une colonne de plus
    déforme le tableau client, qui compte 7 colonnes au détail."""
    grilles = _grilles()
    assert "Source quantité" not in grilles[_DETAIL][0]
    assert "Source quantité" not in grilles[_SYNTHESE][0]
    assert len(grilles[_DETAIL][0]) == 7
    assert len(grilles[_SYNTHESE][0]) == 8


def test_lecart_est_garde_contre_labsence_de_valeur():
    for formule in _col(_grilles()[_SYNTHESE], "Ecart BaseQuantities / IFC OpenShell"):
        assert formule.startswith("=IF(OR("), (
            f"{formule} : sans garde, une colonne vide donne #DIV/0! sur toute la colonne"
        )
        assert "E" in formule and "/D" in formule, f"{formule} : le gabarit porte un écart relatif"


# ── 2. Le type porte le composite, pas la classe IFC ────────────────────────


def test_le_type_nest_pas_le_nom_constant_de_lelement():
    types = set(_col(_grilles()[_DETAIL], "Type"))
    assert "Dalle" not in types, "le Name IFC vaut « Dalle » pour 97 dalles sur 116"
    assert types == {"Béton 300", "Bois lamellé-collé 80"}


def test_le_type_joint_le_composite_et_lepaisseur_en_millimetres():
    """« Béton » + 0,30 m → « Béton 300 », la forme exacte du gabarit."""
    assert "Béton 300" in set(_col(_grilles()[_DETAIL], "Type"))
    assert "Bois lamellé-collé 80" in set(_col(_grilles()[_DETAIL], "Type"))


# ── 3. L'étage vient de structure_tree ──────────────────────────────────────


def test_letage_est_resolu_depuis_structure_tree():
    """Non-vacuité : aucune dalle ne porte d'attribut d'étage plat, et l'arbre
    n'emploie que les libellés courts. Sans lecture de l'arbre, la colonne sort
    vide — c'est ce qui se produisait sur les 116 dalles réelles."""
    etages = _col(_grilles()[_DETAIL], "Étage")
    assert all(etages), f"colonne Étage non résolue : {etages}"
    assert set(etages) == {"RDC_A/B", "R+1_C/D"}


def test_type_et_etage_separent_les_groupes():
    """Sans eux, les quatre dalles s'effondraient en une seule ligne — c'est ce
    qui ramenait les 49 groupes du gabarit à 5."""
    rows = _grilles()[_DETAIL]
    par_cle = dict(
        zip(
            zip(_col(rows, "Type"), _col(rows, "Étage"), strict=True),
            _col(rows, "Nombre"),
            strict=True,
        )
    )
    assert ("Béton 300", "RDC_A/B") in par_cle
    assert ("Béton 300", "R+1_C/D") in par_cle
    assert ("Bois lamellé-collé 80", "RDC_A/B") in par_cle
    assert sum(_col(rows, "Nombre")) == 4, "les quatre dalles doivent être comptées une fois"


def test_un_groupe_mixte_donne_deux_lignes_de_provenances_distinctes():
    """``SL2`` (native, 297,18) et ``SL4`` (calculée, 100,0) partagent type et
    étage. Les fondre en une ligne ferait basculer 297,18 m² NATIFS dans la
    colonne « Surface IFC OpenShell » : le livrable annoncerait une provenance
    fausse pour la majorité de la surface affichée. La provenance entre donc
    dans la clé de regroupement, et un cas mixte donne DEUX lignes."""
    rows = _grilles()[_DETAIL]
    mixtes = [
        (d, o, n)
        for t, e, d, o, n in zip(
            _col(rows, "Type"),
            _col(rows, "Étage"),
            _col(rows, "BaseQuantities.NetArea"),
            _col(rows, "Surface IFC OpenShell"),
            _col(rows, "Nombre"),
            strict=True,
        )
        if (t, e) == ("Béton 300", "R+1_C/D")
    ]
    assert len(mixtes) == 2, f"groupe mixte fondu en {len(mixtes)} ligne(s)"
    assert sorted(mixtes, key=lambda x: x[0] is None) == [(297.18, None, 1), (None, 100.0, 1)]


# ── 4. Ce que le livrable ne dit pas, il le dit ─────────────────────────────


def test_aucun_total_surface_de_plancher_nest_produit():
    """Le gabarit totalise 19 des 49 groupes sous « Surface de plancher ».
    Totaliser nos 49 dalles sous ce libellé afficherait une surface de plancher
    qui n'en est pas une — la même erreur que le pivot Zones/Espaces."""
    plat = [str(c) for rows in _grilles().values() for r in rows for c in r if c is not None]
    synthese = [str(c) for r in _grilles()[_SYNTHESE] for c in r if c is not None]
    assert not any("SUM(" in c for c in synthese), "un total est écrit sous la synthèse"
    assert any("Surface de plancher" in c for c in plat), (
        "l'absence de ce total doit être expliquée, pas passée sous silence"
    )


def test_la_note_de_methode_chiffre_ce_qui_nest_pas_un_plancher():
    note = _grilles()["Note de méthode"]
    par_cle = {r[0]: r[1] for r in note if len(r) == 2}
    assert par_cle["groupes_de_dalles"] == 4  # le groupe mixte compte deux lignes
    assert par_cle["surface_toutes_dalles_m2"] == pytest.approx(624.95)
    texte = " ".join(str(c) for r in note for c in r)
    assert "n'est pas une surface de plancher" in texte
    assert "arbitrage métier" in texte
