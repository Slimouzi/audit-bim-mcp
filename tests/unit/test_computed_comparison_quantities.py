"""La valeur calculée IFC OpenShell est **conservée à côté** de la native.

*Gap-only* décide quelle valeur fait **autorité**, pas laquelle mérite d'être
retenue. Jusqu'ici la fusion jetait la valeur calculée dès qu'une native
existait — un ``continue`` avant d'enregistrer quoi que ce soit. Conséquence
mesurée sur ``250613_MN_BAT (2).ifc`` : les 116 dalles portant toutes leur
``NetArea`` native, **aucune** valeur IFC OpenShell ne survivait, et les
colonnes de comparaison des livrables sortaient vides *par construction* — les
colonnes d'écart avec elles.

Deux traces distinctes, à ne pas confondre :

- ``computed_base_quantities`` — les quantités **effectivement fusionnées**.
  Répond à « cette BaseQuantity du pset vient-elle d'un calcul ? » et décide
  dans quelle colonne un livrable écrit sa valeur ;
- ``computed_comparison_quantities`` — **toutes** les quantités calculées,
  fusionnées ou non. Répond à « que vaut le calcul pour cet élément ? ».

Élargir la première aurait fait passer pour calculées des quantités natives, et
cassé les provenances livrées en #210 / #211 / #212.

Les quatre cas de la matrice sont figés ici.
"""

from __future__ import annotations

import pytest

from audit_bim.extraction.computed_quantities import merge_into_snapshot
from audit_bim.extraction.model_data import ModelSnapshot
from audit_bim.reporting.avp_snapshot import build_shab_from_snapshot

_DETAIL = "TDB 2022 01.3 - Export Zones..."


def _espace(uuid: str, nom: str, native: float | None) -> dict:
    psets = []
    if native is not None:
        psets.append(
            {
                "name": "BaseQuantities",
                "properties": [{"definition": {"name": "NetFloorArea"}, "value": native}],
            }
        )
    return {
        "uuid": uuid,
        "type": "IfcSpace",
        "name": uuid,
        "longname": nom,
        "object_type": None,
        "property_sets": psets,
    }


def _zone(membres: list[str]) -> dict:
    return {"uuid": "Z1", "name": "LGT-01", "spaces": [{"uuid": u} for u in membres]}


def _zone_element() -> dict:
    return {
        "uuid": "Z1",
        "type": "IfcZone",
        "name": "LGT-01",
        "attributes": {
            "properties": [{"definition": {"name": "ObjectType"}, "value": "Zone Logement T3"}]
        },
    }


def _contrat(quantites: dict[str, float]) -> dict:
    return {
        "schema": "computed_base_quantities/v1",
        "quantities": [
            {
                "global_id": gid,
                "ifc_class": "IfcSpace",
                "qto": "Qto_SpaceBaseQuantities",
                "quantity": "NetFloorArea",
                "value": valeur,
                "unit": "m2",
                "method": "ifcopenshell_geometry",
                "status": "computed",
                "source": "computed_ifcopenshell",
            }
            for gid, valeur in quantites.items()
        ],
    }


def _snapshot(espaces: list[dict]) -> ModelSnapshot:
    return ModelSnapshot(
        project={"name": "P"},
        model={"name": "M.ifc"},
        spaces=[{"uuid": e["uuid"], "name": e["name"]} for e in espaces],
        zones=[_zone([e["uuid"] for e in espaces])],
        elements=[*espaces, _zone_element()],
    ).index()


def _colonnes(snap: ModelSnapshot) -> dict[str, tuple]:
    """``libellé de pièce → (surface native, surface IFC OpenShell)``."""
    ms, _total = build_shab_from_snapshot(snap)
    rows = next(g.rows for g in ms.grids if g.title.startswith("TDB 2022 01.3"))
    entetes = rows[0]
    i_piece = entetes.index("Pièce")
    i_natif = entetes.index("Surface Nette (Qté de Base)")
    i_calc = entetes.index("Surface IFC OpenShell")
    return {r[i_piece]: (r[i_natif], r[i_calc]) for r in rows[1:] if r[i_piece]}


# ── La trace de fusion reste ce qu'elle était ──────────────────────────────


