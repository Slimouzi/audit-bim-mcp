"""Extraction AVP depuis la maquette IFC.

Les exports SHAB, Zones/Espaces, Enveloppe, Menuiseries et Plancher sont
construits à partir du ``ModelSnapshot`` de l'``AuditResult``. Les surfaces
proviennent des quantités IFC extraites de la maquette, ou de valeurs calculées
équivalentes exposées dans les propriétés de snapshot.

Principes :

- **Jamais inventer** : une surface introuvable reste ``None`` (rendue
  ``NOT_AVAILABLE``) ; la **méthode IFC/OpenShell** de chaque valeur est tracée
  dans une colonne dédiée.
- **Tolérance** casse / accents / espaces sur les layers et les noms de
  propriétés (normalisation ``_norm``).
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any

from ..extraction.model_data import ModelSnapshot
from .avp_sources import (
    ENVELOPPE_MOA_HEADERS,
    ENVELOPPE_MOA_SHEET,
    AvpSources,
    EnveloppeSource,
    MenuiseriesSource,
    MultiSheetSource,
    SheetGrid,
    SheetTable,
)
from .word_report import NOT_AVAILABLE

# ── Sélection de l'enveloppe ────────────────────────────────────────────────
#
# Murs d'enveloppe : sélection **par layer** (critère autoritaire I3F).
# Classes retenues : IfcWall + IfcWallStandardCase.
#
# Décision explicite : **IfcCurtainWall EXCLU**. Les murs-rideaux sont des
# façades vitrées comptées en *menuiseries* (surface des ouvertures), pas en
# surface de murs opaques d'enveloppe ; par ailleurs le layer cible
# « MURS - Extérieurs périphériques.Exnd » ne les porte pas. Pour l'inclure
# un jour, ajouter la classe ici et adapter la synthèse façades/menuiseries.
_ENVELOPE_WALL_CLASSES = ("IfcWall", "IfcWallStandardCase")
# Un mur d'enveloppe est reconnu par le MOTIF distinctif « extérieurs
# périphériques » (normalisé), et non par un libellé exact : le nom de calque
# réel varie selon l'export ArchiCAD — préfixe de code chantier (« 221 - »),
# suffixe de vue (« .Exnd » / « .Exndo »). Un match exact ratait des murs réels
# comme « 221 - MURS - Extérieurs périphériques.Exndo » → annexe Enveloppe vide.
# ``_ENVELOPE_LAYER`` n'est plus qu'un libellé canonique de repli d'affichage.
_ENVELOPE_LAYER = "MURS - Extérieurs périphériques"

# Ordre de résolution des surfaces (BaseQuantities), puis repli propriété.
_WALL_BQ_ORDER = ("NetSideArea", "GrossSideArea", "NetArea", "GrossArea")
_SPACE_BQ_ORDER = ("NetFloorArea", "GrossFloorArea", "NetArea", "GrossArea")
_WINDOW_BQ_AREA = ("Area", "NetArea", "GrossArea")
_SUPERFICIE_PROP = "Superficie calculée"
_IFC_OPEN_SHELL_BQ = "IFC OpenShell - BaseQuantities"
_IFC_OPEN_SHELL_PROP = "IFC OpenShell - Superficie calculée"

# Classes de menuiseries : IFC2x3 (IfcWindow/IfcDoor) **et** IFC4
# (…StandardCase). Sans les StandardCase, un modèle IFC4 sortirait une
# annexe Menuiseries quasi vide sans erreur.
_MENUISERIE_CLASSES = ("IfcWindow", "IfcWindowStandardCase", "IfcDoor", "IfcDoorStandardCase")

#: Périmètre du livrable « Fenêtres » (ex-« Menuiseries »). Le gabarit client
#: ``TDB 2022 05.1 - Fenêtres Ok`` ne contient QUE des lignes ``Fenêtre`` : y
#: ajouter des portes déplace le compteur et change le sens du document. Les
#: portes feront l'objet d'un livrable distinct, si un gabarit client existe —
#: on n'en invente pas la forme.
_FENETRE_CLASSES = ("IfcWindow", "IfcWindowStandardCase")


def _norm(s: Any) -> str:
    """Normalise pour comparaison tolérante (accents / casse / espaces)."""
    if s is None:
        return ""
    txt = unicodedata.normalize("NFKD", str(s))
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    return " ".join(txt.lower().split())


# Motif distinctif cherché dans le nom de calque (tolérant accents/casse/espaces).
_ENVELOPE_LAYER_TOKEN = _norm("Extérieurs périphériques")


# ── Accesseurs bas niveau (tolérants) ───────────────────────────────────────


def _attr(el: dict, name: str) -> str:
    """Attribut IFC natif (Name, LongName…) sous forme de chaîne ('' si absent)."""
    key = name.lower()
    v = el.get(key) or el.get(name)
    if v in (None, ""):
        for prop in (el.get("attributes") or {}).get("properties") or []:
            nm = (prop.get("definition") or {}).get("name") or ""
            if nm.lower() == key:
                v = prop.get("value")
                break
    return "" if v in (None, "") else str(v)


def _label(el: dict) -> str:
    """Libellé exporté : LongName, sinon Name (fallback si LongName vide)."""
    ln = _attr(el, "LongName")
    if ln.strip():
        return ln.strip()
    return _attr(el, "Name").strip()


def _ifc_type(el: dict) -> str:
    ot = _attr(el, "ObjectType")
    if ot.strip():
        return ot.strip()
    pt = _attr(el, "PredefinedType")
    if pt.strip():
        return pt.strip()
    return el.get("type") or ""


def _storey(el: dict) -> str:
    for key in ("storey", "building_storey", "storey_name", "floor", "parent"):
        v = el.get(key)
        if isinstance(v, dict):
            nm = v.get("name")
            if nm:
                return str(nm)
        elif isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _material(el: dict) -> str:
    for m in el.get("material_list") or []:
        nm = (m.get("material") or {}).get("name")
        if nm:
            return str(nm)
    return ""


def _is_bq_pset(pset: dict) -> bool:
    return bool(re.match(r"^(basequantities|qto_|quantit)", _norm(pset.get("name"))))


def _base_quantity_ordered(el: dict, names: tuple[str, ...]) -> float | None:
    """Première BaseQuantity trouvée dans l'ordre ``names`` (accent-insensible)."""
    for name in names:
        target = _norm(name)
        for pset in el.get("property_sets") or []:
            if not _is_bq_pset(pset):
                continue
            for prop in pset.get("properties") or []:
                if _norm((prop.get("definition") or {}).get("name")) == target:
                    val = prop.get("value")
                    if isinstance(val, (int, float)):
                        return float(val)
    return None


