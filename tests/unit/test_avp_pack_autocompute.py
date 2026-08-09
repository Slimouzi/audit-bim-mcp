"""Le pack résout lui-même ses contrats géométriques (mode « self-healing »).

Le tool ne doit pas dépendre d'une consigne : si les ``BaseQuantities``
manquent, il retrouve ou calcule le contrat
``computed_base_quantities/v1``, le fusionne, puis génère. Idem pour
``envelope_quantities/v1``.

Le calcul est appelé comme une **fonction Python** (``geometry_backend``), pas
via un second serveur MCP : un import est déterministe, testable, et ne dépend
pas de ce que le harnais a énuméré au démarrage. Ces tests substituent donc le
backend — sans avoir besoin d'ifcopenshell.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from audit_bim.extraction.geometry_backend import GeometryBackendUnavailable
from audit_bim.extraction.model_data import ModelSnapshot
from audit_bim.mcp.session import _Session, current_session
from audit_bim.profiles.i3f.tools_reporting import generate_avp_i3f_pack as tr_generate_avp_i3f_pack
from audit_bim.reporting import avp_autocompute


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("AUDIT_INPUT_DIR", str(tmp_path))
    sess = _Session()
    sess.snapshot = _snapshot_sans_quantites()
    token = current_session.set(sess)
    try:
        yield sess, tmp_path
    finally:
        current_session.reset(token)


def _snapshot_sans_quantites() -> ModelSnapshot:
    return ModelSnapshot(
        project={"name": "Dieppe"},
        model={"name": "DIEPPE-7427L.ifc"},
        spaces=[
            {"uuid": "SP1", "type": "IfcSpace", "name": "SEJOUR", "longname": "SEJOUR"},
            {"uuid": "SP2", "type": "IfcSpace", "name": "CHAMBRE", "longname": "CHAMBRE"},
        ],
        elements=[
            {"uuid": "W1", "type": "IfcWindow", "name": "F25"},
            {"uuid": "SL1", "type": "IfcSlab", "name": "Dalle"},
        ],
    ).index()


def _payload(valeur_espace=24.5, ifc_file="DIEPPE-7427L.ifc"):
    return {
        "schema": "computed_base_quantities/v1",
        # ``ifc_file`` est toujours renseigné par le vrai producteur : c'est ce
        # qui permet de vérifier qu'un contrat porte bien sur le modèle actif.
        "source": {
            "producer": "ifc-geometry",
            "tool": "export_computed_base_quantities",
            "ifc_file": ifc_file,
        },
        "created_at": "2026-08-02T08:00:00+00:00",
        "quantities": [
            _q("SP1", "IfcSpace", "Qto_SpaceBaseQuantities", "NetFloorArea", valeur_espace),
            _q("SP2", "IfcSpace", "Qto_SpaceBaseQuantities", "NetFloorArea", 12.98),
            _q("W1", "IfcWindow", "Qto_WindowBaseQuantities", "Width", 0.6),
            _q("W1", "IfcWindow", "Qto_WindowBaseQuantities", "Height", 1.3),
            _q("SL1", "IfcSlab", "Qto_SlabBaseQuantities", "NetArea", 156.4),
        ],
        "coverage": {"n_elements": 4, "n_computed": 5, "n_failed": 0},
        "warnings": [],
    }


def _q(gid, cls, qto, name, value):
    return {
        "global_id": gid,
        "ifc_class": cls,
        "qto": qto,
        "quantity": name,
        "value": value,
        "unit": "m2" if "Area" in name else "m",
        "method": "ifcopenshell_geometry",
        "status": "computed",
        "source": "computed_ifcopenshell",
    }


@pytest.fixture
def ifc_disponible(session):
    """Un .ifc du modèle actif, présent dans le dossier d'entrée."""
    _sess, tmp_path = session
    fichier = tmp_path / "DIEPPE-7427L.ifc"
    fichier.write_text("ISO-10303-21;", encoding="utf-8")
    return fichier