def test_la_valeur_native_reste_dans_le_pset():
    """*Gap-only* inchangé : la native n'est jamais remplacée."""
    snap = _snapshot([_espace("SP1", "CHAMBRE", 99.9)])
    couverture = merge_into_snapshot(snap, _contrat({"SP1": 24.5}))
    assert couverture["n_gap_kept"] == 1 and couverture["n_merged"] == 0
    el = snap.element_by_uuid["SP1"]
    valeurs = [
        p["value"]
        for pset in el["property_sets"]
        for p in pset["properties"]
        if p["definition"]["name"] == "NetFloorArea"
    ]
    assert valeurs == [99.9], "la valeur calculée s'est glissée dans le pset natif"


def test_la_trace_de_fusion_ne_seelargit_pas_aux_valeurs_ecartees():
    """``computed_base_quantities`` ne doit lister que ce qui a été fusionné :
    l'élargir ferait passer pour calculée une BaseQuantity native, et casserait
    la provenance des livrables Fenêtres / Zones/Espaces / Plancher."""
    snap = _snapshot([_espace("SP1", "CHAMBRE", 99.9)])
    merge_into_snapshot(snap, _contrat({"SP1": 24.5}))
    el = snap.element_by_uuid["SP1"]
    assert el.get("computed_base_quantities") in (None, [])
    assert [c["value"] for c in el["computed_comparison_quantities"]] == [24.5]


# ── Les quatre cas de la matrice ───────────────────────────────────────────


def test_native_et_calcul_presents_donnent_deux_valeurs_visibles():
    """Le cas qui n'existait pas : une vraie comparaison."""
    snap = _snapshot([_espace("SP1", "CHAMBRE", 99.9)])
    merge_into_snapshot(snap, _contrat({"SP1": 24.5}))
    assert _colonnes(snap)["CHAMBRE"] == (99.9, 24.5)


def test_native_seule_laisse_la_colonne_ifc_openshell_vide():
    snap = _snapshot([_espace("SP1", "CHAMBRE", 99.9)])
    merge_into_snapshot(snap, _contrat({}))
    assert _colonnes(snap)["CHAMBRE"] == (99.9, None)


def test_calcul_seul_remplit_la_colonne_ifc_openshell_et_pas_la_native():
    """*Gap-fill* : la valeur entre dans le pset, mais elle reste calculée —
    l'écrire en colonne native la ferait passer pour une donnée maquette."""
    snap = _snapshot([_espace("SP1", "CHAMBRE", None)])
    couverture = merge_into_snapshot(snap, _contrat({"SP1": 24.5}))
    assert couverture["n_merged"] == 1
    assert _colonnes(snap)["CHAMBRE"] == (None, 24.5)


def test_aucune_des_deux_laisse_les_deux_colonnes_vides():
    snap = _snapshot([_espace("SP1", "CHAMBRE", None)])
    merge_into_snapshot(snap, _contrat({}))
    assert _colonnes(snap)["CHAMBRE"] == (None, None)


# ── Non-vacuité : les cas coexistent dans un même classeur ─────────────────


def test_les_quatre_cas_coexistent_sans_se_contaminer():
    """Un livrable réel mélange les provenances. Chaque ligne doit porter la
    sienne — c'est ce qu'un booléen unique par groupe ne savait pas faire."""
    snap = _snapshot(
        [
            _espace("SP1", "DEUX", 99.9),
            _espace("SP2", "NATIVE", 50.0),
            _espace("SP3", "CALCUL", None),
            _espace("SP4", "RIEN", None),
        ]
    )
    merge_into_snapshot(snap, _contrat({"SP1": 24.5, "SP3": 12.0}))
    colonnes = _colonnes(snap)
    assert colonnes["DEUX"] == (99.9, 24.5)
    assert colonnes["NATIVE"] == (50.0, None)
    assert colonnes["CALCUL"] == (None, 12.0)
    assert colonnes["RIEN"] == (None, None)


def test_lecart_devient_calculable_quand_les_deux_valeurs_existent():
    """L'écart n'était pas seulement vide : il ne POUVAIT pas être rempli. La
    formule reste gardée, mais sa garde cesse enfin de se déclencher."""
    snap = _snapshot([_espace("SP1", "CHAMBRE", 100.0)])
    merge_into_snapshot(snap, _contrat({"SP1": 110.0}))
    natif, calcule = _colonnes(snap)["CHAMBRE"]
    assert natif is not None and calcule is not None
    assert calcule / natif - 1 == pytest.approx(0.10)


# ── Menuiseries : une surface ne se cherche que parmi des surfaces ─────────


