"""``reports`` : la sélection doit gouverner aussi les contrôles, pas que l'écriture.

#214 a fait de ``reports`` une vraie sélection **à l'écriture**, mais les gates
QA sont restées globales : elles préflightaient tout le catalogue. Mesuré —
``reports=["shab_maquette"]`` sur une maquette dont les fenêtres n'ont aucune
dimension refusait le pack **pour Menuiseries**, un rapport qui n'était pas
demandé et qui n'aurait pas été écrit.

Et la validation ne vivait que dans la façade MCP : ``write_avp_i3f_report_pack``
— ré-exporté, appelé directement par des tests et des scripts — acceptait une
clé inconnue en l'ignorant.

Le contrat tient désormais dans **un seul** helper de cœur,
``_normalize_report_selection`` : il déduplique, refuse l'inconnu et le bloqué,
et rend l'ensemble des rapports effectivement produisibles. C'est lui qui décide
de ce qui est écrit **et** de ce qui est contrôlé — deux copies de cette règle
avaient déjà divergé une fois.
"""

from __future__ import annotations

import pytest

from audit_bim.extraction.model_data import ModelSnapshot
from audit_bim.reporting.avp.pack import _normalize_report_selection
from audit_bim.reporting.avp_i3f import (
    AvpQaError,
    AvpReportSelectionError,
    write_avp_i3f_report_pack,
)


def _espace(aire: float | None = 12.0) -> dict:
    psets = (
        [
            {
                "name": "BaseQuantities",
                "properties": [{"definition": {"name": "NetFloorArea"}, "value": aire}],
            }
        ]
        if aire is not None
        else []
    )
    return {
        "uuid": "SP1",
        "type": "IfcSpace",
        "name": "S1",
        "longname": "CHAMBRE",
        "property_sets": psets,
    }


def _snapshot_shab_ok_fenetres_sans_dimensions() -> ModelSnapshot:
    """SHAB exploitable, menuiseries SANS aucune dimension.

    C'est le cas qui départage : un préflight global refuse le pack pour
    Menuiseries même quand seule la SHAB est demandée.
    """
    zone = {
        "uuid": "Z1",
        "type": "IfcZone",
        "name": "LGT-01",
        "attributes": {
            "properties": [{"definition": {"name": "ObjectType"}, "value": "Zone Logement T2"}]
        },
    }
    fenetre = {"uuid": "W1", "type": "IfcWindow", "name": "F25", "property_sets": []}
    return ModelSnapshot(
        project={"name": "P"},
        model={"name": "M.ifc"},
        spaces=[{"uuid": "SP1", "name": "S1"}],
        zones=[{"uuid": "Z1", "name": "LGT-01", "spaces": [{"uuid": "SP1"}]}],
        elements=[_espace(), fenetre, zone],
    ).index()


def _generer(tmp_path, snap, **kw):
    return write_avp_i3f_report_pack(None, tmp_path / "out", snapshot=snap, export_pdf=False, **kw)


def _enveloppe_geometrique():
    """Enveloppe issue du contrat ``envelope.json``, calculée **sans filtre**.

    ``superficie_calque_total`` n'est pas décoratif : c'est lui qui fait
    reconnaître la source comme structurée. Sans lui, le repli snapshot la
    remplace et le test passerait pour la mauvaise raison — la gate ne verrait
    jamais de mode ``geometric``.
    """
    from audit_bim.reporting.avp_sources import EnveloppeSource, SheetTable

    env = EnveloppeSource(
        table=SheetTable(title="Enveloppe", headers=["Composant"], rows=[["Mur", 10.0]]),
    )
    env.superficie_calque_total = 10.0
    env.filter_mode = "geometric"
    return env


# ── Le préflight suit la sélection ─────────────────────────────────────────