@pytest.fixture
def backend(monkeypatch):
    """Substitue le calcul géométrique et compte les appels réels."""
    appels = {"quantites": 0, "enveloppe": 0, "valeur": 24.5}

    def _quantites(ifc_path):
        appels["quantites"] += 1
        return _payload(appels["valeur"], ifc_file=str(ifc_path))

    def _enveloppe(ifc_path, **kw):
        appels["enveloppe"] += 1
        return {
            "schema": "envelope_quantities/v1",
            "source": {"producer": "ifc-geometry", "ifc_file": str(ifc_path)},
            "created_at": "2026-08-02T08:00:00+00:00",
            "summary": {"superficie_facades_m2": 2071.18, "shab_m2": 2164.68},
            "par_type": [
                {"type": "ME_36", "etages": ["RDC"], "net_side_area_m2": 2071.18, "n": 24}
            ],
            "hors_filtre_type": [],
            "diagnostics": {},
        }

    monkeypatch.setattr(avp_autocompute, "compute_quantities_payload", _quantites)
    monkeypatch.setattr(avp_autocompute, "compute_envelope_payload", _enveloppe)
    return appels


def _generer(**kw):
    return tr_generate_avp_i3f_pack(
        project_name="Dieppe",
        project_code="7427L",
        phase="APD",
        auditor_name="Stanislas Limouzi",
        export_pdf=False,
        **kw,
    )


def _nombres(path):
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    vals = [
        c
        for ws in wb.worksheets
        for row in ws.iter_rows(values_only=True)
        for c in row
        if isinstance(c, (int, float)) and not isinstance(c, bool)
    ]
    wb.close()
    return vals


def _annexe(res, libelle):
    return next(p for p in res["paths"] if libelle in Path(p).name)


# ── cas nominal : aucune consigne, le pack se soigne tout seul ──────────


def test_pack_computes_quantities_by_default(session, ifc_disponible, backend):
    """L'API cible : ni ``computed_quantities_json`` ni étape préalable."""
    res = _generer()

    assert res.get("status") not in ("error", "needs_context"), res
    assert backend["quantites"] == 1
    auto = res["auto_computed"]["quantities"]
    assert auto["computed"] is True
    assert auto["reused"] is False
    assert Path(auto["json_path"]).is_file()


@pytest.mark.parametrize(
    ("libelle", "attendue"),
    [
        ("export SHAB maquette", 24.5),
        ("Export Zones et Espaces", 12.98),
        ("export Menuiseries", 0.6),
        ("export plancher", 156.4),
    ],
)
def test_annexes_are_filled_after_autocompute(session, ifc_disponible, backend, libelle, attendue):
    res = _generer()
    assert res.get("status") not in ("error", "needs_context"), res

    nombres = _nombres(_annexe(res, libelle))
    assert any(abs(n - attendue) < 0.01 for n in nombres), (
        f"{attendue} absente de « {libelle} » (valeurs : {sorted(set(nombres))[:10]})"
    )


# ── réutilisation vs recalcul ──────────────────────────────────────────


def test_existing_contract_is_reused_without_recomputing(session, ifc_disponible, backend):
    premier = _generer()
    assert premier.get("status") not in ("error", "needs_context"), premier
    assert backend["quantites"] == 1

    second = _generer()
    assert second.get("status") not in ("error", "needs_context"), second
    assert backend["quantites"] == 1, "un contrat déjà calculé ne doit pas être recalculé"
    assert second["auto_computed"]["quantities"]["reused"] is True


def test_force_recompute_replaces_the_contract(session, ifc_disponible, backend):
    premier = _generer()
    chemin = Path(premier["auto_computed"]["quantities"]["json_path"])
    assert backend["quantites"] == 1

    backend["valeur"] = 99.9  # la maquette a changé
    second = _generer(force_recompute_quantities=True)

    assert backend["quantites"] == 2
    assert second["auto_computed"]["quantities"]["reused"] is False
    doc = json.loads(chemin.read_text(encoding="utf-8"))
    valeurs = [q["value"] for q in doc["quantities"] if q["global_id"] == "SP1"]
    assert valeurs == [99.9], "le contrat doit être remplacé, pas conservé"

    nombres = _nombres(_annexe(second, "export SHAB maquette"))
    assert any(abs(n - 99.9) < 0.01 for n in nombres)


