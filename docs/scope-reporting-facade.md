# `audit_bim/reporting` — carte avant décision

**Mesure, aucun code runtime**, hors `scripts/inventory_reporting_modules.py`.
Ce document existe pour qu'on ne nettoie pas `reporting` au jugé.

## Le constat qui change la nature du lot

`audit_bim/query` était une façade : 152 lignes de pur passe-plat, supprimées
sans conséquence. **`reporting` n'en est pas une.** 23 modules, **8 986 lignes**,
dont **5 016 dans dix modules qui écrivent un fichier**.

| Nature | Modules | Lignes |
|---|---:|---:|
| Façade pure vers `bim-reporting` | 0 | — |
| Sans attache directe mesurée | 2 | 7 |
| Lié au livrable I3F par ses appelants | 9 | 2 812 |
| Orchestration I3F | 12 | **6 167** |

**Aucune catégorie ne s'appelle « neutre »**, et c'est délibéré. Le mot serait lu
comme « extractible » par le lot suivant, alors que la mesure ne dit que « aucune
attache trouvée ». `sans_attache_directe` nomme ce qui a été constaté ;
`lié_livrable_i3f` nomme ce que les appelants révèlent. Il ne reste dans la
première que deux `__init__.py`, pour sept lignes.

La façade réelle représente **1,9 %** du module. Le reste est de la production
de livrables. Poser la question en termes de « suppression de façade » conduirait
à supprimer 153 lignes et à déclarer le sujet clos, alors que les 8 000 autres
sont précisément ce qu'un second AMO ne peut pas réutiliser aujourd'hui.

## Ce qui était façade — et ce qui n'en était pas

**Une première version de cet inventaire en annonçait trois.** Elle classait
« façade » tout module qui déléguait au socle, sans attache mesurée, et faisait
moins de 120 lignes. Le critère de taille était un raccourci ; deux des trois
ne survivaient pas à la lecture.

| Module | Verdict | Pourquoi |
|---|---|---|
| `pdf_export.py` (16 l) | **façade, retirée** | imports + `__all__`, rien d'autre |
| `bimdata_brand.py` (51 l) | **à conserver** | fige `search_from` sur son propre fichier |
| `theming.py` (86 l) | **à conserver** | `THEME_COLORS`, `SEVERITY_COLORS`, alias client |

`bimdata_brand` est le cas instructif. Il ne ré-exporte pas `find_logo` : il
l'appelle en figeant l'origine de la recherche sur **ce fichier-ci**. Une fois le
socle installé en dépendance, `bim_reporting.brand.__file__` pointe dans
`site-packages` et la remontée « sibling » ne trouve plus rien — **le logo
disparaîtrait des livrables sans la moindre erreur**, puisque son absence dégrade
proprement vers le wordmark texte. Le supprimer aurait produit un livrable
silencieusement dégradé.

`theming` porte une palette indexée sur les thèmes d'audit et des alias au
vocabulaire client, tenus hors du socle de façon délibérée — sa docstring
l'explique déjà.

Le critère porte désormais sur ce qu'un module **définit** : une façade a des
imports, un `__all__`, éventuellement des alias de noms. Rien qui se définisse,
rien qui se compose.

## Ce qui est de l'orchestration I3F

Douze modules, 5 980 lignes. Trois familles :

**Le pack AVP** (`avp/`, `avp_sources`, `avp_report_catalog`, `avp/models`) —
2 900 lignes. Vocabulaire client mesuré dans les **chaînes écrites**, pas les
docstrings : `avp/docx_analyse.py` porte `i3f`, `cch`, `avp` et `3f` ;
`avp/xlsx_controle.py` les trois premiers ; `avp/xlsx_enveloppe.py` et
`avp_sources.py` portent `3f`. Ce ne sont pas des commentaires : ce sont des
titres de sections et des en-têtes de colonnes qui finissent dans le classeur.

**Les deux livrables principaux** — `word_report.py` (1 074 l, 12 appelants) et
`xlsx_annex.py` (417 l, 8 appelants). Ils lisent déjà le profil : les lots C1/C2
en ont sorti le narratif et la structure. Ce qui reste est de l'assemblage.

**Le contexte** — `context.py` (780 l, 5 appelants), qui porte encore `cch` dans
ses chaînes malgré `ReferenceFramework`.

## La nuance qui commande le découpage

**`avp_snapshot.py` — 1 614 lignes, aucune dépendance I3F, aucun terme client.**
Mesuré sur ses seuls imports, c'est le plus gros bloc sans attache du module.
Mais ses cinq appelants sont `avp/pack`, `avp/docx_analyse`, `avp_availability`,
`avp_i3f` et `tools_reporting` : **il n'existe que pour alimenter le pack AVP**.

`avp/xlsx_common` en était le sixième : il importait `_SRC_COMPUTED` pour
reconnaître le libellé de provenance en bout de ligne. Cette colonne ayant
disparu de tous les livrables, le writer ne lit plus que ses propres en-têtes —
un couplage de moins, obtenu en retirant du code mort, pas en refactorant.