def test_un_rapport_non_demande_ne_bloque_pas_le_pack(tmp_path):
    """``reports=["shab_maquette"]`` réussit malgré des fenêtres sans dimension."""
    pack = _generer(
        tmp_path, _snapshot_shab_ok_fenetres_sans_dimensions(), reports=["shab_maquette"]
    )
    assert pack.shab_xlsx is not None and pack.shab_xlsx.exists()
    assert pack.menuiseries_xlsx is None


def test_le_meme_manque_refuse_bien_quand_le_rapport_est_demande(tmp_path):
    """Contre-épreuve indispensable : sans elle, « ne pas bloquer » passerait
    aussi si la gate avait été simplement désactivée."""
    with pytest.raises(AvpQaError) as refus:
        _generer(tmp_path, _snapshot_shab_ok_fenetres_sans_dimensions(), reports=["menuiseries"])
    assert "Menuiseries" in str(refus.value)


def test_une_enveloppe_non_demandee_ne_bloque_pas(tmp_path):
    """La gate ``envelope_filter_mode`` ne doit s'appliquer qu'à un livrable
    d'enveloppe réellement demandé : un contrat calculé sans filtre ne rend pas
    faux un export Zones/Espaces."""

    snap = _snapshot_shab_ok_fenetres_sans_dimensions()
    from audit_bim.reporting.avp_sources import AvpSources

    pack = _generer(
        tmp_path,
        snap,
        sources=AvpSources(enveloppe=_enveloppe_geometrique()),
        reports=["zones_espaces"],
    )
    assert pack.zones_espaces_xlsx is not None and pack.zones_espaces_xlsx.exists()
    assert pack.enveloppe_xlsx is None


def test_une_enveloppe_demandee_bloque_toujours(tmp_path):
    """Contre-épreuve : la gate n'a pas été neutralisée, seulement bornée."""
    from audit_bim.reporting.avp_sources import AvpSources

    with pytest.raises(AvpQaError) as refus:
        _generer(
            tmp_path,
            _snapshot_shab_ok_fenetres_sans_dimensions(),
            sources=AvpSources(enveloppe=_enveloppe_geometrique()),
            reports=["surface_enveloppe"],
        )
    assert "enveloppe" in str(refus.value).lower()


# ── Le contrat vit dans le cœur, pas seulement dans la façade MCP ──────────


def test_le_builder_refuse_lui_meme_une_cle_inconnue(tmp_path):
    """``write_avp_i3f_report_pack`` est ré-exporté et appelé directement : le
    contrat doit tenir sans passer par le tool."""
    with pytest.raises(AvpReportSelectionError) as refus:
        _generer(tmp_path, _snapshot_shab_ok_fenetres_sans_dimensions(), reports=["typo"])
    assert refus.value.unknown == ("typo",)
    assert "plancher" in refus.value.known
    assert not (tmp_path / "out").exists(), "un dossier a été créé malgré le refus"


def test_le_builder_refuse_lui_meme_un_rapport_bloque(tmp_path):
    with pytest.raises(AvpReportSelectionError) as refus:
        _generer(tmp_path, _snapshot_shab_ok_fenetres_sans_dimensions(), reports=["plancher"])
    assert "plancher" in refus.value.blocked
    assert not (tmp_path / "out").exists()


# ── Le helper lui-même ─────────────────────────────────────────────────────


def test_la_normalisation_deduplique_et_reduit_au_produisible():
    assert _normalize_report_selection(["shab_maquette", "shab_maquette"]) == frozenset(
        {"shab_maquette"}
    )
    tous = _normalize_report_selection(None)
    assert "plancher" not in tous, "un rapport bloqué n'est pas produisible"
    assert {"shab_maquette", "zones_espaces", "menuiseries"} <= tous


def test_la_normalisation_signale_les_deux_motifs_a_la_fois():
    """Une demande qui cumule les deux fautes doit les rendre toutes les deux :
    corriger l'une pour découvrir l'autre coûte un aller-retour."""
    with pytest.raises(AvpReportSelectionError) as refus:
        _normalize_report_selection(["typo", "plancher", "shab_maquette"])
    assert refus.value.unknown == ("typo",)
    assert "plancher" in refus.value.blocked