# ── impossibilité : demande CIBLÉE, jamais vague ───────────────────────


def test_missing_ifc_asks_for_ifc_path(session, backend):
    """Aucun .ifc : la question porte sur ``ifc_path``, pas sur « les quantités »."""
    res = _generer()

    assert res["status"] == "needs_context"
    assert res["error"] == "cannot_compute_quantities"
    assert res["missing"] == ["ifc_path"]
    assert "download_model_ifc" in res["message"]
    assert backend["quantites"] == 0


def test_missing_backend_names_the_backend(session, ifc_disponible, monkeypatch):
    """Backend non installé : le message nomme le paquet et l'extra."""

    def _absent(*_a, **_k):
        raise GeometryBackendUnavailable()

    monkeypatch.setattr(avp_autocompute, "compute_quantities_payload", _absent)

    res = _generer()

    assert res["status"] == "needs_context"
    assert res["missing"] == ["geometry_backend"]
    assert "ifc-geometry-mcp" in res["message"]


def test_no_deliverable_written_when_autocompute_fails(session, tmp_path, backend):
    """Un échec d'auto-résolution ne laisse aucun livrable derrière lui."""
    res = _generer(output_dir="pack_echec")

    assert res["status"] == "needs_context"
    livrables = list(Path(tmp_path).rglob("*.xlsx")) + list(Path(tmp_path).rglob("*.docx"))
    assert livrables == [], f"livrables écrits malgré l'échec : {livrables}"


def test_autocompute_can_be_disabled(session, ifc_disponible, backend):
    """``auto_compute_quantities=False`` restaure le refus explicite."""
    res = _generer(auto_compute_quantities=False, auto_compute_envelope=False)

    assert res["status"] == "error"
    assert res["error"] == "missing_quantities"
    assert backend["quantites"] == 0


# ── enveloppe ──────────────────────────────────────────────────────────


def test_envelope_is_not_computed_when_not_expected(session, ifc_disponible, backend):
    """Sans mur d'enveloppe dans la maquette, aucun calcul n'est lancé."""
    res = _generer()

    assert res.get("status") not in ("error", "needs_context"), res
    assert backend["enveloppe"] == 0
    assert res["auto_computed"]["envelope"] is None


def test_envelope_is_computed_when_walls_are_present(session, ifc_disponible, backend):
    sess, _ = session
    snap = sess.snapshot
    snap.elements.append(
        {
            "uuid": "M1",
            "type": "IfcWall",
            "name": "Mur",
            "layers": [{"name": "221 - MURS - Extérieurs périphériques.Exndo"}],
        }
    )
    sess.snapshot = snap.index()

    res = _generer()

    assert res.get("status") not in ("error", "needs_context"), res
    assert backend["enveloppe"] == 1
    assert res["auto_computed"]["envelope"]["computed"] is True


def test_empty_envelope_decomposition_is_an_explicit_error(session, ifc_disponible, monkeypatch):
    """Aucun type retenu → erreur nommant les motifs, pas une annexe vide."""
    sess, _ = session
    snap = sess.snapshot
    snap.elements.append(
        {
            "uuid": "M1",
            "type": "IfcWall",
            "name": "Mur",
            "layers": [{"name": "221 - MURS - Extérieurs périphériques.Exndo"}],
        }
    )
    sess.snapshot = snap.index()

    monkeypatch.setattr(
        avp_autocompute,
        "compute_envelope_payload",
        lambda *_a, **_k: {
            "schema": "envelope_quantities/v1",
            "summary": {"superficie_facades_m2": 0.0, "shab_m2": 0.0},
            "par_type": [],
            "hors_filtre_type": [],
        },
    )
    monkeypatch.setattr(avp_autocompute, "compute_quantities_payload", lambda *_a, **_k: _payload())

    res = _generer()

    assert res["status"] == "needs_context"
    assert res["error"] == "cannot_compute_envelope"
    assert res["missing"] == ["envelope_layer_pattern"]
    assert "^ME[ _]" in res["message"]