def _fenetre(uuid: str, ot: str, *, largeur=None, hauteur=None) -> dict:
    """Fenêtre SANS aucune BaseQuantity native : tout viendra du calcul."""
    return {
        "uuid": uuid,
        "type": "IfcWindow",
        "name": ot,
        "object_type": ot,
        "property_sets": [],
        **(
            {}
            if largeur is None
            else {
                "property_sets": [
                    {
                        "name": "BaseQuantities",
                        "properties": [
                            {"definition": {"name": "Width"}, "value": largeur},
                            {"definition": {"name": "Height"}, "value": hauteur},
                        ],
                    }
                ]
            }
        ),
    }


def _contrat_menuiserie(mesures: dict[str, tuple[float, float]]) -> dict:
    return {
        "schema": "computed_base_quantities/v1",
        "quantities": [
            {
                "global_id": gid,
                "ifc_class": "IfcWindow",
                "qto": "Qto_WindowBaseQuantities",
                "quantity": nom,
                "value": valeur,
                "unit": "m",
                "method": "ifcopenshell_geometry",
                "status": "computed",
                "source": "computed_ifcopenshell",
            }
            for gid, (larg, haut) in mesures.items()
            for nom, valeur in (("Width", larg), ("Height", haut))
        ],
    }


def _table_menuiseries(elements: list[dict], contrat: dict):
    from audit_bim.reporting.avp_snapshot import build_menuiseries_from_snapshot

    snap = ModelSnapshot(project={"name": "P"}, model={"name": "M.ifc"}, elements=elements).index()
    merge_into_snapshot(snap, contrat)
    src, _total = build_menuiseries_from_snapshot(snap)
    return src.table


def test_la_surface_calculee_nest_jamais_une_largeur():
    """``_comparison_quantity`` rend le **premier nom trouvé**. Chercher la
    surface dans une liste commençant par ``Width`` faisait sortir la largeur
    dans la colonne « Surface IFC OpenShell » : une fenêtre calculée 2 × 3
    affichait **2** au lieu de 6, sans erreur ni cellule vide."""
    table = _table_menuiseries(
        [_fenetre("W1", "Fenêtre 200")], _contrat_menuiserie({"W1": (2.0, 3.0)})
    )
    ligne = table.rows[0]
    largeur_calc, hauteur_calc, surface_calc = ligne[7], ligne[8], ligne[9]
    assert (largeur_calc, hauteur_calc) == (2.0, 3.0)
    assert surface_calc == 6.0, (
        f"surface calculée = {surface_calc} : la colonne porte une dimension, pas une surface"
    )


def test_deux_fenetres_aux_calculs_differents_ne_fusionnent_pas():
    """Mêmes type, matériau et dimensions NATIVES, calculs différents. Ne garder
    que la largeur retenue dans la clé les fondait en une ligne, qui reprenait
    les dimensions du premier élément vu — le défaut « groupe mixte » sous une
    forme plus fine."""
    elements = [
        _fenetre("W1", "Fenêtre 200", largeur=2.0, hauteur=3.0),
        _fenetre("W2", "Fenêtre 200", largeur=2.0, hauteur=3.0),
    ]
    table = _table_menuiseries(elements, _contrat_menuiserie({"W1": (2.1, 3.0), "W2": (2.4, 3.0)}))

    assert len(table.rows) == 2, f"groupes fondus : {table.rows}"
    natives = {(r[3], r[4]) for r in table.rows}
    calculees = {(r[7], r[8]) for r in table.rows}
    assert natives == {(2.0, 3.0)}, "les dimensions natives sont bien identiques"
    assert calculees == {(2.1, 3.0), (2.4, 3.0)}, (
        f"les calculs distincts doivent rester distincts : {calculees}"
    )
    assert [r[6] for r in table.rows] == [1, 1], "chaque ligne ne compte qu'un élément"


# ── La méthode de calcul filtre la comparaison ─────────────────────────────


def _contrat_bbox(mesures: dict[str, tuple[float, float]]) -> dict:
    doc = _contrat_menuiserie(mesures)
    for q in doc["quantities"]:
        q["method"] = "ifcopenshell_bbox"
    return doc