Son code ne connaît pas le référentiel ; personne d'autre ne l'appelle. C'est la
même nuance que celle rencontrée dans l'inventaire du socle partagé — du code
générique suspendu à un amont I3F — et elle mène à la même conclusion : le
classer « extractible » promettrait à un second AMO une brique dont il n'aurait
aucun usage.

Six autres modules sont dans ce cas, dont `avp_autocompute` (621 l),
`avp_availability` (278 l) et `avp_i3f` (60 l). **Le script les classe
lui-même** `lié_livrable_i3f` : une première version les rangeait en « neutre »
et laissait ce document rétablir la nuance à la main — un lecteur exécutant le
script y aurait lu l'inverse de ce qu'il énonce.

## Contrats de sortie à préserver

Dix modules écrivent. Trois contrats, de nature différente :

| Contrat | Produit par | Ce qui doit être prouvé |
|---|---|---|
| **Rapport Word** | `word_report.py` | ordre des sections, textes du profil, déterminisme (corrigé en #160) |
| **Annexe XLSX** | `xlsx_annex.py` | **noms d'onglets et en-têtes** — clés techniques, pas du texte |
| **Pack AVP** | `avp/pack.py` + 8 modules | six annexes, un rapport Word, et la note de méthode |

Le deuxième mérite une insistance. `ReportStructureSpec` porte
`referential_sheet_name` et `finding_reference_column_label` comme **valeurs
littérales**, précisément parce qu'un nom d'onglet peut être référencé par un TCD
ou un rapprochement côté maîtrise d'ouvrage. Le changer ne casse rien chez nous
et casse un usage aval sans signal. **La recette de ces contrats passe par
l'ouverture du fichier produit, pas par une comparaison de texte.**

## Lots proposés

Du moins risqué au plus engageant. Chacun a son critère de parité.

### Lot R1 — retirer la façade de rendu — **fait**

`pdf_export.py` supprimé ; son unique appelant (`avp/pack.py`) importe
`bim_reporting.pdf` directement. `theming.py` et `bimdata_brand.py` **restent** :
ce ne sont pas des façades, cf. plus haut.

*Parité* : goldens MCP inchangés, suite verte, garde-fou statique interdisant la
réapparition d'une couche locale de pur ré-export. **Aucun fichier produit ne
change** — le module ne composait rien.

*Gain réel* : 16 lignes. Le gain annoncé de 153 lignes reposait sur un critère
de classification faux, et deux de ces modules auraient dégradé les livrables.

### Lot R2 — sortir le vocabulaire client des classeurs AVP

Les quatre modules qui écrivent `i3f` / `cch` / `avp` / `3f` dans des cellules.
Le mécanisme existe déjà (`ReportNarrativeSpec`, `ReportStructureSpec`) ; il
s'agit de l'étendre au pack, pas d'en inventer un.

*Parité* : **ouverture du classeur produit avant / après**, onglet par onglet,
en-tête par en-tête. Une comparaison de code ne suffit pas. À faire sur le modèle
de recette connu (`MCP_Audit` / `250613_MN_BAT`), comme la recette de release.

*Prérequis honnête* : ce lot n'a d'intérêt que si un second AMO doit produire un
pack équivalent. Tant que `bim_in_motion` ne produit aucun livrable, il
paramètre du texte que personne d'autre ne lira.

### Lot R3 — `context.py`

Sortir les dernières chaînes `cch`. Petit, mais 5 appelants dont deux outils MCP.

*Parité* : rapport Word et annexe XLSX identiques sur le modèle de recette.

### Ce qu'il ne faut pas faire

**Extraire `avp_snapshot.py` vers un socle.** C'est le candidat qui paraît le
plus évident — 1 057 lignes sans attache mesurée — et c'est le piège : il n'a qu'un usage,
le pack AVP. L'extraire déplacerait du code sans créer de réutilisation, et
ajouterait une dépendance inter-paquets à un seul consommateur.

**Toucher au pack AVP avant qu'un second AMO ait un besoin de livrable.**
L'inventaire du socle partagé a établi la règle : on extrait ce qu'un second
consommateur a dû réimplémenter, pas ce qui semble générique.

## Recommandation

**R1 est fait, et il est minuscule** : une façade, seize lignes. Le lot méritait
d'exister pour ce qu'il a révélé, pas pour ce qu'il a supprimé — deux modules
sur trois auraient dégradé les livrables s'ils avaient été traités comme
annoncé, dont un silencieusement.

R2 et R3 devraient attendre que `bim_in_motion` ait un besoin de livrable réel.
Aujourd'hui, ils paramétreraient des textes pour un lecteur qui n'existe pas —
et leur recette exige d'ouvrir des classeurs, ce qui a un coût qu'aucun besoin
ne justifie encore.

## Reproduire l'inventaire

```bash
python scripts/inventory_reporting_modules.py          # tableau
python scripts/inventory_reporting_modules.py --json   # données brutes
```

Les chiffres de ce document sortent de cette commande, et un test les y
confronte : un inventaire recopié à la main cesse d'être vrai au premier commit.
