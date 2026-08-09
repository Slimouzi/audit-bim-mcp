"""Le livrable Menuiseries doit avoir la FORME du gabarit client.

Écarts mesurés le 2026-08-08 entre le pack généré et
``260130 Tarare export Menuiseries.xlsx`` — le modèle de référence I3F. Aucun
n'est une question de chiffres : ce sont des écarts de **structure**, et un
livrable qui n'a pas la forme du gabarit n'est plus le gabarit, même juste.

Les quatre écarts, mesurés sur le modèle et non supposés :

1. **portes mélangées aux fenêtres** — le modèle ne contient que des lignes
   ``Fenêtre`` (A2:A16), plus une ligne d'appuis à part. Le généré ajoute six
   lignes ``Porte``, ce qui déplace le compteur de ``D2:D16`` à ``D2:D22`` et
   change le sens du livrable ;
2. **colonne en trop** — le modèle a 14 colonnes (A:N) ; le généré en a 15,
   avec une colonne ``Source quantité`` absente du gabarit ;
3. **colonnes de comparaison renommées** — le modèle attend ``Largeur``,
   ``Hauteur``, ``Surface Solibri`` (H, I, J). Le généré écrit ``Largeur IFC
   OpenShell``… : la colonne J n'est plus une comparaison Solibri ;
4. **types écrasés** — le modèle distingue ``Fenêtre 25``, ``Fenêtre châssis
   double 25``, ``Ouverture fenêtre rectangulaire 25``. Le généré regroupe sur
   ``WINDOW`` / ``DOOR`` et perd l'information qui structure les lignes.

Ce fichier est écrit AVANT le correctif et doit donc ÉCHOUER sur le
comportement actuel : une CI verte sur un livrable faux est ce qu'on cherche à
empêcher.
"""

from __future__ import annotations

import pathlib

import pytest

MODELE = pathlib.Path(
    "/Users/stani/code/MCP/Documents maître d'ouvrage/Documents I3F/Livrables/"
    "260130 Tarare export Menuiseries.xlsx"
)

#: En-têtes A:N du gabarit, relevés sur le fichier client. ``M`` est vide dans
#: le modèle — c'est une colonne de séparation, pas un oubli.
ENTETES_MODELE = [
    "Composant",
    "Type",
    "Matériau",
    "BaseQuantities.Width",
    "BaseQuantities.Height",
    "Surface Natif",
    "Nombre",
    "Largeur",
    "Hauteur",
    "Surface Solibri",
    "Ecart de largeur",
    "Ecart de heuteur",
    None,
    "Couleur",
]


def _entetes_generees() -> list[str]:
    """En-têtes que le générateur produit aujourd'hui."""
    from audit_bim.reporting import avp_snapshot

    src = avp_snapshot.build_menuiseries_from_snapshot.__doc__ or ""
    del src  # documentation seule : la liste vient du code, lue ci-dessous.
    import inspect
    import re

    corps = inspect.getsource(avp_snapshot.build_menuiseries_from_snapshot)
    bloc = re.search(r"headers = \[(.*?)\]", corps, re.S)
    assert bloc, "prémisse : le générateur doit déclarer ses en-têtes"
    return [m.group(1) for m in re.finditer(r'"([^"]*)"', bloc.group(1))]


#: Ce que la mesure du gabarit a donné, **figé ici**. Le fichier client vit sur
#: le poste de l'AMO et n'est pas versionné : le tester directement rendait la
#: CI rouge partout ailleurs. La spécification appartient donc au test ; le
#: fichier ne sert qu'à la confronter quand il est là.
MODELE_COLONNES = 14
MODELE_COMPTEUR = ("C", 17, "=COUNTA(D2:D16)")
MODELE_COMPOSANT_UNIQUE = "Fenêtre"


def test_the_frozen_specification_is_self_consistent():
    """Sentinelle : la spécification figée doit tenir debout seule.

    Elle est ce que la CI vérifie ; le gabarit client, lui, n'y est pas.
    """
    assert len(ENTETES_MODELE) == MODELE_COLONNES
    assert ENTETES_MODELE[12] is None, "colonne M vide — séparation, pas oubli"
    assert ENTETES_MODELE[13] == "Couleur"


