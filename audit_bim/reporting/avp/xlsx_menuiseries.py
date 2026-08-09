"""Builder « export Menuiseries » (.xlsx)."""

from __future__ import annotations

from pathlib import Path

import xlsxwriter

from ..word_report import NOT_AVAILABLE
from ..xlsx_annex import write_safe
from .models import _COMPUTED_METHODO_NOTE
from .xlsx_common import _moa_formats, _rows_have_computed, _safe_sheet, _write_moa_grid


def _build_menuiseries_xlsx(path, sources, meta) -> Path:
    src = sources.menuiseries if sources else None
    wb = xlsxwriter.Workbook(str(path), {"strings_to_formulas": False})
    fmts = _moa_formats(wb)
    # Proximité I3F : conserver le nom d'onglet source (« TDB 2022 05.1… »).
    ws = wb.add_worksheet(_safe_sheet((src.sheet_title if src else None) or "Menuiseries"))
    table = src.table if src else None
    if table is None or not table.headers:
        write_safe(ws, 0, 0, NOT_AVAILABLE, fmts["data"])
        wb.close()
        return path

    rows = [list(table.headers), *table.rows]
    ncols = max(len(r) for r in rows) if rows else len(table.headers)
    rows.append([""] * ncols)
    summary = [""] * ncols
    if ncols > 1:
        summary[1] = "Nombre de types de menuiseries"
    if ncols > 2:
        last = max(2, len(table.rows) + 1)
        summary[2] = f"=COUNTA(D2:D{last})" if table.rows else (src.nombre_types if src else None)
    rows.append(summary)
    end = _write_moa_grid(ws, fmts, rows, start_row=0)
    if _rows_have_computed(table.rows, headers=table.headers):
        write_safe(ws, end + 1, 0, _COMPUTED_METHODO_NOTE, fmts["note"])
    wb.close()
    return path