# ── orchestrateur, testé directement ───────────────────────────────────


def test_ensure_reuses_then_recomputes(session, ifc_disponible, backend):
    sess, _ = session
    premier = avp_autocompute.ensure_computed_quantities_json(sess.snapshot)
    assert premier["computed"] is True

    second = avp_autocompute.ensure_computed_quantities_json(sess.snapshot)
    assert second["reused"] is True
    assert backend["quantites"] == 1

    force = avp_autocompute.ensure_computed_quantities_json(sess.snapshot, force=True)
    assert force["computed"] is True
    assert backend["quantites"] == 2


def test_contract_is_written_under_the_export_sandbox(session, ifc_disponible, backend):
    sess, tmp_path = session
    res = avp_autocompute.ensure_computed_quantities_json(sess.snapshot)

    chemin = Path(res["json_path"])
    assert chemin.is_file()
    assert avp_autocompute.CONTRACTS_SUBDIR in chemin.parts
    assert str(chemin).startswith(str(tmp_path)), "le contrat doit rester sous AUDIT_OUTPUT_DIR"


# ── garde-fous : sandbox, ambiguïté, appartenance au modèle ────────────


def test_ifc_path_outside_the_sandbox_is_refused(session, tmp_path, backend):
    """Un ``.ifc`` hors des racines autorisées est refusé, jamais lu."""
    dehors = tmp_path.parent / "hors_sandbox.ifc"
    dehors.write_text("ISO-10303-21;", encoding="utf-8")

    res = _generer(ifc_path=str(dehors))

    assert res["status"] == "needs_context"
    assert res["missing"] == ["ifc_path"]
    assert "sandbox" in res["message"].lower()
    assert backend["quantites"] == 0, "aucun calcul ne doit être lancé"


def test_two_ifc_with_the_same_stem_are_refused(session, tmp_path, backend):
    """Deux maquettes homonymes → refus, jamais un choix arbitraire."""
    sous_dossier = tmp_path / "autre"
    sous_dossier.mkdir()
    (tmp_path / "DIEPPE-7427L.ifc").write_text("ISO-10303-21;", encoding="utf-8")
    (sous_dossier / "DIEPPE-7427L.ifc").write_text("ISO-10303-21;", encoding="utf-8")

    res = _generer()

    assert res["status"] == "needs_context"
    assert res["missing"] == ["ifc_path"]
    assert "Plusieurs fichiers .ifc" in res["message"]
    assert backend["quantites"] == 0


def test_contract_from_another_model_is_not_reused(session, ifc_disponible, backend):
    """Un contrat d'une AUTRE maquette n'est jamais réutilisé."""
    sess, tmp_path = session
    etranger = avp_autocompute.contracts_dir() / "DIEPPE-7427L_computed_quantities.json"
    etranger.write_text(json.dumps(_payload(ifc_file="UN-AUTRE-CHANTIER.ifc")), encoding="utf-8")

    res = _generer()

    assert res.get("status") not in ("error", "needs_context"), res
    assert backend["quantites"] == 1, "le contrat étranger doit être recalculé"
    assert res["auto_computed"]["quantities"]["reused"] is False


def test_contract_without_provenance_is_not_reused(session, ifc_disponible, backend):
    """Provenance inconnue = on ne parie pas, on recalcule."""
    doc = _payload()
    doc["source"].pop("ifc_file")
    (avp_autocompute.contracts_dir() / "DIEPPE-7427L_computed_quantities.json").write_text(
        json.dumps(doc), encoding="utf-8"
    )

    res = _generer()

    assert res.get("status") not in ("error", "needs_context"), res
    assert backend["quantites"] == 1
    assert res["auto_computed"]["quantities"]["reused"] is False