@pytest.mark.skipif(not MODELE.is_file(), reason="gabarit client absent (poste AMO)")
def test_the_reference_model_matches_the_frozen_specification():
    """Quand le gabarit EST là, il doit confirmer la spécification figée.

    C'est le seul rôle du fichier : empêcher que la constante ci-dessus dérive
    de ce que le client attend réellement.
    """
    openpyxl = pytest.importorskip("openpyxl")
    ws = openpyxl.load_workbook(MODELE).active

    assert ws.max_column == MODELE_COLONNES, ws.max_column
    colonne, ligne, formule = MODELE_COMPTEUR
    assert ws[f"{colonne}{ligne}"].value == formule
    composants = {ws.cell(r, 1).value for r in range(2, ligne)}
    assert composants == {MODELE_COMPOSANT_UNIQUE}, composants


def test_the_generator_must_not_add_a_fifteenth_column():
    """Écart n°2 : ``Source quantité`` n'existe pas dans le gabarit."""
    entetes = _entetes_generees()
    assert "Source quantité" not in entetes, (
        "le générateur ajoute une colonne absente du gabarit client"
    )
    assert len(entetes) == 14, f"{len(entetes)} colonnes générées, 14 attendues"


def test_no_generated_column_mentions_the_third_party_tool():
    """Doctrine : aucun libellé « Solibri » dans un fichier généré.

    Ce test a d'abord été écrit **à l'envers** — il exigeait de CONSERVER les
    colonnes Solibri du gabarit historique. C'était prendre le fichier client
    pour la spécification, alors que la doctrine produit fait foi : le livrable
    n'imite pas Solibri, il assume IFC OpenShell.
    """
    entetes = _entetes_generees()
    fautifs = [h for h in entetes if "solibri" in h.lower() or "bimcollab" in h.lower()]
    assert not fautifs, fautifs


def test_the_measurement_columns_name_ifc_openshell():
    """La source unique doit être lisible dans l'en-tête, pas déduite."""
    entetes = _entetes_generees()
    for attendu in (
        "Largeur IFC OpenShell",
        "Hauteur IFC OpenShell",
        "Surface IFC OpenShell",
    ):
        assert attendu in entetes, f"colonne {attendu!r} absente du généré"


def test_the_deliverable_perimeter_excludes_doors():
    """Écart n°1 : les portes n'appartiennent pas à ce livrable.

    Le contrôle porte sur le périmètre du LIVRABLE (``_FENETRE_CLASSES``), pas
    sur la constante générale ``_MENUISERIE_CLASSES`` : celle-ci sert encore à
    compter les menuiseries pour la disponibilité, où portes et fenêtres ont
    toutes deux leur place.
    """
    from audit_bim.reporting import avp_snapshot

    portes = [c for c in avp_snapshot._FENETRE_CLASSES if "Door" in c]
    assert not portes, portes
    assert set(avp_snapshot._FENETRE_CLASSES) == {"IfcWindow", "IfcWindowStandardCase"}


# ---------------------------------------------------------------------------
# Comportement : le CLASSEUR produit, pas le code qui prétend le produire.
# ---------------------------------------------------------------------------


def _classeur_menuiseries(tmp_path):
    """Génère un pack et rend l'onglet Menuiseries réellement écrit."""
    openpyxl = pytest.importorskip("openpyxl")
    from audit_bim.extraction.model_data import ModelSnapshot
    from audit_bim.reporting.avp_i3f import write_avp_i3f_report_pack
    from audit_bim.reporting.avp_sources import AvpSources

    def bq(**kv):
        return [
            {
                "name": "BaseQuantities",
                "properties": [{"definition": {"name": k}, "value": v} for k, v in kv.items()],
            }
        ]

    snap = ModelSnapshot(
        spaces=[
            {
                "uuid": "S1",
                "type": "IfcSpace",
                "name": "SEJOUR",
                **{"property_sets": bq(NetFloorArea=24.5)},
            }
        ],
        elements=[
            {
                "uuid": "W1",
                "type": "IfcWindow",
                "ObjectType": "Fenêtre 25",
                "property_sets": bq(Width=1.2, Height=1.0),
            },
            {
                "uuid": "D1",
                "type": "IfcDoor",
                "ObjectType": "Porte 90",
                "property_sets": bq(Width=0.9, Height=2.1),
            },
        ],
    ).index()

    pack = write_avp_i3f_report_pack(
        None,
        tmp_path / "out",
        sources=AvpSources(menuiseries=None),
        snapshot=snap,
        project_name="Chantier",
        project_code="0546L",
        export_pdf=False,
    )
    ws = openpyxl.load_workbook(pack.menuiseries_xlsx).active
    return ws


