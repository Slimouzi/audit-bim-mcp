"""Périmètre et sémantique des colonnes du livrable **Zones/Espaces**.

Le livrable précédent était lisible et **disait un total faux pour l'usage
I3F** : le pivot de synthèse totalisait tout ce que la maquette contient —
annexes et espaces non rattachés à une zone compris — là où le gabarit client
ne totalise que les pièces habitables zonées.

Cinq règles sont figées ici, chacune correspondant à un défaut mesuré face à
``260130 Tarare Export Zones et Espaces.xlsx`` :

1. le **pivot** ne totalise que le périmètre SHAB (zoné, hors annexe) ;
2. le **détail** ne porte que des espaces zonés — annexes comprises, elles
   existent dans la maquette et le gabarit les montre ;
3. ce qui est écarté est **tracé** (note de méthode) : un livrable client peut
   filtrer, mais il doit dire ce qu'il filtre quand la donnée existe ;
4. ``Type Pièce`` porte la **typologie du logement** (T3/T4/T5, ANNEXES), pas
   la classe IFC de l'espace ;
5. ``Surface IFC OpenShell`` et ``Surface Nette (Qté de Base)`` ne peuvent pas
   recevoir la même valeur — sinon l'écart est vide par construction et se lit
   comme une concordance vérifiée alors que rien ne l'a été (doctrine #210).

Le corpus de test est **synthétique mais bâti sur la forme réelle** des charges
utiles BIMData : ``structure_tree`` typé en libellés courts (``storey`` /
``space``), typologie portée par l'``ObjectType`` de l'``IfcZone``, libellé
d'espace en ``LongName``. C'est précisément là qu'un corpus inventé ne prouvait
rien : la fixture historique écrivait ``IfcBuildingStorey`` — l'orthographe que
le code attendait — donc la colonne Étage sortait vide en réel sans qu'aucun
test ne le voie.
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

import pytest

from audit_bim.extraction.model_data import ModelSnapshot
from audit_bim.reporting.avp_snapshot import (
    build_shab_from_snapshot,
    build_zones_espaces_from_snapshot,
)

# ── Corpus synthétique, à la forme des charges utiles réelles ───────────────

_T3, _T4 = "Zone Logement T3", "Zone Logement T4"


def _space(uuid: str, longname: str, area: float, *, computed: bool = False) -> dict:
    """Élément ``IfcSpace`` tel que le renvoie ``/element/raw``.

    Le libellé métier vit dans ``LongName`` ; ``ObjectType`` est **nul** — c'est
    la raison pour laquelle ``Type Pièce`` ne peut pas se lire sur l'espace.
    """
    el = {
        "uuid": uuid,
        "type": "IfcSpace",
        "name": uuid,
        "longname": longname,
        "object_type": None,
        "property_sets": [
            {
                "name": "BaseQuantities",
                "properties": [{"definition": {"name": "NetFloorArea"}, "value": area}],
            }
        ],
    }
    if computed:
        el["computed_base_quantities"] = [{"quantity": "NetFloorArea", "value": area}]
    return el


def _zone_element(uuid: str, name: str, object_type: str) -> dict:
    """Élément ``IfcZone`` tel que le renvoie ``/element/raw``.

    La typologie du logement est portée **deux fois** par la charge utile
    réelle : en ``object_type`` (snake_case, propre à BIMData) et dans le bloc
    ``attributes.properties`` sous son nom IFC ``ObjectType``. Seul le second
    est lu — reproduire les deux évite un corpus qui ferait passer le test par
    un chemin que la production n'emprunte pas.
    """
    return {
        "uuid": uuid,
        "type": "IfcZone",
        "name": name,
        "object_type": object_type,
        "attributes": {
            "properties": [
                {"definition": {"name": "Name"}, "value": name},
                {"definition": {"name": "ObjectType"}, "value": object_type},
            ]
        },
    }


def _snapshot() -> ModelSnapshot:
    """Deux logements zonés, deux annexes, un espace hors zone.

    Trois totaux volontairement distincts, pour qu'aucune assertion ne puisse
    passer par accident :

    - périmètre SHAB (zoné, hors annexe) : 12 + 20 + 15 = **47,0** ;
    - détail (zoné, annexes comprises)   : 47 + 3 + 5   = **55,0** ;
    - maquette entière                   : 55 + 100     = **155,0**.
    """
    spaces_raw = [
        _space("SP_A", "CHAMBRE 01", 12.0),
        _space("SP_B", "SEJOUR", 20.0, computed=True),
        _space("SP_C", "CELLIER", 3.0),
        _space("SP_D", "CHAMBRE 01", 15.0),
        _space("SP_E", "TERRASSE", 5.0),
        _space("SP_F", "PMR", 100.0),  # rattaché à aucune zone
    ]
    zones_raw = [_zone_element("Z1", "LGT-01", _T3), _zone_element("Z2", "LGT-02", _T4)]
    # Route /zone : la liste des espaces membres, sans aucun type métier.
    zones = [
        {
            "uuid": "Z1",
            "name": "LGT-01",
            "spaces": [{"uuid": u} for u in ("SP_A", "SP_B", "SP_C")],
        },
        {"uuid": "Z2", "name": "LGT-02", "spaces": [{"uuid": u} for u in ("SP_D", "SP_E")]},
    ]
    # structure_tree réel : types en libellés courts, étage porté par l'arbre
    # SEUL (aucun attribut d'étage sur les espaces).
    tree = [
        {
            "type": "project",
            "uuid": "P1",
            "name": "P",
            "children": [
                {
                    "type": "building",
                    "uuid": "B1",
                    "name": "BAT",
                    "children": [
                        {
                            "type": "storey",
                            "uuid": "ST1",
                            "name": "R+1",
                            "children": [
                                {"type": "space", "uuid": u, "name": u, "children": []}
                                for u in ("SP_A", "SP_B", "SP_C", "SP_D", "SP_E", "SP_F")
                            ],
                        }
                    ],
                }
            ],
        }
    ]
    return ModelSnapshot(
        project={"name": "P"},
        model={"name": "M.ifc"},
        storeys=[{"uuid": "ST1", "name": "R+1"}],
        spaces=[{"uuid": s["uuid"], "name": s["name"]} for s in spaces_raw],
        zones=zones,
        elements=[*spaces_raw, *zones_raw],
        structure_tree=tree,
    ).index()


def _grids() -> dict[str, list[list]]:
    ms = build_zones_espaces_from_snapshot(_snapshot())
    return {g.title: g.rows for g in ms.grids}


def _detail() -> list[list]:
    return next(rows for title, rows in _grids().items() if title.startswith("TDB 2022 01.3"))


def _col(rows: list[list], name: str) -> list:
    idx = rows[0].index(name)
    return [r[idx] for r in rows[1:]]


# ── 1. Le pivot ne totalise que le périmètre SHAB ───────────────────────────


def test_pivot_totalise_le_perimetre_shab_seulement():
    """47,0 — ni 55,0 (avec annexes) ni 155,0 (avec les espaces hors zone)."""
    pivot = _grids()["Feuil2"]
    total = pivot[-1][-1]
    assert total == pytest.approx(47.0), (
        f"total général {total} : le pivot totalise hors périmètre SHAB "
        "(55,0 = annexes incluses, 155,0 = espaces non zonés inclus)"
    )


def test_pivot_ignore_les_espaces_non_zones():
    pivot = _grids()["Feuil2"]
    plat = [c for row in pivot for c in row]
    assert "PMR" not in plat, "un espace non rattaché à une zone entre dans le pivot"


def test_pivot_ignore_les_annexes():
    pivot = _grids()["Feuil2"]
    plat = [c for row in pivot for c in row]
    for annexe in ("CELLIER", "TERRASSE"):
        assert annexe not in plat, f"l'annexe {annexe} contribue au total SHAB"


def test_le_total_shab_exporte_est_celui_du_pivot():
    """Le dénominateur du ratio FAC/SHAB et le total affiché sont le même
    nombre — sinon le document dit deux chiffres pour une seule grandeur."""
    _ms, total = build_shab_from_snapshot(_snapshot())
    assert total == pytest.approx(47.0)


# ── 2. Le détail ne porte que des espaces zonés ─────────────────────────────


def test_detail_ne_contient_que_des_espaces_zones():
    pieces = _col(_detail(), "Pièce (Nombre)")
    assert "PMR" not in pieces
    assert len(pieces) == 5


def test_detail_montre_les_annexes():
    """Les annexes sortent du total SHAB, pas du document : les masquer ferait
    disparaître de la maquette des espaces qui y sont."""
    pieces = _col(_detail(), "Pièce (Nombre)")
    assert "CELLIER" in pieces and "TERRASSE" in pieces


# ── 3. Ce qui est écarté est tracé ──────────────────────────────────────────


def test_note_de_methode_chiffre_les_espaces_ecartes():
    note = _grids()["Feuil1"]
    assert note, "Feuil1 est vide : 100 m² de maquette écartés sans aucune trace"
    par_cle = {row[0]: row[1] for row in note if len(row) == 2}
    assert par_cle["espaces_non_zones_count"] == 1
    assert par_cle["espaces_non_zones_surface_m2"] == pytest.approx(100.0)
    assert par_cle["annexes_count"] == 2
    assert par_cle["annexes_surface_m2"] == pytest.approx(8.0)
    assert par_cle["total_shab_m2"] == pytest.approx(47.0)


def test_note_de_methode_dit_pourquoi():
    texte = "\n".join(str(c) for row in _grids()["Feuil1"] for c in row)
    assert "non rattachés à une zone" in texte
    assert "annexe non habitable" in texte


def test_export_shab_porte_aussi_la_note_de_methode():
    ms, _total = build_shab_from_snapshot(_snapshot())
    assert "Note de méthode" in [g.title for g in ms.grids]


# ── 4. Type Pièce porte la typologie du logement ────────────────────────────


def test_type_piece_nest_pas_la_classe_ifc():
    types = set(_col(_detail(), "Type Pièce"))
    assert "IfcSpace" not in types
    assert not types & {"INTERNAL", "EXTERNAL"}


def test_type_piece_vient_de_la_typologie_de_la_zone():
    rows = _detail()
    par_zone_et_piece = dict(
        zip(
            zip(_col(rows, "Nom Zone"), _col(rows, "Pièce (Nombre)"), strict=True),
            _col(rows, "Type Pièce"),
            strict=True,
        )
    )
    assert par_zone_et_piece[("LGT-01", "CHAMBRE 01")] == "T3"
    assert par_zone_et_piece[("LGT-01", "SEJOUR")] == "T3"
    assert par_zone_et_piece[("LGT-02", "CHAMBRE 01")] == "T4"


def test_type_piece_dune_annexe_est_annexes():
    rows = _detail()
    par_piece = dict(zip(_col(rows, "Pièce (Nombre)"), _col(rows, "Type Pièce"), strict=True))
    assert par_piece["CELLIER"] == "ANNEXES"
    assert par_piece["TERRASSE"] == "ANNEXES"


# ── 5. G et H ne peuvent pas venir de la même source ────────────────────────


def test_chaque_colonne_ne_recoit_que_sa_propre_source():
    """Ce qui est interdit n'est pas que les deux colonnes soient remplies —
    c'est qu'elles le soient avec la **même** valeur.

    Le corpus de ce fichier ne porte aucune valeur de comparaison : chaque
    ligne n'a donc qu'une source, et l'écart reste vide. La matrice complète —
    dont le cas ``native + calcul``, celui qui rend l'écart exploitable — est
    figée dans ``test_computed_comparison_quantities.py``.
    """
    rows = _detail()
    openshell = _col(rows, "Surface IFC OpenShell")
    natif = _col(rows, "Surface Nette (Qté de Base)")
    assert any(v is not None for v in natif), "aucune mesure native : le test serait vide"
    for g, h in zip(openshell, natif, strict=True):
        assert not (g is not None and h is not None and g == h), (
            f"G={h} H={h} : la même valeur dans les deux colonnes ne compare rien"
        )


def test_la_provenance_decide_de_la_colonne():
    rows = _detail()
    pieces = _col(rows, "Pièce (Nombre)")
    openshell = dict(zip(pieces, _col(rows, "Surface IFC OpenShell"), strict=True))
    natif = dict(zip(pieces, _col(rows, "Surface Nette (Qté de Base)"), strict=True))
    # SEJOUR est la seule surface issue du calcul géométrique.
    assert openshell["SEJOUR"] == pytest.approx(20.0)
    assert natif["SEJOUR"] is None
    assert natif["CHAMBRE 01"] is not None
    assert openshell["CHAMBRE 01"] is None


def test_detail_a_les_douze_colonnes_du_gabarit():
    entetes = _detail()[0]
    assert len(entetes) == 12, f"{len(entetes)} colonnes : le gabarit en compte 12 (A:L)"
    assert "Source quantité" not in entetes, (
        "la provenance se lit à l'emplacement de la valeur ; une 13ᵉ colonne "
        "déforme le tableau client"
    )


def test_ecart_est_relatif_et_garde_contre_labsence_de_valeur():
    ecarts = _col(_detail(), "écarts")
    for f in ecarts:
        assert "/G" in f, f"écart absolu {f} : le gabarit porte un écart relatif"
        assert f.startswith("=IF(OR("), (
            f"{f} : sans garde, une colonne vide donne #DIV/0! sur toute la colonne"
        )


# ── 6. L'étage vient de structure_tree, à son orthographe réelle ────────────


def test_etage_resolu_depuis_un_structure_tree_reel():
    """Non-vacuité : l'arbre n'emploie QUE les libellés courts (``storey`` /
    ``space``) et aucun espace ne porte d'attribut d'étage. Restreindre le
    parcours aux classes IFC longues vide la colonne — c'est ce qui se
    produisait en réel sur 316 espaces."""
    etages = _col(_detail(), "Étage")
    assert etages and all(e == "R+1" for e in etages), f"colonne Étage non résolue : {etages}"


# ── Confrontation au gabarit client, quand la maquette réelle est là ────────

#: Mesuré sur ``260130 Tarare Export Zones et Espaces.xlsx`` (onglets ``Feuil2``
#: et ``TDB 2022 01.3 - Export Zones...``). La spécification vit ICI ; le
#: classeur ne sert qu'à la confronter. Tolérance : les surfaces du classeur
#: viennent d'un export humain, celles du généré des BaseQuantities BIMData —
#: 0,3 m² d'écart sur 2 165 (0,014 %).
_REF_PIVOT_TOTAL = 2164.98
_REF_DETAIL_ROWS = 300
_REF_TYPE_PIECE = {"T4": 192, "T3": 45, "T5": 39}
_REF_ANNEXES = 24

_SNAPSHOT_REEL = Path("out/.audit_cache/snapshot_e47fc988e6862cfd.json.gz")


@pytest.mark.skipif(
    not _SNAPSHOT_REEL.exists(),
    reason="snapshot de la maquette réelle absent (poste AMO, non versionné)",
)
def test_maquette_reelle_retrouve_les_comptes_du_gabarit():
    payload = json.loads(gzip.open(_SNAPSHOT_REEL).read())
    snap = ModelSnapshot(**{k: v for k, v in payload.items() if not k.startswith("_")}).index()
    grids = {g.title: g.rows for g in build_zones_espaces_from_snapshot(snap).grids}
    detail = next(rows for t, rows in grids.items() if t.startswith("TDB 2022 01.3"))

    assert len(detail) - 1 == _REF_DETAIL_ROWS
    total = grids["Feuil2"][-1][-1]
    assert total == pytest.approx(_REF_PIVOT_TOTAL, abs=1.0), (
        f"pivot {total} contre {_REF_PIVOT_TOTAL} au gabarit"
    )

    idx = detail[0].index("Type Pièce")
    comptes: dict[str, int] = {}
    for row in detail[1:]:
        comptes[row[idx]] = comptes.get(row[idx], 0) + 1
    for typologie, attendu in _REF_TYPE_PIECE.items():
        assert comptes.get(typologie) == attendu, f"{typologie} : {comptes.get(typologie)}"
    # Le gabarit sépare 18 « ANNEXES » de 6 « Balcons et terrasses ». Aucune
    # donnée de la maquette ne porte cette distinction — on ne la fabrique pas.
    assert comptes.get("ANNEXES") == _REF_ANNEXES


@pytest.mark.skipif(
    os.environ.get("CI") is None and not _SNAPSHOT_REEL.exists(),
    reason="hors CI et sans maquette réelle",
)
def test_la_specification_reste_coherente():
    """Garde-fou de cohérence des constantes ci-dessus, vérifié partout : le
    total du gabarit est bien celui des seules typologies habitables."""
    assert sum(_REF_TYPE_PIECE.values()) + _REF_ANNEXES == _REF_DETAIL_ROWS


# ── 7. La note « valeurs NON contractuelles » suit la provenance ────────────


def test_la_note_non_contractuelle_survit_au_retrait_de_la_colonne():
    """Régression silencieuse : la note méthodo était déclenchée par la
    présence du libellé « Calculée (IfcOpenShell) » dans une cellule. Retirer
    la colonne ``Source quantité`` la faisait donc disparaître du livrable —
    sans erreur, la note étant facultative. Elle doit suivre le mécanisme
    actuel : une valeur sous un en-tête « IFC OpenShell »."""
    from audit_bim.reporting.avp.xlsx_common import _rows_have_computed

    detail = _detail()
    assert _rows_have_computed(detail), (
        "une surface calculée est présente mais la note méthodo ne sera pas apposée"
    )


def test_la_note_ne_sapplique_pas_a_des_quantites_natives():
    """Non-vacuité dans l'autre sens : sans aucune valeur calculée, la note ne
    doit pas être apposée — sinon elle qualifierait de « non contractuelles »
    des BaseQuantities natives."""
    from audit_bim.reporting.avp.xlsx_common import _rows_have_computed

    detail = _detail()
    entetes = detail[0]
    calc = entetes.index("Surface IFC OpenShell")
    natif = entetes.index("Surface Nette (Qté de Base)")
    natives = [list(entetes)]
    for ligne in detail[1:]:
        copie = list(ligne)
        copie[natif] = copie[calc] if copie[calc] is not None else copie[natif]
        copie[calc] = None
        natives.append(copie)
    assert not _rows_have_computed(natives)


def test_une_valeur_de_comparaison_remplit_les_deux_colonnes_et_rend_lecart_exploitable():
    """Non-vacuité du cas central : native **et** calcul sur la même ligne.

    Tant que la fusion jetait la valeur calculée dès qu'une native existait, ce
    cas ne pouvait pas se produire — la colonne « Surface IFC OpenShell » était
    vide par construction et la garde de l'écart se déclenchait toujours. Un
    test qui exigeait « une seule colonne remplie » passait donc pour la
    mauvaise raison : il protégeait une impossibilité.
    """
    from audit_bim.extraction.computed_quantities import merge_into_snapshot

    snap = _snapshot()
    merge_into_snapshot(
        snap,
        {
            "schema": "computed_base_quantities/v1",
            "quantities": [
                {
                    "global_id": "SP_A",
                    "ifc_class": "IfcSpace",
                    "qto": "Qto_SpaceBaseQuantities",
                    "quantity": "NetFloorArea",
                    "value": 12.6,  # natif 12,0 → écart +5 %
                    "unit": "m2",
                    "method": "geometry",
                    "status": "computed",
                    "source": "computed_ifcopenshell",
                }
            ],
        },
    )
    rows = next(
        g.rows
        for g in build_zones_espaces_from_snapshot(snap).grids
        if g.title.startswith("TDB 2022 01.3")
    )
    entetes = rows[0]
    i_piece = entetes.index("Pièce (Nombre)")
    i_calc = entetes.index("Surface IFC OpenShell")
    i_natif = entetes.index("Surface Nette (Qté de Base)")
    ligne = next(r for r in rows[1:] if r[i_piece] == "CHAMBRE 01" and r[i_natif] == 12.0)

    assert ligne[i_natif] == 12.0, "la native doit rester dans sa colonne"
    assert ligne[i_calc] == 12.6, "la valeur de comparaison doit remplir la colonne calculée"
    assert ligne[i_calc] / ligne[i_natif] - 1 == pytest.approx(0.05), "écart exploitable"

    # …et la garde de la formule ne se déclenche plus : les deux cellules
    # référencées sont non vides, donc l'écart s'affiche vraiment.
    formule = ligne[entetes.index("écarts")]
    assert formule.startswith("=IF(OR(")
    assert "G" in formule and "H" in formule