def test_une_dimension_bbox_nalimente_aucune_colonne_de_comparaison():
    """``ifcopenshell_bbox`` mesure l'ENCOMBREMENT, pas la largeur nominale.

    Mesuré sur la maquette réelle : natif 1,4 contre calculé 1,519, jusqu'à
    +83 % d'écart, là où les espaces (``ifcopenshell_geometry``) restent sous
    1,3 % et les dalles à 0 %. Afficher cet écart produirait une colonne qui
    ressemble à un contrôle vérifié sans en être un — mieux vaut un blanc
    expliqué qu'un écart faux.
    """
    table = _table_menuiseries(
        [_fenetre("W1", "Fenêtre 200", largeur=1.4, hauteur=2.0)],
        _contrat_bbox({"W1": (1.519, 2.3)}),
    )
    ligne = table.rows[0]
    assert (ligne[3], ligne[4]) == (1.4, 2.0), "la native doit rester affichée"
    assert (ligne[7], ligne[8], ligne[9]) == (None, None, None), (
        f"des valeurs bbox ont alimenté les colonnes de comparaison : {ligne[7:10]}"
    )


def test_la_note_dit_pourquoi_les_colonnes_de_comparaison_sont_vides():
    """Un blanc muet se lit comme « aucun calcul disponible ». Ici le calcul
    existe : il a été écarté."""
    from audit_bim.reporting.avp_snapshot import build_menuiseries_from_snapshot

    snap = ModelSnapshot(
        project={"name": "P"},
        model={"name": "M.ifc"},
        elements=[_fenetre("W1", "Fenêtre 200", largeur=1.4, hauteur=2.0)],
    ).index()
    merge_into_snapshot(snap, _contrat_bbox({"W1": (1.519, 2.3)}))
    src, _total = build_menuiseries_from_snapshot(snap)

    texte = " ".join(src.notes)
    assert "NON comparables" in texte
    assert "boîte" in texte and "englobante" in texte
    assert "ifcopenshell_bbox" in texte


def test_une_methode_comparable_alimente_bien_la_comparaison():
    """Contre-épreuve : la garde ne doit pas vider toutes les comparaisons.
    Les espaces et les dalles restent mesurés par ``ifcopenshell_geometry``."""
    snap = _snapshot([_espace("SP1", "CHAMBRE", 100.0)])
    merge_into_snapshot(snap, _contrat({"SP1": 110.0}))
    assert _colonnes(snap)["CHAMBRE"] == (100.0, 110.0)


def test_une_methode_inconnue_nest_pas_comparable_par_defaut():
    """Liste blanche assumée : un producteur futur qui n'annonce pas sa méthode
    ne doit pas se retrouver comparé par accident."""
    snap = _snapshot([_espace("SP1", "CHAMBRE", 100.0)])
    doc = _contrat({"SP1": 110.0})
    doc["quantities"][0]["method"] = "methode_future_inconnue"
    merge_into_snapshot(snap, doc)
    assert _colonnes(snap)["CHAMBRE"] == (100.0, None)


# ── Une valeur non comparable ne débloque aucune génération ────────────────


def _snapshot_fenetre_sans_native() -> ModelSnapshot:
    """Une fenêtre SANS aucune BaseQuantity : seule la fusion peut la doter."""
    return ModelSnapshot(
        project={"name": "P"},
        model={"name": "M.ifc"},
        elements=[{"uuid": "W1", "type": "IfcWindow", "name": "F25", "property_sets": []}],
    ).index()


def test_une_dimension_bbox_ne_debloque_pas_la_generation(tmp_path):
    """Le trou que la garde bbox avait ouvert.

    La fusion injecte la valeur calculée dans le pset quand la native manque.
    ``_base_quantity_ordered`` la lisait alors comme n'importe quelle autre, et
    la porte QA déclarait Menuiseries débloqué — pendant que le writer, lui,
    écartait cette même valeur comme non comparable. Le pack produisait une
    ligne de menuiserie dont TOUTES les colonnes de mesure étaient vides :
    exactement ce que la gate ``missing_quantities`` existe pour empêcher.
    """
    from audit_bim.reporting.avp.pack import _qa_missing_quantities
    from audit_bim.reporting.avp_snapshot import count_menuiseries_with_dimensions

    snap = _snapshot_fenetre_sans_native()
    assert _qa_missing_quantities(snap) == ["Menuiseries"], "état initial : rien à afficher"

    merge_into_snapshot(snap, _contrat_bbox({"W1": (1.519, 2.3)}))

    assert count_menuiseries_with_dimensions(snap) == 0, (
        "une dimension bbox n'est pas affichable : elle ne doit pas être comptée"
    )
    assert _qa_missing_quantities(snap) == ["Menuiseries"], (
        "la fusion d'une valeur non comparable a débloqué la génération"
    )

    # …et le refus se produit AVANT écriture : aucun fichier ne doit exister.
    from audit_bim.reporting.avp.models import AvpQaError
    from audit_bim.reporting.avp_i3f import write_avp_i3f_report_pack

    sortie = tmp_path / "pack"
    with pytest.raises(AvpQaError, match="Menuiseries"):
        write_avp_i3f_report_pack(None, sortie, snapshot=snap, export_pdf=False)
    assert not sortie.exists() or not list(sortie.iterdir()), (
        "un classeur a été écrit avant le refus"
    )