def test_foreign_envelope_contract_is_not_reused(session, ifc_disponible, backend):
    """Idem pour l'enveloppe : un envelope.json d'un autre modèle est ignoré."""
    sess, _ = session
    snap = sess.snapshot
    snap.elements.append(
        {
            "uuid": "M1",
            "type": "IfcWall",
            "name": "Mur",
            "layers": [{"name": "221 - MURS - Extérieurs périphériques.Exndo"}],
        }
    )
    sess.snapshot = snap.index()
    (avp_autocompute.contracts_dir() / "DIEPPE-7427L_envelope.json").write_text(
        json.dumps(
            {
                "schema": "envelope_quantities/v1",
                "source": {"ifc_file": "UN-AUTRE-CHANTIER.ifc"},
                "summary": {"superficie_facades_m2": 1.0, "shab_m2": 1.0},
                "par_type": [{"type": "X", "etages": [], "net_side_area_m2": 1.0, "n": 1}],
                "hors_filtre_type": [],
            }
        ),
        encoding="utf-8",
    )

    res = _generer()

    assert res.get("status") not in ("error", "needs_context"), res
    assert backend["enveloppe"] == 1, "l'enveloppe étrangère doit être recalculée"


def test_explicit_patterns_trigger_envelope_even_without_layer(session, ifc_disponible, backend):
    """Motifs explicites = demande explicite.

    Le cas réel : BIMData ne remonte pas le calque, mais IfcOpenShell sait le
    lire dans l'IFC. S'en tenir au snapshot ferait rater l'enveloppe sur les
    maquettes qui en ont justement besoin.
    """
    sess, _ = session
    assert not any(el.get("layers") for el in sess.snapshot.elements), (
        "le snapshot de test ne porte aucun calque"
    )

    res = _generer(
        envelope_layer_pattern="221|extérieurs périphériques",
        envelope_type_pattern="^ME[ _]",
    )

    assert res.get("status") not in ("error", "needs_context"), res
    assert backend["enveloppe"] == 1
    assert res["auto_computed"]["envelope"]["computed"] is True


# ── corrélation au modèle actif : disponible ≠ pertinent ───────────────


def test_unrelated_single_ifc_is_never_used(session, tmp_path, backend):
    """Snapshot Dieppe + un seul .ifc, celui d'un AUTRE chantier → refus.

    « Le seul fichier du dossier » n'est pas un repli : calculer dessus
    livrerait les surfaces d'un autre bâtiment, sans aucun signal.
    """
    (tmp_path / "250613_MN_BAT.ifc").write_text("ISO-10303-21;", encoding="utf-8")

    res = _generer()

    assert res["status"] == "needs_context"
    assert res["missing"] == ["ifc_path"]
    assert backend["quantites"] == 0, "aucun calcul sur une maquette non corrélée"


def test_unrelated_envelope_json_is_ignored_and_recomputed(session, tmp_path, backend):
    """Un ``*_envelope.json`` d'un autre modèle n'est pas repris."""
    ifc = tmp_path / "DIEPPE-7427L.ifc"
    ifc.write_text("ISO-10303-21;", encoding="utf-8")
    etranger = tmp_path / "250613_MN_BAT_envelope.json"
    etranger.write_text(
        json.dumps(
            {
                "schema": "envelope_quantities/v1",
                "source": {"ifc_file": "250613_MN_BAT.ifc"},
                "summary": {"superficie_facades_m2": 1.0, "shab_m2": 1.0},
                "par_type": [{"type": "X", "etages": [], "net_side_area_m2": 1.0, "n": 1}],
                "hors_filtre_type": [],
            }
        ),
        encoding="utf-8",
    )

    res = _generer(
        envelope_layer_pattern="221|extérieurs périphériques",
        envelope_type_pattern="^ME[ _]",
    )

    assert res.get("status") not in ("error", "needs_context"), res
    assert backend["enveloppe"] == 1, "l'enveloppe étrangère doit être recalculée"
    assert "250613_MN_BAT" not in str(res["envelope_json_used"])


