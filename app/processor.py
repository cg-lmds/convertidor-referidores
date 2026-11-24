from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from collections import defaultdict
from datetime import datetime
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u200c", "").replace("\u200b", "").strip()
    return text


def _normalize_header(value: Any) -> str:
    text = _normalize_text(value).lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\s+", " ", text)
    return text


def _normalize_doc(value: Any) -> str:
    text = _normalize_text(value).upper()
    text = re.sub(r"[\s\-\.]", "", text)
    return text


def _clean_cedula(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        if float(value).is_integer():
            return str(int(value))
    text = _normalize_text(value)
    if text.endswith(".0"):
        text = text[:-2]
    return re.sub(r"\D", "", text)


def _to_float(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = _normalize_text(value)
    # Handle European/Venezuelan formats if string
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0


def _sanitize_filename(name: str) -> str:
    clean = _normalize_text(name)
    clean = re.sub(r'[<>:"/\\|?*]+', "_", clean)
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    return clean[:80] if clean else "Sin_Referidor"


def _find_col(headers: list[str], candidates: list[str]) -> int | None:
    norm_headers = [_normalize_header(h) for h in headers]
    for cand in candidates:
        nc = _normalize_header(cand)
        for idx, nh in enumerate(norm_headers):
            if nh == nc:
                return idx
    for cand in candidates:
        nc = _normalize_header(cand)
        for idx, nh in enumerate(norm_headers):
            if nc in nh:
                return idx
    return None


def parse_referidores(file_bytes: bytes) -> dict[str, str]:
    """Read REFERIDORES.xlsx and return {DOC_ID: Referidor}."""
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    try:
        ws = wb["Reporte"] if "Reporte" in wb.sheetnames else wb.active

        header_row_idx = None
        headers: list[str] = []
        for idx, row in enumerate(ws.iter_rows(min_row=1, max_row=10, max_col=20, values_only=True), start=1):
            row_vals = [_normalize_text(v) for v in row]
            norm_vals = [_normalize_header(v) for v in row_vals]
            if ("cedula" in norm_vals and "referido" in norm_vals) or (
                any("cedula" in v for v in norm_vals) and any("referid" in v for v in norm_vals)
            ):
                header_row_idx = idx
                headers = row_vals
                break

        if header_row_idx is None:
            raise ValueError(
                "No se encontraron las columnas 'Nac', 'Cedula' y 'Referido' en el archivo de Referidores."
            )

        idx_nac = _find_col(headers, ["nac", "nacionalidad"])
        idx_ced = _find_col(headers, ["cedula", "doc. ident.", "documento"])
        idx_ref = _find_col(headers, ["referido", "referidor"])

        if idx_ced is None or idx_ref is None:
            raise ValueError("El archivo de Referidores debe contener las columnas 'Cedula' y 'Referido'.")

        max_needed = max(c for c in [idx_nac, idx_ced, idx_ref] if c is not None) + 1
        ref_map: dict[str, str] = {}

        for row in ws.iter_rows(min_row=header_row_idx + 1, max_col=max_needed, values_only=True):
            if not row or len(row) <= max(idx_ced, idx_ref):
                continue
            ced_raw = row[idx_ced]
            if ced_raw is None or _normalize_text(ced_raw) == "":
                continue

            if idx_nac is not None and len(row) > idx_nac and row[idx_nac] is not None:
                nac = _normalize_text(row[idx_nac]).upper()
                ced = _clean_cedula(ced_raw)
                doc_id = f"{nac}{ced}" if nac else ced
            else:
                doc_id = _normalize_doc(ced_raw)

            if not doc_id:
                continue

            ref_name = _normalize_text(row[idx_ref])
            if doc_id not in ref_map:
                ref_map[doc_id] = ref_name

        return ref_map
    finally:
        wb.close()


def parse_and_merge(
    origen_bytes: bytes,
    referidores_bytes: bytes,
    tasa: float,
) -> dict[str, Any]:
    """Cross-reference DATA ORIGEN with REFERIDORES and calculate USD amounts."""
    if tasa <= 0:
        raise ValueError("La tasa del día debe ser mayor que 0.")

    ref_map = parse_referidores(referidores_bytes)

    wb = openpyxl.load_workbook(io.BytesIO(origen_bytes), data_only=True, read_only=True)
    try:
        ws = wb["Report"] if "Report" in wb.sheetnames else wb.active

        header_row_idx = None
        headers: list[str] = []
        for idx, row in enumerate(ws.iter_rows(min_row=1, max_row=10, max_col=45, values_only=True), start=1):
            row_vals = [_normalize_text(v) for v in row]
            norm_vals = [_normalize_header(v) for v in row_vals]
            if any("doc" in v and "ident" in v for v in norm_vals) or any(
                "nombre del cliente" in v for v in norm_vals
            ):
                header_row_idx = idx
                headers = row_vals
                break

        if header_row_idx is None:
            raise ValueError(
                "No se encontraron los encabezados esperados en DATA ORIGEN (ej. 'Doc. Ident.', 'Nombre del cliente')."
            )

        idx_doc = _find_col(headers, ["doc. ident.", "documento de identidad", "documento", "cedula"])
        idx_nombre = _find_col(headers, ["nombre del cliente", "nombre", "cliente"])
        idx_tel = _find_col(headers, ["telefono", "teléfono", "celular"])
        idx_email = _find_col(headers, ["email", "correo", "correo electronico"])
        idx_pmin = _find_col(headers, ["pago minimo pendiente bs", "pago minimo pendiente", "pago minimo"])
        idx_dtot = _find_col(headers, ["deuda total pendiente bs", "deuda total pendiente", "pago total pendiente"])
        idx_venc = _find_col(headers, ["tipo venc (max)", "tipo de vencimiento", "tipo venc"])

        missing = []
        if idx_doc is None:
            missing.append("Doc. Ident.")
        if idx_nombre is None:
            missing.append("Nombre del cliente")
        if idx_pmin is None:
            missing.append("Pago mínimo pendiente Bs")
        if missing:
            raise ValueError(f"Faltan columnas requeridas en DATA ORIGEN: {', '.join(missing)}")

        needed_indices = [
            c for c in [idx_doc, idx_nombre, idx_tel, idx_email, idx_pmin, idx_dtot, idx_venc] if c is not None
        ]
        max_needed = max(needed_indices) + 1

        records: list[dict[str, Any]] = []
        matched_count = 0
        unmatched_count = 0
        vencimiento_types: set[str] = set()

        for row in ws.iter_rows(min_row=header_row_idx + 1, max_col=max_needed, values_only=True):
            if not row or len(row) <= idx_doc:
                continue
            raw_doc = row[idx_doc]
            if raw_doc is None or _normalize_text(raw_doc) == "":
                continue

            doc_id = _normalize_doc(raw_doc)
            nombre = _normalize_text(row[idx_nombre]) if idx_nombre is not None and len(row) > idx_nombre else ""
            telefono = _normalize_text(row[idx_tel]) if idx_tel is not None and len(row) > idx_tel else ""
            email = _normalize_text(row[idx_email]) if idx_email is not None and len(row) > idx_email else ""
            pmin_bs = _to_float(row[idx_pmin]) if idx_pmin is not None and len(row) > idx_pmin else 0.0
            dtot_bs = _to_float(row[idx_dtot]) if idx_dtot is not None and len(row) > idx_dtot else 0.0
            tipo_venc = (
                _normalize_text(row[idx_venc]) if idx_venc is not None and len(row) > idx_venc else "Sin Clasificar"
            )
            if not tipo_venc:
                tipo_venc = "Sin Clasificar"

            referidor = ref_map.get(doc_id, "")
            if referidor:
                matched_count += 1
            else:
                unmatched_count += 1
                referidor = "Sin Referidor"

            pmin_usd = round(pmin_bs / tasa, 2)
            dtot_usd = round(dtot_bs / tasa, 2)

            vencimiento_types.add(tipo_venc)
            records.append(
                {
                    "documento": doc_id,
                    "nombre": nombre,
                    "telefono": telefono,
                    "email": email,
                    "pago_minimo_bs": round(pmin_bs, 2),
                    "pago_minimo_usd": pmin_usd,
                    "pago_total_bs": round(dtot_bs, 2),
                    "pago_total_usd": dtot_usd,
                    "tipo_vencimiento": tipo_venc,
                    "referidor": referidor,
                }
            )

        return {
            "records": records,
            "tasa": tasa,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "stats": {
                "total_records": len(records),
                "matched_count": matched_count,
                "unmatched_count": unmatched_count,
                "referidores_catalog_size": len(ref_map),
                "vencimiento_types": sorted(vencimiento_types),
            },
        }
    finally:
        wb.close()


def filter_records(
    records: list[dict[str, Any]],
    only_with_debt: bool = False,
    only_overdue: bool = False,
    vencimientos: list[str] | None = None,
    include_sin_referidor: bool = True,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    venc_set = set(vencimientos) if vencimientos else None

    for rec in records:
        if not include_sin_referidor and rec["referidor"] == "Sin Referidor":
            continue
        if only_with_debt and rec["pago_minimo_usd"] <= 0:
            continue
        if only_overdue and rec["tipo_vencimiento"].lower() == "vigente":
            continue
        if venc_set is not None and rec["tipo_vencimiento"] not in venc_set:
            continue
        filtered.append(rec)

    return filtered


def summarize_by_referidor(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "referidor": "",
            "total_clientes": 0,
            "clientes_vencidos": 0,
            "clientes_con_pago_minimo": 0,
            "pago_minimo_usd": 0.0,
            "pago_total_usd": 0.0,
        }
    )

    for rec in records:
        ref = rec["referidor"]
        g = groups[ref]
        g["referidor"] = ref
        g["total_clientes"] += 1
        if rec["tipo_vencimiento"].lower() != "vigente":
            g["clientes_vencidos"] += 1
        if rec["pago_minimo_usd"] > 0:
            g["clientes_con_pago_minimo"] += 1
        g["pago_minimo_usd"] += rec["pago_minimo_usd"]
        g["pago_total_usd"] += rec["pago_total_usd"]

    result = []
    for g in groups.values():
        g["pago_minimo_usd"] = round(g["pago_minimo_usd"], 2)
        g["pago_total_usd"] = round(g["pago_total_usd"], 2)
        result.append(g)

    result.sort(key=lambda x: (-x["pago_minimo_usd"], x["referidor"].lower()))
    return result


def build_excel_bytes(
    records: list[dict[str, Any]],
    include_pago_total: bool = True,
    sheet_title: str = "Cartera",
) -> bytes:
    """Generate a styled .xlsx workbook matching FORMATO.xlsx."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title[:31]

    if include_pago_total:
        headers = [
            "Documento de Identidad",
            "Nombre del Cliente",
            "Teléfono",
            "Email",
            "Pago Mínimo Pendiente (USD)",
            "Pago Total Pendiente (USD)",
            "Tipo de Vencimiento",
            "Referidor",
        ]
        col_widths = [24, 34, 15, 34, 26, 26, 20, 30]
    else:
        headers = [
            "Documento de Identidad",
            "Nombre del Cliente",
            "Teléfono",
            "Email",
            "Pago Mínimo Pendiente (USD)",
            "Tipo de Vencimiento",
            "Referidor",
        ]
        col_widths = [24, 34, 15, 34, 26, 20, 30]

    header_fill = PatternFill(start_color="2B4C7E", end_color="2B4C7E", fill_type="solid")
    header_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    zebra_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    white_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
    body_font = Font(name="Calibri", size=10, bold=False, color="1E293B")
    thin_side = Side(style="thin", color="E2E8F0")
    border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

    ws.row_dimensions[1].height = 26
    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_align
        cell.border = border
        ws.column_dimensions[get_column_letter(col_idx)].width = col_widths[col_idx - 1]

    # Sort records by Pago Mínimo USD descending
    sorted_records = sorted(records, key=lambda r: (-r["pago_minimo_usd"], r["nombre"].lower()))

    for row_offset, rec in enumerate(sorted_records, start=2):
        fill = zebra_fill if row_offset % 2 == 0 else white_fill
        if include_pago_total:
            row_values = [
                rec["documento"],
                rec["nombre"],
                rec["telefono"],
                rec["email"],
                rec["pago_minimo_usd"],
                rec["pago_total_usd"],
                rec["tipo_vencimiento"],
                rec["referidor"],
            ]
            currency_cols = {5, 6}
        else:
            row_values = [
                rec["documento"],
                rec["nombre"],
                rec["telefono"],
                rec["email"],
                rec["pago_minimo_usd"],
                rec["tipo_vencimiento"],
                rec["referidor"],
            ]
            currency_cols = {5}

        for col_idx, val in enumerate(row_values, start=1):
            cell = ws.cell(row=row_offset, column=col_idx, value=val)
            cell.font = body_font
            cell.fill = fill
            cell.border = border
            if col_idx == 3:
                cell.number_format = "@"
                cell.alignment = Alignment(horizontal="left", vertical="center")
            elif col_idx in currency_cols:
                cell.number_format = "$#,##0.00"
                cell.alignment = Alignment(horizontal="right", vertical="center")
            elif col_idx == 1:
                cell.alignment = Alignment(horizontal="left", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center")

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(1, len(sorted_records) + 1)}"

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output.getvalue()


def build_all_zip_bytes(
    records: list[dict[str, Any]],
    include_pago_total: bool = True,
) -> bytes:
    """Build an in-memory ZIP archive with one .xlsx per referidor."""
    by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        by_ref[rec["referidor"]].append(rec)

    zip_buf = io.BytesIO()
    used_names: set[str] = set()

    with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for ref_name in sorted(by_ref.keys(), key=lambda s: s.lower()):
            base = _sanitize_filename(ref_name)
            filename = f"{base}.xlsx"
            counter = 2
            while filename.lower() in used_names:
                filename = f"{base}_{counter}.xlsx"
                counter += 1
            used_names.add(filename.lower())

            xlsx_bytes = build_excel_bytes(
                by_ref[ref_name],
                include_pago_total=include_pago_total,
                sheet_title="Cartera",
            )
            zf.writestr(filename, xlsx_bytes)

    zip_buf.seek(0)
    return zip_buf.getvalue()
