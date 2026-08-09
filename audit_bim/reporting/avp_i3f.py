"""Pack de livrables AVP I3F (Tarare 0546L) — génération BIMData.

Produit, à partir d'un ``AuditResult`` (snapshot/audit BIMData) et des
quantités IFC extraites/calculées depuis la maquette, le pack des livrables AVP
**produisibles** :

1. ``… Contrôle Maquettes.xlsx`` — grille de contrôle + stats conformité.
2. ``… AVP - export SHAB maquette.xlsx``.
3. ``… Export Zones et Espaces.xlsx``.
4. ``… Extraction surface enveloppe.xlsx`` (+ ratio FAC/SHAB, Seuil 3F).
5. ``… export Menuiseries.xlsx``.
6. ``… Rapport analyse BIM.docx`` (+ ``.pdf`` best-effort) — rapport consolidé,
   **toujours** produit : c'est lui qui porte l'analyse.

``… export plancher.xlsx`` n'est **plus produit** : le rapport est bloqué tant
que la règle métier de sélection des 19 groupes contribuant à la Surface de
plancher n'est pas définie (``ReportSpec.blocked_reason``). Le paramètre
``reports`` restreint en outre l'écriture aux clés catalogue nommées ; une clé
inconnue ou bloquée fait lever ``AvpReportSelectionError`` **avant** toute
écriture.

Principes :

- **Réutilise** l'infra de reporting existante : ``write_safe`` protège les
  cellules Excel contre l'injection ; les helpers ``word_report`` restent la
  base du Word consolidé. Pas de stack parallèle.
- **Ne jamais inventer** : donnée absente du snapshot / des calculs IFC →
  ``NOT_AVAILABLE``.
- **Maquette-first** pour les exports : SHAB, zones/espaces, enveloppe,
  menuiseries et plancher utilisent les valeurs extraites de la maquette ou
  calculées via la chaîne IFC/OpenShell. Les .xlsx MOA éventuellement fournis
  servent au contexte documentaire (identité projet, seuils, template futur),
  pas de source autoritaire pour les surfaces issues d'outils externes.
- **Fidélité « tables à plat »** : mêmes onglets, colonnes, ordre, unités
  et vocabulaire métier, avec colonnes IFC OpenShell explicites.

Ce module est désormais une **façade** : l'implémentation vit dans le package
interne :mod:`audit_bim.reporting.avp`. Les ré-exports ci-dessous préservent la
compatibilité de tous les imports historiques.
"""

from __future__ import annotations

from .avp.models import (  # noqa: F401
    AvpMeta,
    AvpQaError,
    AvpReportPack,
    AvpReportSelectionError,
)
from .avp.pack import write_avp_i3f_report_pack  # noqa: F401
from .avp.xlsx_common import _count_business_rows  # noqa: F401
from .avp.xlsx_controle import (  # noqa: F401
    _audit_stats,
    _count_controle_rows,
    _zone_finding_kind,
)
from .avp.xlsx_enveloppe import _build_enveloppe_xlsx  # noqa: F401
from .avp_snapshot import build_sources_from_snapshot  # noqa: F401
from .word_report import NOT_AVAILABLE  # noqa: F401

__all__ = [
    "NOT_AVAILABLE",
    "AvpMeta",
    "AvpQaError",
    "AvpReportPack",
    "AvpReportSelectionError",
    "_audit_stats",
    "_build_enveloppe_xlsx",
    "_count_business_rows",
    "_count_controle_rows",
    "_zone_finding_kind",
    "build_sources_from_snapshot",
    "write_avp_i3f_report_pack",
]