def test_downloaded_ifc_is_used_even_with_a_cache_name(session, tmp_path, backend):
    """Le .ifc de ``download_model_ifc`` porte un nom de CACHE, pas le nom métier.

    Il est mémorisé en session : c'est la corrélation la plus sûre, et elle ne
    dépend d'aucune convention de nommage.
    """
    sess, _ = session
    # `download_model_ifc` suppose `set_active_model` : la session porte donc
    # toujours les identifiants qui permettent de rattacher le cache à la cible.
    sess.cloud_id, sess.project_id, sess.model_id = "34140", "3281472", "1744293"
    cache = tmp_path / "ifc"
    cache.mkdir()
    fichier = cache / "34140_3281472_1744293_2026-08-02.ifc"
    fichier.write_text("ISO-10303-21;", encoding="utf-8")
    sess.ifc_path = str(fichier)

    res = _generer()

    assert res.get("status") not in ("error", "needs_context"), res
    assert backend["quantites"] == 1


def test_bimdata_cache_is_matched_by_ids(session, tmp_path, backend):
    """Sans chemin en session, le cache est retrouvé par ses identifiants."""
    sess, _ = session
    sess.cloud_id, sess.project_id, sess.model_id = "34140", "3281472", "1744293"
    cache = tmp_path / "ifc"
    cache.mkdir()
    (cache / "34140_3281472_1744293_2026-08-02.ifc").write_text("ISO-10303-21;", encoding="utf-8")

    res = _generer()

    assert res.get("status") not in ("error", "needs_context"), res
    assert backend["quantites"] == 1


def test_explicit_envelope_json_is_accepted_and_validated(session, tmp_path, backend):
    """L'utilisateur peut forcer un envelope.json : sandboxé et schéma validé."""
    ifc = tmp_path / "DIEPPE-7427L.ifc"
    ifc.write_text("ISO-10303-21;", encoding="utf-8")
    fourni = tmp_path / "mon_envelope.json"
    fourni.write_text(
        json.dumps(
            {
                "schema": "envelope_quantities/v1",
                "source": {"ifc_file": "DIEPPE-7427L.ifc"},
                "summary": {"superficie_facades_m2": 2071.18, "shab_m2": 2164.68},
                "par_type": [
                    {"type": "ME_36", "etages": ["RDC"], "net_side_area_m2": 2071.18, "n": 24}
                ],
                "hors_filtre_type": [],
            }
        ),
        encoding="utf-8",
    )

    res = _generer(envelope_json=str(fourni))

    assert res.get("status") not in ("error", "needs_context"), res
    assert backend["enveloppe"] == 0, "un fichier fourni n'est pas recalculé"
    assert res["envelope_json_used"].endswith("mon_envelope.json")


def test_explicit_envelope_json_with_unknown_schema_is_refused(session, tmp_path, backend):
    ifc = tmp_path / "DIEPPE-7427L.ifc"
    ifc.write_text("ISO-10303-21;", encoding="utf-8")
    mauvais = tmp_path / "mauvais_envelope.json"
    mauvais.write_text(json.dumps({"schema": "autre/v9"}), encoding="utf-8")

    with pytest.raises(ValueError, match="Schéma non reconnu"):
        _generer(envelope_json=str(mauvais))


# ── changement de modèle : le .ifc de l'ancienne cible ne doit pas suivre ──


def test_stale_session_ifc_is_not_used_after_model_change(session, tmp_path, backend):
    """Télécharger MN_BAT, basculer sur Dieppe, générer : jamais MN_BAT.

    C'est le scénario réel : ``download_model_ifc`` mémorise un chemin, puis
    ``set_active_model`` change de cible. Sans recroisement, le calcul repartait
    sur la maquette précédente et livrait les surfaces d'un autre bâtiment.
    """
    sess, _ = session
    ancien = tmp_path / "250613_MN_BAT.ifc"
    ancien.write_text("ISO-10303-21;", encoding="utf-8")
    sess.ifc_path = str(ancien)  # hérité de la cible précédente

    res = _generer()  # le snapshot de session est Dieppe

    assert res["status"] == "needs_context"
    assert res["missing"] == ["ifc_path"]
    assert backend["quantites"] == 0, "aucun calcul sur l'IFC de l'ancienne cible"


