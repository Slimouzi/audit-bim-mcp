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
                "method": "geometry",
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