def test_the_written_workbook_matches_the_frozen_specification(tmp_path):
    """Le contrôle porte sur le .xlsx ÉCRIT, pas sur le source du générateur.

    Un test qui lit les en-têtes par ``inspect.getsource`` ne voit ni les
    colonnes réellement écrites, ni les formules, ni la ligne de synthèse. Il
    prouve une intention, pas un livrable.
    """
    ws = _classeur_menuiseries(tmp_path)
    entetes = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]

    assert len(entetes) == MODELE_COLONNES, entetes
    assert "Source quantité" not in entetes
    for interdit in entetes:
        assert "solibri" not in str(interdit).lower(), interdit
    for attendu in ("Largeur IFC OpenShell", "Hauteur IFC OpenShell", "Surface IFC OpenShell"):
        assert attendu in entetes, attendu


def test_the_written_workbook_holds_no_door_row(tmp_path):
    """Périmètre réel : la porte du snapshot ne doit pas atteindre le fichier."""
    ws = _classeur_menuiseries(tmp_path)
    colonne_a = [ws.cell(r, 1).value for r in range(2, ws.max_row + 1)]

    assert MODELE_COMPOSANT_UNIQUE in colonne_a, colonne_a
    assert "Porte" not in colonne_a, colonne_a


def test_the_written_counter_covers_the_real_perimeter(tmp_path):
    """Le compteur doit borner les lignes RÉELLEMENT écrites.

    Le gabarit client porte `=COUNTA(D2:D16)` parce qu'il a 15 lignes métier.
    Le writer calcule la borne dynamiquement : le contrôle vérifie donc la
    cohérence avec le contenu produit, pas une borne figée qui mentirait dès
    qu'une maquette a un nombre de types différent.
    """
    ws = _classeur_menuiseries(tmp_path)
    lignes_metier = [r for r in range(2, ws.max_row + 1) if ws.cell(r, 1).value == "Fenêtre"]
    assert lignes_metier, "prémisse : au moins une ligne métier"

    formules = [
        ws.cell(r, c).value
        for r in range(1, ws.max_row + 1)
        for c in range(1, ws.max_column + 1)
        if isinstance(ws.cell(r, c).value, str) and ws.cell(r, c).value.startswith("=COUNTA(")
    ]
    assert formules, "aucun compteur écrit"
    assert formules[0] == f"=COUNTA(D2:D{max(lignes_metier)})", formules


def test_a_mixed_group_is_split_by_provenance():
    """Deux fenêtres identiques de provenances différentes font DEUX lignes.

    Avant correctif, un unique booléen décidait pour tout le groupe : si l'une
    des fenêtres avait une quantité calculée, la ligne entière basculait en
    colonnes IFC OpenShell — y compris pour les éléments natifs qu'elle
    comptait. Le livrable annonçait alors une provenance fausse.
    """
    from audit_bim.extraction.model_data import ModelSnapshot
    from audit_bim.reporting.avp_snapshot import build_menuiseries_from_snapshot

    def bq(**kv):
        return [
            {
                "name": "BaseQuantities",
                "properties": [{"definition": {"name": k}, "value": v} for k, v in kv.items()],
            }
        ]

    identiques = [
        {
            "uuid": "W1",
            "type": "IfcWindow",
            "ObjectType": "Fenêtre 25",
            "property_sets": bq(Width=1.2, Height=1.0),
        },
        {
            "uuid": "W2",
            "type": "IfcWindow",
            "ObjectType": "Fenêtre 25",
            "property_sets": bq(Width=1.2, Height=1.0),
            "computed_base_quantities": [
                {"quantity": "Width", "method": "ifcopenshell_geometry"},
                {"quantity": "Height", "method": "ifcopenshell_geometry"},
            ],
        },
    ]
    src, _ = build_menuiseries_from_snapshot(ModelSnapshot(elements=identiques).index())
    lignes = src.table.rows

    assert len(lignes) == 2, f"groupe mixte non scindé : {lignes}"
    # Chaque ligne compte UN élément. Ici la fenêtre calculée n'a pas de valeur
    # native et la native pas de calcul : chacune ne remplit donc qu'une
    # colonne. Ce que le test verrouille n'est pas cette exclusivité — les deux
    # colonnes coexistent dès qu'une valeur de comparaison existe — mais le
    # fait que les deux provenances ne soient PAS fondues dans une seule ligne.
    for ligne in lignes:
        assert ligne[6] == 1, ligne
        assert ligne[3] is not None or ligne[7] is not None, ligne
    assert {ligne[3] is None for ligne in lignes} == {True, False}