def test_set_active_model_clears_the_memorised_ifc(session, monkeypatch):
    """``set_active_model`` invalide le chemin mémorisé, comme les autres caches."""
    from audit_bim.profiles.i3f import tools_session

    sess, _ = session
    sess.ifc_path = "/chemin/vers/250613_MN_BAT.ifc"
    monkeypatch.setattr(tools_session, "BIMDataClient", lambda **_kw: object())

    tools_session.set_active_model(cloud_id="34140", project_id="3281472", model_id="1744293")

    assert sess.ifc_path is None
    assert sess.snapshot is None  # cohérent avec les autres invalidations


def test_contract_from_a_bimdata_cache_ifc_is_reused(session, tmp_path, backend):
    """P2 : un contrat calculé depuis un .ifc de CACHE reste réutilisable.

    Sa provenance porte le nom d'identifiants BIMData, pas le nom métier :
    sans reconnaissance de cette forme, chaque génération recalculait.
    """
    sess, _ = session
    sess.cloud_id, sess.project_id, sess.model_id = "34140", "3281472", "1744293"
    cache = tmp_path / "ifc"
    cache.mkdir()
    fichier = cache / "34140_3281472_1744293_2026-08-02.ifc"
    fichier.write_text("ISO-10303-21;", encoding="utf-8")
    sess.ifc_path = str(fichier)

    premier = _generer()
    assert premier.get("status") not in ("error", "needs_context"), premier
    assert backend["quantites"] == 1

    second = _generer()
    assert second.get("status") not in ("error", "needs_context"), second
    assert backend["quantites"] == 1, "le contrat du cache BIMData doit être réutilisé"
    assert second["auto_computed"]["quantities"]["reused"] is True


# ── plafond de taille : une maquette n'est pas un classeur ─────────────


def test_large_ifc_is_accepted(session, tmp_path, backend, monkeypatch):
    """Une maquette dépassant le plafond des DOCUMENTS reste lisible.

    ``safe_input_path`` applique ``AUDIT_MAX_INPUT_MB`` (50 Mo), calibré pour
    des classeurs et des PDF. Les maquettes réelles le dépassent largement — la
    maquette de référence pèse 167 Mo — et ``download_model_ifc`` en accepte
    jusqu'à 500 Mo. Appliquer le plafond des documents refuserait tous les
    modèles de production ; c'est ce qui se produisait.
    """
    monkeypatch.setenv("AUDIT_MAX_INPUT_MB", "1")  # plafond documents très bas
    monkeypatch.setenv("AUDIT_MAX_IFC_MB", "500")
    gros = tmp_path / "DIEPPE-7427L.ifc"
    gros.write_bytes(b"0" * (2 * 1024 * 1024))  # 2 Mo > plafond documents

    res = _generer(ifc_path=str(gros))

    assert res.get("status") not in ("error", "needs_context"), res
    assert backend["quantites"] == 1


def test_ifc_beyond_its_own_cap_is_refused(session, tmp_path, backend, monkeypatch):
    """Le plafond spécifique aux maquettes, lui, s'applique bien."""
    monkeypatch.setenv("AUDIT_MAX_INPUT_MB", "1")
    monkeypatch.setenv("AUDIT_MAX_IFC_MB", "1")
    gros = tmp_path / "DIEPPE-7427L.ifc"
    gros.write_bytes(b"0" * (2 * 1024 * 1024))

    res = _generer(ifc_path=str(gros))

    assert res["status"] == "needs_context"
    assert "AUDIT_MAX_IFC_MB" in res["message"]
    assert backend["quantites"] == 0


def test_path_traversal_is_refused_even_for_large_ifc(session, tmp_path, backend, monkeypatch):
    """Le repli « gros fichier » ne rouvre pas la porte à une traversée."""
    monkeypatch.setenv("AUDIT_MAX_INPUT_MB", "1")
    gros = tmp_path / "DIEPPE-7427L.ifc"
    gros.write_bytes(b"0" * (2 * 1024 * 1024))

    res = _generer(ifc_path=f"{tmp_path}/../{tmp_path.name}/DIEPPE-7427L.ifc")

    assert res["status"] == "needs_context"
    assert backend["quantites"] == 0


