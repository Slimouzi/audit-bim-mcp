"""L'inventaire de `audit_bim/reporting` doit rester vrai.

`docs/scope-reporting-facade.md` sert de base de décision aux lots R1–R3 : ses
chiffres viennent du code, et doivent le rester. Un document d'inventaire
recopié à la main cesse d'être exact au premier commit — et continue d'être cité
comme s'il l'était.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "scope-reporting-facade.md"

sys.path.insert(0, str(REPO / "scripts"))


@pytest.fixture(scope="module")
def report() -> dict:
    from inventory_reporting_modules import analyse

    return analyse()


def test_the_document_figures_match_the_measurement(report):
    counts = Counter(m["kind"] for m in report["modules"])
    lines = Counter()
    for m in report["modules"]:
        lines[m["kind"]] += m["lines"]

    assert len(report["modules"]) == 23
    assert sum(m["lines"] for m in report["modules"]) == 8815
    assert counts["façade"] == 0
    assert counts["orchestration_i3f"] == 12 and lines["orchestration_i3f"] == 6117
    assert counts["lié_livrable_i3f"] == 9 and lines["lié_livrable_i3f"] == 2691
    assert counts["sans_attache_directe"] == 2 and lines["sans_attache_directe"] == 7

    text = DOC.read_text(encoding="utf-8")
    for claim in (
        "**8 815 lignes**",
        "| Façade pure vers `bim-reporting` | 0 | — |",
        "| Lié au livrable I3F par ses appelants | 9 | 2 691 |",
    ):
        assert claim in text, f"le document ne porte plus : {claim}"


def test_no_category_is_named_neutral(report):
    """« Neutre » serait lu comme « extractible » par le lot suivant.

    La sortie machine et le document doivent dire la même chose. Une version
    antérieure classait 2 023 lignes du bloc AVP en « neutre » et laissait le
    document rétablir la nuance en prose : un lecteur exécutant le script y
    aurait lu l'inverse de ce que le document énonce.
    """
    kinds = {m["kind"] for m in report["modules"]}
    assert not [k for k in kinds if k == "neutre" or k.startswith("neutre_")], kinds
    assert kinds <= {"façade", "sans_attache_directe", "lié_livrable_i3f", "orchestration_i3f"}

    # Ce qui reste sans attache ne doit plus être qu'un résidu.
    residual = [m for m in report["modules"] if m["kind"] == "sans_attache_directe"]
    assert {m["module"] for m in residual} == {"__init__.py", "avp/__init__.py"}


def test_no_pure_facade_remains(report):
    """R1 a retiré la seule façade pure ; les deux autres n'en étaient pas.

    ``bimdata_brand`` fige l'origine de recherche des assets sur son propre
    fichier — le supprimer ferait chercher dans ``site-packages`` et le logo
    disparaîtrait des livrables *sans erreur*. ``theming`` porte une palette
    indexée sur les thèmes d'audit et des alias au vocabulaire client. Un
    critère de taille les avait classés « façade » ; le critère porte désormais
    sur ce qu'un module **définit**.
    """
    assert not [m for m in report["modules"] if m["kind"] == "façade"]

    for name in ("theming.py", "bimdata_brand.py"):
        entry = next(m for m in report["modules"] if m["module"] == name)
        assert entry["kind"] != "façade", name

    text = " ".join(DOC.read_text(encoding="utf-8").split())
    assert "le logo disparaîtrait des livrables" in text


def test_the_facade_criterion_rejects_a_module_that_defines_something():
    """Non-vacuité du critère corrigé, sur les deux formes qui l'avaient trompé."""
    import ast

    from inventory_reporting_modules import _is_pure_reexport

    assert _is_pure_reexport(ast.parse('"""Doc."""\nfrom x import y\n__all__ = ["y"]\n'))
    assert _is_pure_reexport(ast.parse("from x import y\nZ = y\n"))
    assert not _is_pure_reexport(ast.parse("from x import y\ndef f():\n    return y()\n"))
    assert not _is_pure_reexport(ast.parse('from x import y\nCOLORS = {"a": "b"}\n'))


def test_writing_modules_are_counted_as_claimed(report):
    """Le nombre de modules qui écrivent commande le coût de recette des lots."""
    writers = [m for m in report["modules"] if m["writes_files"]]
    assert len(writers) == 10
    assert sum(m["lines"] for m in writers) == 5006
    assert "**5 006 dans dix modules qui écrivent un fichier**" in DOC.read_text(encoding="utf-8")


def test_avp_snapshot_is_classified_by_use_not_only_by_imports(report):
    """La nuance qui commande le découpage — portée par le **script**, pas la prose.

    Le module n'a aucune dépendance I3F ni terme client. Ce n'est pas pour
    autant une brique extractible : tous ses appelants servent le livrable. La
    classification doit le dire d'elle-même.
    """
    entry = next(m for m in report["modules"] if m["module"] == "avp_snapshot.py")

    assert entry["attaches"] == [] and entry["client_terms"] == []
    assert entry["lines"] == 1520
    assert (
        len(entry["consumers"]) == 5
    )  # xlsx_common découplé : plus de libellé de provenance à lire
    assert entry["deliverable_bound"] is True
    assert entry["kind"] == "lié_livrable_i3f", "le script doit porter la nuance lui-même"

    # Comparaison insensible aux retours à la ligne : le document est du texte
    # rédigé, ses phrases se replient. Assertion sur le fond, pas sur la mise en
    # forme — sinon le test casse au premier reformatage et n'apprend rien.
    text = " ".join(DOC.read_text(encoding="utf-8").split())
    assert "il n'existe que pour alimenter le pack AVP" in text
    assert "Extraire `avp_snapshot.py` vers un socle." in text


def test_client_vocabulary_is_measured_in_written_strings_not_docstrings(report):
    """Non-vacuité du signal client — et preuve qu'il ne vient pas des commentaires.

    Le contrôle porte sur les chaînes littérales hors docstrings : ce sont elles
    qui finissent dans une cellule. Sans cette restriction, un module qui *parle*
    du CCH passerait pour un module qui *l'écrit*.
    """
    flagged = {m["module"]: m["client_terms"] for m in report["modules"] if m["client_terms"]}
    assert "avp/docx_analyse.py" in flagged
    assert set(flagged["avp/docx_analyse.py"]) >= {"i3f", "cch", "avp"}

    # Contre-épreuve réelle : la sélection doit retenir la chaîne écrite et
    # écarter la docstring qui parle du même sujet. Une version antérieure se
    # contentait de vérifier qu'un fichier existait — une non-vacuité qui ne
    # mordait pas.
    import ast

    from inventory_reporting_modules import _shipped_texts

    probe = ast.parse(
        '"""Docstring de module : ce module parle du CCH I3F."""\n'
        "def f():\n"
        '    """Docstring de fonction : encore le CCH."""\n'
        '    return "Référence CCH"\n'
    )
    texts = _shipped_texts(probe)
    assert texts == ["Référence CCH"], texts