def _prop_any_pset(el: dict, prop_name: str) -> float | None:
    """Valeur numérique d'une propriété cherchée dans **tous** les Psets
    (accent-insensible) — sert au repli « Superficie calculée »."""
    target = _norm(prop_name)
    for pset in el.get("property_sets") or []:
        for prop in pset.get("properties") or []:
            if _norm((prop.get("definition") or {}).get("name")) == target:
                val = prop.get("value")
                if isinstance(val, (int, float)):
                    return float(val)
    return None


def _surface_with_source(el: dict, bq_order: tuple[str, ...]) -> tuple[float | None, str | None]:
    """Surface + traçabilité : BaseQuantities (ordre) → « Superficie calculée »."""
    v = _base_quantity_ordered(el, bq_order)
    if v is not None:
        return v, _IFC_OPEN_SHELL_BQ
    v = _prop_any_pset(el, _SUPERFICIE_PROP)
    if v is not None:
        return v, _IFC_OPEN_SHELL_PROP
    return None, None


def _rich(snap: ModelSnapshot, item: dict) -> dict:
    """Version la plus riche d'un élément (index par UUID = psets/layers)."""
    u = item.get("uuid")
    if u and u in snap.element_by_uuid:
        return snap.element_by_uuid[u]
    return item


def _envelope_layer_name(el: dict) -> str | None:
    """Nom réel du calque d'enveloppe de l'élément (motif « extérieurs
    périphériques »), ou ``None`` si aucun calque ne correspond."""
    for layer in el.get("layers") or []:
        if isinstance(layer, dict) and _ENVELOPE_LAYER_TOKEN in _norm(layer.get("name")):
            return layer.get("name")
    return None


def _has_envelope_layer(el: dict) -> bool:
    return _envelope_layer_name(el) is not None


def _envelope_walls(snap: ModelSnapshot) -> list[dict]:
    walls: list[dict] = []
    for cls in _ENVELOPE_WALL_CLASSES:
        walls.extend(snap.of_class(cls))
    return [w for w in walls if _has_envelope_layer(w)]


def count_envelope_walls(snap: ModelSnapshot | None) -> int:
    """Nombre de murs d'enveloppe exploitables (pour la QA gate)."""
    return len(_envelope_walls(snap)) if snap is not None else 0


#: Classes de murs pouvant **porter** une enveloppe, indépendamment de toute
#: convention de calque. ``IfcCurtainWall`` est inclus ici sans l'être dans
#: ``_ENVELOPE_WALL_CLASSES`` : un mur-rideau est de la façade, et une maquette
#: dont l'enveloppe est en mur-rideau doit elle aussi déclencher le refus si
#: l'annexe sort vide.
_CANDIDATE_ENVELOPE_WALL_CLASSES = (
    "IfcWall",
    "IfcWallStandardCase",
    "IfcCurtainWall",
)


def count_candidate_envelope_walls(snap: ModelSnapshot | None) -> int:
    """Murs **susceptibles** de constituer l'enveloppe, sans exigence de calque.

    :func:`count_envelope_walls` reconnaît l'enveloppe par le calque ArchiCAD
    « extérieurs périphériques ». C'est le bon critère pour *construire* l'annexe,
    mais le mauvais pour décider s'il **fallait** en produire une : un export
    Revit n'expose aucun calque, son compte tombe donc à zéro et la QA gate se
    tait — laissant sortir un pack « OK » avec une annexe Enveloppe vide, sur une
    maquette qui a pourtant 1 574 murs.

    Ce compteur répond à la question « cette maquette a-t-elle de quoi avoir une
    enveloppe ? », sans présumer de l'outil de modélisation.
    """
    if snap is None:
        return 0
    return sum(len(snap.of_class(cls)) for cls in _CANDIDATE_ENVELOPE_WALL_CLASSES)


def count_menuiseries(snap: ModelSnapshot | None) -> int:
    """Nombre de menuiseries exploitables (IfcWindow + IfcDoor)."""
    if snap is None:
        return 0
    return sum(len(snap.of_class(cls)) for cls in _MENUISERIE_CLASSES)


# --------------------------------------------------------------------------- #
#  Comptage des QUANTITÉS exploitables (QA gate « colonnes vides »)
# --------------------------------------------------------------------------- #
#
# Un livrable avec des lignes mais des colonnes de quantités **toutes vides**
# est plus dangereux qu'un livrable vide : il a l'air complet. Cela arrive
# quand le snapshot BIMData ne porte pas de BaseQuantities et que les quantités
# calculées (contrat ``computed_base_quantities/v1``) n'ont pas été fusionnées.
# Ces compteurs mesurent la cause — combien d'éléments portent réellement la
# quantité attendue — et non le symptôme dans le fichier produit.


def count_spaces_with_area(snap: ModelSnapshot | None) -> int:
    """Espaces portant une surface exploitable (BaseQuantities ou repli)."""
    if snap is None:
        return 0
    n = 0
    for item in snap.spaces or []:
        surface, _src = _surface_with_source(_rich(snap, item), _SPACE_BQ_ORDER)
        if surface is not None:
            n += 1
    return n


def count_menuiseries_with_dimensions(snap: ModelSnapshot | None) -> int:
    """Menuiseries portant une largeur **ou** une hauteur exploitable."""
    if snap is None:
        return 0
    n = 0
    for cls in _MENUISERIE_CLASSES:
        for item in snap.of_class(cls):
            el = _rich(snap, item)
            if (
                _base_quantity_ordered(el, ("Width", "OverallWidth")) is not None
                or _base_quantity_ordered(el, ("Height", "OverallHeight")) is not None
            ):
                n += 1
    return n


def count_planchers_with_area(snap: ModelSnapshot | None) -> int:
    """Dalles portant une aire exploitable (``NetArea`` et replis)."""
    if snap is None:
        return 0
    n = 0
    for cls in _SLAB_CLASSES:
        for item in snap.of_class(cls):
            el = _rich(snap, item)
            if _base_quantity_ordered(el, _SLAB_BQ_ORDER) is not None:
                n += 1
    return n