# ── Provenance des contrats fournis explicitement ────────────────────────
# Le trou fermé ici : les contrats auto-résolus étaient corrélés au modèle
# actif, mais un chemin passé à la main entrait sans aucun contrôle de cible.
# Un contrat calculé sur une autre maquette produisait alors des surfaces
# étrangères sous le nom du projet courant — la classe d'erreur que
# ``verify_active_model`` ferme du côté de la cible.


def test_explicit_envelope_json_from_another_model_is_refused(session, tmp_path, backend):
    _sess, _ = session
    etranger = tmp_path / "etranger_envelope.json"
    etranger.write_text(
        json.dumps(
            {
                "schema": "envelope_quantities/v1",
                "source": {"producer": "ifc-geometry", "ifc_file": "250613_MN_BAT.ifc"},
                "created_at": "2026-08-02T08:00:00+00:00",
                "summary": {"superficie_facades_m2": 2071.18, "shab_m2": 2164.68},
                "par_type": [
                    {"type": "ME_36", "etages": ["RDC"], "net_side_area_m2": 2071.18, "n": 24}
                ],
                "hors_filtre_type": [],
                "diagnostics": {},
            }
        ),
        encoding="utf-8",
    )

    res = _generer(envelope_json=str(etranger))

    assert res["status"] == "error"
    assert res["error"] == "contract_model_mismatch"
    assert res["parametre"] == "envelope_json"
    assert res["contract_source_ifc_file"] == "250613_MN_BAT.ifc"
    assert "250613_MN_BAT.ifc" in res["message"]


def test_explicit_computed_quantities_from_another_model_is_refused(session, tmp_path, backend):
    etranger = tmp_path / "etranger_quantities.json"
    etranger.write_text(json.dumps(_payload(ifc_file="250613_MN_BAT.ifc")), encoding="utf-8")

    res = _generer(computed_quantities_json=str(etranger))

    assert res["status"] == "error"
    assert res["error"] == "contract_model_mismatch"
    assert res["parametre"] == "computed_quantities_json"
    assert res["contract_source_ifc_file"] == "250613_MN_BAT.ifc"


def test_explicit_contract_named_after_bimdata_cache_is_accepted(session, tmp_path, backend):
    """Le nom de cache ``<cloud>_<projet>_<modele>_`` reste une provenance valide.

    ``download_model_ifc`` ne nomme pas le fichier d'après le modèle BIMData :
    exiger le nom métier rejetterait tout contrat calculé depuis la maquette
    téléchargée — c'est-à-dire le cas nominal.
    """
    sess, _ = session
    sess.cloud_id, sess.project_id, sess.model_id = "34140", "3281472", "1744246"
    cache = tmp_path / "34140_3281472_1744246_nodate_computed_quantities.json"
    cache.write_text(
        json.dumps(_payload(ifc_file="34140_3281472_1744246_nodate.ifc")), encoding="utf-8"
    )

    res = _generer(computed_quantities_json=str(cache))

    assert res.get("error") != "contract_model_mismatch", res
    assert res["computed_source_ifc_file"] == "34140_3281472_1744246_nodate.ifc"


def test_pack_reports_active_target_for_traceability(session, ifc_disponible, backend):
    """Le retour dit sur quelle cible et quel .ifc le pack a réellement été bâti."""
    sess, _ = session
    sess.cloud_id, sess.project_id, sess.model_id = "34140", "3281472", "1744246"

    res = _generer()

    assert res.get("status") not in ("error", "needs_context"), res
    assert res["active_cloud_id"] == "34140"
    assert res["active_project_id"] == "3281472"
    assert res["active_model_id"] == "1744246"
    assert res["computed_source_ifc_file"].endswith("DIEPPE-7427L.ifc")
