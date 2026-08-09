"""Fusion des BaseQuantities **calculées** (MCP ifc-geometry) dans le snapshot.

Consomme le contrat JSON ``computed_base_quantities/v1`` produit par
``export_computed_base_quantities`` (MCP ifc-geometry) et fusionne les valeurs
dans le snapshot BIMData courant, en **gap-only** :

- **jointure** par ``BimObject.uuid == global_id`` (GlobalId IFC) ;
- **jamais d'écrasement** : une BaseQuantity déjà présente (native BIMData) est
  conservée telle quelle ; on ne comble que les vides ;
- entrées ``status != "computed"`` (skipped / failed) **ignorées** ;
- ``global_id`` inconnu du snapshot → **ignoré avec warning** ;
- **provenance par valeur** conservée sur l'élément (``computed_base_quantities``)
  : ``source="computed_ifcopenshell"``, ``method``, ``unit``, ``status``.

Deux traces distinctes, à ne pas confondre :

- ``computed_base_quantities`` — les quantités **effectivement fusionnées**.
  Elle répond à « cette BaseQuantity du pset vient-elle d'un calcul ? », et
  c'est elle qui décide dans quelle colonne un livrable écrit sa valeur ;
- ``computed_comparison_quantities`` — **toutes** les quantités calculées,
  fusionnées ou non. Elle répond à « que vaut le calcul IFC OpenShell pour cet
  élément ? », indépendamment de ce que porte la maquette.

La seconde existe parce que *gap-only* décide quelle valeur fait **autorité**,
pas laquelle mérite d'être **retenue**. Jeter la valeur calculée dès qu'une
native existait rendait toute comparaison impossible par construction : sur une
maquette portant ses BaseQuantities — le cas courant — les colonnes « IFC
OpenShell » des livrables sortaient vides, et les colonnes d'écart avec elles.

La valeur calculée est injectée dans les ``property_sets`` de l'élément (pset
``Qto_*BaseQuantities``) → lue **à l'identique** par les builders AVP
(``_base_quantity_ordered``) et par ``bim_object_from_element``
(``_extract_base_quantities``), sans distinction de source côté lecture.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from bim_core.contracts import (
    SCHEMA_COMPUTED_BASE_QUANTITIES_V1,
    SOURCE_COMPUTED,
    load_computed_base_quantities,
)

EXPORT_SCHEMA = SCHEMA_COMPUTED_BASE_QUANTITIES_V1

# Préfixes de pset reconnus comme BaseQuantities (aligné bim_query / avp).
_BQ_PREFIXES = ("basequantities", "qto_", "quantit")


def load_computed_quantities(json_path: str | Path) -> dict[str, Any]:
    """Charge et **valide** le contrat ``computed_base_quantities/v1``.

    La validation est déléguée à
    :func:`bim_core.contracts.load_computed_base_quantities` — politique de
    schéma commune à tous les MCP : document V1 accepté, schéma inconnu ou
    invalide **refusé**, fichier historique sans ``schema`` migré vers V1 avec
    l'avertissement ``legacy_schema_missing``.

    Renvoie le **document** (dict) : la fusion en aval lit ``quantities`` telle
    quelle, indépendamment de la source du fichier.

    Raises:
        ContractError: fichier absent, illisible, schéma inconnu/invalide ou
            forme non reconnue. Sous-classe de ``ValueError`` — les appelants
            qui attrapaient ``ValueError`` restent compatibles.
    """
    return load_computed_base_quantities(str(json_path)).to_document()


def json_digest(json_path: str | Path) -> str:
    """Empreinte courte (sha256 tronqué) du contenu du JSON — pour la clé de cache."""
    return hashlib.sha256(Path(json_path).read_bytes()).hexdigest()[:16]


def _has_quantity(element: dict, qty_name: str) -> bool:
    """Vrai si l'élément porte déjà cette BaseQuantity (valeur numérique)."""
    target = (qty_name or "").lower()
    for pset in element.get("property_sets") or []:
        pname = (pset.get("name") or "").lower()
        if not pname.startswith(_BQ_PREFIXES):
            continue
        for prop in pset.get("properties") or []:
            pn = ((prop.get("definition") or {}).get("name") or "").lower()
            val = prop.get("value")
            if pn == target and isinstance(val, (int, float)) and not isinstance(val, bool):
                return True
    return False


def _inject(element: dict, qto_name: str, qty_name: str, value: float) -> None:
    """Ajoute la quantité dans un pset BaseQuantities (du même ``qto_name`` si
    présent, sinon nouveau) — forme lue par les builders et bim_object."""
    psets = element.setdefault("property_sets", [])
    pset = next((p for p in psets if (p.get("name") or "") == qto_name), None)
    if pset is None:
        pset = {"name": qto_name, "properties": []}
        psets.append(pset)
    pset.setdefault("properties", []).append(
        {"definition": {"name": qty_name}, "value": float(value)}
    )


def merge_into_snapshot(snapshot, doc: dict[str, Any]) -> dict[str, Any]:
    """Fusionne (gap-only) les quantités calculées de ``doc`` dans ``snapshot``.

    Mute les éléments **indexés par uuid** (ceux que lisent ``_rich`` côté AVP et
    ``get_object_detail``). Renvoie une **couverture** sérialisable.
    """
    index = snapshot.element_by_uuid or {}
    n_merged = n_gap_kept = n_skipped_status = n_unknown = 0
    warnings: list[str] = []

    for q in doc.get("quantities") or []:
        if q.get("status") != "computed" or q.get("value") is None:
            n_skipped_status += 1
            continue
        gid = q.get("global_id")
        element = index.get(gid)
        if element is None:
            n_unknown += 1
            if len(warnings) < 50:
                warnings.append(f"global_id inconnu dans le snapshot, ignoré : {gid}")
            continue
        qty = q.get("quantity")
        qto = q.get("qto") or "Qto_BaseQuantities"
        trace = {
            "quantity": qty,
            "qto": qto,
            "value": float(q["value"]),
            "unit": q.get("unit"),
            "method": q.get("method"),
            "status": q.get("status"),
            "source": q.get("source") or SOURCE_COMPUTED,
        }
        # La valeur calculée est TOUJOURS conservée comme donnée de comparaison,
        # qu'elle ait été fusionnée ou écartée. C'est ce qui rend une vraie
        # comparaison possible : jusqu'ici, un ``continue`` la jetait dès qu'une
        # native existait, si bien que les colonnes « IFC OpenShell » des
        # livrables étaient vides par construction sur toute maquette portant
        # ses BaseQuantities.
        element.setdefault("computed_comparison_quantities", []).append(dict(trace))
        if _has_quantity(element, qty):
            n_gap_kept += 1  # valeur BIMData existante → jamais écrasée
            continue
        _inject(element, qto, qty, q["value"])
        # ``computed_base_quantities`` reste la trace de la FUSION : elle dit
        # « cette BaseQuantity du pset vient d'un calcul ». Les provenances
        # livrées en #210/#211/#212 la lisent — l'élargir ferait passer pour
        # calculées des quantités natives.
        element.setdefault("computed_base_quantities", []).append(trace)
        n_merged += 1

    return {
        "n_merged": n_merged,
        "n_gap_kept": n_gap_kept,
        "n_skipped_status": n_skipped_status,
        "n_unknown_uuid": n_unknown,
        "warnings": warnings,
    }