def snapshot_shab_total(snap: ModelSnapshot | None) -> float | None:
    """SHAB totale de la maquette : somme des surfaces des espaces avec le
    **même repli** que les annexes (BaseQuantities puis « Superficie
    calculée »). ``None`` si aucune surface exploitable."""
    if snap is None:
        return None
    total = 0.0
    found = False
    for sp in snap.spaces or []:
        v, _ = _surface_with_source(_rich(snap, sp), _SPACE_BQ_ORDER)
        if v is not None:
            total += v
            found = True
    return round(total, 2) if found else None


# ── Relations zone → espaces, étage → espaces ───────────────────────────────


def _child_space_uuids(container: dict) -> list[str]:
    """UUID des espaces listés par un conteneur (zone ou étage)."""
    out: list[str] = []
    for key in ("spaces", "space_uuids", "elements", "related_spaces", "space_ids", "children"):
        v = container.get(key)
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict):
                    u = item.get("uuid") or item.get("id")
                    if u:
                        out.append(str(u))
    return out


def _zone_member_uuids(zone: dict) -> list[str]:
    return _child_space_uuids(zone)


def _space_zone_uuid(space: dict) -> str | None:
    for key in ("zone", "zone_uuid", "zone_id"):
        v = space.get(key)
        if isinstance(v, dict):
            u = v.get("uuid") or v.get("id")
            if u:
                return str(u)
        elif isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _storey_label(storey: dict) -> str:
    return _label(storey) or _attr(storey, "Name")


#: ``structure_tree`` désigne la nature d'un nœud tantôt par la **classe IFC**
#: (``IfcBuildingStorey``), tantôt par un **libellé court** (``storey``) — c'est
#: cette seconde forme que renvoie l'API BIMData en réel. Ne reconnaître que la
#: première rendait muets tous les replis par l'arbre : la colonne Étage sortait
#: vide sur 316 espaces qui portaient pourtant tous leur étage. Les fixtures de
#: test employaient la forme longue, donc rien ne le voyait.
_NODE_KINDS = {
    "ifcbuildingstorey": "storey",
    "buildingstorey": "storey",
    "storey": "storey",
    "ifczone": "zone",
    "zone": "zone",
    "ifcspace": "space",
    "space": "space",
}


def _node_kind(node: dict) -> str:
    """Nature d'un nœud de ``structure_tree``, quelle que soit son écriture."""
    return _NODE_KINDS.get(_norm(node.get("type")).replace(" ", ""), "")