def test_une_dimension_comparable_debloque_bien_la_generation():
    """Contre-épreuve : la garde ne bloque pas tout. Sans elle, le test
    précédent passerait pour une bonne raison — parce que rien ne débloque
    jamais — et ne prouverait rien."""
    from audit_bim.reporting.avp.pack import _qa_missing_quantities
    from audit_bim.reporting.avp_snapshot import count_menuiseries_with_dimensions

    snap = _snapshot_fenetre_sans_native()
    merge_into_snapshot(snap, _contrat_menuiserie({"W1": (1.2, 2.1)}))  # ifcopenshell_geometry

    assert count_menuiseries_with_dimensions(snap) == 1
    assert _qa_missing_quantities(snap) == []


def test_la_disponibilite_nannonce_pas_partial_computed_sur_du_bbox():
    """Même règle côté annonce : promettre un livrable « partiel » alors
    qu'aucune valeur n'atteindra une cellule, c'est promettre du vide."""
    from audit_bim.reporting.avp_availability import inspect_avp_report_availability

    snap = _snapshot_fenetre_sans_native()
    merge_into_snapshot(snap, _contrat_bbox({"W1": (1.519, 2.3)}))
    men = {a.key: a for a in inspect_avp_report_availability(snap)}["menuiseries"]
    assert men.computed_assisted is False
    assert men.status != "partial_computed"
    assert any("Width" in m or "Height" in m or "argeur" in m for m in men.missing_data), (
        f"le manque doit être nommé : {men.missing_data}"
    )


def _snapshot_deux_fenetres(native_sur_w1: bool) -> ModelSnapshot:
    """W1 porte ses dimensions natives, W2 n'a rien — seule la fusion la dote."""
    w1 = {"uuid": "W1", "type": "IfcWindow", "name": "F25", "property_sets": []}
    if native_sur_w1:
        w1["property_sets"] = [
            {
                "name": "BaseQuantities",
                "properties": [
                    {"definition": {"name": "Width"}, "value": 1.2},
                    {"definition": {"name": "Height"}, "value": 2.1},
                ],
            }
        ]
    w2 = {"uuid": "W2", "type": "IfcWindow", "name": "F30", "property_sets": []}
    return ModelSnapshot(project={"name": "P"}, model={"name": "M.ifc"}, elements=[w1, w2]).index()


def test_un_modele_mixte_nannonce_pas_partial_computed_pour_du_bbox():
    """Cas où la garde de ``_uses_computed_quantity`` se joue vraiment.

    L'exigence est satisfaite par les fenêtres NATIVES ; la question devient
    « le calcul a-t-il apporté quelque chose ? ». Répondre oui sur des valeurs
    bbox annoncerait un déblocage qui n'atteint aucune cellule.
    """
    from audit_bim.reporting.avp_availability import inspect_avp_report_availability

    snap = _snapshot_deux_fenetres(native_sur_w1=True)
    merge_into_snapshot(snap, _contrat_bbox({"W2": (1.519, 2.3)}))
    men = {a.key: a for a in inspect_avp_report_availability(snap)}["menuiseries"]
    assert men.can_generate is True, "les fenêtres natives restent générables"
    assert men.computed_assisted is False, "des valeurs bbox ont été comptées comme un apport"
    assert men.status != "partial_computed"


def test_un_modele_mixte_annonce_partial_computed_pour_une_methode_comparable():
    """Contre-épreuve du précédent : avec une méthode comparable, l'apport du
    calcul doit bien être annoncé — sinon la garde masquerait tout apport."""
    from audit_bim.reporting.avp_availability import inspect_avp_report_availability

    snap = _snapshot_deux_fenetres(native_sur_w1=True)
    merge_into_snapshot(snap, _contrat_menuiserie({"W2": (1.4, 2.2)}))
    men = {a.key: a for a in inspect_avp_report_availability(snap)}["menuiseries"]
    assert men.computed_assisted is True
    assert men.status == "partial_computed"