def _walk_structure_tree(snap: ModelSnapshot) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Parcourt ``structure_tree`` (source hiérarchique BIMData) → mappings
    ``space_uuid → [étages]`` et ``space_uuid → [zones]`` (par conteneur
    ancêtre). Complète les relations plates quand les étages/zones ne sont
    portés que par l'arborescence spatiale.
    """
    st_map: dict[str, list[str]] = defaultdict(list)
    zn_map: dict[str, list[str]] = defaultdict(list)

    def add(m: dict[str, list[str]], u: str | None, name: str | None) -> None:
        if u and name and name not in m[u]:
            m[u].append(name)

    def visit(node: dict, storey: str | None, zone: str | None) -> None:
        ntype = _node_kind(node)
        nname = node.get("name") or node.get("long_name")
        if ntype == "storey":
            storey = nname or storey
        elif ntype == "zone":
            zone = nname or zone
        elif ntype == "space" and node.get("uuid"):
            add(st_map, node.get("uuid"), storey)
            add(zn_map, node.get("uuid"), zone)
        for child in node.get("children") or []:
            visit(child, storey, zone)

    for root in snap.structure_tree or []:
        visit(root, None, None)
    return st_map, zn_map


def _zone_members_from_tree(snap: ModelSnapshot) -> dict[str, list[str]]:
    """``zone_uuid → [space_uuid]`` depuis ``structure_tree``.

    Permet de retrouver les pièces d'une IfcZone quand ``/zone`` ne porte
    pas la liste ``spaces`` (fréquent en réel) mais que l'arborescence
    spatiale BIMData contient bien Zone → Space.
    """
    out: dict[str, list[str]] = defaultdict(list)

    def visit(node: dict, zone_uuid: str | None) -> None:
        ntype = _node_kind(node)
        nuuid = node.get("uuid")
        if ntype == "zone":
            zone_uuid = nuuid or zone_uuid
        elif ntype == "space" and nuuid and zone_uuid and nuuid not in out[zone_uuid]:
            out[zone_uuid].append(nuuid)
        for child in node.get("children") or []:
            visit(child, zone_uuid)

    for root in snap.structure_tree or []:
        visit(root, None)
    return out


def _build_space_zone_map(snap: ModelSnapshot) -> dict[str, list[str]]:
    """``space_uuid → [noms de zones]`` (un espace peut être dans plusieurs
    zones ; ex. duplex rattaché à des zones d'étage distinctes)."""
    zmap: dict[str, list[str]] = defaultdict(list)
    spaces = snap.spaces or []
    for z in snap.zones or []:
        zname = _label(_rich(snap, z)) or _attr(z, "Name")
        if not zname:
            continue
        members = _zone_member_uuids(z)
        if not members:
            members = [sp.get("uuid") for sp in spaces if _space_zone_uuid(sp) == z.get("uuid")]
        for u in members:
            if u and zname not in zmap[u]:
                zmap[u].append(zname)
    # Complément : zones portées par l'arborescence spatiale BIMData.
    _, zn_tree = _walk_structure_tree(snap)
    for u, names in zn_tree.items():
        for name in names:
            if name not in zmap[u]:
                zmap[u].append(name)
    return zmap


def _build_space_storey_map(snap: ModelSnapshot) -> dict[str, list[str]]:
    """``space_uuid → [noms d'étages]``.

    Multi-valué : un même espace peut être rattaché à plusieurs étages
    (ex. **duplex** dont la zone traverse deux niveaux), et un étage peut
    lister ses espaces. On agrège toutes les sources sans jamais inventer.
    """
    smap: dict[str, list[str]] = defaultdict(list)
    storeys = snap.storeys or []
    by_uuid = {st.get("uuid"): st for st in storeys}

    def _add(u: str | None, name: str | None) -> None:
        if u and name and name not in smap[u]:
            smap[u].append(name)

    # 1. étage → ses espaces
    for st in storeys:
        sname = _storey_label(st)
        for u in _child_space_uuids(st):
            _add(u, sname)
    # 2. espace → son/ses étage(s) (attribut nom direct + référence UUID)
    for sp in snap.spaces or []:
        u = sp.get("uuid")
        _add(u, _storey(sp) or None)
        for key in ("storey", "building_storey", "storey_uuid", "storey_id", "parent", "storeys"):
            v = sp.get(key)
            refs = v if isinstance(v, list) else [v]
            for ref in refs:
                ru = None
                if isinstance(ref, str):
                    ru = ref
                elif isinstance(ref, dict):
                    ru = ref.get("uuid") or ref.get("id")
                if ru and ru in by_uuid:
                    _add(u, _storey_label(by_uuid[ru]))
    # 3. arborescence spatiale BIMData (structure_tree) — source
    # hiérarchique de référence quand l'étage n'est porté que par l'arbre.
    st_tree, _ = _walk_structure_tree(snap)
    for u, names in st_tree.items():
        for name in names:
            _add(u, name)
    return smap


# ── Builders (snapshot → dataclasses source AVP) ────────────────────────────

# « Zone » = zone(s) contenant l'espace ; « Étage » = étage(s) — les deux
# multi-valués (séparés par « / ») pour couvrir les duplex (zone traversant
# plusieurs niveaux) et un espace rattaché à plusieurs zones d'étage.
_ZONE_DETAIL_HEADERS_SHAB = [
    "Composant",
    "Nom Zone",
    "Type de Zone",
    "Groupes",
    "Pièce",
    "Type Pièce",
    "Surface IFC OpenShell",
    "Surface Nette (Qté de Base)",
    "Étage",
    "Surface Brute (Qté de Base)",
    "Couleur",
    "écarts",
]
_ZONE_DETAIL_HEADERS_ZONES = [
    "Composant",
    "Nom Zone",
    "Type de Zone",
    "Groupes",
    "Pièce (Nombre)",
    "Type Pièce",
    "Surface IFC OpenShell",
    "Surface Nette (Qté de Base)",
    "Étage",
    "Surface Brute (Qté de Base)",
    "Couleur",
    "écarts",
]

_MULTI_SEP = " / "

# « Source quantité » : distingue une valeur **native BIMData** (« Maquette ») d'une
# valeur **calculée géométriquement** puis fusionnée (« Calculée (IfcOpenShell) »).
# Un gap (aucune valeur) n'est **jamais masqué** → NOT_AVAILABLE.
_SRC_MODEL = "Maquette"
_SRC_COMPUTED = "Calculée (IfcOpenShell)"

#: Racines de libellés désignant une **annexe non habitable**, exclue du total
#: SHAB. La règle vient de la **définition réglementaire de la surface
#: habitable**, pas du classeur client : celui-ci sert à la *vérifier*, jamais à
#: la produire. La déduire des libellés présents dans une maquette donnée ferait
#: une règle vraie pour cette maquette et fausse pour la suivante.
#:
#: Les annexes restent **visibles** dans l'onglet de détail — elles existent
#: dans la maquette — mais ne contribuent pas au total du pivot SHAB.
_SHAB_EXCLUDED_SPACE_LABELS = (
    "CELLIER",
    "BALCON",
    "TERRASSE",
    "LOGGIA",
    "CAVE",
    "GARAGE",
    "COMBLE",
    "LOCAL TECHNIQUE",
    "LOCAL VELO",
    "PARKING",
)
_SHAB_EXCLUDED_NORM = tuple(_norm(lbl) for lbl in _SHAB_EXCLUDED_SPACE_LABELS)

#: Libellé porté par la colonne « Type Pièce » d'une annexe. Le gabarit client
#: distingue en outre « Balcons et terrasses » d'« ANNEXES », mais **aucune
#: donnée de la maquette ne permet cette distinction** (mêmes libellés, mêmes
#: attributs `InteriorOrExteriorSpace`) : elle relève d'un arbitrage humain.
#: On ne la fabrique donc pas — sans effet sur le total, les deux étant exclues.
_TYPE_PIECE_ANNEXE = "ANNEXES"

#: Typologie de logement portée par l'``ObjectType`` de l'IfcZone
#: (« Zone Logement T4 » → « T4 »). C'est de LÀ que vient la colonne « Type
#: Pièce » du gabarit : l'IfcSpace, lui, ne porte aucun type métier — le lire
#: sur l'espace ne pouvait donner que la classe IFC (« IfcSpace »).
_TYPOLOGIE_RE = re.compile(r"\bT\s?\d+\b", re.IGNORECASE)


def _est_annexe_shab(label: str) -> bool:
    """Le libellé d'espace désigne-t-il une annexe exclue de la SHAB ?"""
    norm = _norm(label)
    if not norm:
        return False
    return any(
        norm == racine or norm.startswith(racine + " ") or norm.startswith(racine + "-")
        for racine in _SHAB_EXCLUDED_NORM
    )


def _typologie_zone(zone_type: str) -> str:
    """« Zone Logement T4 » → « T4 » ; à défaut, le type de zone tel quel."""
    found = _TYPOLOGIE_RE.search(zone_type or "")
    return found.group(0).upper().replace(" ", "") if found else (zone_type or "").strip()


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _round2(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _ifc_component_label(ifc_class: str | None) -> str:
    if ifc_class in ("IfcWindow", "IfcWindowStandardCase"):
        return "Fenêtre"
    if ifc_class in ("IfcDoor", "IfcDoorStandardCase"):
        return "Porte"
    if ifc_class in ("IfcSlab", "IfcCovering"):
        return "Dalle"
    if ifc_class in ("IfcSpace",):
        return "Pièce"
    if ifc_class in ("IfcZone",):
        return "Zone"
    return ifc_class or ""


#: Valeurs de ``PredefinedType`` qui ne DÉSIGNENT rien : ce sont les énumérations
#: IFC, pas des types métier. « WINDOW » à la place de « Fenêtre châssis double
#: 25 » fait disparaître l'information qui structure les lignes du livrable.
_PREDEFINED_TYPE_GENERIQUES = {"WINDOW", "DOOR", "NOTDEFINED", "USERDEFINED", ""}


def _object_type_or_name(el: dict) -> str:
    """Type métier affiché dans les exports MOA.

    Ordre : ``ObjectType`` (type projet, posé par l'outil de modélisation), puis
    ``Name``, puis ``PredefinedType`` **s'il dit quelque chose**. L'énumération
    IFC passe en dernier et seulement si elle n'est pas générique : la préférer
    au ``Name`` regroupait toutes les fenêtres sous « WINDOW ».
    """
    for attr in ("ObjectType", "Name"):
        value = _attr(el, attr)
        if value.strip():
            return value.strip()
    predefini = _attr(el, "PredefinedType").strip()
    if predefini and predefini.upper() not in _PREDEFINED_TYPE_GENERIQUES:
        return predefini
    return el.get("type") or ""


def _computed_qty_names(el: dict) -> set[str]:
    """Noms des BaseQuantities issues de la fusion géométrique (Lot 3)."""
    return {
        c.get("quantity") for c in (el.get("computed_base_quantities") or []) if c.get("quantity")
    }


def _quantity_source(el: dict, has_value: bool, qty_names: tuple[str, ...]) -> str:
    """« Maquette » (natif) / « Calculée (IfcOpenShell) » (fusionnée) / NOT_AVAILABLE."""
    if not has_value:
        return NOT_AVAILABLE
    return _SRC_COMPUTED if _computed_qty_names(el) & set(qty_names) else _SRC_MODEL


def _space_surface(sp: dict) -> tuple[float | None, float | None]:
    net, _src = _surface_with_source(sp, _SPACE_BQ_ORDER)
    gross = _base_quantity_ordered(sp, ("GrossFloorArea", "GrossArea"))
    if gross is None:
        gross = net
    return net, gross


def _zone_member_map(snap: ModelSnapshot) -> dict[str, list[str]]:
    spaces = snap.spaces or []
    tree_members = _zone_members_from_tree(snap)
    out: dict[str, list[str]] = {}
    for z in snap.zones or []:
        members = _zone_member_uuids(z)
        if not members:
            members = [sp.get("uuid") for sp in spaces if _space_zone_uuid(sp) == z.get("uuid")]
        if not members:
            members = list(tree_members.get(z.get("uuid"), []))
        out[str(z.get("uuid"))] = [u for u in members if u]
    return out


def _space_records(snap: ModelSnapshot) -> list[dict[str, Any]]:
    spaces = snap.spaces or []
    if not spaces:
        return []
    by_uuid = {sp.get("uuid"): _rich(snap, sp) for sp in spaces}
    zone_members = _zone_member_map(snap)
    storey_map = _build_space_storey_map(snap)
    _, tree_zone_map = _walk_structure_tree(snap)
    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_record(
        sp_uuid: str | None,
        z: dict | None,
        first_in_zone: bool,
        *,
        zone_name_override: str | None = None,
    ) -> None:
        if not sp_uuid or sp_uuid not in by_uuid:
            return
        sp = by_uuid[sp_uuid]
        zrich = _rich(snap, z) if z else None
        net, gross = _space_surface(sp)
        storeys = storey_map.get(sp_uuid, [])
        if not storeys:
            direct = _storey(sp)
            if direct:
                storeys = [direct]
        has_zone = z is not None or bool(zone_name_override)
        zone_type = _ifc_type(zrich) if zrich else ("IfcZone" if has_zone else "")
        piece = _label(sp)
        annexe = _est_annexe_shab(piece)
        records.append(
            {
                "component": "Zone" if has_zone else "Pièce",
                "first_component": "Zone"
                if has_zone and first_in_zone
                else ("Pièce" if not has_zone else ""),
                "zone_name": zone_name_override
                or ((_label(zrich) or _attr(z or {}, "Name")) if z else ""),
                "zone_type": zone_type,
                "group": _attr(sp, "Name") or piece,
                "piece": piece,
                # Typologie du logement (via la zone), ou « ANNEXES ». Un espace
                # sans zone n'a pas de typologie : ne rien écrire plutôt que de
                # retomber sur la classe IFC, qui ne dit rien du métier.
                "piece_type": _TYPE_PIECE_ANNEXE
                if annexe
                else (_typologie_zone(zone_type) if has_zone else ""),
                "net": net,
                "gross": gross,
                "storey": _MULTI_SEP.join(storeys),
                "source": _quantity_source(sp, net is not None, _SPACE_BQ_ORDER),
                # Périmètre : « zoné » décide de la présence dans les onglets
                # métier, « annexe » de la contribution au total SHAB.
                "zoned": has_zone,
                "annexe": annexe,
            }
        )
        seen.add(sp_uuid)

    for z in snap.zones or []:
        members = zone_members.get(str(z.get("uuid")), [])
        for i, sp_uuid in enumerate(members):
            add_record(sp_uuid, z, i == 0)

    for sp in spaces:
        sp_uuid = sp.get("uuid")
        if sp_uuid not in seen:
            tree_zones = tree_zone_map.get(sp_uuid, [])
            add_record(
                sp_uuid,
                None,
                True,
                zone_name_override=_MULTI_SEP.join(tree_zones) if tree_zones else None,
            )

    return records


def _zonage_present(records: list[dict[str, Any]]) -> bool:
    """La maquette porte-t-elle un zonage exploitable (au moins un espace
    rattaché à une ``IfcZone``) ?"""
    return any(r["zoned"] for r in records)


def _records_detail(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Population des onglets métier : **espaces rattachés à une zone**.

    Annexes comprises — elles existent dans la maquette et le gabarit les
    montre — mais sans les espaces non zonés, qui n'ont ni zone ni typologie et
    feraient raconter au détail une population différente de celle du pivot.

    **Le filtre ne s'applique que si la maquette est zonée.** Sans aucune
    ``IfcZone``, le retenir viderait le livrable : rien ne se mélange, il n'y a
    rien à départager. Le régime appliqué est écrit dans la note de méthode —
    un total qui ne repose pas sur un zonage ne se lit pas comme une SHAB par
    logement.
    """
    if not _zonage_present(records):
        return list(records)
    return [r for r in records if r["zoned"]]


def _records_shab(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Périmètre du **total SHAB** : la population du détail, hors annexes.

    L'exclusion des annexes, elle, ne dépend pas du zonage : c'est une règle de
    la définition de la surface habitable, pas une règle de regroupement.
    """
    return [r for r in _records_detail(records) if not r["annexe"]]


def _records_hors_perimetre(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Espaces présents dans la maquette mais absents des onglets métier."""
    if not _zonage_present(records):
        return []
    return [r for r in records if not r["zoned"]]


def _somme_nette(records: list[dict[str, Any]]) -> float:
    return sum((_num(r["net"]) or 0.0) for r in records)


def _zone_detail_grid(
    records: list[dict[str, Any]], *, title: str, zones_variant: bool = False
) -> SheetGrid | None:
    """Onglet de détail — **12 colonnes A:L**, la forme du gabarit client.

    Deux règles de doctrine, les mêmes qu'au livrable Fenêtres (#210) :

    - **jamais de valeur recopiée pour simuler une comparaison**. G et H
      recevaient LITTÉRALEMENT la même variable ``net`` : l'écart L était donc
      vide par construction et se lisait comme une concordance vérifiée alors
      que rien ne l'avait été. Après la fusion *gap-only*, une quantité est soit
      native, soit calculée — jamais les deux. On remplit donc **la colonne qui
      correspond à la provenance**, et l'autre reste vide ;
    - la provenance se lisant à l'emplacement de la valeur, la 13ᵉ colonne
      ``Source quantité`` — absente du gabarit — n'a plus lieu d'être.

    L'écart garde la **forme relative** du gabarit, mais gardé : sans les deux
    valeurs, il n'y a pas d'écart à afficher (et ``H/G-1`` donnerait ``#DIV/0!``).
    """
    lignes = _records_detail(records)
    if not lignes:
        return None
    headers = list(_ZONE_DETAIL_HEADERS_ZONES if zones_variant else _ZONE_DETAIL_HEADERS_SHAB)
    rows: list[list[Any]] = [list(headers)]
    for rec in lignes:
        excel_row = len(rows) + 1
        net = _round2(rec["net"])
        gross = _round2(rec["gross"])
        calculee = rec["source"] == _SRC_COMPUTED
        openshell = net if calculee else None
        natif = None if calculee else net
        rows.append(
            [
                rec["first_component"],
                rec["zone_name"],
                rec["zone_type"],
                rec["group"],
                rec["piece"],
                rec["piece_type"],
                openshell,
                natif,
                rec["storey"],
                gross,
                "",
                f'=IF(OR(G{excel_row}="",H{excel_row}=""),"",'
                f'IF(H{excel_row}/G{excel_row}-1=0,"",H{excel_row}/G{excel_row}-1))',
            ]
        )
    if not zones_variant:
        rows.append([""] * len(headers))
        rows.append(
            [*[""] * 11, f"=SUBTOTAL(9,L2:L{len(lignes) + 1})"],
        )
    return SheetGrid(title=title, rows=rows)


def _pivot_grid(records: list[dict[str, Any]], *, title: str, first_label: str) -> SheetGrid | None:
    """Pivot de synthèse — attend des enregistrements **déjà réduits au
    périmètre SHAB** (cf. :func:`_records_shab`).

    Ce pivot ne filtre rien lui-même : il totalise ce qu'on lui donne. C'est
    pourquoi il recevait tout — annexes et espaces non zonés compris — et
    annonçait un total juste arithmétiquement mais faux pour l'usage I3F.
    """
    if not records:
        return None
    piece_order: list[str] = []
    for rec in records:
        piece = rec["piece"] or NOT_AVAILABLE
        if piece not in piece_order:
            piece_order.append(piece)

    by_zone_type: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    by_zone: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for rec in records:
        ztype = rec["zone_type"] or NOT_AVAILABLE
        zname = rec["zone_name"] or NOT_AVAILABLE
        piece = rec["piece"] or NOT_AVAILABLE
        area = _num(rec["net"]) or 0.0
        by_zone_type[ztype][piece] += area
        by_zone[(ztype, zname)][piece] += area

    rows: list[list[Any]] = [
        [],
        [],
        [title, "Pièces"],
        [first_label, *piece_order, "Total général"],
    ]
    for ztype in sorted(by_zone_type):
        totals = by_zone_type[ztype]
        rows.append(
            [
                ztype,
                *[_round2(totals.get(p, 0.0)) or "" for p in piece_order],
                _round2(sum(totals.values())),
            ]
        )
        for (_zt, zname), vals in sorted(by_zone.items()):
            if _zt != ztype:
                continue
            rows.append(
                [
                    zname,
                    *[_round2(vals.get(p, 0.0)) or "" for p in piece_order],
                    _round2(sum(vals.values())),
                ]
            )
    grand = [
        sum((_num(rec["net"]) or 0.0) for rec in records if (rec["piece"] or NOT_AVAILABLE) == p)
        for p in piece_order
    ]
    rows.append(["Total général", *[_round2(v) or "" for v in grand], _round2(sum(grand))])
    return SheetGrid(title="Feuil1" if title.startswith("SHAB") else "Feuil2", rows=rows)


def _note_methode_rows(records: list[dict[str, Any]]) -> list[list[Any]]:
    """Trace de ce que les onglets métier ont filtré.

    Règle générale : **un livrable client peut filtrer, mais il doit tracer ce
    qu'il filtre quand la donnée existe dans la maquette.** Écarter 191,5 m²
    sans le dire est propre visuellement et faux opérationnellement — le maître
    d'ouvrage doit voir ce qui est hors périmètre du document qu'il lit.
    """
    hors = _records_hors_perimetre(records)
    annexes = [r for r in _records_detail(records) if r["annexe"]]
    zonage = _zonage_present(records)
    entete: list[list[Any]] = [
        ["Note de méthode"],
        [],
        ["zonage_present", "oui" if zonage else "non"],
    ]
    if not zonage:
        # Sans zonage, le total n'est pas une SHAB par logement : le dire ici
        # plutôt que de laisser lire un chiffre pour ce qu'il n'est pas.
        entete.append(
            [
                "Aucune IfcZone dans la maquette : le livrable liste tous les "
                "espaces et le total ne peut pas être ventilé par logement."
            ]
        )
    return [
        *entete,
        [],
        [
            "Espaces présents dans la maquette mais exclus du livrable "
            "Zones/Espaces car non rattachés à une zone."
        ],
        ["espaces_non_zones_count", len(hors)],
        ["espaces_non_zones_surface_m2", _round2(_somme_nette(hors))],
        [],
        [
            "Les espaces dont le libellé correspond à une annexe non habitable "
            "sont exclus du total SHAB selon la règle documentaire SHAB. Ils "
            "restent traçables dans le rapport."
        ],
        ["annexes_count", len(annexes)],
        ["annexes_surface_m2", _round2(_somme_nette(annexes))],
        ["annexes_libelles", _MULTI_SEP.join(sorted({r["piece"] for r in annexes}))],
        [],
        ["total_shab_m2", _round2(_somme_nette(_records_shab(records)))],
    ]


def build_shab_from_snapshot(snap: ModelSnapshot) -> tuple[MultiSheetSource | None, float | None]:
    """Export SHAB MOA depuis la maquette : pivot + détail espaces.

    Le total renvoyé est celui du **périmètre SHAB** (espaces zonés, hors
    annexes) : c'est lui qui alimente le ratio FAC/SHAB, et un ratio dont le
    dénominateur ne serait pas le total affiché par le pivot ferait dire deux
    chiffres différents au même document.
    """
    records = _space_records(snap)
    if not records:
        return None, None
    pivot = _pivot_grid(_records_shab(records), title="SHAB (Qté de Base)", first_label="Logement")
    detail = _zone_detail_grid(records, title="TDB 2022 01.3 - Export Zones...")
    note = SheetGrid(title="Note de méthode", rows=_note_methode_rows(records))
    grids = [g for g in (pivot, detail, note) if g is not None]
    total = round(_somme_nette(_records_shab(records)), 4)
    return MultiSheetSource(grids=grids), total


def build_zones_espaces_from_snapshot(snap: ModelSnapshot) -> MultiSheetSource | None:
    """Export Zones/Espaces MOA depuis la maquette : pivot + détail + Feuil1.

    ``Feuil2`` (pivot) et l'onglet de détail restent au périmètre du gabarit —
    espaces zonés uniquement ; ``Feuil1``, jusqu'ici vide, porte la note de
    méthode et le décompte de ce qui a été écarté.
    """
    records = _space_records(snap)
    if not records:
        return None
    pivot = _pivot_grid(
        _records_shab(records),
        title="Somme de Surface Nette (Qté de Base)",
        first_label="Étiquettes de lignes",
    )
    detail = _zone_detail_grid(
        records,
        title="TDB 2022 01.3 - Export Zones...",
        zones_variant=True,
    )
    note = SheetGrid(title="Feuil1", rows=_note_methode_rows(records))
    grids = [g for g in (pivot, detail, note) if g is not None]
    return MultiSheetSource(grids=grids) if grids else None


def build_menuiseries_from_snapshot(
    snap: ModelSnapshot,
) -> tuple[MenuiseriesSource | None, float | None]:
    """Export **Fenêtres** depuis la maquette (``IfcWindow`` seul).

    Structure du gabarit client ``TDB 2022 05.1 - Fenêtres Ok`` : **14 colonnes
    A:N**, la colonne M restant vide (séparation — la supprimer décalerait
    N = Couleur).

    Deux règles de doctrine s'appliquent ici :

    - **source unique assumée** — aucun libellé « Solibri » ; les colonnes de
      mesure disent IFC OpenShell ;
    - **jamais de valeur recopiée pour simuler une comparaison**. Après la
      fusion *gap-only*, une quantité est soit native, soit calculée — **jamais
      les deux**. Écrire la même valeur en D et en H produisait un écart K
      systématiquement vide, qui se lisait comme une concordance vérifiée alors
      que rien ne l'avait été. On remplit donc **la colonne qui correspond à la
      provenance**, et l'autre reste vide. C'est aussi ce qui rend inutile
      l'ancienne 15ᵉ colonne « Source quantité », étrangère au gabarit : la
      provenance se lit désormais à l'emplacement de la valeur.
    """
    items = [el for cls in _FENETRE_CLASSES for el in snap.of_class(cls)]
    if not items:
        return None, None
    headers = [
        "Composant",
        "Type",
        "Matériau",
        "BaseQuantities.Width",
        "BaseQuantities.Height",
        "Surface Natif",
        "Nombre",
        "Largeur IFC OpenShell",
        "Hauteur IFC OpenShell",
        "Surface IFC OpenShell",
        "Ecart BaseQuantities / IFC OpenShell (largeur)",
        "Ecart BaseQuantities / IFC OpenShell (hauteur)",
        "",
        "Couleur",
    ]
    men_qty = ("Width", "Height", "OverallWidth", "OverallHeight")
    area_qty = (*men_qty, *_WINDOW_BQ_AREA)
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    total = 0.0
    any_area = False
    for item in items:
        w = _rich(snap, item)
        width = _base_quantity_ordered(w, ("Width", "OverallWidth"))
        height = _base_quantity_ordered(w, ("Height", "OverallHeight"))
        surf, _src = _surface_with_source(w, _WINDOW_BQ_AREA)
        if surf is None and width is not None and height is not None:
            surf = width * height
        if surf is not None:
            total += surf
            any_area = True
        ot = _object_type_or_name(w)
        # La PROVENANCE entre dans la clé de regroupement. Sans elle, deux
        # fenêtres de même type et mêmes dimensions — l'une native, l'autre
        # calculée — tombaient dans le même groupe, et un unique booléen
        # décidait pour les deux : le livrable annonçait alors une provenance
        # fausse pour la moitié des éléments comptés sur la ligne.
        calculee = bool(_computed_qty_names(w) & set(area_qty))
        key = (
            _ifc_component_label(w.get("type")),
            ot,
            _material(w),
            _round2(width),
            _round2(height),
            calculee,
        )
        entry = groups.setdefault(
            key,
            {
                "surface": 0.0,
                "surface_found": False,
                "count": 0,
                "computed": False,
            },
        )
        entry["count"] += 1
        if surf is not None:
            entry["surface"] += surf
            entry["surface_found"] = True
        # ``computed`` est désormais porté par la CLÉ : tous les éléments d'un
        # groupe partagent la même provenance, par construction.
        entry["computed"] = calculee

    rows: list[list[Any]] = []
    for key, entry in sorted(
        groups.items(), key=lambda kv: tuple("" if v is None else v for v in kv[0])
    ):
        component, ot, material, width, height, _provenance = key
        excel_row = len(rows) + 2
        surface = _round2(entry["surface"]) if entry["surface_found"] else None
        # La provenance décide de la COLONNE, pas d'un libellé en bout de ligne.
        # Une valeur calculée par IFC OpenShell va en H/I/J ; une valeur native
        # de la maquette va en D/E/F. Jamais les deux : elles ne coexistent pas.
        calculee = entry["computed"]
        natif = (None, None, None) if calculee else (width, height, surface)
        openshell = (width, height, surface) if calculee else (None, None, None)
        rows.append(
            [
                component,
                ot,
                material,
                *natif,
                entry["count"],
                *openshell,
                f'=IF(H{excel_row}-D{excel_row}=0,"",H{excel_row}-D{excel_row})',
                f'=IF(I{excel_row}-E{excel_row}=0,"",I{excel_row}-E{excel_row})',
                "",
                "",
            ]
        )
    table = SheetTable(title="Menuiseries", headers=headers, rows=rows)
    src = MenuiseriesSource(
        table=table,
        sheet_title="TDB 2022 05.1 - Fenêtres Ok",
        nombre_types=(len(groups) or None),
    )
    return src, (round(total, 4) if any_area else None)


def build_enveloppe_from_snapshot(snap: ModelSnapshot) -> EnveloppeSource | None:
    """Extraction surface enveloppe depuis la maquette (murs du layer cible)."""
    walls = _envelope_walls(snap)
    if not walls:
        return None
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    total = 0.0
    any_surface = False
    for w in walls:
        surf, src = _surface_with_source(w, _WALL_BQ_ORDER)
        if surf is not None:
            total += surf
            any_surface = True
        typ = _object_type_or_name(w)
        layer = _envelope_layer_name(w) or _ENVELOPE_LAYER
        key = (typ, layer)
        entry = groups.setdefault(key, {"area": 0.0, "found": False, "count": 0, "storeys": []})
        entry["count"] += 1
        if surf is not None:
            entry["area"] += surf
            entry["found"] = True
        storey = _storey(w)
        if storey and storey not in entry["storeys"]:
            entry["storeys"].append(storey)

    rows: list[list[Any]] = []
    for (typ, _layer), entry in sorted(groups.items()):
        area = _round2(entry["area"]) if entry["found"] else None
        rows.append(
            [
                "Mur",
                typ,
                _MULTI_SEP.join(entry["storeys"]),
                area,
                area,
                None,
                None,
                None,
                entry["count"],
                None,
            ]
        )
    table = SheetTable(title=ENVELOPPE_MOA_SHEET, headers=list(ENVELOPPE_MOA_HEADERS), rows=rows)
    return EnveloppeSource(
        table=table,
        sheet_title=ENVELOPPE_MOA_SHEET,
        superficie_facades=(round(total, 4) if any_surface else None),
    )


_SLAB_CLASSES = ("IfcSlab", "IfcCovering")
_SLAB_BQ_ORDER = ("NetArea", "GrossArea", "NetSideArea")


def count_planchers(snap: ModelSnapshot | None) -> int:
    """Nombre de dalles/planchers exploitables (IfcSlab, repli IfcCovering)."""
    if snap is None:
        return 0
    return sum(len(snap.of_class(cls)) for cls in _SLAB_CLASSES)


def build_plancher_from_snapshot(snap: ModelSnapshot) -> MultiSheetSource | None:
    """Export plancher MOA depuis la maquette : détail + synthèse écarts."""
    slabs = [el for cls in _SLAB_CLASSES for el in snap.of_class(cls)]
    if not slabs:
        return None
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in slabs:
        el = _rich(snap, item)
        surf, src = _surface_with_source(el, _SLAB_BQ_ORDER)
        key = (_ifc_component_label(el.get("type")), _object_type_or_name(el), _storey(el))
        entry = groups.setdefault(
            key,
            {"area": 0.0, "found": False, "count": 0, "computed": False},
        )
        entry["count"] += 1
        if surf is not None:
            entry["area"] += surf
            entry["found"] = True
        if _computed_qty_names(el) & set(_SLAB_BQ_ORDER):
            entry["computed"] = True

    detail_rows: list[list[Any]] = [
        [
            "Composant",
            "Type",
            "Étage",
            "BaseQuantities.NetArea",
            "Surface IFC OpenShell",
            "Nombre",
            "Couleur",
            "Source quantité",
        ]
    ]
    summary_rows: list[list[Any]] = [
        [
            "Composant",
            "Type",
            "Étage",
            "BaseQuantities.NetArea",
            "Surface IFC OpenShell",
            "Ecart",
            "Nombre",
            "Couleur",
            "Source quantité",
        ]
    ]
    for key, entry in sorted(groups.items()):
        component, typ, storey = key
        area = _round2(entry["area"]) if entry["found"] else None
        source = (
            _SRC_COMPUTED
            if entry["computed"]
            else (_SRC_MODEL if entry["found"] else NOT_AVAILABLE)
        )
        detail_rows.append([component, typ, storey, area, area, entry["count"], "", source])
        excel_row = len(summary_rows) + 1
        summary_rows.append(
            [
                component,
                typ,
                storey,
                area,
                area,
                f'=IF(E{excel_row}-D{excel_row}=0,"",E{excel_row}/D{excel_row}-1)',
                entry["count"],
                "",
                source,
            ]
        )
    return MultiSheetSource(
        grids=[
            SheetGrid(title="TDB 2022 xx.2 - Dalles Ok", rows=detail_rows),
            SheetGrid(title="Planchers", rows=summary_rows),
        ]
    )


def build_sources_from_snapshot(snap: ModelSnapshot) -> AvpSources:
    """Construit un jeu de sources AVP **cohérent** depuis la maquette.

    Les grandeurs croisées (SHAB, superficie menuiseries, ratio FAC/SHAB)
    sont calculées uniquement quand les termes existent (jamais inventées).
    """
    shab_ms, shab_total = build_shab_from_snapshot(snap)
    zones_ms = build_zones_espaces_from_snapshot(snap)
    men_src, men_area = build_menuiseries_from_snapshot(snap)
    env_src = build_enveloppe_from_snapshot(snap)
    plancher_src = build_plancher_from_snapshot(snap)

    if env_src is not None:
        env_src.shab = shab_total
        env_src.superficie_menuiseries = men_area
        if env_src.superficie_facades is not None and shab_total:
            env_src.ratio_fac_shab = round(env_src.superficie_facades / shab_total, 4)

    return AvpSources(
        shab=shab_ms,
        zones_espaces=zones_ms,
        enveloppe=env_src,
        menuiseries=men_src,
        plancher=plancher_src,
    )
